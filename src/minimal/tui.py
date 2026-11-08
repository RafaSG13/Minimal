"""The interactive terminal UI, built with Textual.

Layout, top to bottom: the ``top`` extension area, the transcript (a welcome
header, then user messages, assistant replies, tool calls and command output),
the ``bottom`` extension area, the working indicator, the prompt and the status
line (model, provider, directory, git branch). An optional ``sidebar`` on the
right holds extension widgets (see
:meth:`~minimal.registry.Registry.register_widget`). Colours and glyphs come
from :mod:`minimal.theme`.

Turns and slash commands run in a worker thread so the UI stays responsive;
:class:`TuiFrontend` hands their output and confirmations back to the UI
thread. Anything printed (slash-command output, extension messages) is shown
in the transcript. Commands marked ``terminal`` run with the UI suspended.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable, Iterator

from rich.text import Text
from textual import events, work
from textual.app import App as TextualApp
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, Label, Markdown, OptionList, SelectionList, Static
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from minimal import theme
from minimal.app import App, ConsoleFrontend
from minimal.commands import ExitRepl, find_command, run_command

PREVIEW_LINES = 8
"""Most output lines shown under a tool call."""


@contextmanager
def _outside_event_loop() -> Iterator[None]:
    """Hide Textual's running loop from synchronous code run on the UI thread.

    Terminal commands (``/setup``'s questionary menus) call ``asyncio.run``,
    which refuses to start while a loop is running in the thread. The UI loop
    is blocked for the whole command anyway, so a nested loop is safe; running
    the command on the UI thread also keeps Ctrl-C working for it.
    """
    loop = asyncio.get_running_loop()
    asyncio.events._set_running_loop(None)
    try:
        yield
    finally:
        asyncio.events._set_running_loop(loop)


def git_status(cwd: str) -> tuple[str, bool] | None:
    """``(branch, dirty)`` for the repository containing ``cwd``, or ``None`` outside one."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "status", "--porcelain=v1", "--branch"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = result.stdout.splitlines()
    if result.returncode != 0 or not lines or not lines[0].startswith("## "):
        return None
    head = lines[0][3:].removeprefix("No commits yet on ")
    return head.split("...")[0], len(lines) > 1


def _short_path(path: str) -> str:
    """``path`` with the home directory written as ``~``."""
    home = str(Path.home())
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


def _version() -> str:
    try:
        return version("minimal")
    except PackageNotFoundError:
        return ""


# --- tool calls ----------------------------------------------------------------


def _one_line(text: str, limit: int = 160) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _tool_title(call: dict) -> Text:
    """The tool call's header line, without the state icon: ``$ cmd``, ``read path``, ``name {json}``."""
    name, inp = call["name"], call["input"]
    title = Text()
    if name == "bash" and isinstance(inp.get("command"), str):
        title.append("$ ", style=theme.GRAY)
        title.append(_one_line(inp["command"]), style="bold")
    elif name in ("read", "write", "edit") and isinstance(inp.get("path"), str):
        title.append(name, style="bold")
        title.append(" ")
        title.append(inp["path"], style=theme.CYAN)
        if name == "read" and ("offset" in inp or "limit" in inp):
            start = inp.get("offset", 1)
            end = f"{start + inp['limit'] - 1}" if isinstance(inp.get("limit"), int) else ""
            title.append(f":{start}-{end}", style=theme.GRAY)
        if name == "write" and isinstance(inp.get("content"), str):
            title.append(f" ({len(inp['content'].splitlines())} lines)", style=theme.GRAY)
    else:
        title.append(name, style="bold")
        if inp:
            title.append(" ")
            title.append(_one_line(json.dumps(inp, ensure_ascii=False), 120), style=theme.GRAY)
    return title


def _preview(lines: list[str], style: str, tail: bool = False) -> Text:
    """At most :data:`PREVIEW_LINES` of ``lines`` (the last ones if ``tail``), saying how many were left out."""
    hidden = len(lines) - PREVIEW_LINES
    shown = (lines[-PREVIEW_LINES:] if tail else lines[:PREVIEW_LINES]) if hidden > 0 else lines
    text = Text()
    if hidden > 0 and tail:
        text.append(f"… {hidden} earlier lines\n", style=theme.DIM_GRAY)
    text.append("\n".join(shown), style=style)
    if hidden > 0 and not tail:
        text.append(f"\n… {hidden} more lines", style=theme.DIM_GRAY)
    return text


def _edit_diff(inp: dict) -> Text:
    """``old_string`` as removed lines and ``new_string`` as added lines."""
    text = Text()
    for sign, key, style in (("-", "old_string", theme.RED), ("+", "new_string", theme.GREEN)):
        lines = inp[key].splitlines() or [""]
        part = _preview([f"{sign} {line}" for line in lines], style)
        if text:
            text.append("\n")
        text.append_text(part)
    return text


class ToolBlock(Static):
    """One tool call: a header line and a preview, on a background that shows its state.

    The CSS classes ``-pending``, ``-success`` and ``-error`` select the background.
    """

    def __init__(self, call: dict) -> None:
        super().__init__(classes="tool -pending")
        self.call = call
        self.icon = Text(theme.RUNNING, style=theme.YELLOW)
        self.body: Text | None = None
        name, inp = call["name"], call["input"]
        if name == "edit" and isinstance(inp.get("old_string"), str) and isinstance(inp.get("new_string"), str):
            self.body = _edit_diff(inp)
        elif name == "write" and isinstance(inp.get("content"), str):
            self.body = _preview(inp["content"].splitlines(), theme.GRAY)
        self.update(self._text())

    def finish(self, result: dict) -> None:
        """Show ``result`` (a ``tool_result`` block) and switch to the success or error state."""
        output = result["content"] if isinstance(result["content"], str) else json.dumps(result["content"])
        lines = output.strip("\n").expandtabs(4).splitlines()
        if result["is_error"]:
            self.icon = Text(theme.ERROR, style=theme.RED)
            self.body = _preview(lines, theme.RED)
            self.set_classes("tool -error")
        else:
            self.icon = Text(theme.SUCCESS, style=theme.GREEN)
            if self.call["name"] not in ("edit", "write") and lines:
                self.body = _preview(lines, theme.GRAY, tail=self.call["name"] == "bash")
            self.set_classes("tool -success")
        self.update(self._text())

    def _text(self) -> Text:
        text = Text.assemble(self.icon, " ", _tool_title(self.call))
        if self.body:
            text.append("\n")
            text.append_text(self.body)
        return text


# --- chrome --------------------------------------------------------------------


def _header(core: App) -> Text:
    """The welcome block: name and version, key hints, loaded extensions."""
    text = Text()
    text.append("minimal", style=f"bold {theme.ACCENT}")
    text.append(f" v{_version()}" if _version() else "", style=theme.DIM_GRAY)
    text.append("\n")
    hints = [("/", "commands"), ("ctrl+p", "palette"), ("ctrl+q", "quit")]
    for i, (key, what) in enumerate(hints):
        if i:
            text.append(theme.DOT, style=theme.DIM_GRAY)
        text.append(key, style=theme.GRAY)
        text.append(f" {what}", style=theme.DIM_GRAY)
    if core.extensions:
        text.append("\n\nextensions ", style=theme.DIM_GRAY)
        text.append(", ".join(p.name for p in core.extensions), style=theme.GRAY)
    return text


class Header(Static):
    """The welcome block at the top of the transcript."""


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question; dismisses with the answer."""

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; background: $background 30%; }
    ConfirmScreen > Vertical {
        width: 80%; max-width: 100; height: auto;
        padding: 1 2; border: round $warning; background: $surface;
        border-title-color: $warning; border-title-style: bold;
    }
    ConfirmScreen #actions { height: auto; margin-top: 1; }
    ConfirmScreen Button { margin-right: 2; min-width: 12; }
    ConfirmScreen #hint { color: $dim; margin-left: 1; }
    """

    BINDINGS = [("y", "answer(True)", "Yes"), ("n", "answer(False)", "No"), ("escape", "answer(False)", "No")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = f"{theme.WARNING} Permission"
            yield Label(self.question, markup=False)
            with Horizontal(id="actions"):
                for label, answer, variant in (("Allow  y", "yes", "success"), ("Deny  n", "no", "error")):
                    button = Button(label, id=answer, variant=variant, compact=True)
                    button.can_focus = False  # answered with keys or a click; no focus ring
                    yield button
                yield Label("esc cancels", id="hint")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class DialogScreen(ModalScreen[Any]):
    """A titled box opened by a command through the frontend; esc cancels (dismisses with ``None``)."""

    DEFAULT_CSS = """
    DialogScreen { align: center middle; background: $background 30%; }
    DialogScreen > Vertical {
        width: 80%; max-width: 100; height: auto; max-height: 80%;
        padding: 1 2; border: round $accent; background: $surface;
        border-title-color: $accent; border-title-style: bold;
    }
    DialogScreen OptionList, DialogScreen SelectionList { height: auto; max-height: 20; }
    DialogScreen .hint { color: $dim; margin-top: 1; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]
    HINT = ""

    def __init__(self, heading: str) -> None:
        super().__init__()
        self.heading = heading  # not ``title``: Screen.title is a reactive

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = self.heading
            yield self.body()
            yield Label(self.HINT, classes="hint")

    def body(self) -> Widget:
        """The widget that takes the answer."""
        raise NotImplementedError

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChoiceScreen(DialogScreen):
    """Pick one option; dismisses with its value."""

    HINT = "enter selects · esc cancels"

    def __init__(self, heading: str, options: list[tuple[str, str]]) -> None:
        super().__init__(heading)
        self.options = options

    def body(self) -> Widget:
        return OptionList(*(Option(Text(label), id=value) for value, label in self.options))

    def on_mount(self) -> None:
        if self.options:
            self.query_one(OptionList).highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option.id)


class ChecklistScreen(DialogScreen):
    """Tick any options; dismisses with the ticked values in option order."""

    HINT = "space toggles · enter applies · esc cancels"
    BINDINGS = [Binding("enter", "apply", "Apply", priority=True)]

    def __init__(self, heading: str, options: list[tuple[str, str, bool]]) -> None:
        super().__init__(heading)
        self.options = options

    def body(self) -> Widget:
        return SelectionList[str](*(Selection(Text(label), value, checked) for value, label, checked in self.options))

    def on_mount(self) -> None:
        if self.options:
            self.query_one(SelectionList).highlighted = 0

    def action_apply(self) -> None:
        ticked = set(self.query_one(SelectionList).selected)
        self.dismiss([value for value, _, _ in self.options if value in ticked])


class TextScreen(DialogScreen):
    """One line of text; dismisses with it stripped, or ``None`` if empty."""

    HINT = "enter confirms · esc cancels"

    def body(self) -> Widget:
        return Input()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()  # otherwise MinimalTui.on_input_submitted sends it as a message
        self.dismiss(event.value.strip() or None)


class TuiFrontend:
    """Shows turns in the TUI. Called from worker threads, or from the UI thread during a terminal command."""

    def __init__(self, tui: MinimalTui) -> None:
        self.tui = tui

    def _on_ui(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run ``fn`` on the UI thread and wait for it (including any awaitable it returns)."""
        if threading.get_ident() == self.tui.ui_thread:
            result = fn(*args)
            if inspect.isawaitable(result):
                self.tui.call_next(lambda: result)
            return None
        return self.tui.call_from_thread(fn, *args)

    def text(self, chunk: str) -> None:
        self._on_ui(self.tui.append_text, chunk)

    def tool_call(self, call: dict) -> None:
        self._on_ui(self.tui.show_tool_call, call)

    def tool_result(self, call: dict, result: dict) -> None:
        self._on_ui(self.tui.show_tool_result, call, result)

    def turn_end(self, error: str | None) -> None:
        self._on_ui(self.tui.end_turn, error)

    def reloaded(self) -> None:
        self._on_ui(self.tui.remount_widgets)

    def _modal(self, screen: ModalScreen) -> Any:
        """Show ``screen`` from a worker thread and wait for its dismiss value (``None`` on quit)."""
        done = threading.Event()
        result: list[Any] = []

        def on_done(value: Any) -> None:
            result.append(value)
            done.set()

        self.tui.pending_modal = done
        self.tui.call_from_thread(lambda: self.tui.push_screen(screen, callback=on_done))
        done.wait()
        self.tui.pending_modal = None
        return result[0] if result else None

    def _in_terminal(self) -> bool:
        """True on the UI thread, which only asks during a suspended terminal command."""
        return threading.get_ident() == self.tui.ui_thread

    def ask(self, question: str) -> bool:
        if self._in_terminal():
            try:
                answer = input(f"{question} [y/N] ")
            except EOFError:
                return False
            return re.fullmatch(r"y|yes", answer.strip(), re.IGNORECASE) is not None
        return bool(self._modal(ConfirmScreen(question)))

    def choose(self, title: str, options: list[tuple[str, str]]) -> str | None:
        if self._in_terminal():
            return ConsoleFrontend().choose(title, options)
        return self._modal(ChoiceScreen(title, options))

    def choose_many(self, title: str, options: list[tuple[str, str, bool]]) -> list[str] | None:
        if self._in_terminal():
            return ConsoleFrontend().choose_many(title, options)
        return self._modal(ChecklistScreen(title, options))

    def ask_text(self, title: str) -> str | None:
        if self._in_terminal():
            return ConsoleFrontend().ask_text(title)
        return self._modal(TextScreen(title))


class MinimalTui(TextualApp):
    """The terminal UI around a :class:`~minimal.app.App`."""

    TITLE = "minimal"

    CSS = """
    #body { height: 1fr; }
    #main { width: 1fr; }
    #transcript { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
    #top, #bottom { height: auto; }
    #sidebar {
        width: 36; padding: 0 1; display: none;
        border-left: solid $border-muted; scrollbar-size-vertical: 1;
    }
    #sidebar.-has-widgets { display: block; }

    Header { padding: 1 1 0 1; }
    .user { background: $user-message-bg; padding: 1 2; margin-top: 1; }
    Markdown.assistant { padding: 0 1; margin-top: 1; }
    Markdown.assistant MarkdownBlock:dark > .code_inline { background: $surface; color: $md-code; }
    Markdown.assistant MarkdownFence { background: $surface; }
    Markdown.assistant MarkdownBullet { color: $accent; }
    .tool { padding: 1 2; margin-top: 1; }
    .tool.-pending { background: $tool-pending-bg; }
    .tool.-success { background: $tool-success-bg; }
    .tool.-error { background: $tool-error-bg; }
    .output { color: $foreground 80%; padding: 0 1; margin-top: 1; }
    .error { color: $error; background: $tool-error-bg; padding: 0 2; margin-top: 1; }

    #working { height: 1; padding: 0 2; margin-top: 1; display: none; }
    #working.-busy { display: block; }
    #composer {
        height: auto; margin-top: 1;
        border-top: solid $border-muted; border-bottom: solid $border-muted;
    }
    #composer:focus-within { border-top: solid $border-blue; border-bottom: solid $border-blue; }
    #prompt-glyph { width: 3; padding: 0 1; color: $accent; text-style: bold; }
    #prompt { border: none; height: 1; padding: 0; background: transparent; }
    #prompt:focus { border: none; }
    #prompt > .input--placeholder { color: $dim; }
    #statusline { height: 1; background: $status-bg; }
    #status-left { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    #status-right { width: auto; color: $dim; padding: 0 1 0 2; }
    """

    def __init__(self, core: App) -> None:
        super().__init__()
        self.register_theme(theme.THEME)
        self.theme = theme.THEME.name
        self.core = core
        self.ui_thread: int | None = None
        self.pending_modal: threading.Event | None = None
        self.assistant: Markdown | None = None
        self.tool_blocks: dict[str, ToolBlock] = {}
        self.output_block: Static | None = None
        self.output_buffer: list[str] = []
        self.output_stderr = False
        self.ext_widgets: list[tuple[str, Widget]] = []
        self.shown_session = core.session
        self.busy_since = 0.0

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield Vertical(id="top")
                with VerticalScroll(id="transcript"):
                    yield Header(_header(self.core))
                yield Vertical(id="bottom")
                yield Static(id="working")
                with Horizontal(id="composer"):
                    yield Static(theme.PROMPT, id="prompt-glyph")
                    yield Input(placeholder="Message, or / for commands", id="prompt")
                with Horizontal(id="statusline"):
                    yield Static(id="status-left")
                    yield Static(id="status-right")
            yield VerticalScroll(id="sidebar")

    async def on_mount(self) -> None:
        self.ui_thread = threading.get_ident()
        self.begin_capture_print(self, stdout=True, stderr=True)
        self.core.frontend = TuiFrontend(self)
        self.spinner = self.set_interval(0.08, self.refresh_working, pause=True)
        await self.remount_widgets()
        for e in self.core.extension_errors:
            self.show_error(f"[extension] could not load {e}")
        self.refresh_status()
        self.query_one("#prompt", Input).focus()

    def on_unmount(self) -> None:
        if self.pending_modal is not None:  # release a worker waiting for an answer
            self.pending_modal.set()

    # --- transcript ----------------------------------------------------------

    def _add(self, widget: Widget) -> Any:
        """Append ``widget`` to the transcript; returns the mount awaitable."""
        self.call_after_refresh(self._follow)
        return self.query_one("#transcript", VerticalScroll).mount(widget)

    def _follow(self) -> None:
        """Keep the transcript scrolled to the bottom once it overflows.

        Anchoring earlier would bottom-align short content; until it fills the
        screen, the conversation grows from the top.
        """
        transcript = self.query_one("#transcript", VerticalScroll)
        if not transcript.is_anchored and transcript.max_scroll_y > 0:
            transcript.anchor()

    def show_error(self, text: str) -> Any:
        return self._add(Static(Text(f"{theme.ERROR} {text}"), classes="error"))

    def on_print(self, event: events.Print) -> None:
        """Show printed text (command output, extension messages) as an output block."""
        transcript = self.query_one("#transcript", VerticalScroll)
        last = transcript.children[-1] if transcript.children else None
        if self.output_block is None or last is not self.output_block or self.output_stderr != event.stderr:
            self.output_block = Static("", markup=False, classes="error" if event.stderr else "output")
            self.output_buffer = []
            self.output_stderr = event.stderr
            self._add(self.output_block)
        self.output_buffer.append(event.text)
        self.output_block.update("".join(self.output_buffer).rstrip("\n"))

    async def append_text(self, chunk: str) -> None:
        # Mount first: Markdown resets its source on mount, which would drop an earlier append.
        if self.assistant is None:
            self.assistant = Markdown("", classes="assistant")
            await self._add(self.assistant)
        await self.assistant.append(chunk)
        self.call_after_refresh(self._follow)

    def show_tool_call(self, call: dict) -> Any:
        self.assistant = None
        block = ToolBlock(call)
        self.tool_blocks[call["id"]] = block
        return self._add(block)

    def show_tool_result(self, call: dict, result: dict) -> None:
        block = self.tool_blocks.pop(call["id"], None)
        if block is not None:
            block.finish(result)
            self.call_after_refresh(self._follow)

    def end_turn(self, error: str | None) -> Any:
        self.assistant = None
        self.tool_blocks.clear()
        if error:
            return self.show_error(f"Error: {error}")
        return None

    # --- extension widgets ---------------------------------------------------

    async def remount_widgets(self) -> None:
        """Replace every extension widget with fresh ones from the registry."""
        for area in ("top", "bottom", "sidebar"):
            await self.query_one(f"#{area}").remove_children()
        self.ext_widgets = []
        for spec in list(self.core.registry.widgets.values()):
            try:
                widget = spec.create(self.core)
                await self.query_one(f"#{spec.area}").mount(widget)
                self.ext_widgets.append((spec.name, widget))
            except Exception as e:
                self.show_error(f"[extension] widget {spec.name}: {e}")
        has_sidebar = any(spec.area == "sidebar" for spec in self.core.registry.widgets.values())
        self.query_one("#sidebar").set_class(has_sidebar, "-has-widgets")
        self.refresh_widgets()

    def refresh_widgets(self) -> None:
        """Call ``refresh_state(core)`` on every extension widget that has it."""
        for name, widget in self.ext_widgets:
            refresh = getattr(widget, "refresh_state", None)
            if not callable(refresh):
                continue
            try:
                refresh(self.core)
            except Exception as e:
                self.show_error(f"[extension] widget {name}: {e}")

    # --- status line and working indicator -----------------------------------

    def refresh_status(self) -> None:
        core = self.core
        sep = Text(f" {theme.SEPARATOR} ", style=theme.DARK_GRAY)
        left = Text(" ")
        left.append(f"{theme.MODEL} {core.model}", style=theme.STATUS_MODEL)
        left.append(f"{theme.DOT}{core.provider_name}", style=theme.GRAY)
        left.append_text(sep)
        left.append(_short_path(core.cwd), style=theme.STATUS_PATH)
        git = git_status(core.cwd)
        if git is not None:
            branch, dirty = git
            left.append_text(sep)
            color = theme.STATUS_GIT_DIRTY if dirty else theme.STATUS_GIT_CLEAN
            left.append(f"{theme.BRANCH} {branch}{' *' if dirty else ''}", style=color)
        self.query_one("#status-left", Static).update(left)
        self.query_one("#status-right", Static).update(
            Text(f"{len(core.extensions)} ext{theme.DOT}session {core.session.id}")
        )

    def set_busy(self, busy: bool) -> None:
        """Lock the prompt and show the working indicator while a turn or command runs."""
        self.query_one("#prompt", Input).disabled = busy
        self.query_one("#working").set_class(busy, "-busy")
        if busy:
            self.busy_since = time.monotonic()
            self.refresh_working()
            self.spinner.resume()
        else:
            self.spinner.pause()

    def refresh_working(self) -> None:
        elapsed = time.monotonic() - self.busy_since
        frame = theme.SPINNER[int(elapsed / 0.08) % len(theme.SPINNER)]
        self.query_one("#working", Static).update(
            Text.assemble((frame, theme.ACCENT), " ", ("Working…", theme.GRAY), (f" {elapsed:.0f}s", theme.DIM_GRAY))
        )

    # --- input ---------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.clear()
        if not line:
            return
        self.set_busy(True)
        if line.startswith("/"):
            cmd = find_command(line[1:].partition(" ")[0], self.core)
            if cmd is not None and cmd.terminal:
                try:
                    with self.suspend(), _outside_event_loop():
                        run_command(line, self.core)
                except ExitRepl:
                    self.exit()
                    return
                self.after_action()
            else:
                self.run_command_worker(line)
            return
        self._add(Static(line, markup=False, classes="user"))
        self.assistant = None
        self.run_turn_worker(line)

    @work(thread=True, exclusive=True, group="agent")
    def run_turn_worker(self, line: str) -> None:
        self.core.run_turn(line)
        self.call_from_thread(self.after_action)

    @work(thread=True, exclusive=True, group="agent")
    def run_command_worker(self, line: str) -> None:
        try:
            run_command(line, self.core)
        except ExitRepl:
            self.call_from_thread(self.exit)
            return
        self.call_from_thread(self.after_action)

    def after_action(self) -> None:
        """After a turn or command: follow a session switch, refresh the status and widgets, re-enable input."""
        if self.core.session is not self.shown_session:
            transcript = self.query_one("#transcript", VerticalScroll)
            transcript.remove_children()
            transcript.anchor(False)
            transcript.mount(Header(_header(self.core)))
            self.shown_session = self.core.session
            self.assistant = None
        self.refresh_status()
        self.refresh_widgets()
        self.set_busy(False)
        self.query_one("#prompt", Input).focus()


def run_tui(core: App) -> None:
    """Run the terminal UI until the user quits."""
    MinimalTui(core).run()
