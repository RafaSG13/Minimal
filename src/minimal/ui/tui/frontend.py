"""TuiFrontend: how a turn running in a worker thread reaches the Textual UI; and the UI's own ``/theme``."""

from __future__ import annotations

import inspect
import threading
from typing import TYPE_CHECKING, Any, Callable, Literal, cast

from textual.content import Content
from textual.screen import ModalScreen

from minimal.core.app import ConsoleFrontend
from minimal.extensions.registry import CommandRun
from minimal.ui.tui.screens import (
    ChecklistScreen,
    ChoiceScreen,
    ConfirmScreen,
    OverlayScreen,
    PermissionScreen,
    PickScreen,
    TextScreen,
)
from minimal.ui.tui.widgets import to_content

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

    def _get(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` on the UI thread and return what it returns (unlike :meth:`_on_ui`, which drops it there)."""
        if threading.get_ident() == self.tui.ui_thread:
            return fn()
        return self.tui.call_from_thread(fn)

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

    def themes(self) -> tuple[list[str], str]:
        """The colour themes on offer and the current one (the TUI's own: they are not part of the Frontend protocol)."""
        return list(self.tui.available_themes), self.tui.theme

    def set_theme(self, name: str) -> bool:
        """Switch to theme ``name`` if the UI offers it; ``False`` if not. The UI fires ``theme_change`` when its colours changed."""
        if name not in self.tui.available_themes:
            return False
        self._on_ui(setattr, self.tui, "theme", name)
        return True

    def copy_text(self, text: str) -> bool:
        """Put ``text`` on the clipboard through the terminal (OSC 52); ``/copy`` calls it. The TUI's own, like :meth:`themes`."""
        self._on_ui(self.tui.copy_to_clipboard, text)
        return True

    def widget(self, name: str) -> object | None:
        return next((widget for widget_name, widget in self.tui.ext_widgets if widget_name == name), None)

    def notify(self, text: str, kind: str) -> None:
        severity = cast("Literal['information', 'warning', 'error']", {"info": "information"}.get(kind, kind))
        self._on_ui(lambda: self.tui.notify(text, severity=severity, markup=False))

    def queued(self) -> None:
        self._on_ui(self.tui.submit_queued)

    def draft(self) -> str:
        return self._get(lambda: self.tui.composer().text)

    def set_draft(self, text: str, append: bool) -> None:
        self._get(lambda: self.tui.composer().set_draft(text, append))

    def show(self, text: str, kind: str) -> None:
        """Draw a transcript block; while the interface is suspended for a terminal command it waits for the resume."""
        if not self.tui.defer_block(text, kind):
            self._on_ui(self.tui.add_block, text, kind)

    def focus_widget(self, name: str, hold: bool) -> bool:
        return bool(self._get(lambda: self.tui.focus_ext_widget(name, hold)))

    def focus_composer(self) -> None:
        self._get(self.tui.focus_composer)

    def mode_changed(self) -> None:
        self._on_ui(self.tui._render_status)

    def invalidate(self) -> None:
        self.tui.invalidate()

    def theme_name(self) -> str | None:
        return self.tui.theme

    def progress(self, text: str | None) -> None:
        self._on_ui(self.tui.set_progress, text)

    def _modal(self, make: Callable[[Callable[[Any], None]], ModalScreen], timeout: float | None = None) -> Any:
        """Show the screen ``make(dismiss)`` builds (on the UI thread) from a worker thread and wait for its dismiss value.

        ``dismiss(value)`` closes the screen with ``value`` from any thread; it does nothing once the screen is closed
        or while another screen is on top of it. The value is ``None`` when the app quits meanwhile or after
        ``timeout`` seconds (``None``: wait for ever), when the screen is closed as if cancelled.
        """
        done = threading.Event()
        result: list[Any] = []
        shown: list[ModalScreen] = []

        def on_done(value: Any) -> None:
            result.append(value)
            done.set()

        def close(value: Any) -> bool:
            """Dismiss the screen; ``False`` if it cannot be yet (not the topmost screen)."""
            if done.is_set():
                return True
            if not shown or self.tui.screen_stack[-1] is not shown[0]:
                return False
            shown[0].dismiss(value)  # calls on_done at once
            return True

        def dismiss(value: Any = None) -> None:
            if self._in_terminal():
                close(value)
            else:
                self.tui.call_from_thread(close, value)

        async def push() -> None:
            screen = make(dismiss)
            shown.append(screen)
            await self.tui.push_screen(screen, callback=on_done)

        self.tui.pending_modals.add(done)
        try:
            # mypy solves call_from_thread's result type as Never when the result is unused.
            self.tui.call_from_thread(push)  # type: ignore[arg-type]
            if not done.wait(timeout):
                while not done.is_set():  # expired: close it as if cancelled (once it is on top)
                    try:
                        if self.tui.call_from_thread(close, None):
                            break
                    except RuntimeError:  # the app is gone
                        break
                    done.wait(0.05)
        finally:
            self.tui.pending_modals.discard(done)
        return result[0] if result else None

    def _in_terminal(self) -> bool:
        """True on the UI thread, which only asks during a suspended terminal command."""
        return threading.get_ident() == self.tui.ui_thread

    def ask(self, question: str, timeout: float | None = None) -> bool:
        if self._in_terminal():
            return ConsoleFrontend().ask(question, timeout)
        return bool(self._modal(lambda _: ConfirmScreen(question), timeout))

    def permission(self, question: str, rule: str | None, saved_in: str | None) -> str:
        if self._in_terminal():
            return ConsoleFrontend().permission(question, rule, saved_in)
        return self._modal(lambda _: PermissionScreen(question, rule, saved_in)) or "deny"

    def choose(self, title: str, options: list[tuple[str, str]], timeout: float | None = None) -> str | None:
        if self._in_terminal():
            return ConsoleFrontend().choose(title, options, timeout)
        return self._modal(lambda _: ChoiceScreen(title, options), timeout)

    def choose_theme(self, title: str, options: list[tuple[str, str]]) -> str | None:
        """Like :meth:`choose`, each label after a sample of the theme's colours."""
        if self._in_terminal():
            return ConsoleFrontend().choose(title, options)
        themes = self.tui.available_themes
        decorated: list[tuple[str, str | Content]] = [
            (value, Content.assemble(_theme_swatch(themes[value]), label) if value in themes else label)
            for value, label in options
        ]
        return self._modal(lambda _: ChoiceScreen(title, decorated))

    def choose_many(
        self, title: str, options: list[tuple[str, str, bool]], timeout: float | None = None
    ) -> list[str] | None:
        if self._in_terminal():
            return ConsoleFrontend().choose_many(title, options, timeout)
        return self._modal(lambda _: ChecklistScreen(title, options), timeout)

    def ask_text(self, title: str, timeout: float | None = None) -> str | None:
        if self._in_terminal():
            return ConsoleFrontend().ask_text(title, timeout)
        return self._modal(lambda _: TextScreen(title), timeout)

    def screen(self, factory: Callable[[Callable[[Any], None]], ModalScreen], timeout: float | None = None) -> Any:
        if self._in_terminal():  # the UI thread cannot wait for a screen: it is the one that draws it
            return ConsoleFrontend().screen(factory, timeout)
        return self._modal(factory, timeout)

    def pick(
        self,
        title: str,
        items: list[tuple[str, str, str]],
        *,
        preview: Callable[[str], str] | None = None,
        on_highlight: Callable[[str], Any] | None = None,
        filter: bool = True,
        timeout: float | None = None,
    ) -> str | None:
        if self._in_terminal():
            return ConsoleFrontend().pick(
                title, items, preview=preview, on_highlight=on_highlight, filter=filter, timeout=timeout
            )
        return self._modal(
            lambda _: PickScreen(title, items, preview=preview, on_highlight=on_highlight, filtered=filter), timeout
        )

    def overlay(self, content: Any, title: str = "", timeout: float | None = None) -> None:
        if self._in_terminal():
            ConsoleFrontend().overlay(content, title, timeout)
            return
        if isinstance(content, list):  # styled lines
            content = Content("\n").join(to_content(line) for line in content)
        elif not isinstance(content, (str, Content)):
            raise TypeError(f"overlay: expected text, styled lines or Content; got {type(content).__name__}")
        self._modal(lambda _: OverlayScreen(title, content), timeout)

    def _call_ui(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run ``fn`` on the UI thread and return its result (:meth:`_on_ui` drops it on the UI thread itself)."""
        if self._in_terminal():
            return fn(*args)
        return self.tui.call_from_thread(fn, *args)

    def transcript_scroll_to(self, message_index: int) -> bool:
        return bool(self._call_ui(self.tui.scroll_to_message, message_index))

    def transcript_mark(self, message_index: int, label: str) -> None:
        self._call_ui(self.tui.mark_message, message_index, label)

    def transcript_unmark(self, message_index: int | None) -> None:
        self._call_ui(self.tui.unmark_message, message_index)

    def transcript_marks(self) -> list[tuple[int, str]]:
        return self._call_ui(lambda: sorted(self.tui.marks.items()))


def theme_command(frontend: TuiFrontend) -> CommandRun:
    """The terminal UI's ``/theme [name]``: pick a colour theme in a list with samples, or switch to ``name``."""

    def run(args: str, ctx: Any) -> None:
        names, current = frontend.themes()
        if not args and len(names) == 1:
            print(f"Only the built-in {names[0]} theme is installed; get more from /ext → Themes.")
            return
        options = [(n, f"{n} (current)" if n == current else n) for n in names]
        name = args or frontend.choose_theme("Theme · more in /ext → Themes", options)
        if name is None:
            return
        if name not in names:
            raise ValueError(f"no theme {name!r}; installed: {', '.join(names)} (more in /ext → Themes)")
        frontend.set_theme(name)
        print(f"Theme: {name}")

    return run
