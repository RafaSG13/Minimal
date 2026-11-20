"""The prompt of the terminal UI: a multi-line composer with history, completion popup and queued messages.

:class:`ComposerPanel` holds what sits above the status line: the messages queued while a turn runs
(:class:`QueuedView`), the completion popup (:class:`CompletionPopup`) and the input row (``#composer``: the ``>`` glyph
and the :class:`Composer`, id ``#prompt``).

Keys in the composer:

- ``Enter`` sends the text. ``Shift+Enter`` and ``Ctrl+J`` insert a newline; ``Alt+Enter`` is the fallback for terminals
  that send a plain Enter for ``Shift+Enter`` (most do, unless they speak the kitty keyboard protocol).
- ``Up`` on the first line and ``Down`` on the last one walk the history (:class:`~minimal.core.history.PromptHistory`);
  the text typed so far comes back after the newest entry.
- ``@`` and some letters offer the project's files; ``/command `` offers the completions of the command's
  :attr:`~minimal.extensions.registry.Command.complete`. ``Up``/``Down`` move in the popup, ``Tab`` puts the highlighted
  one in the text (and the next one on the next press), ``Enter`` accepts a file, ``Esc`` closes it.
- ``Right`` (or ``Tab`` without a popup) accepts the ghost completion of a command name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.message import Message
from textual.suggester import Suggester
from textual.widgets import Static, TextArea

from minimal.core import mentions
from minimal.core.history import DEFAULT_SIZE, PromptHistory, history_path
from minimal.core.host import Host
from minimal.ui.tui import theme

DEFAULT_MAX_LINES = 8
"""Rows the composer grows to before it scrolls, when ``ui.composer_max_lines`` is not set."""

POPUP_ROWS = 8
"""Completions the popup shows at once."""

QUEUED_ROWS = 3
"""Queued messages drawn above the composer; more are counted."""

PLACEHOLDER = "Message, or / for commands"

NEWLINE_KEYS = ("shift+enter", "ctrl+j", "alt+enter")
"""Keys that insert a newline instead of sending."""


def ui_setting(host: Host, key: str, default: Any) -> Any:
    """The value of the ``ui.<key>`` setting, or ``default`` when no layer sets it."""
    value = host.settings.scalar(f"ui.{key}")[0]
    return default if value is None else value


class CommandSuggester(Suggester):
    """Completes a slash command's name in the prompt (→ accepts), built-in and extension commands alike."""

    def __init__(self, host: Host) -> None:
        super().__init__(use_cache=False, case_sensitive=True)  # extension commands change on reload
        self.host = host

    def suggest(self, value: str) -> str | None:
        """The command line that completes ``value``, or ``None``."""
        return next((line for line in self.host.complete(value) if line != value), None)

    async def get_suggestion(self, value: str) -> str | None:
        return self.suggest(value)


@dataclass
class Completion:
    """What the popup offers: ``items`` replace the text between ``start`` and ``end`` (document locations).

    ``kind`` is ``"command"`` (the slash commands starting what was typed, with their descriptions: ``Enter`` runs the
    highlighted one, ``Tab`` completes it), ``"mention"`` (project files after ``@``) or ``"args"`` (a command's
    :attr:`Command.complete`).
    """

    kind: str
    items: list[str]
    start: tuple[int, int]
    end: tuple[int, int]
    notes: list[str] = field(default_factory=list)


class CompletionPopup(Static):
    """The list of completions, drawn above the composer; the highlighted one is what ``Tab`` inserts next."""

    DEFAULT_CSS = """
    CompletionPopup { display: none; height: auto; padding: 0 3; }
    CompletionPopup.-open { display: block; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=False, **kwargs)
        self.items: list[str] = []
        self.notes: list[str] = []
        self.index = 0

    def show(self, items: list[str], index: int = 0, notes: list[str] | None = None) -> None:
        """Offer ``items`` with ``index`` highlighted (``notes``: a description after each, dimmed); nothing to offer
        hides the popup."""
        self.items, self.index, self.notes = items, index, notes or []
        self.set_class(bool(items), "-open")
        if items:
            first = max(0, index - POPUP_ROWS + 1)
            rows = []
            for i, item in enumerate(items[first : first + POPUP_ROWS], first):
                pieces = [("▸ " if i == index else "  ", "$accent"), (item, "$accent bold" if i == index else "$muted")]
                if i < len(self.notes) and self.notes[i]:
                    pieces.append((f"  {self.notes[i]}", "$dim"))
                rows.append(Content.assemble(*pieces))
            self.update(Content("\n").join(rows))

    def move(self, step: int) -> None:
        """Highlight the next (``1``) or previous (``-1``) completion, wrapping around."""
        if self.items:
            self.show(self.items, (self.index + step) % len(self.items), self.notes)


class QueuedView(Static):
    """The messages waiting for the turn to end, dimmed, one line each."""

    DEFAULT_CSS = """
    QueuedView { display: none; height: auto; padding: 0 3; text-wrap: nowrap; text-overflow: ellipsis; }
    QueuedView.-open { display: block; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=False, **kwargs)

    def set_messages(self, messages: list[str]) -> None:
        """Show ``messages`` as ``queued: <first line>``; the rest of a long list is counted."""
        rows = []
        for message in messages[:QUEUED_ROWS]:
            first, _, rest = message.strip().partition("\n")
            rows.append(Content.assemble((f"queued: {first}{' …' if rest else ''}", "$dim")))
        if len(messages) > QUEUED_ROWS:
            rows.append(Content.assemble((f"queued: … and {len(messages) - QUEUED_ROWS} more", "$dim")))
        self.set_class(bool(rows), "-open")
        self.update(Content("\n").join(rows) if rows else "")


class Composer(TextArea):
    """The multi-line prompt: see the module docstring for its keys.

    It posts :class:`Submitted` on Enter and leaves the text alone: the app calls :meth:`commit` once it accepted it.
    """

    DEFAULT_CSS = """
    Composer { width: 1fr; height: 1; }
    """

    class Submitted(Message):
        """The user pressed Enter: ``value`` is the text, stripped."""

        def __init__(self, composer: Composer, value: str) -> None:
            super().__init__()
            self.composer = composer
            self.value = value

    def __init__(
        self, host: Host, popup: CompletionPopup, report: Callable[[str, Exception], None], **kwargs: Any
    ) -> None:
        self.host = host
        self.popup = popup
        self.report = report
        self._suggester = CommandSuggester(host)
        self._completion: Completion | None = None
        self._applied = False  # Tab put a completion in the text: Enter does not apply another
        self._frozen: tuple[str, tuple[int, int]] | None = None  # text and cursor after our own edit: keep the popup
        self._nav: int | None = None  # index into the history while walking it
        self._draft = ""  # what was typed before walking the history
        self._history_error = False
        self.prompts: PromptHistory | None = None
        super().__init__(
            "",
            soft_wrap=True,
            show_line_numbers=False,
            tab_behavior="focus",
            compact=True,
            highlight_cursor_line=False,
            placeholder=PLACEHOLDER,
            **kwargs,
        )

    # --- text, height ---------------------------------------------------------

    @property
    def max_lines(self) -> int:
        """Rows the composer grows to (``ui.composer_max_lines``)."""
        return ui_setting(self.host, "composer_max_lines", DEFAULT_MAX_LINES)

    def on_mount(self) -> None:
        size = ui_setting(self.host, "history_size", DEFAULT_SIZE)
        self.prompts = PromptHistory(history_path(self.host.status().cwd), size)

    def on_resize(self) -> None:
        self.call_after_refresh(self._fit)

    def _fit(self) -> None:
        """Grow or shrink to the text, up to :attr:`max_lines` rows."""
        self.styles.height = min(max(1, self.wrapped_document.height), self.max_lines)

    def set_text(self, text: str) -> None:
        """Replace the text (undoable), put the cursor at its end and show no completions for it."""
        self.replace(text, self.document.start, self.document.end)
        self.move_cursor(self.document.end)
        self._close_popup()
        self._freeze()
        self._fit()

    def _freeze(self) -> None:
        self._frozen = (self.text, self.cursor_location)

    def commit(self, value: str) -> None:
        """Remember the accepted ``value`` in the history and empty the composer."""
        if self.prompts is not None:
            self.prompts.size = ui_setting(self.host, "history_size", DEFAULT_SIZE)
            try:
                self.prompts.add(value)
            except OSError as e:
                if not self._history_error:
                    self._history_error = True
                    self.app.notify(f"Could not save the prompt history: {e}", severity="warning", markup=False)
        self._nav = None
        self._draft = ""
        self.set_text("")

    # --- history --------------------------------------------------------------

    def _history_older(self) -> bool:
        entries = self.prompts.entries if self.prompts is not None else []
        if not entries:
            return False
        if self._nav is None:
            self._draft = self.text
            self._nav = len(entries)
        if self._nav > 0:
            self._nav -= 1
            self.set_text(entries[self._nav])
        return True

    def _history_newer(self) -> bool:
        if self._nav is None or self.prompts is None:
            return False
        self._nav += 1
        if self._nav >= len(self.prompts.entries):
            self._nav = None
            self.set_text(self._draft)
        else:
            self.set_text(self.prompts.entries[self._nav])
        return True

    def action_cursor_up(self, select: bool = False) -> None:
        if not select:
            if self.popup.items:
                self.popup.move(-1)
                return
            if self.wrapped_document.location_to_offset(self.cursor_location).y == 0 and self._history_older():
                return
        super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:
        if not select:
            if self.popup.items:
                self.popup.move(1)
                return
            last = self.wrapped_document.location_to_offset(self.cursor_location).y >= self.wrapped_document.height - 1
            if last and self._history_newer():
                return
        super().action_cursor_down(select)

    # --- keys -----------------------------------------------------------------

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        if key == "enter":
            self._stop(event)
            self._enter()
        elif key in NEWLINE_KEYS:
            self._stop(event)
            start, end = self.selection
            self.replace("\n", min(start, end), max(start, end))
        elif key == "tab" and self.popup.items:
            self._stop(event)
            self._tab()
        elif key == "tab" and self.suggestion:
            self._stop(event)
            self.insert(self.suggestion)
        elif key == "escape" and self.popup.items:
            self._stop(event)
            self._close_popup()

    @staticmethod
    def _stop(event: events.Key) -> None:
        event.stop()
        event.prevent_default()

    def _enter(self) -> None:
        """Send the text, or accept the file the popup offers."""
        if self._completion is not None and self._completion.kind == "mention":
            if not self._applied:
                self._apply()
            self._close_popup()
            return
        if self._completion is not None and self._completion.kind == "command" and self.popup.items:
            item = self.popup.items[self.popup.index]
            if item != self.text:
                self.replace(item, self._completion.start, self._completion.end)
        self._close_popup()
        self.post_message(self.Submitted(self, self.text.strip()))

    def _tab(self) -> None:
        """Put the highlighted completion in the text and highlight the next one, for the next ``Tab``."""
        self._apply()
        self._applied = True
        self.popup.move(1)

    # --- completion -----------------------------------------------------------

    def update_suggestion(self) -> None:
        """Ghost text after a slash command's name (``/re`` → ``load``), when the cursor is at its end."""
        text = self.text
        ghost = ""
        if text.startswith("/") and "\n" not in text and self.cursor_at_end_of_text and self.selection.is_empty:
            full = self._suggester.suggest(text)
            ghost = full[len(text) :] if full else ""
        self.suggestion = ghost

    def on_text_area_changed(self) -> None:
        self._fit()
        self._refresh_completion()

    def on_text_area_selection_changed(self) -> None:
        self._refresh_completion()

    def _close_popup(self) -> None:
        self._completion = None
        self._applied = False
        self.popup.show([])

    def _refresh_completion(self) -> None:
        if self._frozen is not None:
            if (self.text, self.cursor_location) == self._frozen:
                return
            self._frozen = None
        completion = self._find_completion()
        self._applied = False
        self._completion = completion
        self.popup.show(completion.items if completion else [], 0, completion.notes if completion else None)

    def _find_completion(self) -> Completion | None:
        if not self.selection.is_empty:
            return None
        row, col = self.cursor_location
        line = self.document[row]
        begin = col
        while begin > 0 and not line[begin - 1].isspace():
            begin -= 1
        if begin < col and line[begin] == "@":
            return self._mention(row, begin + 1, col, line)
        text = self.text
        if text.startswith("/") and "\n" not in text and self.cursor_at_end_of_text:
            return self._args(text) if " " in text else self._command(text)
        return None

    def _mention(self, row: int, start: int, col: int, line: str) -> Completion | None:
        end = col
        while end < len(line) and not line[end].isspace():
            end += 1
        found = mentions.match_files(line[start:col], mentions.project_files(self.host.status().cwd))
        if not found or found == [line[start:end]]:
            return None
        return Completion("mention", found, (row, start), (row, end))

    def _command(self, text: str) -> Completion | None:
        """The slash commands that start ``text`` (all of them for ``/``), each with its description."""
        names = self.host.complete(text)
        if not names or names == [text]:
            return None
        commands = self.host.commands()
        notes = [commands[name[1:]].description if name[1:] in commands else "" for name in names]
        return Completion("command", names, (0, 0), self.document.end, notes)

    def _args(self, text: str) -> Completion | None:
        name, _, args = text[1:].partition(" ")
        try:
            found = self.host.complete_args(text)
        except Exception as e:
            self.report(name, e)
            return None
        if not found or found == [args]:
            return None
        return Completion("args", found, (0, len(name) + 2), self.document.end)

    def _apply(self) -> None:
        """Replace the text the completion covers with the highlighted item."""
        completion, popup = self._completion, self.popup
        if completion is None or not popup.items:
            return
        item = popup.items[popup.index]
        self.replace(item, completion.start, completion.end)
        completion.end = (completion.start[0], completion.start[1] + len(item))
        self._freeze()


class ComposerPanel(Vertical):
    """Queued messages, the completion popup and the input row (``>`` and the :class:`Composer`)."""

    DEFAULT_CSS = """
    ComposerPanel { height: auto; }
    """

    def __init__(self, host: Host, report: Callable[[str, Exception], None]) -> None:
        super().__init__(id="composer-panel")
        self.host = host
        self.report = report

    def compose(self) -> ComposeResult:
        popup = CompletionPopup(id="completions")
        yield QueuedView(id="queued")
        yield popup
        with Horizontal(id="composer"):
            yield Static(theme.PROMPT, id="prompt-glyph")
            yield Composer(self.host, popup, self.report, id="prompt")
