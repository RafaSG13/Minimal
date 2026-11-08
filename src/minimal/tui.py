"""The interactive terminal UI, built with Textual.

Layout: a status line, then the transcript with the ``top`` and ``bottom``
extension areas around it and the prompt below, and an optional ``sidebar``
on the right for extension widgets (see
:meth:`~minimal.registry.Registry.register_widget`).

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
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from textual import events, work
from textual.app import App as TextualApp
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Footer, Input, Label, Markdown, Static

from minimal.app import App
from minimal.commands import ExitRepl, find_command, run_command


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


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question; dismisses with the answer."""

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Vertical {
        width: 80%; max-width: 100; height: auto;
        padding: 1 2; border: thick $warning; background: $surface;
    }
    ConfirmScreen Horizontal { height: auto; margin-top: 1; }
    ConfirmScreen Button { margin-right: 2; }
    """

    BINDINGS = [("y", "answer(True)", "Yes"), ("n", "answer(False)", "No"), ("escape", "answer(False)", "No")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.question, markup=False)
            with Horizontal():
                yield Button("Yes", id="yes", variant="primary")
                yield Button("No", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


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

    def turn_end(self, error: str | None) -> None:
        self._on_ui(self.tui.end_turn, error)

    def reloaded(self) -> None:
        self._on_ui(self.tui.remount_widgets)

    def ask(self, question: str) -> bool:
        if threading.get_ident() == self.tui.ui_thread:  # only inside a suspended terminal command
            try:
                answer = input(f"{question} [y/N] ")
            except EOFError:
                return False
            return re.fullmatch(r"y|yes", answer.strip(), re.IGNORECASE) is not None
        done = threading.Event()
        result: list[bool] = []

        def on_done(answer: bool | None) -> None:
            result.append(bool(answer))
            done.set()

        self.tui.pending_confirm = done
        self.tui.call_from_thread(lambda: self.tui.push_screen(ConfirmScreen(question), callback=on_done))
        done.wait()
        self.tui.pending_confirm = None
        return result[0] if result else False


class MinimalTui(TextualApp):
    """The terminal UI around a :class:`~minimal.app.App`."""

    TITLE = "minimal"

    CSS = """
    #status { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    #body { height: 1fr; }
    #main { width: 1fr; }
    #transcript { height: 1fr; padding: 0 1; }
    #top, #bottom { height: auto; }
    #sidebar { width: 36; border-left: solid $panel; display: none; }
    #sidebar.-has-widgets { display: block; }
    .user { color: $accent; margin-top: 1; }
    .tool { color: $text-muted; }
    .error { color: $error; }
    """

    def __init__(self, core: App) -> None:
        super().__init__()
        self.core = core
        self.ui_thread: int | None = None
        self.pending_confirm: threading.Event | None = None
        self.assistant: Markdown | None = None
        self.output_block: Static | None = None
        self.output_buffer: list[str] = []
        self.output_stderr = False
        self.ext_widgets: list[tuple[str, Widget]] = []
        self.shown_session = core.session

    def compose(self) -> ComposeResult:
        yield Static(id="status", markup=False)
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield Vertical(id="top")
                yield VerticalScroll(id="transcript")
                yield Vertical(id="bottom")
                yield Input(placeholder="Message, or /help", id="prompt")
            yield VerticalScroll(id="sidebar")
        yield Footer()

    async def on_mount(self) -> None:
        self.ui_thread = threading.get_ident()
        self.begin_capture_print(self, stdout=True, stderr=True)
        self.core.frontend = TuiFrontend(self)
        self.query_one("#transcript", VerticalScroll).anchor()
        await self.remount_widgets()
        for e in self.core.extension_errors:
            self.show_error(f"[extension] could not load {e}")
        self.refresh_status()
        self.query_one("#prompt", Input).focus()

    def on_unmount(self) -> None:
        if self.pending_confirm is not None:  # release a worker waiting for an answer
            self.pending_confirm.set()

    # --- transcript ----------------------------------------------------------

    def _add(self, widget: Widget) -> Any:
        """Append ``widget`` to the transcript; returns the mount awaitable."""
        return self.query_one("#transcript", VerticalScroll).mount(widget)

    def show_error(self, text: str) -> Any:
        return self._add(Static(text, markup=False, classes="error"))

    def on_print(self, event: events.Print) -> None:
        """Show printed text (command output, extension messages) as an output block."""
        transcript = self.query_one("#transcript", VerticalScroll)
        last = transcript.children[-1] if transcript.children else None
        if self.output_block is None or last is not self.output_block or self.output_stderr != event.stderr:
            self.output_block = Static("", markup=False, classes="error" if event.stderr else "output")
            self.output_buffer = []
            self.output_stderr = event.stderr
            transcript.mount(self.output_block)
        self.output_buffer.append(event.text)
        self.output_block.update("".join(self.output_buffer).rstrip("\n"))

    async def append_text(self, chunk: str) -> None:
        # Mount first: Markdown resets its source on mount, which would drop an earlier append.
        if self.assistant is None:
            self.assistant = Markdown("", classes="assistant")
            await self._add(self.assistant)
        await self.assistant.append(chunk)

    def show_tool_call(self, call: dict) -> Any:
        self.assistant = None
        args = json.dumps(call["input"], ensure_ascii=False)[:120]
        return self._add(Static(f"› {call['name']} {args}", markup=False, classes="tool"))

    def end_turn(self, error: str | None) -> Any:
        self.assistant = None
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

    def refresh_status(self) -> None:
        core = self.core
        self.query_one("#status", Static).update(
            f"minimal · {core.provider_name} · {core.model} · session {core.session.id} · "
            f"{len(core.extensions)} extension(s) · /help"
        )

    # --- input ---------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.clear()
        if not line:
            return
        event.input.disabled = True
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
        self._add(Static(f"> {line}", markup=False, classes="user"))
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
            self.query_one("#transcript", VerticalScroll).remove_children()
            self.shown_session = self.core.session
            self.assistant = None
        self.refresh_status()
        self.refresh_widgets()
        prompt = self.query_one("#prompt", Input)
        prompt.disabled = False
        prompt.focus()


def run_tui(core: App) -> None:
    """Run the terminal UI until the user quits."""
    MinimalTui(core).run()
