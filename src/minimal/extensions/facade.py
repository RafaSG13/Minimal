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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypedDict

from minimal import config
from minimal.extensions.metadata import Metadata
from minimal.extensions.registry import CommandRun, EventHandler, Registry, Tool, ToolRun, check_event
from minimal.providers.base import Provider, Reply, Usage


class ToolCallEvent(TypedDict):
    """Payload of ``tool_call``: fired before a tool runs. Return ``{"block": True, "reason": str}`` to veto it."""

    name: str
    input: dict
    ctx: Any
    """The :class:`~minimal.core.context.Context`."""


class ToolResultEvent(TypedDict):
    """Payload of ``tool_result``: fired after a tool ran. Return ``{"output": str}`` to rewrite what the model sees."""

    name: str
    input: dict
    output: str
    is_error: bool
    ctx: Any


class TurnEndEvent(TypedDict):
    """Payload of ``turn_end``: fired when the model answered without calling tools."""

    messages: list[dict]
    """The history of the loop that ended (not ``ctx.messages`` when ``nested``)."""
    ctx: Any
    nested: bool
    """The loop ran inside a tool, such as a subagent."""


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
    this request only (the history does not change). Do not mutate the payload; return copies.
    """

    ctx: Any
    messages: list[dict]
    system: str
    tools: list[dict]
    nested: bool
    """The loop runs inside a tool, such as a subagent."""


class AfterResponseEvent(TypedDict):
    """Payload of ``after_response``: a reply was appended to the history."""

    ctx: Any
    reply: Reply
    usage: Usage | None
    nested: bool


class ShutdownEvent(TypedDict):
    """Payload of ``shutdown``: minimal is exiting."""

    ctx: Any


def _first_line(doc: str | None) -> str:
    return doc.strip().splitlines()[0] if doc and doc.strip() else ""


def _matches(default: object, value: object) -> bool:
    """Whether a user ``value`` has the type of ``default``: ``bool`` is strict, a ``float`` default takes an ``int``."""
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float))
    return isinstance(value, type(default))


class Extension:
    """The ``minimal`` argument of an extension's ``register(minimal)``.

    Attributes:
        id: ``name`` or ``category/name`` (``tools/subagent``): the key of its settings
            and of ``extensions.disabled``.
        path: The ``.py`` file or package folder.
        scope: ``"global"``, ``"project"`` or ``"cli"`` (``--ext``).
        version: Its ``__version__``; ``""`` if unset.
    """

    def __init__(
        self,
        *,
        id: str,
        path: Path,
        scope: str,
        metadata: Metadata,
        registry: Registry,
        settings: config.Settings,
        errors: list[str],
    ) -> None:
        self._id, self._path, self._scope, self._metadata = id, path, scope, metadata
        self._registry = registry
        self._user_settings = settings.extension_settings(id)
        self._errors = errors
        self._settings: Mapping[str, Any] = types.MappingProxyType({})
        self._declared = False

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
    def tools(self) -> Mapping[str, Tool]:
        """Every registered tool by name (read-only, live): call ``tools["bash"].run(input, ctx)`` to reuse one."""
        return types.MappingProxyType(self._registry.tools)

    # --- registering ---------------------------------------------------------

    def register_tool(
        self,
        name: str,
        run: ToolRun,
        description: str = "",
        input_schema: dict | None = None,
        confirm: bool | Callable[[dict, Any], bool] = False,
        subject_key: str | None = None,
    ) -> None:
        """Add a tool the model can call as ``run(input, ctx)``; see :meth:`Registry.register_tool`.

        ``subject_key`` names the ``input`` field that permission rules ``name(pattern)`` match.
        """
        self._registry.register_tool(name, run, description, input_schema, confirm, subject_key)

    def register_command(self, name: str, run: CommandRun, description: str = "", terminal: bool = False) -> None:
        """Add the slash command ``/name``, run as ``run(args, ctx)``; see :meth:`Registry.register_command`."""
        self._registry.register_command(name, run, description, terminal)

    def register_widget(self, name: str, create: Callable[[Any], Any], area: str = "sidebar") -> None:
        """Add a terminal UI widget built by ``create(ctx)``; see :meth:`Registry.register_widget`."""
        self._registry.register_widget(name, create, area)

    def register_status(self, name: str, render: Callable[[Any], Any], side: str = "right") -> None:
        """Add a status-line segment drawn by ``render(ctx)``; see :meth:`Registry.register_status`."""
        self._registry.register_status(name, render, side)

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

    def append_system_prompt(self, text: str) -> None:
        """Add a paragraph to the end of the system prompt."""
        self._registry.append_system_prompt(text)

    def on(self, event: str, handler: EventHandler | None = None):
        """Subscribe ``handler(event)`` to ``event``: one of :data:`~minimal.extensions.registry.EVENTS`
        (``tool_call``, ``tool_result``, ``turn_end``, ``session_start``, ``session_switch``,
        ``turn_start``, ``before_request``, ``after_response``, ``shutdown``).

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
    ) -> Callable[[ToolRun], ToolRun]:
        """Decorator registering ``fn(input, ctx)`` as a tool: the name defaults to the function's,
        the description to the first line of its docstring."""

        def decorate(fn: ToolRun) -> ToolRun:
            self.register_tool(
                name or fn.__name__,
                fn,
                description or _first_line(fn.__doc__),
                input_schema,
                confirm,
                subject_key,
            )
            return fn

        return decorate

    def command(
        self, name: str | None = None, *, description: str = "", terminal: bool = False
    ) -> Callable[[CommandRun], CommandRun]:
        """Decorator registering ``fn(args, ctx)`` as ``/name``: the name defaults to the function's,
        the description to the first line of its docstring."""

        def decorate(fn: CommandRun) -> CommandRun:
            self.register_command(name or fn.__name__, fn, description or _first_line(fn.__doc__), terminal)
            return fn

        return decorate

    # --- settings ------------------------------------------------------------

    def declare_settings(self, defaults: dict) -> Mapping[str, Any]:
        """Declare this extension's settings and their defaults; return the effective values (read-only).

        Users set them under ``extensions.settings.<id>`` in settings.json. A key not
        in ``defaults``, or a value whose type differs from its default's, is reported
        as a reload error and ignored.

        Raises:
            ValueError: if called twice.
        """
        if self._declared:
            raise ValueError("declare_settings was already called")
        self._declared = True
        values = dict(defaults)
        for key, value in self._user_settings.items():
            if key not in defaults:
                self._errors.append(f"{self._id}: unknown setting '{key}' (declared: {', '.join(defaults)})")
            elif not _matches(defaults[key], value):
                self._errors.append(f"{self._id}: setting '{key}' must be {type(defaults[key]).__name__}")
            else:
                values[key] = value
        self._settings = types.MappingProxyType(values)
        return self._settings

    @property
    def settings(self) -> Mapping[str, Any]:
        """What :meth:`declare_settings` returned; empty before it is called."""
        return self._settings
