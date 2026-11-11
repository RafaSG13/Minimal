"""Transcript widgets: tool-call blocks (with diffs and highlighted previews) and the welcome header."""

from __future__ import annotations

import difflib
import json
import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.content import Content
from textual.highlight import highlight
from textual.widgets import Static

from minimal.core.app import App
from minimal.extensions.registry import LOGO_ALIGN, LogoSpec
from minimal.ui.tui import theme

PREVIEW_LINES = 8
"""Most output lines shown under a tool call."""

EXPANDED_LINES = 500
"""Most output lines shown when a tool call is expanded."""

DIFF_CONTEXT = 2
"""Unchanged lines kept around each change of an edit's diff."""

MAX_LOCATE_BYTES = 2_000_000
"""Largest file searched for the line an edit starts at."""

TAGLINE = "bare by default. yours by config."
"""Second line of the default welcome header."""


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
