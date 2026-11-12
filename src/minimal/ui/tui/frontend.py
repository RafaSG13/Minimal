"""TuiFrontend: how a turn running in a worker thread reaches the Textual UI."""

from __future__ import annotations

import inspect
import threading
from typing import TYPE_CHECKING, Any, Callable

from textual.content import Content
from textual.screen import ModalScreen

from minimal.core.app import ConsoleFrontend
from minimal.ui.tui.screens import ChecklistScreen, ChoiceScreen, ConfirmScreen, TextScreen

if TYPE_CHECKING:
    from minimal.ui.tui.app import MinimalTui



def _theme_swatch(t: Any) -> Content:
    """A sample of a Textual theme: ``Aa`` in its text colours, then a block of each accent colour."""
    parts: list[str | tuple[str, str]] = [(" Aa ", f"{t.foreground} on {t.background}"), " "]
    for color in (t.primary, t.secondary, t.success, t.warning, t.error):
        if color is not None:
            parts.append(("█", color))
    parts.append("  ")
    return Content.assemble(*parts)



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

    def widget(self, name: str) -> object | None:
        return next((widget for widget_name, widget in self.tui.ext_widgets if widget_name == name), None)

    def notify(self, text: str, kind: str) -> None:
        severity = {"info": "information"}.get(kind, kind)
        self._on_ui(lambda: self.tui.notify(text, severity=severity, markup=False))

    def queued(self) -> None:
        self._on_ui(self.tui.submit_queued)

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
