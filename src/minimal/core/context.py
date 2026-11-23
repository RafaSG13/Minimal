"""What tools, slash commands, event handlers and widgets of extensions receive as ``ctx``: a :class:`Context`.

There is one per :class:`~minimal.core.app.App`, and one more for each agent a tool starts with
:meth:`Context.run_agent`: the ``ctx`` that agent's tools and events get. It differs from the app's only in
:attr:`Context.agent_id`, :attr:`Context.parent_id` and :attr:`Context.cancelled` (that agent's own flag), so
what a tool starts with it is attributed to that agent. Its properties read the app
live, so a ``ctx`` kept by a widget always sees the current session, model and
tools, and it is safe to use from the thread running a turn.
"""

from __future__ import annotations

import json
import re
import threading
import time
import types
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from minimal import config
from minimal.core import agent
from minimal.core.agents import MAIN_AGENT, AgentInfo, AgentResult, AgentRun, UsageTotals, current_call
from minimal.core.git import GitInfo
from minimal.core.layout import resolve_keys
from minimal.core.session import (
    SessionInfo,
    created_at,
    list_sessions,
    new_id,
    open_session,
    prompt_text,
    read_session_info,
    read_session_infos,
)
from minimal.extensions.registry import (
    KeyInfo,
    ModeSpec,
    ThemeInfo,
    Tool,
    TurnInterrupted,
    check_setting,
    styled_lines,
)
from minimal.providers.base import Reply, result_text

if TYPE_CHECKING:
    from minimal.core.app import App


NOTIFY_KINDS = ("info", "warning", "error")
"""The kinds of :meth:`Context.notify`."""

SHOW_KINDS = ("output", "error")
"""The kinds of :meth:`Context.show`: the transcript's output and error blocks."""


def _timeout_kwargs(method: str, timeout: float | None) -> dict[str, float]:
    """The ``timeout`` keyword to pass a frontend (nothing for ``None``, so a frontend written before API 2.4 keeps
    working), after checking it is a finite number of seconds > 0."""
    if timeout is None:
        return {}
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise ValueError(f"{method}: timeout must be a number of seconds > 0, or None")
    return {"timeout": min(float(timeout), threading.TIMEOUT_MAX)}


class UI:
    """Questions to the user, through whatever frontend is showing minimal at call time.

    Every method that waits for an answer takes ``timeout`` (API 2.4): seconds after which the dialog closes and the
    method returns what a cancel returns (``False`` for :meth:`ask`, else ``None``), so a worker never hangs on a
    user who walked away. ``None`` (the default) waits for ever. The plain terminal honours it only when stdin is a
    terminal.
    """

    def __init__(self, app: App) -> None:
        self._app = app

    def ask(self, question: str, *, timeout: float | None = None) -> bool:
        """Ask a yes/no question; return the answer (``False`` if ``timeout`` seconds pass first)."""
        return self._app.frontend.ask(question, **_timeout_kwargs("ask", timeout))

    def choose(self, title: str, options: list[tuple[str, str]], *, timeout: float | None = None) -> str | None:
        """Pick one ``(value, label)`` option; return its value, or ``None`` if cancelled or ``timeout`` seconds pass."""
        return self._app.frontend.choose(title, options, **_timeout_kwargs("choose", timeout))

    def choose_many(
        self, title: str, options: list[tuple[str, str, bool]], *, timeout: float | None = None
    ) -> list[str] | None:
        """Tick any ``(value, label, checked)`` options; return the ticked values in option order, or ``None`` if
        cancelled or ``timeout`` seconds pass."""
        return self._app.frontend.choose_many(title, options, **_timeout_kwargs("choose_many", timeout))

    def ask_text(self, title: str, *, timeout: float | None = None) -> str | None:
        """Ask for one line of text; return it stripped, or ``None`` if empty, cancelled or ``timeout`` seconds pass."""
        return self._app.frontend.ask_text(title, **_timeout_kwargs("ask_text", timeout))

    def screen(self, factory: Callable[[Callable[[Any], None]], Any], *, timeout: float | None = None) -> Any:
        """Show a modal Textual screen and return what it is dismissed with. API 2.4.

        ``factory(dismiss)`` is called on the UI thread and returns a :class:`textual.screen.ModalScreen`; calling
        ``dismiss(value)`` (from the screen's handlers, or any thread) closes it and makes ``value`` the result, as
        does ``screen.dismiss(value)``. It returns ``None`` if the screen is closed without a value, the app quits or
        ``timeout`` seconds pass (the screen is closed). ``dismiss`` does nothing after the screen closed or while
        another screen covers it. Call it from a command, tool or event handler running in a worker thread: not from
        the UI thread (widget callbacks, ``terminal=True`` commands), which cannot wait.

        Raises:
            RuntimeError: when minimal runs in the plain terminal: there is no screen to show, only the terminal UI has
                them. What ``factory`` raises propagates.
            TypeError: if ``factory`` is not callable.
            ValueError: for a bad ``timeout``.
        """
        if not callable(factory):
            raise TypeError("screen: factory must be callable: factory(dismiss) -> ModalScreen")
        return self._app.frontend.screen(factory, **_timeout_kwargs("screen", timeout))

    def pick(
        self,
        title: str,
        items: Sequence[tuple[str, ...]],
        *,
        preview: Callable[[str], str] | None = None,
        on_highlight: Callable[[str], Any] | None = None,
        filter: bool = True,
        timeout: float | None = None,
    ) -> str | None:
        """Pick one item from a list the user filters by typing (fuzzy, case-insensitive); return its value. API 2.4.

        Args:
            title: Heading of the dialog.
            items: ``(value, label)`` or ``(value, label, detail)`` tuples of strings: ``label`` is listed (and
                matched), ``detail`` follows it dimmed (and is matched); ``value`` is what is returned and must be
                unique.
            preview: ``preview(value) -> str`` gives the text of a side pane that follows the highlighted row. Called
                on the UI thread: keep it fast. An exception shows in the pane.
            on_highlight: ``on_highlight(value)`` is called on the UI thread each time the highlighted row changes
                (typing moves it too), the row first highlighted when the dialog opens included: preview a theme as
                the cursor passes it. An exception is reported once and does not close the dialog.
            filter: ``False`` shows no filter box; the list only moves.
            timeout: Seconds before the dialog closes as if cancelled.

        Returns:
            The picked value; ``None`` if cancelled, timed out, or ``items`` is empty (nothing is shown then).
            The plain terminal shows a numbered list instead (no filter, preview or ``on_highlight``).

        Raises:
            ValueError: for an item that is not 2 or 3 strings, a repeated value, or a bad ``timeout``.
        """
        kwargs = _timeout_kwargs("pick", timeout)
        rows: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for item in items:
            if not (isinstance(item, tuple) and len(item) in (2, 3) and all(isinstance(part, str) for part in item)):
                raise ValueError("pick: items must be (value, label) or (value, label, detail) tuples of strings")
            if item[0] in seen:
                raise ValueError(f"pick: value {item[0]!r} is listed twice")
            seen.add(item[0])
            rows.append((item[0], item[1], item[2] if len(item) == 3 else ""))
        if not rows:
            return None
        return self._app.frontend.pick(
            title, rows, preview=preview, on_highlight=on_highlight, filter=filter, **kwargs
        )

    def overlay(self, content: Any, title: str = "", *, timeout: float | None = None) -> None:
        """Show read-only text in a scrollable box over the transcript until the user closes it with Esc. API 2.4.

        For help, key lists and diffs. ``content`` is text, or styled lines as ``register_panel`` renders them (a list
        of lines, each a string, a ``(text, style)`` pair or a list of those; ``$name`` styles follow the theme), or a
        Textual ``Content`` in the terminal UI. Returns when it is closed or after ``timeout`` seconds. The plain
        terminal prints the text.

        Raises:
            ValueError: for invalid styled lines or a bad ``timeout``.
            TypeError: for another kind of ``content`` (terminal UI).
        """
        kwargs = _timeout_kwargs("overlay", timeout)
        if isinstance(content, (list, tuple)):
            content = styled_lines(list(content))
        self._app.frontend.overlay(content, title, **kwargs)

    def get_draft(self) -> str:
        """The text in the prompt box that the user has not sent; ``""`` if it is empty or the frontend has no prompt box. API 2.4."""
        return self._app.frontend.draft()

    def set_draft(self, text: str, *, append: bool = False) -> None:
        """Put ``text`` in the prompt box, replacing the draft (or after it with ``append``), cursor at the end. API 2.4.

        The user can undo it, it opens no completion popup and it does not send anything. A frontend without a
        prompt box ignores it.

        Raises:
            ValueError: if ``text`` is not a str.
        """
        if not isinstance(text, str):
            raise ValueError("set_draft: text must be a str")
        self._app.frontend.set_draft(text, append)


class Transcript:
    """The conversation as the terminal UI draws it (``ctx.transcript``): jump to a message, mark messages, search them.

    API 2.4. Messages are named by their index in :attr:`Context.messages`. Marks live as long as the session in view:
    another session (``/new``, ``/resume``, ``fork``) clears them and a ``truncate`` drops those it cut. The plain
    terminal has no transcript: :meth:`scroll_to` returns ``False`` and :meth:`mark` shows nothing, but
    :meth:`find` searches the messages anyway. Safe to call from any thread.
    """

    def __init__(self, app: App) -> None:
        self._app = app

    def _check(self, name: str, index: object) -> int:
        """``index`` if it is an index into the current messages, else ``ValueError``."""
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(self._app.messages):
            raise ValueError(f"{name}: message_index must be an integer in 0..{len(self._app.messages) - 1}")
        return index

    def scroll_to(self, message_index: int) -> bool:
        """Scroll the transcript so that the block showing ``messages[message_index]`` is at the top; follow-the-bottom
        stops until the user scrolls back down. ``False`` if there is no such block: an index out of range, a message
        before the window the UI redraws (the last 200 messages) or one that shows nothing."""
        if isinstance(message_index, bool) or not isinstance(message_index, int):
            raise ValueError("scroll_to: message_index must be an integer")
        if not 0 <= message_index < len(self._app.messages):
            return False
        return self._app.frontend.transcript_scroll_to(message_index)

    def mark(self, message_index: int, label: str = "") -> None:
        """Mark ``messages[message_index]``: its block gets a gutter in the theme's accent colour and the CSS class
        ``-marked`` (stable, for ``register_css``), and ``label`` as its tooltip. Marking again replaces the label.

        Raises:
            ValueError: for an index outside the messages.
        """
        self._app.frontend.transcript_mark(self._check("mark", message_index), str(label))

    def unmark(self, message_index: int | None = None) -> None:
        """Remove the mark of ``messages[message_index]`` (nothing happens if it has none), or every mark if ``None``."""
        self._app.frontend.transcript_unmark(message_index)

    def marks(self) -> list[tuple[int, str]]:
        """The marked messages as ``(index, label)`` pairs, by index."""
        return self._app.frontend.transcript_marks()

    def find(self, text: str, *, regex: bool = False) -> list[int]:
        """The indexes of the messages that contain ``text``, in order.

        A message's text is what the transcript shows of it: the text of the user's and the assistant's messages, a
        tool call's name and input as JSON, a tool result's text; thinking is not searched. ``text`` is matched case
        insensitively; with ``regex=True`` it is a regular expression (:func:`re.search`, case sensitive unless it
        says ``(?i)``).

        Raises:
            ValueError: if ``text`` is empty or not a valid regular expression.
        """
        if not text:
            raise ValueError("find: text is empty")
        search: Callable[[str], object]
        if regex:
            try:
                search = re.compile(text).search
            except re.error as e:
                raise ValueError(f"find: invalid regular expression: {e}") from e
        else:
            needle = text.casefold()
            search = lambda haystack: needle in haystack.casefold()  # noqa: E731
        return [i for i, message in enumerate(list(self._app.messages)) if search(_searchable(message["content"]))]


def _searchable(content: Any) -> str:
    """The text of a message's ``content`` that :meth:`Transcript.find` searches."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "tool_use":
            parts.append(f"{block.get('name', '')} {json.dumps(block.get('input', {}), ensure_ascii=False)}")
        elif kind == "tool_result":
            parts.append(result_text(block.get("content", "")))
    return "\n".join(parts)


class ContextSettings:
    """Read and write minimal's settings from an extension: ``ctx.settings``. API 2.4.

    Keys are dotted as in ``settings.json`` (``ui.sidebar``, ``reasoning_effort``, ``permissions.allow``,
    ``extensions.settings.tui_tools.tools``); see ``docs/settings.md`` for the keys and how layers merge. Reads see the
    settings as the last reload loaded them; a write goes to one file and, by default, reloads once so it takes
    effect (the terminal UI re-lays itself out, as after ``/ui``).
    """

    def __init__(self, app: App) -> None:
        self._app = app

    def _deep(self) -> dict[str, set[str]]:
        return {
            ext: {key for key, entry in spec.schema.items() if entry.get("merge") == "deep"}
            for ext, spec in self._app.registry.declared_settings.items()
        }

    def get(self, key: str, default: Any = None) -> Any:
        """The effective value of ``key``, with every settings layer merged the way minimal reads them (a project's
        trust-gated keys only once the project is trusted); ``default`` if no layer sets it.

        Lists such as ``permissions.allow`` are the layers' lists joined; ``ui.widgets`` and friends are merged entry by
        entry; ``extensions.settings.<id>.<name>`` honours the extension's ``"merge": "deep"``. The value is a copy.
        Settings an extension declared with ``declare_settings`` and the user did not set read as ``default``: use
        ``minimal.settings`` for those values with their defaults.

        Raises:
            ValueError: if ``key`` is not a setting.
        """
        return self._app.settings.get(key, default, deep=self._deep())

    def layers(self, key: str) -> dict[str, Any]:
        """Which settings files set ``key``: ``{scope: value}`` for the ``global``, ``project`` and ``local`` layers that
        set it and apply, lowest first (the last one wins for a scalar); ``{}`` if none does. The values are the
        files' own, not merged.

        Raises:
            ValueError: if ``key`` is not a setting.
        """
        return self._app.settings.layers_setting(key)

    def update(
        self, changes: Mapping[str | tuple[str, ...], Any], *, scope: str = "global", apply: bool = True
    ) -> None:
        """Set settings in the ``scope`` file and, with ``apply``, reload once so they take effect; ``None`` as a value deletes the key.

        All changes are checked first and written together atomically (``config.write_private``): if any is refused,
        nothing is written. A key may be a tuple of names for one that contains a dot. Pass ``apply=False`` to batch
        several writes and reload once yourself with :meth:`Context.reload`.

        The same rules as when settings are loaded apply: the value must have the type ``docs/settings.md`` gives
        (and, for ``extensions.settings.<id>.<name>`` of a loaded extension, satisfy its ``declare_settings`` schema),
        ``marketplaces``, ``prompt.base`` and ``ui.css`` can only be set in the ``global`` scope, and the trust-gated
        keys (``permissions.allow``, ``extensions.disabled``, ``extensions.settings``) can be written to the ``project``
        or ``local`` scope only while the project's settings are trusted. Writing one changes what ``/trust`` reviewed,
        so the reload leaves the project's trust-gated settings and extensions unapplied until the user runs ``/trust``
        again (the reload's notice says so). A project file is the repository's: ``project`` scope writes
        ``.minimal/settings.json`` there.

        Runs on whichever thread calls it, reload included: from a thread outside a turn it reloads the registry live.

        Args:
            changes: Dotted key (or tuple of names) -> value.
            scope: ``"global"`` (``~/.minimal/settings.json``), ``"project"`` or ``"local"``.
            apply: Reload after writing.

        Raises:
            ValueError: for an unknown scope or key, an invalid value (the message says what is expected), a key the
                scope may not hold, or a settings file that is not valid JSON (it is left untouched).
            OSError: if the file cannot be written.
        """
        for key, value in changes.items():
            self._check_extension_value(key, value)
        config.update_settings(changes, scope=scope, cwd=self._app.cwd, trusted=self._app.settings.trusted)
        if apply:
            self._app.reload_extensions()

    def unset(self, key: str | tuple[str, ...], *, scope: str = "global", apply: bool = True) -> None:
        """Remove ``key`` from the ``scope`` file (``update({key: None}, ...)``): the lower layers' value, or the default, applies again."""
        self.update({key: None}, scope=scope, apply=apply)

    def _check_extension_value(self, key: str | tuple[str, ...], value: Any) -> None:
        """Check ``extensions.settings.<id>.<name>`` against the schema of the loaded extension ``<id>``."""
        base, rest = config.split_key(key)
        if base != "extensions.settings" or len(rest) != 2 or value is None:
            return
        spec = self._app.registry.declared_settings.get(rest[0])
        if spec is None or rest[1] not in spec.defaults:
            return
        problem = check_setting(spec.defaults[rest[1]], value, spec.schema.get(rest[1]))
        if problem:
            raise ValueError(f"extensions.settings.{rest[0]}.{rest[1]} {problem}, got {value!r}")


class Context:
    """The ``ctx`` of tools, commands, events and widgets.

    Attributes:
        ui: Questions to the user (:class:`UI`).
        settings: Read and write the settings, layers merged (:class:`ContextSettings`; API 2.4).
        transcript: The conversation as the UI shows it (:class:`Transcript`; API 2.4).
    """

    def __init__(self, app: App, run: AgentRun | None = None) -> None:
        self._app = app
        self._run = run
        self.ui = UI(app)
        self.settings = ContextSettings(app)
        self.transcript = Transcript(app)

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
        """Working directory of tools and of the system prompt: the app's, or for the ``ctx`` of an agent started
        with ``run_agent(cwd=...)`` (and the agents it starts) that folder (API 2.4)."""
        return (self._run.cwd if self._run is not None else None) or self._app.cwd

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
    def max_turns(self) -> int:
        """Most model requests one user turn may make (the ``agent.max_turns`` setting, default 50): also the default of
        ``run_agent``'s ``max_turns``. API 2.3."""
        return self._app.settings.max_turns

    @property
    def reasoning_effort(self) -> str | None:
        """How much the model is asked to think: ``"low"``, ``"medium"`` or ``"high"`` (the ``reasoning_effort`` setting or
        :meth:`set_reasoning_effort`); ``None`` for the provider's default. API 2.3. For the ``ctx`` of an agent started
        with ``run_agent(reasoning_effort=...)`` (and the agents it starts) that value (API 2.4)."""
        return (self._run.reasoning_effort if self._run is not None else None) or self._app.reasoning_effort

    @property
    def context_tokens(self) -> int | None:
        """Size of the context after the last reply that reported usage; ``None`` if none did."""
        return self._app.context_tokens

    @property
    def context_window(self) -> int | None:
        """The current model's context size in tokens, or ``None`` if unknown. API 2.3.

        The ``context_window`` setting (an integer) wins; else the provider's own table
        (:attr:`~minimal.providers.base.ProviderSpec.context_windows`), else minimal's table of the built-in providers'
        model families by name prefix. Divide :attr:`context_tokens` by it for a gauge."""
        return self._app.context_window

    @property
    def theme(self) -> ThemeInfo:
        """The current colour theme, every colour resolved to ``#rrggbb`` (:class:`~minimal.extensions.registry.ThemeInfo`). API 2.3.

        The terminal UI's theme (the built-in ``minimal`` one when no UI shows themes); read it again after
        ``theme_change``. Use it for colours a style string cannot name: ``ctx.theme.colors["accent"]``."""
        return self._app.theme_info()

    def set_theme(self, name: str, *, save: bool = False) -> bool:
        """Switch the terminal UI to colour theme ``name`` (what ``/theme <name>`` does); return whether it did. API 2.4.

        ``theme_change`` fires as for any switch (from the UI thread, soon after this returns), and the UI redraws what
        extensions show. An unknown name, or a frontend without themes (the console), returns ``False`` without a message:
        ``ctx.theme.name`` is the current one and ``/theme`` lists the installed ones. ``save`` also stores ``name`` as the
        ``theme`` setting in ``~/.minimal/settings.json`` (no reload).

        Raises:
            ValueError: if ``save`` is set and the settings file cannot be written (the theme is switched anyway).
        """
        return self._app.set_theme(name, save=save)

    def invalidate(self) -> None:
        """Ask the UI to redraw panels, status segments and widgets now. API 2.3.

        For code that learns of a change off the UI thread (a poller, a subscription): call it from any thread instead
        of polling with a panel ``interval``. It is cheap and coalesced: calls arriving while a redraw is already
        pending share it. The terminal UI calls each panel's ``render``, each status segment and each widget's
        ``refresh_state``; a frontend that shows none of those does nothing."""
        self._app.invalidate()

    def git(self) -> GitInfo | None:
        """The state of the working directory's git repository (:class:`~minimal.core.git.GitInfo`: branch, ahead/behind,
        counts of staged, unstaged and untracked files, up to 50 changed files). API 2.3.

        One background poller shared by every caller refreshes it every ``git.refresh`` seconds (default 3) and calls
        :meth:`invalidate` when it changes; the first call starts it. Cheap: it returns the cached result and never runs
        git itself. ``None`` until the first poll ends and outside a git repository."""
        return self._app.git_info()

    def keybindings(self) -> list[KeyInfo]:
        """Every bound key as a help screen lists it: minimal's own actions (as ``ui.keys`` remapped them) then the
        extensions' keybindings (:class:`~minimal.extensions.registry.KeyInfo`). API 2.3. A binding the user unbound
        is left out."""
        bound, _ = resolve_keys(self._app.registry, self._app.settings)
        return [
            KeyInfo(b.key, b.id, b.description, "built-in" if b.spec is None else b.spec.ext) for b in bound if b.key
        ]

    @property
    def tools(self) -> Mapping[str, Tool]:
        """Every registered tool by name (read-only, live)."""
        return types.MappingProxyType(self._app.registry.tools)

    @property
    def cancelled(self) -> bool:
        """Whether the user interrupted the turn or command in progress, or (for the ``ctx`` of an agent started
        with :meth:`run_agent`) :meth:`cancel_agent` stopped this agent; poll it in long loops."""
        return self._flag().is_set()

    def _wrap_command(self, argv: list[str]) -> list[str]:
        """``argv`` as the registered command wrapper (``Extension.set_command_wrapper``) rewrites it; ``argv`` itself
        when there is none. Used by ``bash``; never runs the command unwrapped when the wrapper fails.

        Raises:
            RuntimeError: if the wrapper raised or returned something that is not a list of strings.
        """
        wrapper = self._app.registry.command_wrapper
        if wrapper is None:
            return argv
        try:
            wrapped = wrapper(list(argv), self)
            if not isinstance(wrapped, list) or not wrapped or not all(isinstance(a, str) for a in wrapped):
                raise TypeError("it must return a non-empty list of strings")
        except Exception as e:
            raise RuntimeError(f"command wrapper failed: {e}") from e
        return wrapped

    def _flag(self) -> threading.Event:
        return self._app.cancelled if self._run is None else self._run.cancel

    def check_cancelled(self) -> None:
        """Raise :class:`~minimal.extensions.registry.TurnInterrupted` if the user interrupted (see :attr:`cancelled`)."""
        agent.raise_if_cancelled(self._flag())

    def system_prompt(self) -> str:
        """The system prompt the next turn would send (for the working directory of this ``ctx``, see :attr:`cwd`)."""
        return self._app.system_prompt(self.cwd)

    def complete(
        self,
        messages: list[dict],
        *,
        system: str | None = None,
        tools: bool = True,
        options: Mapping[str, Any] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> Reply:
        """One model request with the current provider and model; no tools are run.

        A failed request goes to the ``provider_error`` handlers, which may retry it as for a turn. The request counts
        in :attr:`usage`, and (API 2.4) fires ``after_response`` with ``source`` ``"complete"``: the reply is not
        added to any history, so a handler that reads ``ctx.messages`` there should skip it.

        Args:
            messages: The conversation to send, in the canonical format.
            system: System prompt (default: :meth:`system_prompt`).
            tools: Offer the registered tools, as a turn does (keeps the provider's prompt cache warm).
            options: Request options (``max_tokens``, ``temperature``, ``reasoning_effort``) over the ones a turn uses.
            model: Another model of the current provider (API 2.3); the request uses its ``max_tokens`` setting.
            reasoning_effort: ``"low"``, ``"medium"`` or ``"high"`` for this request only (API 2.4); it beats
                :attr:`reasoning_effort` and ``options["reasoning_effort"]``.

        Raises:
            ValueError: if ``reasoning_effort`` is not ``None`` or one of those.
            TurnInterrupted: if the user interrupts while the reply streams.
        """
        app = self._app
        app.check_reasoning_effort(reasoning_effort, "complete: reasoning_effort")
        opts = {**app.request_options(app.provider_name, model, self.reasoning_effort), **(options or {})}
        if reasoning_effort is not None:
            opts["reasoning_effort"] = reasoning_effort
        assert app.provider is not None  # startup picks a provider before any extension runs
        provider, model_name = app.provider, model or app.model
        system_text = self.system_prompt() if system is None else system
        tool_list = agent.tool_specs(app.registry, gate=app.mode_block_reason) if tools else []
        who = {"agent_id": self.agent_id, "parent_id": self.parent_id}
        switch = app.fallback_provider if model is None else app.create_provider
        flag = self._flag()
        reply = None
        attempt = 0
        while reply is None:
            try:
                reply = provider.stream(
                    model=model_name,
                    system=system_text,
                    messages=messages,
                    tools=tool_list,
                    on_text=lambda _chunk: self.check_cancelled(),
                    **({"options": opts} if opts else {}),
                )
            except TurnInterrupted:
                raise
            except Exception as e:
                attempt += 1
                retry = agent._provider_error(
                    e, attempt, app.registry, self, provider, model_name, self._run is not None, switch, flag, who
                )
                if retry is None:
                    raise
                provider, model_name = retry
                self.check_cancelled()
        app.usage.add(reply.usage, model_name, provider.name)
        app.registry.emit(
            "after_response",
            {
                "ctx": self,
                "reply": reply,
                "usage": reply.usage,
                "nested": self._run is not None,
                "model": model_name,
                "provider": provider.name,
                "source": "complete",
                **who,
            },
        )
        return reply

    def complete_text(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 1024,
        messages: list[dict] | None = None,
        options: Mapping[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Ask the model one question and return its answer as text: one request, no tools, not added to the history.

        API 2.3. A thin layer over :meth:`complete` for utilities such as a title, a summary or a classification:
        by default the conversation is not sent, only ``prompt``. The request counts in :attr:`usage` and a failure
        goes to the ``provider_error`` handlers.

        Args:
            prompt: The user message. May be empty if ``messages`` is given and ends with a user message.
            system: The system prompt; none by default (pass :meth:`system_prompt` to send the session's).
            model: Another model of the current provider; default the current model.
            max_tokens: The most tokens the answer may have.
            messages: A conversation to send before ``prompt`` (API 2.4), in the canonical format, e.g. ``ctx.messages``
                for a summary of the history. It should end with an assistant message when ``prompt`` is not empty.
            options: Further request options (``temperature``...), which beat ``max_tokens`` (API 2.4).
            reasoning_effort: As for :meth:`complete` (API 2.4).

        Raises:
            ValueError: if both ``prompt`` and ``messages`` are empty, ``max_tokens`` is not a positive integer or
                ``reasoning_effort`` is invalid.
            TurnInterrupted: if the user interrupts while the reply streams.
        """
        if not prompt.strip() and not messages:
            raise ValueError("complete_text: prompt is empty")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise ValueError("complete_text: max_tokens must be an integer >= 1")
        send = list(messages or [])
        if prompt.strip():
            send.append({"role": "user", "content": prompt})
        reply = self.complete(
            send,
            system=system or "",
            tools=False,
            options={"max_tokens": max_tokens, **(options or {})},
            model=model,
            reasoning_effort=reasoning_effort,
        )
        return result_text(reply.content)

    @property
    def usage(self) -> UsageTotals:
        """Tokens and requests this minimal process has spent so far (API 2.3): every model request of the user's
        turns, of the agents they start and of :meth:`complete`, since it started. A new or resumed session does not
        reset it, and it is not saved: ``minimal -c`` starts counting again. A snapshot, not live.

        API 2.4: ``usage.by_model`` splits the counts per ``"provider/model"``, e.g. for a cost budget:
        ``sum(prices[m].input * t.input_tokens + prices[m].output * t.output_tokens for m, t in usage.by_model.items())``
        with a price table of your own (minimal knows none)."""
        return self._app.usage.totals

    def progress(self, text: str | None, *, key: str | None = None) -> None:
        """Show ``text`` as a one-line progress note next to the working indicator while a tool or command runs;
        ``None`` clears it.

        API 2.3. Only the first line is shown, at most 120 characters; the terminal UI also clears it when the
        turn or command ends. Call it as often as you like, from any thread (the console prints each change on stderr
        when it is a terminal).

        API 2.4: every tool call has a slot of its own, so tools running at once (parallel ``read_only`` calls, agents)
        do not overwrite each other. The frontend shows the slot set last, and ``"… (+n more)"`` while ``n`` others
        are live. A call's slot is cleared when the call ends, and ``progress(None)`` clears the caller's own. Outside
        a tool call (a command, a widget, a thread the tool started) the slot is the agent's (``"main"`` for the user's
        turn and commands).

        Args:
            text: The note, or ``None`` to clear the slot.
            key: Another slot's name, for notes that are not tied to the running call (a background job): it stays
                until ``progress(None, key=...)`` or the turn or command ends.

        Raises:
            TypeError: if ``text`` is neither a string nor ``None``, or ``key`` is neither a string nor ``None``.
        """
        if text is not None:
            if not isinstance(text, str):
                raise TypeError("progress: text must be a string or None")
            text = " ".join(text.split("\n", 1)[0].split())[:120] or None
        if key is not None and not isinstance(key, str):
            raise TypeError("progress: key must be a string or None")
        slot = key if key is not None else getattr(current_call, "key", None) or self.agent_id
        self._app.progress_slots.set(slot, text)

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
        cwd: str | None = None,
        reasoning_effort: str | None = None,
        mentions: bool = False,
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
            name, system, tools, model, provider, max_turns, cwd, reasoning_effort, mentions: See :meth:`run_agent_result`.

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
            cwd=cwd,
            reasoning_effort=reasoning_effort,
            mentions=mentions,
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
        cwd: str | None = None,
        reasoning_effort: str | None = None,
        mentions: bool = False,
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
            max_turns: Most model requests it may make (default :attr:`max_turns`).
            cwd: A folder (relative to this ``ctx``'s :attr:`cwd`) the agent works in, API 2.4: the ``ctx.cwd`` its
                tools and events get (so the built-in tools, ``bash`` and the "outside the workspace" check use it), its
                default system prompt's working directory (and the project instructions it reads there) and its
                permission checks follow it. The agents it starts inherit it. Other agents, and this one's parent,
                keep theirs; any number may run at once in different folders. Default: this ``ctx``'s.
            reasoning_effort: ``"low"``, ``"medium"`` or ``"high"`` for this agent's requests only (and the agents it
                starts), API 2.4; ``None``: the current :attr:`reasoning_effort`. It replaces a ``before_request``
                handler that changed the effort.
            mentions: Expand ``@path`` mentions in ``prompt`` as the terminal UI does for the user's messages
                (the files' content is attached to the agent's first message), resolved against ``cwd``. API 2.4.

        Returns:
            Its :class:`~minimal.core.agents.AgentResult`.

        Raises:
            ValueError: if ``tools`` and ``exclude_tools`` are both given, ``tools`` names a tool that is not
                registered, ``max_turns`` is not a positive integer, ``cwd`` is not an existing folder,
                ``reasoning_effort`` is invalid, or ``provider`` is unknown or lacks its credentials.
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
            cwd=cwd,
            reasoning_effort=reasoning_effort,
            mentions=mentions,
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

    def fork(self, at_index: int | None = None) -> str:
        """Continue in a new session that starts as a copy of this one up to message ``at_index``; return its id.

        API 2.3. The copy holds ``messages[:at_index]`` (every message if ``at_index`` is ``None``); the current
        session stays saved as it is, and the new session's header names it as its ``parent``. Fires
        ``session_switch``, and the terminal UI redraws the transcript from the new session.

        Raises:
            ValueError: if a turn is running (a command or a tool's ``ctx`` of the user's turn cannot fork it),
                ``at_index`` is not in ``0..len(messages)``, or the cut would separate an assistant ``tool_use``
                from the ``tool_result`` message after it: cut before the call or after its results.
        """
        return self._app.fork_session(at_index)

    def truncate(self, at_index: int) -> int:
        """Drop the messages from ``at_index`` on from the current session, and from its file; return how many.

        API 2.3. ``ctx.messages`` is cut in place and the session file is rewritten atomically, so a crash never
        leaves half a history. The terminal UI redraws the transcript. It does not fire ``session_switch``: this is
        still the same session. ``fork`` first to keep the dropped messages.

        Raises:
            ValueError: as for :meth:`fork`.
        """
        return self._app.truncate_session(at_index)

    def sessions(self, *, all_projects: bool = False) -> list[str]:
        """Saved session ids, oldest first: this project's (see ``cwd``), or every project's with ``all_projects``."""
        return list_sessions(None if all_projects else self._app.cwd)

    def session_info(self, session_id: str | None = None) -> SessionInfo:
        """What session ``session_id`` (default the current one) says about itself, without its messages. API 2.4.

        A :class:`~minimal.core.session.SessionInfo`: id, title, parent, cwd, a preview of the first user message,
        mtime, number of messages, creation time and whether another process holds it open. A saved session is read
        from the first lines of its file and a count of the rest, so it is cheap for a long one. The current session
        is read live (unsaved messages count; ``mtime`` is the file's, or now if it was never saved). A file that is
        damaged (no header, lines that are not JSON) gives what could be read, not an error.

        Raises:
            ValueError: if ``session_id`` is not a valid id, or there is no such session or it cannot be read.
        """
        app = self._app
        current = app.session
        if session_id is None or session_id == current.id:
            try:
                mtime = current.file.stat().st_mtime
            except OSError:
                mtime = time.time()
            first = next((m for m in current.messages if m.get("role") == "user" and prompt_text(m)), None)
            return SessionInfo(
                current.id,
                current.title,
                current.parent,
                current.cwd or app.cwd,
                " ".join(prompt_text(first).split())[:200] if first is not None else "",
                mtime,
                len(current.messages),
                created_at(current.id),
                current._lock_fd is not None,
            )
        return read_session_info(session_id)

    def sessions_info(self, limit: int | None = None, *, all_projects: bool = False) -> list[SessionInfo]:
        """:meth:`session_info` of the saved sessions, most recently written first: this project's, or every project's
        with ``all_projects``; at most ``limit`` of them. API 2.4.

        Files are ordered by modification time and read only until ``limit`` matched. The current session appears
        once it is saved. A damaged file is listed with what could be read; a file that vanishes meanwhile is left out.
        """
        return read_session_infos(None if all_projects else self._app.cwd, limit)

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
        return self._app.reload_summary()

    def commands(self) -> dict[str, str]:
        """Every slash command name (without ``/``) and its description: built-in and extension commands,
        an extension's replacing a built-in of the same name."""
        return {name: cmd.description for name, cmd in self._app.commands().items()}

    def run_command(self, line: str, *, builtin: bool = False) -> None:
        """Run slash command ``line`` (``"/model x"``) as if the user typed it; its output is printed.

        ``builtin`` runs the built-in command even when an extension replaced it, so a replacement can
        hand over to the original. Errors are reported, not raised.

        The user's interrupt flag (:attr:`cancelled`) is cleared first only when no turn or other command is running
        (API 2.4), so a timer thread calling this cannot swallow the user's Esc. To run a command after the turn in
        progress instead, :meth:`queue_user_message` it (``"/name args"``).
        """
        self._app.run_command(line, builtin=builtin)

    def models(self) -> list[str]:
        """The current provider's models, queried from its API (falling back to its default); the current one is included."""
        return self._app.available_models()[0]

    def set_model(self, name: str) -> None:
        """Use model ``name`` of the current provider from the next request on."""
        self._app.model = name

    def set_reasoning_effort(self, value: str | None) -> None:
        """Ask for ``"low"``, ``"medium"`` or ``"high"`` reasoning effort from the next request on, or ``None`` for the
        provider's default. It beats the ``reasoning_effort`` setting until minimal restarts. A provider or model that
        cannot think more or less ignores it. API 2.3.

        Raises:
            ValueError: if ``value`` is none of those.
        """
        self._app.set_reasoning_effort(value)

    def widget(self, name: str) -> object | None:
        """The widget the frontend shows for extension widget ``name`` (see ``register_widget``); ``None`` if none."""
        return self._app.frontend.widget(name)

    def notify(self, text: str, kind: str = "info") -> None:
        """Show a short notice to the user (a toast in the terminal UI); ``kind`` is one of :data:`NOTIFY_KINDS`.

        API 2.4: fires ``notify`` (``{ctx, text, kind, source}``) first, so an extension can keep a history of notices.

        Raises:
            ValueError: if ``kind`` is not one of them.
        """
        if kind not in NOTIFY_KINDS:
            raise ValueError(f"notify: kind must be one of {', '.join(NOTIFY_KINDS)}")
        self._app.notify(text, kind, "extension")

    def show(self, text: str, kind: str = "output") -> None:
        """Add ``text`` to the conversation as a block that stays: ``kind`` is one of :data:`SHOW_KINDS`. API 2.4.

        Unlike :meth:`notify` (a toast that vanishes) and ``print`` (lost when a ``terminal`` command or key resumes
        the interface), the block is kept in the transcript. Called while the interface is suspended for a
        ``terminal`` command or key it is held and drawn once the interface is back. Safe from any thread; blank text
        is ignored. The plain terminal prints it (``error`` on stderr).

        Raises:
            ValueError: if ``kind`` is not one of them or ``text`` is not a str.
        """
        if kind not in SHOW_KINDS:
            raise ValueError(f"show: kind must be one of {', '.join(SHOW_KINDS)}")
        if not isinstance(text, str):
            raise ValueError("show: text must be a str")
        if text.strip():
            self._app.frontend.show(text, kind)

    def focus_widget(self, name: str, *, hold: bool = False) -> bool:
        """Give the keyboard focus to the widget of ``register_widget(name, ...)`` (to its first focusable part if it is
        not focusable itself); returns whether it took it. API 2.4.

        ``False`` if the widget is not shown (the user hid it, or no terminal UI), cannot take focus, or the UI
        is showing a dialog. By default the UI moves the focus back to the prompt box when a turn or command ends.
        With ``hold=True`` it does not, for a widget that wants to keep the keyboard (an embedded terminal, a file
        browser): the hold ends by itself when the widget is removed (a reload, the user hiding it) or the focus leaves it
        (the user clicked elsewhere), when it reaches the key ``escape`` (a widget that handles ``escape`` itself keeps
        the focus), and on :meth:`focus_composer`. Safe from any thread.
        """
        return self._app.frontend.focus_widget(name, hold)

    def focus_composer(self) -> None:
        """Give the keyboard focus to the prompt box and end any hold of :meth:`focus_widget`. API 2.4. Safe from any thread."""
        self._app.frontend.focus_composer()

    def git_refresh(self) -> None:
        """Ask the shared git poller behind :meth:`git` to look again now, after you changed files or branches. API 2.4.

        Cheap, returns at once and does nothing until :meth:`git` started the poller; the new state shows in :meth:`git`
        within moments, and the poller calls :meth:`invalidate` when it differs. minimal does this itself after each turn
        and command; call it when something else changed the repository (a ``terminal`` command, a timer).
        """
        self._app.poke_git()

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
