"""What tools, slash commands, event handlers and widgets of extensions receive as ``ctx``: a :class:`Context`.

There is one per :class:`~minimal.core.app.App`, and one more for each agent a tool starts with
:meth:`Context.run_agent`: the ``ctx`` that agent's tools and events get. It differs from the app's only in
:attr:`Context.agent_id`, :attr:`Context.parent_id` and :attr:`Context.cancelled` (that agent's own flag), so
what a tool starts with it is attributed to that agent. Its properties read the app
live, so a ``ctx`` kept by a widget always sees the current session, model and
tools, and it is safe to use from the thread running a turn.
"""

from __future__ import annotations

import threading
import types
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from minimal.core import agent
from minimal.core.agents import MAIN_AGENT, AgentInfo, AgentResult, AgentRun
from minimal.core.session import list_sessions, new_id, open_session
from minimal.extensions.loader import reload_summary
from minimal.extensions.registry import ModeSpec, Tool
from minimal.providers.base import Reply

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

    def __init__(self, app: App, run: AgentRun | None = None) -> None:
        self._app = app
        self._run = run
        self.ui = UI(app)

    @property
    def agent_id(self) -> str:
        """The agent this ``ctx`` belongs to: ``"main"`` for the user's turn, commands and widgets, else the id of
        the ``run_agent`` agent whose tool or event handler got it."""
        return MAIN_AGENT if self._run is None else self._run.id

    @property
    def parent_id(self) -> str | None:
        """The id of the agent that started :attr:`agent_id`; ``None`` for ``"main"``."""
        return None if self._run is None else self._run.parent_id

    @property
    def project_trusted(self) -> bool:
        """Whether the user trusted the current project: ``/trust`` accepted its present extensions and
        trust-gated settings, or minimal runs with ``--trust-project``. A project with nothing for ``/trust`` to
        review is not trusted unless ``--trust-project`` is set."""
        return self._app.settings.project_trusted

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
        """Whether the user interrupted the turn or command in progress, or (for the ``ctx`` of an agent started
        with :meth:`run_agent`) :meth:`cancel_agent` stopped this agent; poll it in long loops."""
        return self._flag().is_set()

    def _flag(self) -> threading.Event:
        return self._app.cancelled if self._run is None else self._run.cancel

    def check_cancelled(self) -> None:
        """Raise :class:`~minimal.extensions.registry.TurnInterrupted` if the user interrupted (see :attr:`cancelled`)."""
        agent.raise_if_cancelled(self._flag())

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
            tools=agent.tool_specs(app.registry, gate=app.mode_block_reason) if tools else [],
            on_text=lambda _chunk: self.check_cancelled(),
            **({"options": opts} if opts else {}),
        )

    def run_agent(
        self,
        prompt: str,
        *,
        system_note: str = "",
        exclude_tools: Iterable[str] = (),
        name: str | None = None,
        system: str | None = None,
        tools: Iterable[str] | None = None,
        model: str | None = None,
        provider: str | None = None,
        max_turns: int | None = None,
    ) -> str:
        """Run ``prompt`` as its own agent loop with an empty history; return the text of its last reply.

        It uses the current provider, model and tools (but ``exclude_tools``), shows
        its tool calls, asks the same confirmations and stops when the user
        interrupts. Its ``turn_end`` has ``nested`` set. It wraps :meth:`run_agent_result` (read it for
        the keyword arguments) but raises what went wrong: the error that ended the loop,
        :class:`~minimal.extensions.registry.TurnInterrupted` if it was interrupted (by the user, or
        :meth:`cancel_agent`), ``RuntimeError`` if it ran out of turns.

        Args:
            system_note: Appended to the system prompt.
            exclude_tools: Tools it may not use.
            name, system, tools, model, provider, max_turns: See :meth:`run_agent_result`.

        Raises:
            ValueError: for an invalid argument (see :meth:`run_agent_result`).
        """
        result, error = self._app.run_agent(
            self.agent_id,
            prompt,
            system_note=system_note,
            exclude_tools=exclude_tools,
            name=name,
            system=system,
            tools=tools,
            model=model,
            provider=provider,
            max_turns=max_turns,
        )
        if error is not None:
            raise error
        return result.text

    def run_agent_result(
        self,
        prompt: str,
        *,
        system_note: str = "",
        exclude_tools: Iterable[str] = (),
        name: str | None = None,
        system: str | None = None,
        tools: Iterable[str] | None = None,
        model: str | None = None,
        provider: str | None = None,
        max_turns: int | None = None,
    ) -> AgentResult:
        """Run ``prompt`` as its own agent loop with an empty history, and say how it went.

        Like :meth:`run_agent`, but a failure is reported in the result, not raised. The agent has its own
        id (``agent_start``, ``agent_end`` and the events of its loop carry it, with this ``ctx``'s
        :attr:`agent_id` as ``parent_id``), is listed by :meth:`agents` and can be stopped alone with
        :meth:`cancel_agent`; the user's interrupt stops it too, and then the turn that started it.
        The current mode's restriction applies to it, whatever ``tools`` says: ``tools`` can only narrow it.

        Args:
            prompt: Its first message.
            system_note: Appended to its system prompt.
            exclude_tools: Tools it may not use; not together with ``tools``.
            name: How ``agent_start`` and :meth:`agents` call it (default ``"agent"``).
            system: Its whole system prompt, instead of :meth:`system_prompt` (the current mode's note and
                ``system_note`` are still added).
            tools: The only tools it may use (an allow-list); not together with ``exclude_tools``.
            model: A model name; with ``provider`` omitted, the current provider's.
            provider: Name of a provider to use for this agent only (its default model unless ``model`` is
                given); the app's current provider and model do not change.
            max_turns: Most model requests it may make (default 50).

        Returns:
            Its :class:`~minimal.core.agents.AgentResult`.

        Raises:
            ValueError: if ``tools`` and ``exclude_tools`` are both given, ``tools`` names a tool that is not
                registered, ``max_turns`` is not a positive integer, or ``provider`` is unknown or lacks its
                credentials.
        """
        return self._app.run_agent(
            self.agent_id,
            prompt,
            system_note=system_note,
            exclude_tools=exclude_tools,
            name=name,
            system=system,
            tools=tools,
            model=model,
            provider=provider,
            max_turns=max_turns,
        )[0]

    def agents(self) -> list[AgentInfo]:
        """A snapshot of the agents of this session, running and finished, in the order they started: ``"main"``
        (once the first turn has begun) and every agent started with :meth:`run_agent`."""
        return self._app.agents.snapshot()

    def cancel_agent(self, agent_id: str) -> bool:
        """Interrupt agent ``agent_id`` and the agents it started; the agent that started it carries on.

        Returns ``False`` if there is no such agent or it is not running. ``"main"`` is the user's turn:
        cancelling it is the user's Esc.
        """
        return self._app.agents.cancel(agent_id)

    @property
    def mode(self) -> str | None:
        """The current mode's name; ``None`` (the default) for no mode: every tool is available."""
        return self._app.mode

    def modes(self) -> dict[str, str]:
        """Every registered mode (``register_mode``) and its description, in registration order."""
        return {name: spec.description for name, spec in self._app.registry.modes.items()}

    def mode_info(self, name: str | None = None) -> ModeSpec | None:
        """The registration (:class:`~minimal.extensions.registry.ModeSpec`: its tools, ``read_only``, note...) of mode
        ``name``, default the current one; ``None`` if there is no such mode or no mode is current."""
        name = self._app.mode if name is None else name
        return None if name is None else self._app.registry.modes.get(name)

    def set_mode(self, name: str | None) -> None:
        """Make ``name`` the current mode, or ``None`` for no mode. Fires ``mode_change`` if it changed.

        It takes effect at once, even in the middle of a turn: the next model request offers the new mode's
        tools and a call to a tool it blocks is refused.

        Raises:
            ValueError: if no mode is called ``name``.
        """
        self._app.set_mode(name)

    def cycle_mode(self) -> str | None:
        """Go to the next mode in registration order, from no mode (``None``) to the first, and from the last
        back to ``None``; return the new one. Does nothing (returns ``None``) when no mode is registered."""
        return self._app.cycle_mode()

    def prompt_sections(self) -> list[str]:
        """The names of the system prompt's sections in order, the built-in ones and extensions' (``register_prompt_section``)."""
        return self._app.registry.prompt_section_names()

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
