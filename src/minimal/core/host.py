"""Host: what a frontend drives minimal with.

A frontend (the terminal UI, the console REPL, one an extension registers with
``register_frontend``) gets a :class:`Host` in ``run(host)``. It shows minimal through
its :class:`~minimal.core.app.Frontend`, installed with :meth:`Host.attach`, and acts on
minimal only through the host: send a message, run a command, interrupt, read the
status and what extensions added to the interface. Nothing here imports a UI toolkit.

Threads: :meth:`Host.submit` and :meth:`Host.run_command` block until the turn or
command ends; a frontend with its own event loop calls them from a worker thread.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from minimal import config
from minimal.core.context import Context
from minimal.extensions.registry import (
    Command,
    CommandRun,
    LogoSpec,
    PanelSpec,
    RendererSpec,
    StatusSpec,
    ThemeSpec,
    WidgetSpec,
)

if TYPE_CHECKING:
    from minimal.core.app import App, Frontend


@dataclass(frozen=True)
class HostStatus:
    """What a status line shows (see :meth:`Host.status`).

    Attributes:
        model: Current model.
        provider: Current provider's name.
        cwd: Working directory.
        session_id: Current session.
        context_tokens: Tokens in the context after the last reply, if the provider reports usage.
        extensions: How many extensions the last reload loaded.
        untrusted: How many project extensions and settings were not applied for lack of ``/trust``; ``0`` if none.
    """

    model: str
    provider: str
    cwd: str
    session_id: str
    context_tokens: int | None
    extensions: int
    untrusted: int


class Host:
    """The interface between minimal and the frontend that runs it."""

    def __init__(self, app: App) -> None:
        self._app = app

    # --- showing minimal ------------------------------------------------------

    def attach(self, frontend: Frontend) -> None:
        """Show turns, questions and notices through ``frontend`` from now on."""
        self._app.frontend = frontend

    def begin(self) -> None:
        """Fire ``session_start``. Call it once, when the frontend is up and can show what extensions print."""
        self._app.begin()

    @property
    def ctx(self) -> Context:
        """The :class:`~minimal.core.context.Context` extensions get: pass it to their widgets and status segments."""
        return self._app.context

    @property
    def messages(self) -> list[dict]:
        """The current session's messages (do not modify them)."""
        return self._app.messages

    @property
    def settings(self) -> config.Settings:
        """The settings the last reload read (``theme``, ``ui``, …)."""
        return self._app.settings

    def status(self) -> HostStatus:
        """Model, provider, directory, session and context size, as a status line shows them."""
        app = self._app
        return HostStatus(
            model=app.model,
            provider=app.provider_name,
            cwd=app.cwd,
            session_id=app.session.id,
            context_tokens=app.context_tokens,
            extensions=len(app.extensions),
            untrusted=app.untrusted.count if app.untrusted is not None else 0,
        )

    @property
    def extension_errors(self) -> list[str]:
        """Failures of the last reload (extensions that did not load, settings errors)."""
        return list(self._app.extension_errors)

    @property
    def extension_skipped(self) -> list[str]:
        """Extensions the last reload did not load because they need another UI toolkit."""
        return list(self._app.extension_skipped)

    def untrusted_notice(self) -> str | None:
        """What the last reload skipped as untrusted, pointing to ``/trust``; ``None`` if nothing."""
        return self._app.untrusted_notice()

    # --- what extensions add to the interface ---------------------------------

    @property
    def widgets(self) -> Mapping[str, WidgetSpec]:
        """Extension widgets by name (``register_widget``); their ``create(ctx)`` builds a Textual widget."""
        return types.MappingProxyType(self._app.registry.widgets)

    @property
    def status_segments(self) -> Mapping[str, StatusSpec]:
        """Extension status-line segments by name (``register_status``)."""
        return types.MappingProxyType(self._app.registry.status)

    @property
    def panels(self) -> Mapping[str, PanelSpec]:
        """Extension panels by name (``register_panel``)."""
        return types.MappingProxyType(self._app.registry.panels)

    @property
    def renderers(self) -> Mapping[str, RendererSpec]:
        """Extension block renderers by kind (``register_renderer``)."""
        return types.MappingProxyType(self._app.registry.renderers)

    @property
    def themes(self) -> Mapping[str, ThemeSpec]:
        """Extension colour themes by name (``register_theme``)."""
        return types.MappingProxyType(self._app.registry.themes)

    @property
    def logo(self) -> LogoSpec | None:
        """The extension logo for the welcome header (``register_logo``), or ``None``."""
        return self._app.registry.logo

    # --- acting on minimal ----------------------------------------------------

    def submit(self, text: str) -> None:
        """Send ``text`` to the model as the user's message and run the turn; returns when it ends.

        Failures and interruptions are reported through the frontend (``turn_end``), not raised.
        """
        self._app.run_turn(text)

    def run_command(self, line: str) -> None:
        """Run a ``/name args`` line; returns when the command ends. Unknown commands and errors are reported.

        Raises:
            ExitRepl: when the command asks to quit.
        """
        self._app.run_command(line)

    def command(self, name: str) -> Command | None:
        """The command ``/name`` would run (an extension's before a built-in), or ``None``.

        A command with ``terminal`` set needs the real terminal: suspend the interface while it runs.
        """
        return self._app.find_command(name)

    def commands(self) -> dict[str, Command]:
        """Every slash command by name."""
        return self._app.commands()

    def complete(self, prefix: str) -> list[str]:
        """The command lines that complete ``prefix`` (``"/re"`` → ``["/reload", "/resume"]``), sorted.

        Only command names are completed: empty for text that is not a ``/`` command name.
        """
        if not prefix.startswith("/") or " " in prefix:
            return []
        typed = prefix[1:]
        return [f"/{name}" for name in sorted(self._app.commands()) if name.startswith(typed)]

    def register_command(self, name: str, run: CommandRun, description: str = "", terminal: bool = False) -> None:
        """Add a built-in command that only this frontend has, called as ``run(args, ctx)``; it survives reloads."""
        self._app.add_frontend_command(name, run, description, terminal)

    def interrupt(self) -> None:
        """Ask the turn in progress to stop at its next streamed chunk, tool call or model request."""
        self._app.cancelled.set()

    @property
    def interrupting(self) -> bool:
        """Whether an interrupt was asked for and the turn has not noticed it yet."""
        return self._app.cancelled.is_set()

    def next_queued(self) -> str | None:
        """The next message an extension queued (``ctx.queue_user_message``), removed from the queue; ``None`` if none.

        Send it with :meth:`submit` (or :meth:`run_command`) as soon as no turn or command is running.
        """
        return self._app.take_queued()
