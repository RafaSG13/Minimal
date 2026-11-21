"""What an extension's ``register(minimal)`` receives: an :class:`Extension`.

One is built per extension. It registers into minimal's internal registry, knows
the extension's id, path, scope and version, and hands out the extension's own
settings from ``extensions.settings.<id>`` in settings.json::

    \"\"\"Greets the user.\"\"\"

    __version__ = "1.0"
    __api__ = "1"

    def register(minimal):
        settings = minimal.declare_settings({"greeting": "hello"})

        @minimal.tool(input_schema={"type": "object", "properties": {}})
        def greet(input, ctx):
            \"\"\"Greet the user.\"\"\"
            return settings["greeting"]

        @minimal.on("tool_call")
        def guard(event):
            ...
"""

from __future__ import annotations

import types
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from minimal import config
from minimal.extensions.metadata import Metadata
from minimal.extensions.registry import (
    CommandComplete,
    CommandRun,
    EventHandler,
    PromptText,
    Registry,
    SettingsSpec,
    Tool,
    ToolRun,
    check_event,
    check_schema,
    check_setting,
    setting_matches,
)
from minimal.providers.base import Provider, ProviderSpec, Reply, Usage

if TYPE_CHECKING:
    from minimal.core.context import Context


class ToolCallEvent(TypedDict):
    """Payload of ``tool_call``: fired before a tool runs. Return ``{"block": True, "reason": str}`` to veto it."""

    name: str
    input: dict
    ctx: Any
    """The :class:`~minimal.core.context.Context` (of the agent that makes the call)."""
    agent_id: str
    """The loop that fired it: ``"main"`` for the user's turn, else the id of a ``ctx.run_agent`` agent."""
    parent_id: str | None
    """The agent whose tool started that loop; ``None`` for ``"main"``."""


class ToolResultEvent(TypedDict):
    """Payload of ``tool_result``: fired after a tool ran. Return ``{"output": str}`` to rewrite what the model sees."""

    name: str
    input: dict
    output: str
    """The tool's text; a picture a tool returned shows as ``[image: media_type, N bytes]``. A rewrite replaces everything, pictures too."""
    is_error: bool
    ctx: Any
    agent_id: str
    parent_id: str | None


class TurnEndEvent(TypedDict):
    """Payload of ``turn_end``: fired when the model answered without calling tools."""

    messages: list[dict]
    """The history of the loop that ended (not ``ctx.messages`` when ``nested``)."""
    ctx: Any
    nested: bool
    """The loop ran inside a tool, such as a subagent."""
    agent_id: str
    parent_id: str | None


class SessionStartEvent(TypedDict):
    """Payload of ``session_start``: fired once per process when the UI is up."""

    ctx: Any
    resumed: bool
    """The session already had messages (``-c``, ``-s``)."""


class SessionSwitchEvent(TypedDict):
    """Payload of ``session_switch``: ``/new``, ``/resume`` or ``ctx.new_session`` replaced the session (the old one is saved)."""

    ctx: Any
    old_id: str
    new_id: str


class TurnStartEvent(TypedDict):
    """Payload of ``turn_start``: the user sent ``text``. Return ``{"text": str}`` to rewrite it before it enters the history."""

    ctx: Any
    text: str


class BeforeRequestEvent(TypedDict):
    """Payload of ``before_request``: fired before each model request.

    Return any of ``{"messages": list, "system": str, "tools": list}`` to send that instead, for
    this request only (the history does not change), and ``{"options": dict}`` to add request options
    (``max_tokens``, ``temperature``). Do not mutate the payload; return copies.
    """

    ctx: Any
    messages: list[dict]
    system: str
    tools: list[dict]
    options: dict
    """The request options so far (``max_tokens``, ``temperature``); empty for the provider's defaults."""
    nested: bool
    """The loop runs inside a tool, such as a subagent."""
    agent_id: str
    parent_id: str | None


class AfterResponseEvent(TypedDict):
    """Payload of ``after_response``: a reply was appended to the history."""

    ctx: Any
    reply: Reply
    usage: Usage | None
    nested: bool
    agent_id: str
    parent_id: str | None


class ShutdownEvent(TypedDict):
    """Payload of ``shutdown``: minimal is exiting."""

    ctx: Any


class ProviderErrorEvent(TypedDict):
    """Payload of ``provider_error``: a model request failed.

    Return ``{"retry_after": seconds}`` to send it again after a wait, or ``{"provider": name,
    "model": name}`` (``model`` optional) to send it again to another provider. Use ``attempt`` to
    give up: return ``None`` and the error propagates.
    """

    ctx: Any
    error: Exception
    attempt: int
    """How many times this request has failed, from 1."""
    provider: str
    model: str
    nested: bool
    agent_id: str
    parent_id: str | None


class AgentStartEvent(TypedDict):
    """Payload of ``agent_start``: an agent loop begins (the user's turn, or ``ctx.run_agent``)."""

    ctx: Any
    agent_id: str
    parent_id: str | None
    name: str
    """``"main"`` for the user's turn, else the ``name`` given to ``run_agent`` (``"agent"`` by default)."""
    prompt: str
    """The user's message, or the agent's prompt."""


class AgentEndEvent(TypedDict):
    """Payload of ``agent_end``: an agent loop ended, whichever way."""

    ctx: Any
    agent_id: str
    parent_id: str | None
    name: str
    status: str
    """``"done"``, ``"interrupted"``, ``"error"`` or ``"max_turns"``."""
    text: str
    """The text of its last reply."""
    usage: Usage
    """Token counts summed over the loop's replies."""
    error: str | None
    """Why it failed (``error``, ``max_turns``); else ``None``."""


class ModeChangeEvent(TypedDict):
    """Payload of ``mode_change``: the current mode changed."""

    ctx: Any
    old: str | None
    """The mode that was current; ``None`` for no mode."""
    new: str | None
    """The mode that is current now; ``None`` for no mode."""


class ThemeChangeEvent(TypedDict):
    """Payload of ``theme_change``: the terminal UI's colour theme changed (``/theme``, the ``Ctrl+P`` picker or a
    reload). ``ctx.theme`` has the new theme's colours. API 2.3."""

    ctx: Any
    name: str
    """The theme that is current now."""
    dark: bool
    """Whether it is a dark theme."""


def _first_line(doc: str | None) -> str:
    return doc.strip().splitlines()[0] if doc and doc.strip() else ""


class Extension:
    """The ``minimal`` argument of an extension's ``register(minimal)``.

    Attributes:
        id: ``name`` or ``category/name`` (``tools/subagent``): the key of its settings
            and of ``extensions.disabled``.
        path: The ``.py`` file or package folder.
        scope: ``"global"``, ``"project"``, ``"cli"`` (``--ext``) or ``"bundled"`` (shipped inside minimal).
        version: Its ``__version__``; ``""`` if unset.
        project_trusted: Whether the current project passed ``/trust`` (or minimal runs with ``--trust-project``).
    """

    def __init__(
        self,
        *,
        id: str,
        path: Path,
        scope: str,
        cwd: str,
        metadata: Metadata,
        registry: Registry,
        settings: config.Settings,
        errors: list[str],
    ) -> None:
        self._project_trusted = settings.project_trusted
        self._id, self._path, self._scope, self._metadata = id, path, scope, metadata
        self._cwd = cwd
        self._registry = registry
        self._all_settings = settings
        self._user_settings = settings.extension_settings(id)
        self._errors = errors
        self._settings: Mapping[str, Any] = types.MappingProxyType({})
        self._declared = False
        self._registered = False

    def _finish_register(self) -> None:
        """Called by the loader when ``register`` returned: :attr:`ctx` is available from now on."""
        self._registered = True

    @property
    def ctx(self) -> Context | None:
        """The app's :class:`~minimal.core.context.Context`, for code that runs outside any tool, command, event or
        widget call: a timer or watcher thread started in ``register``, a subscription, a ``Panel`` callback. API 2.4.

        ``None`` while ``register`` runs (the extensions are still loading: a thread that starts there should read
        ``minimal.ctx`` when it fires, not at start) and always under ``minimal ext check`` and in any registry that no app
        owns. After ``register`` returned it is the same live ``ctx`` as commands get (``agent_id`` ``"main"``), also on
        the extension object a ``/reload`` creates. An old object's thread, if :meth:`on_dispose` did not stop it, would
        keep reaching the app through it. It is safe from any thread, but calling ``ctx.run_command`` or
        ``ctx.settings.update`` there acts on the app live; see their notes.
        """
        return self._registry.context if self._registered else None

    def report_problem(self, text: str) -> None:
        """Record a non-fatal problem of this extension: it still loads. API 2.4.

        For settings or an environment the extension can work around (two settings that contradict each other, a missing
        optional binary, a value it clamped). The reload summary and the terminal UI show it as a warning
        (``Warning: <id>: <text>``), ``/ext list`` lists it and ``minimal ext check`` prints a ``WARN`` line for it
        without failing the exit status. Unlike an exception from ``register`` it does not mark the extension as failed
        (``reload_extensions`` does not raise for it), and unlike a setting rejected by ``declare_settings`` it is yours
        to word.

        Raises:
            ValueError: if ``text`` is empty.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("report_problem: text must be a non-empty string")
        self._registry.problems.append(f"{self._id}: {' '.join(text.split())}")

    @property
    def id(self) -> str:
        return self._id

    @property
    def path(self) -> Path:
        return self._path

    @property
    def scope(self) -> str:
        return self._scope

    @property
    def version(self) -> str:
        return self._metadata.version

    @property
    def project_trusted(self) -> bool:
        """Whether the user trusted the current project: ``/trust`` accepted its present extensions and trust-gated
        settings, or minimal was started with ``--trust-project``. Fixed for this load: ``/trust`` reloads.

        Read it before acting on what a repository controls (its prompt text, servers, scripts). A project with
        nothing for ``/trust`` to review (no project extensions, no trust-gated settings) is not trusted unless
        ``--trust-project`` is set.
        """
        return self._project_trusted

    @property
    def tools(self) -> Mapping[str, Tool]:
        """Every registered tool by name (read-only, live): call ``tools["bash"].run(input, ctx)`` to reuse one."""
        return types.MappingProxyType(self._registry.tools)

    def data_dir(self, scope: str = "project") -> Path:
        """A private folder (mode 0700) for this extension's files, outside the repository; created if missing.

        ``"project"``: ``~/.minimal/data/projects/<key>/<id>``, one per working directory (``<key>``
        is a hash of its real path). ``"global"``: ``~/.minimal/data/global/<id>``, shared by every
        project. ``<id>`` is :attr:`id` (``tools/subagent`` is a subfolder).

        Raises:
            ValueError: if ``scope`` is neither.
        """
        if scope == "project":
            path = config.project_data_dir(self._cwd) / self._id
        elif scope == "global":
            path = config.HOME_DIR / "data" / "global" / self._id
        else:
            raise ValueError('data_dir: scope must be "project" or "global"')
        config.private_dir(path)
        return path

    # --- registering ---------------------------------------------------------

    def register_tool(
        self,
        name: str,
        run: ToolRun,
        description: str = "",
        input_schema: dict | None = None,
        confirm: bool | Callable[[dict, Any], bool] = False,
        subject_key: str | None = None,
        read_only: bool = False,
    ) -> None:
        """Add a tool the model can call as ``run(input, ctx)``; see :meth:`Registry.register_tool`.

        ``subject_key`` names the ``input`` field that permission rules ``name(pattern)`` match.
        ``read_only`` (API 2.3) says the tool changes nothing and is thread-safe, so calls to it may run concurrently
        with each other (see :attr:`~minimal.extensions.registry.Tool.read_only`).
        """
        self._registry.register_tool(name, run, description, input_schema, confirm, subject_key, read_only)

    def register_command(
        self,
        name: str,
        run: CommandRun,
        description: str = "",
        terminal: bool = False,
        complete: CommandComplete | None = None,
    ) -> None:
        """Add the slash command ``/name``, run as ``run(args, ctx)``; see :meth:`Registry.register_command`.

        ``complete(args, ctx)`` returns candidate completions of the text typed after ``/name `` (API 2.3).
        """
        self._registry.register_command(name, run, description, terminal, complete)

    def register_widget(
        self,
        name: str,
        create: Callable[[Any], Any],
        area: str = "sidebar",
        *,
        order: int = 0,
        title: str | None = None,
        collapsible: bool = False,
    ) -> None:
        """Add a terminal UI widget built by ``create(ctx)``; see :meth:`Registry.register_widget`.

        ``area`` may also be ``"left"`` or ``"footer"``, and ``order`` stacks an area's widgets. API 2.3.
        ``title`` draws a heading above the widget and ``collapsible`` lets a click on it collapse the widget. API 2.3."""
        self._registry.register_widget(name, create, area, order=order, title=title, collapsible=collapsible)

    def register_status(
        self, name: str, render: Callable[[Any], Any], side: str = "right", *, order: int = 0
    ) -> None:
        """Add a status-line segment drawn by ``render(ctx)``; see :meth:`Registry.register_status`.

        ``order`` sorts the segments of a side (API 2.3)."""
        self._registry.register_status(name, render, side, order=order)

    def register_panel(
        self,
        name: str,
        render: Callable[[Any], Any],
        area: str = "sidebar",
        live: Callable[[Any, dict], Any] | None = None,
        interval: float = 0.0,
        *,
        order: int = 0,
        title: str | None = None,
        collapsible: bool = False,
    ) -> None:
        """Add a panel of styled text lines the terminal UI can draw; see :meth:`Registry.register_panel`. API 1.3.

        ``area`` may also be ``"left"`` or ``"footer"``, and ``order`` stacks an area's panels and widgets. API 2.3.
        ``title`` draws a heading line above the panel and ``collapsible`` lets a click or Enter on it collapse the
        panel to it (the user's ``ui.widgets.<name>.title`` / ``collapsed`` and ``/ui title|collapse`` override). API 2.3."""
        self._registry.register_panel(
            name, render, area, live, interval, order=order, title=title, collapsible=collapsible
        )

    def register_keybinding(
        self,
        key: str,
        run: Callable[[Any], Any],
        description: str = "",
        *,
        show: bool = False,
        name: str = "",
    ) -> None:
        """Bind the global key ``key`` (``ctrl+o``, ``alt+x``, ``f5``) to ``run(ctx)``; see :meth:`Registry.register_keybinding`.

        ``run`` runs on a worker thread like a slash command, so it may ask the user questions and use ``ctx``;
        a key is ignored while a turn, a command or a dialog is running. Keys the UI needs
        (:data:`~minimal.extensions.registry.RESERVED_KEYS`) are refused. The user can give the binding another
        key with ``ui.keys``, under ``"<extension id>:<name or description>"``. If another extension already bound
        the key, this one replaces it and the reload reports it. API 2.3.

        Raises:
            ValueError: if ``key`` is not a valid, unreserved key or ``run`` is not callable.
        """
        replaced = self._registry.register_keybinding(
            key, run, description, show=show, name=name, ext=self._id
        )
        if replaced is not None:
            who = f"{replaced.ext}'s " if replaced.ext and replaced.ext != self._id else "its own earlier "
            self._errors.append(f"{self._id}: key {replaced.key} was bound by {who}binding {replaced.label!r}; replaced")

    def register_css(self, name: str, css: str) -> None:
        """Add a Textual stylesheet for the terminal UI, replacing the one of the same ``name`` this extension added;
        see :meth:`Registry.register_css`. The stable selectors to style are in ``docs/extensions.md``. API 2.3.

        Raises:
            ValueError: if ``name`` is empty or ``css`` is not a string.
        """
        self._registry.register_css(name, css, self._id)

    def register_renderer(self, kind: str, render: Callable[[Any, dict], Any]) -> None:
        """Draw a transcript block kind (``fence:<language>``, ``blockquote:alert``, ``tool:<name>``) with ``render(ctx, block)``; see :meth:`Registry.register_renderer`. API 1.3."""
        self._registry.register_renderer(kind, render)

    def register_theme(
        self, name: str, *, dark: bool, background: str, foreground: str, accent: str, **colors: str
    ) -> None:
        """Add a colour theme; see :meth:`Registry.register_theme`."""
        self._registry.register_theme(
            name, dark=dark, background=background, foreground=foreground, accent=accent, **colors
        )

    def register_logo(self, art: list[str], colors: dict[str, str], align: str = "center") -> None:
        """Replace the welcome header's wordmark with pixel art; see :meth:`Registry.register_logo`."""
        self._registry.register_logo(art, colors, align)

    def register_provider(
        self,
        name: str,
        create: Callable[[], Provider],
        default_model: str,
        env_keys: tuple[str, ...] = (),
        description: str = "",
    ) -> None:
        """Add a model integration; see :meth:`Registry.register_provider`."""
        self._registry.register_provider(name, create, default_model, env_keys, description)

    def add_provider(self, spec: ProviderSpec) -> None:
        """Add a ready-made :class:`~minimal.providers.base.ProviderSpec`, e.g. ``openai_compatible(...)``.

        Raises:
            ValueError: if ``name`` or ``default_model`` is empty or ``create`` is not callable.
        """
        self._registry.add_provider(spec)

    def append_system_prompt(self, text: str) -> None:
        """Add a paragraph to the end of the system prompt."""
        self._registry.append_system_prompt(text)

    def register_prompt_section(self, name: str, text: PromptText, *, replace: str | None = None) -> None:
        """Add a named section to the system prompt, or replace one (``replace="base"``); see :meth:`Registry.register_prompt_section`. API 2.2."""
        self._registry.register_prompt_section(name, text, replace=replace)

    def remove_prompt_section(self, name: str) -> None:
        """Take a section out of the system prompt; see :meth:`Registry.remove_prompt_section`. API 2.2."""
        self._registry.remove_prompt_section(name)

    def register_mode(
        self,
        name: str,
        *,
        description: str,
        tools: Iterable[str] | None = None,
        exclude_tools: Iterable[str] = (),
        system_note: str = "",
        read_only: bool = False,
    ) -> None:
        """Add a mode (a restriction on the tools the model may use, with an optional prompt note); see :meth:`Registry.register_mode`. API 2.2."""
        self._registry.register_mode(
            name,
            description=description,
            tools=tools,
            exclude_tools=exclude_tools,
            system_note=system_note,
            read_only=read_only,
        )

    def on(self, event: str, handler: EventHandler | None = None):
        """Subscribe ``handler(event)`` to ``event``: one of :data:`~minimal.extensions.registry.EVENTS`
        (``tool_call``, ``tool_result``, ``turn_end``, ``session_start``, ``session_switch``,
        ``turn_start``, ``before_request``, ``after_response``, ``shutdown``, ``provider_error``,
        ``agent_start``, ``agent_end``, ``mode_change``, ``theme_change``).

        Without ``handler``, returns a decorator that subscribes the decorated function.

        Raises:
            ValueError: if ``event`` is unknown.
        """
        check_event(event)
        if handler is not None:
            self._registry.on(event, handler)
            return handler

        def decorate(fn: EventHandler) -> EventHandler:
            self._registry.on(event, fn)
            return fn

        return decorate

    def on_dispose(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        """Call ``fn()`` before the extensions are reloaded and when minimal exits; returns ``fn``, so it works as a decorator.

        Stop the threads, timers and subprocesses the extension started there: a reload imports it
        again and calls ``register`` on a fresh ``minimal``. Disposers run last registered first; one
        that raises is reported and the rest still run.

        Raises:
            ValueError: if ``fn`` is not callable.
        """
        self._registry.on_dispose(fn)
        return fn

    def tool(
        self,
        name: str | None = None,
        *,
        description: str = "",
        input_schema: dict | None = None,
        confirm: bool | Callable[[dict, Any], bool] = False,
        subject_key: str | None = None,
        read_only: bool = False,
    ) -> Callable[[ToolRun], ToolRun]:
        """Decorator registering ``fn(input, ctx)`` as a tool: the name defaults to the function's,
        the description to the first line of its docstring. ``read_only`` (API 2.3): see :meth:`register_tool`."""

        def decorate(fn: ToolRun) -> ToolRun:
            self.register_tool(
                name or fn.__name__,
                fn,
                description or _first_line(fn.__doc__),
                input_schema,
                confirm,
                subject_key,
                read_only,
            )
            return fn

        return decorate

    def command(
        self,
        name: str | None = None,
        *,
        description: str = "",
        terminal: bool = False,
        complete: CommandComplete | None = None,
    ) -> Callable[[CommandRun], CommandRun]:
        """Decorator registering ``fn(args, ctx)`` as ``/name``: the name defaults to the function's,
        the description to the first line of its docstring. ``complete`` is as in :meth:`register_command`."""

        def decorate(fn: CommandRun) -> CommandRun:
            self.register_command(name or fn.__name__, fn, description or _first_line(fn.__doc__), terminal, complete)
            return fn

        return decorate

    def keybinding(
        self, key: str, *, description: str = "", show: bool = False, name: str = ""
    ) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
        """Decorator binding the global key ``key`` to ``fn(ctx)``: the description defaults to the first line of its
        docstring; see :meth:`register_keybinding`. API 2.3."""

        def decorate(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
            self.register_keybinding(key, fn, description or _first_line(fn.__doc__), show=show, name=name)
            return fn

        return decorate

    # --- settings ------------------------------------------------------------

    def declare_settings(self, defaults: dict, schema: Mapping[str, Mapping[str, Any]] | None = None) -> Mapping[str, Any]:
        """Declare this extension's settings and their defaults; return the effective values (read-only).

        Users set them under ``extensions.settings.<id>`` in settings.json. A key not
        in ``defaults``, or a value whose type differs from its default's, is reported
        as a reload error and ignored.

        ``schema`` (API 2.3) documents and constrains settings: it maps a key of ``defaults`` to
        ``{"description": str, "enum": [...], "min": n, "max": n}`` (every field optional; ``enum`` lists the allowed
        values, ``min`` and ``max`` bound a number). A user's value that is the wrong type, not in ``enum`` or out of
        range is a collected load error naming ``extensions.settings.<id>.<key>``, and the default is used. The
        descriptions, enums and ranges are shown by ``/ext settings``, which also edits the values.

        ``"merge"`` (API 2.4) in an entry of a setting whose default is a ``dict``: ``"replace"`` (the default) lets the highest
        settings layer that sets the key replace the lower layers' value, as for every setting; ``"deep"`` merges the
        objects of the global, project and local layers key by key (nested objects too; anything else, lists included,
        is replaced by the higher layer), so a project can add one entry to a user's table. The trust rules
        for a project's ``extensions.settings`` apply as before. To refuse a combination of settings that is valid one by one,
        check it after this call and :meth:`report_problem` (the extension keeps loading) or raise (it fails).

        Raises:
            ValueError: if called twice, or ``schema`` is malformed: it names a key that is not in ``defaults``, an
                entry is not an object of those fields, or a default does not satisfy its own entry
                (``minimal ext check`` reports it).
        """
        if self._declared:
            raise ValueError("declare_settings was already called")
        schema = {key: dict(entry) if isinstance(entry, Mapping) else entry for key, entry in (schema or {}).items()}
        check_schema(defaults, schema)
        self._declared = True
        deep = {key for key, entry in schema.items() if entry.get("merge") == "deep"}
        user = self._all_settings.extension_settings(self._id, deep) if deep else self._user_settings
        values = dict(defaults)
        for key, value in user.items():
            if key not in defaults:
                self._errors.append(f"{self._id}: unknown setting '{key}' (declared: {', '.join(defaults)})")
            elif key in schema:
                problem = check_setting(defaults[key], value, schema[key])
                if problem:
                    self._errors.append(
                        f"{self._id}: extensions.settings.{self._id}.{key} {problem}, got {value!r}; using the default {defaults[key]!r}"
                    )
                else:
                    values[key] = value
            elif not setting_matches(defaults[key], value):
                self._errors.append(f"{self._id}: setting '{key}' must be {type(defaults[key]).__name__}")
            else:
                values[key] = value
        self._settings = types.MappingProxyType(values)
        self._registry.declared_settings[self._id] = SettingsSpec(self._id, dict(defaults), schema, dict(values))
        return self._settings

    @property
    def settings(self) -> Mapping[str, Any]:
        """What :meth:`declare_settings` returned; empty before it is called."""
        return self._settings
