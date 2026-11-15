"""What tools, slash commands, event handlers and widgets of extensions receive as ``ctx``: a :class:`Context`.

There is one per :class:`~minimal.core.app.App`. Its properties read the app
live, so a ``ctx`` kept by a widget always sees the current session, model and
tools, and it is safe to use from the thread running a turn.
"""

from __future__ import annotations

import types
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from minimal.core import agent
from minimal.core.session import list_sessions, new_id, open_session
from minimal.extensions.loader import reload_summary
from minimal.extensions.registry import Tool
from minimal.providers.base import Reply, result_text

if TYPE_CHECKING:
    from minimal.core.app import App


NOTIFY_KINDS = ("info", "warning", "error")
"""The kinds of :meth:`Context.notify`."""


class UI:
    """Questions to the user, through whatever frontend is showing minimal at call time."""

    def __init__(self, app: App) -> None:
        self._app = app

    def ask(self, question: str) -> bool:
        """Ask a yes/no question; return the answer."""
        return self._app.frontend.ask(question)

    def choose(self, title: str, options: list[tuple[str, str]]) -> str | None:
        """Pick one ``(value, label)`` option; return its value, or ``None`` if cancelled."""
        return self._app.frontend.choose(title, options)

    def choose_many(self, title: str, options: list[tuple[str, str, bool]]) -> list[str] | None:
        """Tick any ``(value, label, checked)`` options; return the ticked values in option order, or ``None`` if cancelled."""
        return self._app.frontend.choose_many(title, options)

    def ask_text(self, title: str) -> str | None:
        """Ask for one line of text; return it stripped, or ``None`` if empty or cancelled."""
        return self._app.frontend.ask_text(title)


class Context:
    """The ``ctx`` of tools, commands, events and widgets.

    Attributes:
        ui: Questions to the user (:class:`UI`).
    """

    def __init__(self, app: App) -> None:
        self._app = app
        self.ui = UI(app)

    @property
    def cwd(self) -> str:
        """Working directory of tools and of the system prompt."""
        return self._app.cwd

    @property
    def messages(self) -> list[dict]:
        """The current session's messages, in the canonical format (live: appending adds to the session)."""
        return self._app.messages

    @property
    def session_id(self) -> str:
        return self._app.session.id

    @property
    def provider_name(self) -> str:
        return self._app.provider_name

    @property
    def model(self) -> str:
        return self._app.model

    @property
    def context_tokens(self) -> int | None:
        """Size of the context after the last reply that reported usage; ``None`` if none did."""
        return self._app.context_tokens

    @property
    def tools(self) -> Mapping[str, Tool]:
        """Every registered tool by name (read-only, live)."""
        return types.MappingProxyType(self._app.registry.tools)

    @property
    def cancelled(self) -> bool:
        """Whether the user interrupted the turn or command in progress; poll it in long loops."""
        return self._app.cancelled.is_set()

    def check_cancelled(self) -> None:
        """Raise :class:`~minimal.extensions.registry.TurnInterrupted` if the user interrupted."""
        agent.raise_if_cancelled(self._app.cancelled)

    def system_prompt(self) -> str:
        """The system prompt the next turn would send."""
        return self._app.system_prompt()

    def complete(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: bool = True,
        options: Mapping[str, Any] | None = None,
    ) -> Reply:
        """One model request with the current provider and model; no tools are run.

        Args:
            messages: The conversation to send, in the canonical format.
            system: System prompt (default: :meth:`system_prompt`).
            tools: Offer the registered tools, as a turn does (keeps the provider's prompt cache warm).
            options: Request options (``max_tokens``, ``temperature``) over the ones a turn uses.

        Raises:
            TurnInterrupted: if the user interrupts while the reply streams.
        """
        app = self._app
        opts = {**app.request_options(), **(options or {})}
        assert app.provider is not None  # startup picks a provider before any extension runs
        return app.provider.stream(
            model=app.model,
            system=self.system_prompt() if system is None else system,
            messages=messages,
            tools=agent.tool_specs(app.registry) if tools else [],
            on_text=lambda _chunk: self.check_cancelled(),
            **({"options": opts} if opts else {}),
        )

    def run_agent(self, prompt: str, *, system_note: str = "", exclude_tools: Iterable[str] = ()) -> str:
        """Run ``prompt`` as its own agent loop with an empty history; return the text of its last reply.

        It uses the current provider, model and tools (but ``exclude_tools``), shows
        its tool calls, asks the same confirmations and stops when the user
        interrupts. Its ``turn_end`` has ``nested`` set.

        Args:
            system_note: Appended to the system prompt.
        """
        app = self._app
        assert app.provider is not None  # startup picks a provider before any extension runs
        reply = agent.run_agent(
            provider=app.provider,
            model=app.model,
            system=self.system_prompt() + ("\n\n" + system_note if system_note else ""),
            messages=[{"role": "user", "content": prompt}],
            registry=app.registry,
            ctx=self,
            on_tool=app.frontend.tool_call,
            on_tool_result=app.frontend.tool_result,
            confirm=app.confirm,
            cancelled=app.cancelled,
            permit=app.permit,
            exclude_tools=frozenset(exclude_tools),
            nested=True,
            options=app.request_options(),
            switch_provider=app.fallback_provider,
        )
        return result_text(reply.content) or ""

    def new_session(self, messages: list[dict]) -> str:
        """Save the current session and continue in a new one holding ``messages``; return the old session's id.

        The new session's header names the old one as its ``parent``. Fires ``session_switch``.
        """
        app = self._app
        old = app.session
        session = open_session(new_id(), app.cwd)
        session.parent = old.id
        app.switch_session(session)
        app.messages.extend(messages)
        app.session.save()
        return old.id

    def sessions(self, *, all_projects: bool = False) -> list[str]:
        """Saved session ids, oldest first: this project's (see ``cwd``), or every project's with ``all_projects``."""
        return list_sessions(None if all_projects else self._app.cwd)

    def read_session(self, session_id: str) -> list[dict]:
        """The messages of saved session ``session_id``.

        Raises:
            ValueError: if there is no such session.
        """
        if session_id not in list_sessions():
            raise ValueError(f"no session {session_id!r}")
        return open_session(session_id).messages

    def reload(self) -> str:
        """Reload every extension from disk; return the summary (loaded, failed, not trusted)."""
        return reload_summary(*self._app.reload_extensions(), self._app.untrusted_notice())

    def commands(self) -> dict[str, str]:
        """Every slash command name (without ``/``) and its description: built-in and extension commands,
        an extension's replacing a built-in of the same name."""
        return {name: cmd.description for name, cmd in self._app.commands().items()}

    def run_command(self, line: str, *, builtin: bool = False) -> None:
        """Run slash command ``line`` (``"/model x"``) as if the user typed it; its output is printed.

        ``builtin`` runs the built-in command even when an extension replaced it, so a replacement can
        hand over to the original. Errors are reported, not raised.
        """
        self._app.run_command(line, builtin=builtin)

    def models(self) -> list[str]:
        """The current provider's models, queried from its API (falling back to its default); the current one is included."""
        return self._app.available_models()[0]

    def set_model(self, name: str) -> None:
        """Use model ``name`` of the current provider from the next request on."""
        self._app.model = name

    def widget(self, name: str) -> object | None:
        """The widget the frontend shows for extension widget ``name`` (see ``register_widget``); ``None`` if none."""
        return self._app.frontend.widget(name)

    def notify(self, text: str, kind: str = "info") -> None:
        """Show a short notice to the user (a toast in the terminal UI); ``kind`` is one of :data:`NOTIFY_KINDS`.

        Raises:
            ValueError: if ``kind`` is not one of them.
        """
        if kind not in NOTIFY_KINDS:
            raise ValueError(f"notify: kind must be one of {', '.join(NOTIFY_KINDS)}")
        self._app.frontend.notify(text, kind)

    def use_provider(self, name: str, model: str | None = None) -> None:
        """Switch to provider ``name`` and ``model`` (default: its default model) from the next request on, keeping the conversation.

        Raises:
            ValueError: if the provider is unknown or its credentials are missing.
        """
        self._app.use_provider(name, model)

    def queue_user_message(self, text: str) -> None:
        """Send ``text`` as the user's next message once the turn or command in progress ends (at once if the UI is idle).

        A text starting with ``/`` runs as a slash command. Messages queue up in order. It does not
        steer the turn in progress: the model sees it only after that turn ends.

        Raises:
            ValueError: if ``text`` is empty.
        """
        if not text.strip():
            raise ValueError("queue_user_message: text is empty")
        self._app.queued.append(text)
        self._app.frontend.queued()
