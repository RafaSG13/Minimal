"""The interactive terminal UI, built with Textual.

Layout, top to bottom: the ``top`` extension area, the transcript (a welcome
header (the built-in wordmark or an extension's pixel-art logo), then user messages, assistant
replies, tool calls and command output), the ``bottom`` extension area, the
working indicator, the prompt and the status line (model, provider, directory,
git branch). An optional ``sidebar`` on the right holds extension widgets (see
:meth:`~minimal.registry.Registry.register_widget`). Colours come from the
active theme's CSS variables and glyphs from :mod:`minimal.theme`.

Turns and slash commands run in a worker thread so the UI stays responsive;
:class:`TuiFrontend` hands their output and confirmations back to the UI
thread. Esc interrupts the turn in progress. Anything printed (slash-command
output, extension messages) is shown in the transcript. Commands marked
``terminal`` run with the UI suspended.
"""

from __future__ import annotations

import asyncio
import difflib
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

from textual import events, work
from textual.app import App as TextualApp
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.highlight import highlight
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widget import Widget
from textual.widgets import Input, Label, Markdown, OptionList, SelectionList, Static
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from minimal import config, theme
from minimal.app import App, ConsoleFrontend
from minimal.commands import BUILTIN_COMMANDS, ExitRepl, find_command, run_command
from minimal.registry import LOGO_ALIGN, LogoSpec

PREVIEW_LINES = 8
"""Most output lines shown under a tool call."""

EXPANDED_LINES = 500
"""Most output lines shown when a tool call is expanded."""

DIFF_CONTEXT = 2
"""Unchanged lines kept around each change of an edit's diff."""

MAX_LOCATE_BYTES = 2_000_000
"""Largest file searched for the line an edit starts at."""

REPLAY_LIMIT = 200
"""Most messages of a resumed session drawn in the transcript."""

TAGLINE = "bare by default. yours by config."
"""Second line of the default welcome header."""


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
            # core.fsmonitor=false: a repository's own config must not make git run a command
            ["git", "-c", "core.fsmonitor=false", "--no-optional-locks", "status", "--porcelain=v1", "--branch"],
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
    return (
        "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path
    )


def _version() -> str:
    try:
        return version("minimal")
    except PackageNotFoundError:
        return ""


# --- tool calls ----------------------------------------------------------------


def _one_line(text: str, limit: int = 160) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _tool_title(call: dict) -> Content:
    """The tool call's header line, without the state icon: ``$ cmd``, ``read path``, ``name {json}``."""
    name, inp = call["name"], call["input"]
    if name == "bash" and isinstance(inp.get("command"), str):
        return Content.assemble(("$ ", "$muted"), (_one_line(inp["command"]), "bold"))
    parts: list[str | tuple[str, str]] = [(name, "bold")]
    if name in ("read", "write", "edit") and isinstance(inp.get("path"), str):
        parts += [" ", (inp["path"], "$secondary")]
        if name == "read" and ("offset" in inp or "limit" in inp):
            start = inp.get("offset", 1)
            end = (
                f"{start + inp['limit'] - 1}"
                if isinstance(inp.get("limit"), int)
                else ""
            )
            parts.append((f":{start}-{end}", "$muted"))
        if name == "write" and isinstance(inp.get("content"), str):
            parts.append((f" ({len(inp['content'].splitlines())} lines)", "$muted"))
    elif inp:
        parts += [" ", (_one_line(json.dumps(inp, ensure_ascii=False), 120), "$muted")]
    return Content.assemble(*parts)


def _edit_start_line(cwd: str | None, inp: dict) -> int | None:
    """The 1-based line of ``inp["path"]`` where the edit starts, or ``None`` if it cannot be found.

    ``old_string`` is looked for first; ``new_string`` is what a replayed (already applied) edit finds.
    """
    path = inp.get("path")
    if cwd is None or not isinstance(path, str):
        return None
    file = Path(cwd, path)
    try:
        if file.stat().st_size > MAX_LOCATE_BYTES:
            return None
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for needle in (inp["old_string"], inp["new_string"]):
        if needle and (idx := text.find(needle)) >= 0:
            return text.count("\n", 0, idx) + 1
    return None


def _diff_line(gutter: Content, sign: str, text: Content | str, style: str) -> Content:
    body = text if isinstance(text, Content) else Content.styled(text, style)
    return Content.assemble(gutter, (f"{sign} ", style), body)


def _changed_spans(a: str, b: str) -> tuple[Content, Content]:
    """``a`` as a removed line and ``b`` as an added line, their differing characters in reverse."""
    removed: list[tuple[str, str]] = []
    added: list[tuple[str, str]] = []
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in ops:
        same = tag == "equal"
        if i2 > i1:
            removed.append((a[i1:i2], "$diff-removed" if same else "$diff-removed reverse"))
        if j2 > j1:
            added.append((b[j1:j2], "$diff-added" if same else "$diff-added reverse"))
    return Content.assemble(*removed), Content.assemble(*added)


def _edit_diff(inp: dict, start: int | None) -> list[Content]:
    """An edit's diff, one line each: changes with :data:`DIFF_CONTEXT` unchanged lines around them.

    With ``start`` (the file line where ``old_string`` begins) every line gets its line number.
    """
    old = inp["old_string"].splitlines() or [""]
    new = inp["new_string"].splitlines() or [""]
    width = len(str(start + max(len(old), len(new)))) if start is not None else 0

    def gutter(n: int) -> Content:
        return Content.styled(f"{n:>{width}} ", "$dim") if start is not None else Content("")

    lines: list[Content] = []
    ops = difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            head = DIFF_CONTEXT if idx > 0 else 0
            tail = DIFF_CONTEXT if idx < len(ops) - 1 else 0
            keep = list(range(i1, i2))
            if len(ops) > 1 and len(keep) > head + tail:
                dropped = len(keep) - head - tail
                keep = keep[:head] + [-dropped] + (keep[-tail:] if tail else [])
            for i in keep:
                if i < 0:
                    pad = " " * (width + 1) if start is not None else ""
                    lines.append(Content.styled(f"{pad}⋯ {-i} unchanged lines", "$dim"))
                else:
                    lines.append(_diff_line(gutter((start or 0) + i), " ", old[i], "$muted"))
        elif tag == "replace" and i2 - i1 == j2 - j1:
            pairs = [_changed_spans(old[i1 + k], new[j1 + k]) for k in range(i2 - i1)]
            for k, (removed, _) in enumerate(pairs):
                lines.append(_diff_line(gutter((start or 0) + i1 + k), "-", removed, "$diff-removed"))
            for k, (_, added) in enumerate(pairs):
                lines.append(_diff_line(gutter((start or 0) + j1 + k), "+", added, "$diff-added"))
        else:
            for i in range(i1, i2):
                lines.append(_diff_line(gutter((start or 0) + i), "-", old[i], "$diff-removed"))
            for j in range(j1, j2):
                lines.append(_diff_line(gutter((start or 0) + j), "+", new[j], "$diff-added"))
    return lines


def _highlighted(text: str, path: str | None) -> list[Content]:
    """``text`` syntax-highlighted for ``path``'s language, one Content per line."""
    lines = highlight(text, path=path, tab_size=4).split("\n")
    if len(lines) != text.count("\n") + 1:  # the highlighter merged or dropped lines
        return [Content(line.expandtabs(4)) for line in text.split("\n")]
    return lines


_READ_LINE = re.compile(r"(\d+)\t")


def _numbered(lines: list[str], path: str | None) -> list[Content]:
    """``read`` output (``N<TAB>text`` lines) highlighted for ``path``, with dim line numbers; other lines (notes) dim."""
    matches = [_READ_LINE.match(line) for line in lines]
    numbers = [m.group(1) for m in matches if m]
    code = iter(
        _highlighted("\n".join(line[m.end() :] for line, m in zip(lines, matches) if m), path)
        if numbers
        else []
    )
    width = max(map(len, numbers), default=0)
    return [
        Content.assemble((m.group(1).rjust(width), "$dim"), "  ", next(code, Content("")))
        if m
        else Content.styled(line.expandtabs(4), "$dim")
        for line, m in zip(lines, matches)
    ]


class ToolBlock(Static):
    """One tool call: a header line and a preview of its body, on a background that shows its state.

    The CSS classes ``-pending``, ``-success`` and ``-error`` select the background.
    A click expands a body longer than :data:`PREVIEW_LINES`, and collapses it again.
    """

    def __init__(self, call: dict, cwd: str | None = None) -> None:
        super().__init__(classes="tool -pending")
        self.call = call
        self.icon = Content.styled(theme.RUNNING, "$warning")
        self.lines: list[Content] = []  # the body, at most EXPANDED_LINES
        self.total = 0  # body lines before the cap
        self.tail = False  # the end of the body matters most (bash output)
        self.expanded = False
        name, inp = call["name"], call["input"]
        if (
            name == "edit"
            and isinstance(inp.get("old_string"), str)
            and isinstance(inp.get("new_string"), str)
        ):
            self._set_lines(_edit_diff(inp, _edit_start_line(cwd, inp)), len)
        elif name == "write" and isinstance(inp.get("content"), str):
            lines = inp["content"].rstrip("\n").split("\n")
            self._set_lines(_highlighted("\n".join(lines[:EXPANDED_LINES]), self._path()), len(lines))
        self.update(self._text())

    def _path(self) -> str | None:
        path = self.call["input"].get("path")
        return path if isinstance(path, str) else None

    def _set_lines(self, lines: list[Content], total: Any, tail: bool = False) -> None:
        """Keep ``lines`` as the body; ``total`` is the uncapped line count, or ``len`` to count ``lines``."""
        self.total = len(lines) if total is len else total
        self.tail = tail
        self.lines = lines[-EXPANDED_LINES:] if tail else lines[:EXPANDED_LINES]

    def finish(self, result: dict) -> None:
        """Show ``result`` (a ``tool_result`` block) and switch to the success or error state."""
        output = (
            result["content"]
            if isinstance(result["content"], str)
            else json.dumps(result["content"])
        )
        output = output.strip("\n")
        name = self.call["name"]
        if result["is_error"]:
            self.icon = Content.styled(theme.ERROR, "$error")
            lines = output.expandtabs(4).splitlines()
            self._set_lines([Content.styled(line, "$error") for line in lines], len)
            self.set_classes("tool -error")
        else:
            self.icon = Content.styled(theme.SUCCESS, "$success")
            if name == "read" and output:
                lines = output.split("\n")
                self._set_lines(_numbered(lines[:EXPANDED_LINES], self._path()), len(lines))
            elif name not in ("edit", "write") and output:
                lines = output.expandtabs(4).splitlines()
                tail = name == "bash"
                kept = lines[-EXPANDED_LINES:] if tail else lines[:EXPANDED_LINES]
                self._set_lines([Content.styled(line, "$muted") for line in kept], len(lines), tail)
            self.set_classes("tool -success")
        self.update(self._text())

    def on_click(self, event: events.Click) -> None:
        if self.total > PREVIEW_LINES:
            self.expanded = not self.expanded
            self.update(self._text())

    def _body(self) -> Content | None:
        """The body as shown: a :data:`PREVIEW_LINES` preview, or all of it when expanded."""
        if not self.lines:
            return None
        hidden = self.total - PREVIEW_LINES
        if hidden <= 0:
            shown = list(self.lines)
        elif not self.expanded:
            if self.tail:
                note = Content.styled(f"… {hidden} earlier lines · click to expand", "$dim")
                shown = [note, *self.lines[-PREVIEW_LINES:]]
            else:
                note = Content.styled(f"… {hidden} more lines · click to expand", "$dim")
                shown = [*self.lines[:PREVIEW_LINES], note]
        else:
            shown = list(self.lines)
            cut = self.total - len(self.lines)
            if cut > 0:
                note = Content.styled(f"… {cut} more lines not shown", "$dim")
                shown.insert(0 if self.tail else len(shown), note)
            shown.append(Content.styled("click to collapse", "$dim"))
        return Content("\n").join(shown)

    def _text(self) -> Content:
        text = Content.assemble(self.icon, " ", _tool_title(self.call))
        body = self._body()
        return Content.assemble(text, "\n", body) if body is not None else text


# --- chrome --------------------------------------------------------------------


def logo_content(spec: LogoSpec) -> Content:
    """``spec``'s pixel art as text: each terminal row draws two pixel rows with half blocks."""
    rows = list(spec.art)
    if len(rows) % 2:
        rows.append("." * len(rows[0]))
    lines = []
    for top_row, bottom_row in zip(rows[0::2], rows[1::2]):
        parts: list[str | tuple[str, str]] = []
        for t, b in zip(top_row, bottom_row):
            top, bottom = spec.colors.get(t), spec.colors.get(b)
            if top is None and bottom is None:
                parts.append(" ")
            elif bottom is None:
                parts.append((theme.UPPER_HALF, top))
            elif top is None:
                parts.append((theme.LOWER_HALF, bottom))
            elif top == bottom:
                parts.append((theme.FULL_BLOCK, top))
            else:
                parts.append((theme.UPPER_HALF, f"{top} on {bottom}"))
        lines.append(Content.assemble(*parts))
    return Content("\n").join(lines)


def _wordmark() -> Content:
    """The default welcome header (no extension logo): the spaced name between two nodes, then the tagline and version."""
    parts: list[str | tuple[str, str]] = [
        (theme.HEADER_NODE, "$muted"),
        "  ",
        ("m i n i m a l", "bold $accent"),
        " ",
        (theme.CURSOR, "$accent"),
        "  ",
        (theme.HEADER_NODE, "$muted"),
        "\n",
        " " * (len(theme.HEADER_NODE) + 2),
        (TAGLINE, "$muted"),
    ]
    if _version():
        parts.append((f" v{_version()}", "$dim"))
    return Content.assemble(*parts)


def _header(core: App) -> Content:
    """The header text under an extension logo: the version (the wordmark shows its own)."""
    parts: list[str | tuple[str, str]] = []
    if core.registry.logo is not None and _version():
        parts += [(f"v{_version()}", "$dim"), "\n"]
    return Content.assemble(*parts)


class Header(Vertical):
    """The welcome block at the top of the transcript: the extension logo or the default wordmark, aligned as the logo asks (left without one), then the header text."""

    def __init__(self, core: App) -> None:
        super().__init__()
        self.core = core

    def compose(self) -> ComposeResult:
        logo = self.core.registry.logo
        align = logo.align if logo is not None else "left"
        for value in LOGO_ALIGN:
            self.set_class(value == align, f"-align-{value}")
        if logo is not None:
            yield Static(logo_content(logo), classes="logo")
        else:
            yield Static(_wordmark(), classes="wordmark")
        yield Static(_header(self.core), classes="info")


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question picked with the arrow keys; dismisses with the answer."""

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; background: $modal-overlay; }
    ConfirmScreen > Vertical {
        width: 80%; max-width: 100; height: auto;
        padding: 1 2; background: $surface;
    }
    ConfirmScreen .title { width: 100%; text-style: bold; margin-bottom: 1; }
    ConfirmScreen #actions { height: auto; margin-top: 1; }
    ConfirmScreen .choice {
        width: auto; min-width: 12; margin-right: 2; padding: 0 2;
        content-align: center middle; background: $panel; color: $muted;
    }
    ConfirmScreen #yes.-selected { background: $success; color: $background; text-style: bold; }
    ConfirmScreen #no.-selected { background: $error; color: $background; text-style: bold; }
    ConfirmScreen #hint { color: $dim; margin-left: 1; }
    """

    BINDINGS = [
        ("left,up", "pick(True)", "Allow"),
        ("right,down", "pick(False)", "Deny"),
        ("tab", "toggle", "Switch"),
        ("enter", "confirm", "Confirm"),
        ("escape", "answer(False)", "Deny"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question
        self.allow = (
            True  # enter on an untouched prompt allows, like most agent harnesses
        )

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(
                Content.assemble((theme.WARNING, "$warning"), " Permission"),
                classes="title",
            )
            yield Label(self.question, markup=False)
            with Horizontal(id="actions"):
                yield Label("Allow", id="yes", classes="choice -selected")
                yield Label("Deny", id="no", classes="choice")
                yield Label("← → choose · enter confirms · esc denies", id="hint")

    def action_pick(self, allow: bool) -> None:
        self.allow = allow
        self.query_one("#yes").set_class(allow, "-selected")
        self.query_one("#no").set_class(not allow, "-selected")

    def action_toggle(self) -> None:
        self.action_pick(not self.allow)

    def action_confirm(self) -> None:
        self.dismiss(self.allow)

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)

    def on_click(self, event: events.Click) -> None:
        if event.widget is not None and event.widget.id in ("yes", "no"):
            self.dismiss(event.widget.id == "yes")


class DialogScreen(ModalScreen[Any]):
    """A titled box opened by a command through the frontend; esc cancels (dismisses with ``None``)."""

    DEFAULT_CSS = """
    DialogScreen { align: center middle; background: $modal-overlay; }
    DialogScreen > Vertical {
        width: 80%; max-width: 100; height: auto; max-height: 80%;
        padding: 1 2; background: $surface;
    }
    DialogScreen .title {
        width: 100%; text-style: bold; margin-bottom: 1;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    DialogScreen OptionList, DialogScreen SelectionList { height: auto; max-height: 20; background: $surface; }
    DialogScreen OptionList:focus, DialogScreen SelectionList:focus { background-tint: $foreground 0%; }
    DialogScreen OptionList > .option-list--option-highlighted,
    DialogScreen OptionList:focus > .option-list--option-highlighted,
    DialogScreen SelectionList > .option-list--option-highlighted,
    DialogScreen SelectionList:focus > .option-list--option-highlighted {
        color: $foreground; background: $panel; text-style: bold;
    }
    DialogScreen SelectionList > .selection-list--button { color: $border-muted; background: $surface; }
    DialogScreen SelectionList > .selection-list--button-highlighted { color: $border-muted; background: $panel; }
    DialogScreen SelectionList > .selection-list--button-selected { color: $success; background: $surface; }
    DialogScreen SelectionList > .selection-list--button-selected-highlighted { color: $success; background: $panel; }
    DialogScreen Input.-textual-compact { background: $panel; padding: 0 1; }
    DialogScreen .hint { color: $dim; margin-top: 1; }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]
    HINT = ""

    def __init__(self, heading: str) -> None:
        super().__init__()
        self.heading = heading  # not ``title``: Screen.title is a reactive

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.heading, classes="title", markup=False)
            yield self.body()
            yield Label(self.HINT, classes="hint")

    def body(self) -> Widget:
        """The widget that takes the answer."""
        raise NotImplementedError

    def action_cancel(self) -> None:
        self.dismiss(None)


class CommandSuggester(Suggester):
    """Completes a slash command's name in the prompt (→ accepts), built-in and extension commands alike."""

    def __init__(self, core: App) -> None:
        super().__init__(use_cache=False, case_sensitive=True)  # extension commands change on reload
        self.core = core

    async def get_suggestion(self, value: str) -> str | None:
        if not value.startswith("/") or " " in value:
            return None
        typed = value[1:]
        for name in sorted({*BUILTIN_COMMANDS, *self.core.registry.commands}):
            if name.startswith(typed) and name != typed:
                return "/" + name
        return None


def _theme_swatch(t: Any) -> Content:
    """A sample of a Textual theme: ``Aa`` in its text colours, then a block of each accent colour."""
    parts: list[str | tuple[str, str]] = [(" Aa ", f"{t.foreground} on {t.background}"), " "]
    for color in (t.primary, t.secondary, t.success, t.warning, t.error):
        if color is not None:
            parts.append(("█", color))
    parts.append("  ")
    return Content.assemble(*parts)


class ChoiceScreen(DialogScreen):
    """Pick one option; dismisses with its value."""

    HINT = "enter selects · esc cancels"

    def __init__(self, heading: str, options: list[tuple[str, str | Content]]) -> None:
        super().__init__(heading)
        self.options = options

    def body(self) -> Widget:
        return OptionList(
            *(
                Option(label if isinstance(label, Content) else Content(label), id=value)
                for value, label in self.options
            ),
            compact=True,
        )

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
        return SelectionList[str](
            *(
                Selection(Content(label), value, checked)
                for value, label, checked in self.options
            ),
            compact=True,
        )

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
        return Input(compact=True)

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
        self._on_ui(self.tui.extensions_reloaded)

    def themes(self) -> tuple[list[str], str | None]:
        return list(self.tui.available_themes), self.tui.theme

    def set_theme(self, name: str) -> None:
        self._on_ui(setattr, self.tui, "theme", name)

    def _modal(self, screen: ModalScreen) -> Any:
        """Show ``screen`` from a worker thread and wait for its dismiss value (``None`` on quit)."""
        done = threading.Event()
        result: list[Any] = []

        def on_done(value: Any) -> None:
            result.append(value)
            done.set()

        self.tui.pending_modal = done
        self.tui.call_from_thread(
            lambda: self.tui.push_screen(screen, callback=on_done)
        )
        done.wait()
        self.tui.pending_modal = None
        return result[0] if result else None

    def _in_terminal(self) -> bool:
        """True on the UI thread, which only asks during a suspended terminal command."""
        return threading.get_ident() == self.tui.ui_thread

    def ask(self, question: str) -> bool:
        if self._in_terminal():
            return ConsoleFrontend().ask(question)
        return bool(self._modal(ConfirmScreen(question)))

    def choose(self, title: str, options: list[tuple[str, str]]) -> str | None:
        if self._in_terminal():
            return ConsoleFrontend().choose(title, options)
        return self._modal(ChoiceScreen(title, options))

    def choose_theme(self, title: str, options: list[tuple[str, str]]) -> str | None:
        """Like :meth:`choose`, each label after a sample of the theme's colours."""
        if self._in_terminal():
            return ConsoleFrontend().choose(title, options)
        themes = self.tui.available_themes
        decorated: list[tuple[str, str | Content]] = [
            (value, Content.assemble(_theme_swatch(themes[value]), label) if value in themes else label)
            for value, label in options
        ]
        return self._modal(ChoiceScreen(title, decorated))

    def choose_many(
        self, title: str, options: list[tuple[str, str, bool]]
    ) -> list[str] | None:
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

    BINDINGS = [
        Binding("escape", "interrupt", "Interrupt turn", show=False),
        Binding("ctrl+b", "toggle_sidebar", "Toggle sidebar", show=False),
    ]

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
    #sidebar.-collapsed { display: none; }

    Header { height: auto; padding: 1 1 0 1; }
    Header.-align-center Static { text-align: center; }
    Header.-align-right Static { text-align: right; }
    Header .logo, Header .wordmark { text-wrap: nowrap; text-overflow: clip; }
    Header .wordmark { margin-bottom: 1; }
    .user { background: $user-message-bg; padding: 1 2; margin-top: 1; }
    Markdown.assistant { padding: 0 1; margin-top: 1; }
    Markdown.assistant MarkdownBlock:dark > .code_inline,
    Markdown.assistant MarkdownBlock:light > .code_inline { background: $surface; color: $md-code; }
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

    def __init__(self, core: App, theme_name: str | None = None) -> None:
        super().__init__()
        self.core = core
        self.sync_themes(theme_name)
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
        self.in_turn = False  # a model turn, not a slash command, is running: esc interrupts it
        self.git: tuple[str, bool] | None = None  # last git_status, refreshed off the UI thread
        self.status_errors: set[str] = set()  # status segments whose error was already shown

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield Vertical(id="top")
                with VerticalScroll(id="transcript"):
                    yield Header(self.core)
                yield Vertical(id="bottom")
                yield Static(id="working")
                with Horizontal(id="composer"):
                    yield Static(theme.PROMPT, id="prompt-glyph")
                    yield Input(
                        placeholder="Message, or / for commands",
                        id="prompt",
                        suggester=CommandSuggester(self.core),
                    )
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
        pending = self.replay_history()
        if pending is not None:
            await pending
        for e in self.core.extension_errors:
            self.show_error(f"[extension] could not load {e}")
        if notice := self.core.untrusted_notice():
            self._add(Static(Content.assemble((theme.WARNING, "$warning"), f" {notice}"), classes="output"))
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

    def replay_history(self) -> Any:
        """Draw the current session's last :data:`REPLAY_LIMIT` messages in the transcript.

        Returns the mount awaitable, or ``None`` if there is nothing to draw.
        """
        messages = self.core.messages
        hidden = max(0, len(messages) - REPLAY_LIMIT)
        widgets: list[Widget] = []
        if hidden:
            widgets.append(Static(f"… {hidden} earlier messages not shown", classes="output"))
        blocks: dict[str, ToolBlock] = {}
        for message in messages[hidden:]:
            content = message["content"]
            if isinstance(content, str):
                widgets.append(Static(content, markup=False, classes="user"))
                continue
            for block in content:
                kind = block.get("type")
                if kind == "text" and message["role"] == "assistant":
                    widgets.append(Markdown(block["text"], classes="assistant"))
                elif kind == "tool_use":
                    blocks[block["id"]] = ToolBlock(block, self.core.cwd)
                    widgets.append(blocks[block["id"]])
                elif kind == "tool_result" and block["tool_use_id"] in blocks:
                    blocks[block["tool_use_id"]].finish(block)
        if not widgets:
            return None
        self.call_after_refresh(self._follow)
        return self.query_one("#transcript", VerticalScroll).mount(*widgets)

    def show_error(self, text: str) -> Any:
        return self._add(Static(Content(f"{theme.ERROR} {text}"), classes="error"))

    def on_print(self, event: events.Print) -> None:
        """Show printed text (command output, extension messages) as an output block."""
        transcript = self.query_one("#transcript", VerticalScroll)
        last = transcript.children[-1] if transcript.children else None
        if (
            self.output_block is None
            or last is not self.output_block
            or self.output_stderr != event.stderr
        ):
            self.output_block = Static(
                "", markup=False, classes="error" if event.stderr else "output"
            )
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
        block = ToolBlock(call, self.core.cwd)
        self.tool_blocks[call["id"]] = block
        mounted = self._add(block)
        self.refresh_live({"kind": "tool_call", "call": call})
        return mounted

    def show_tool_result(self, call: dict, result: dict) -> None:
        block = self.tool_blocks.pop(call["id"], None)
        if block is not None:
            block.finish(result)
            self.call_after_refresh(self._follow)
        self.refresh_live({"kind": "tool_result", "call": call, "result": result})

    def end_turn(self, error: str | None) -> Any:
        self.assistant = None
        self.tool_blocks.clear()
        if error:
            return self.show_error(f"Error: {error}")
        return None

    # --- extension themes and widgets ----------------------------------------

    def sync_themes(self, prefer: str | None = None) -> None:
        """Offer the built-in theme plus the ones extensions registered, and nothing else.

        Textual's own themes are dropped: they lack the UI's CSS variables.
        Switches to ``prefer`` if offered, and to the default if the current theme is gone.
        """
        wanted = theme.available(self.core.registry)
        for t in wanted.values():
            self.register_theme(t)
        if prefer in wanted:
            self.theme = prefer
        elif self.theme not in wanted:
            self.theme = theme.DEFAULT
        for name in set(self.available_themes) - set(wanted):
            self.unregister_theme(name)

    async def extensions_reloaded(self) -> None:
        """Pick up the reloaded registry: its themes (an edited one repaints), its widgets and the header (logo and extensions list)."""
        self.sync_themes()
        self.refresh_css(animate=False)
        await self.remount_widgets()
        for header in self.query(Header):
            await header.recompose()

    async def remount_widgets(self) -> None:
        """Replace every extension widget with fresh ones from the registry."""
        for area in ("top", "bottom", "sidebar"):
            await self.query_one(f"#{area}").remove_children()
        self.ext_widgets = []
        self.status_errors.clear()
        for spec in list(self.core.registry.widgets.values()):
            try:
                widget = spec.create(self.core)
                await self.query_one(f"#{spec.area}").mount(widget)
                self.ext_widgets.append((spec.name, widget))
            except Exception as e:
                self.show_error(f"[extension] widget {spec.name}: {e}")
        has_sidebar = any(
            spec.area == "sidebar" for spec in self.core.registry.widgets.values()
        )
        self.query_one("#sidebar").set_class(has_sidebar, "-has-widgets")
        self.refresh_widgets()

    def _call_widgets(self, method: str, *args: Any) -> None:
        """Call ``method(*args)`` on every extension widget that has it, showing any error."""
        for name, widget in self.ext_widgets:
            fn = getattr(widget, method, None)
            if not callable(fn):
                continue
            try:
                fn(*args)
            except Exception as e:
                self.show_error(f"[extension] widget {name}: {e}")

    def refresh_widgets(self) -> None:
        """Call ``refresh_state(core)`` on every extension widget that has it."""
        self._call_widgets("refresh_state", self.core)

    def refresh_live(self, event: dict) -> None:
        """Call ``refresh_live(core, event)`` on every extension widget that has it, then redraw the status line."""
        self._call_widgets("refresh_live", self.core, event)
        self._render_status()

    def action_toggle_sidebar(self) -> None:
        """Hide or show the sidebar of extension widgets."""
        self.query_one("#sidebar").toggle_class("-collapsed")

    # --- status line and working indicator -----------------------------------

    def refresh_status(self) -> None:
        """Redraw the status line now, then update its git part in the background."""
        self._render_status()
        self.refresh_git()

    @work(thread=True, exclusive=True, group="git", exit_on_error=False)
    def refresh_git(self) -> None:
        """Run ``git status`` in a worker thread (it can take up to a second) and show the result."""
        git = git_status(self.core.cwd)
        try:
            self.call_from_thread(self._set_git, git)
        except RuntimeError:  # the app closed meanwhile
            pass

    def _set_git(self, git: tuple[str, bool] | None) -> None:
        if git != self.git:
            self.git = git
            self._render_status()

    def _status_segments(self, side: str) -> list[Content]:
        """The extension segments for ``side`` of the status line; a failing one is skipped and reported once."""
        segments = []
        for spec in list(self.core.registry.status.values()):
            if spec.side != side:
                continue
            try:
                value = spec.render(self.core)
            except Exception as e:
                if spec.name not in self.status_errors:
                    self.status_errors.add(spec.name)
                    self.show_error(f"[extension] status {spec.name}: {e}")
                continue
            if value is not None:
                segments.append(value if isinstance(value, Content) else Content(str(value)))
        return segments

    def _render_status(self) -> None:
        core = self.core
        sep = (f" {theme.SEPARATOR} ", "$border-muted")
        parts: list[str | tuple[str, str] | Content] = [
            " ",
            (f"{theme.MODEL} {core.model}", "$status-model"),
            (f"{theme.DOT}{core.provider_name}", "$muted"),
            sep,
            (_short_path(core.cwd), "$status-path"),
        ]
        git = self.git
        if git is not None:
            branch, dirty = git
            color = "$status-git-dirty" if dirty else "$status-git-clean"
            parts += [sep, (f"{theme.BRANCH} {branch}{' *' if dirty else ''}", color)]
        for segment in self._status_segments("left"):
            parts += [sep, segment]
        self.query_one("#status-left", Static).update(Content.assemble(*parts))
        right = f"{len(core.extensions)} ext{theme.DOT}session {core.session.id}"
        tokens = core.context_tokens
        if tokens is not None:
            right = f"{tokens / 1000:.1f}k ctx{theme.DOT}{right}"
        untrusted = []
        if core.untrusted is not None:
            untrusted.append(Content.assemble((f"{theme.WARNING} {len(core.untrusted.sources)} untrusted", "$warning")))
        self.query_one("#status-right", Static).update(
            Content(theme.DOT).join([*self._status_segments("right"), *untrusted, Content(right)])
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
        parts: list[str | tuple[str, str]] = [(frame, "$accent"), " "]
        if self.in_turn and self.core.cancelled.is_set():
            parts.append(("Interrupting…", "$muted"))
        else:
            parts += [("Working…", "$muted"), (f" {elapsed:.0f}s", "$dim")]
            if self.in_turn:
                parts.append((" · esc interrupts", "$dim"))
        self.query_one("#working", Static).update(Content.assemble(*parts))

    def action_interrupt(self) -> None:
        """Ask the turn in progress to stop (see :class:`~minimal.registry.TurnInterrupted`)."""
        if self.in_turn and not self.core.cancelled.is_set():
            self.core.cancelled.set()
            self.refresh_working()

    # --- input ---------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.clear()
        if not line:
            return
        self.in_turn = not line.startswith("/")
        self.set_busy(True)
        self.refresh_live({"kind": "start", "turn": self.in_turn})
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
            transcript.mount(Header(self.core))
            self.replay_history()
            self.shown_session = self.core.session
            self.assistant = None
        self.refresh_status()
        self.refresh_widgets()
        self.in_turn = False
        self.set_busy(False)
        self.query_one("#prompt", Input).focus()


def run_tui(core: App) -> None:
    """Run the terminal UI until the user quits; remember the theme picked with /theme or ctrl+p."""
    tui = MinimalTui(core, config.load_theme())
    started = tui.theme
    tui.run()
    if tui.theme != started:
        config.save_theme(tui.theme)
