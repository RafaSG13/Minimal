"""The registry: the one place where tools, slash commands, model providers,
system-prompt additions, event handlers, UI widgets, status-line segments, UI
colour themes and the header logo are collected.

Built-in tools, built-in providers and extensions register through it (extensions
through their :class:`~minimal.extensions.facade.Extension`). The agent loop and
the terminal UI read from it. ``ctx`` below is the
:class:`~minimal.core.context.Context`.

Events (:data:`EVENTS`) and their payloads:

    ``tool_call``    ``{name, input, ctx}``
        Fired before a tool runs. A handler may return
        ``{"block": True, "reason": str}`` to veto the call. A handler
        that raises also blocks the call: a guard that crashed did not approve it.
    ``tool_result``  ``{name, input, output, is_error, ctx}``
        Fired after a tool runs, with its whole output. A handler may return
        ``{"output": str}`` to rewrite what the model sees (clipped afterwards).
    ``turn_end``     ``{messages, ctx, nested}``
        Fired when the model finishes answering without further tool calls.
        ``messages`` is the history of the loop that ended; ``nested`` is true
        when that loop ran inside a tool (a subagent), whose ``messages`` are
        not ``ctx.messages``. A handler that is slow (compaction) should call
        ``ctx.check_cancelled()``, which raises :class:`TurnInterrupted` when the user interrupted.
    ``session_start``  ``{ctx, resumed}``
        Fired once per process when the UI is up; ``resumed`` is true if the
        session already had messages. A reload does not fire it again.
    ``session_switch`` ``{ctx, old_id, new_id}``
        Fired when ``/new``, ``/resume`` or ``ctx.new_session`` replace the
        session, after the old one is saved.
    ``turn_start``   ``{ctx, text}``
        Fired when the user sends a message, before it enters the history. The
        last ``{"text": str}`` with a non-empty string rewrites the message.
    ``before_request`` ``{ctx, messages, system, tools, options, nested}``
        Fired before each model request. ``{"messages": list}``, ``{"system": str}``
        and ``{"tools": list}`` (combined by key, the last wins) are sent instead,
        for that request only: the history does not change; ``{"options": dict}``
        is merged into the request options (``max_tokens``, ``temperature``).
        Handlers must not mutate what they receive; they return copies.
    ``after_response`` ``{ctx, reply, usage, nested}``
        Fired after each reply is appended to the history. Results are ignored.
    ``shutdown``     ``{ctx}``
        Fired when minimal exits.
    ``provider_error`` ``{ctx, error, attempt, provider, model, nested}``
        Fired when a model request of a loop fails (``attempt`` counts the failures
        of that request, from 1). The first result with ``{"retry_after": seconds}``
        or ``{"provider": name, "model": name}`` decides: switch provider if asked,
        wait, and send the request again. No such result: the error propagates.
        Text already streamed before the failure stays shown.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from minimal.providers.base import Provider, ProviderSpec

ToolRun = Callable[[dict, Any], Any]
"""Tool implementation: ``run(input, ctx) -> str``. Raise to report an error."""

CommandRun = Callable[[str, Any], Any]
"""Slash-command implementation: ``run(args, ctx)``."""

EventHandler = Callable[[dict], Any]
"""Event handler: ``handler(payload) -> dict | None``."""


class TurnInterrupted(Exception):
    """The user interrupted the turn in progress (``ctx.cancelled`` was set).

    Raised by :func:`~minimal.core.agent.run_agent`, and by an event handler to abort the turn;
    :meth:`Registry.emit` lets it through instead of reporting it like other handler errors.
    """


EVENTS = (
    "tool_call",
    "tool_result",
    "turn_end",
    "session_start",
    "session_switch",
    "turn_start",
    "before_request",
    "after_response",
    "shutdown",
    "provider_error",
)
"""Events an extension can subscribe to with ``on``."""

WIDGET_AREAS = ("top", "bottom", "sidebar")
"""Where an extension widget can be placed in the terminal UI."""


def check_event(event: str) -> None:
    """Raise ``ValueError`` unless ``event`` is one of :data:`EVENTS`."""
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}; use one of {', '.join(EVENTS)}")


STATUS_SIDES = ("left", "right")
"""Where an extension status-line segment can be placed."""

THEME_COLORS = (
    "surface",
    "panel",
    "secondary",
    "success",
    "warning",
    "error",
    "muted",
    "dim",
    "border",
    "focus",
    "user_bg",
    "pending_bg",
    "success_bg",
    "error_bg",
    "code",
    "model",
    "path",
    "status_bg",
    "git_clean",
    "git_dirty",
    "selection",
    "diff_added",
    "diff_removed",
)
"""Optional colours of :meth:`Registry.register_theme`; the terminal UI derives any left out."""

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")

LOGO_TRANSPARENT = " ."
"""Pixel characters of :meth:`Registry.register_logo` art that are not drawn."""

LOGO_ALIGN = ("left", "center", "right")
"""Values of :meth:`Registry.register_logo`'s ``align``."""

_THEME_VARIABLE = re.compile(r"\$[a-z][a-z0-9-]*")


@dataclass
class Tool:
    """A tool the model can call.

    Attributes:
        name: Unique name the model uses to call the tool.
        description: Tells the model what the tool does and when to use it.
        input_schema: JSON schema of the ``input`` object.
        run: Implementation, called as ``run(input, ctx)``.
        confirm: Ask the user before running (tools with side effects): ``True``,
            or ``confirm(input, ctx) -> bool`` deciding per call.
        subject_key: The ``input`` field that permission rules ``name(pattern)``
            match (``command`` for ``bash``, ``path`` for the file tools); ``None``
            lets only bare ``name`` rules match the tool.
    """

    name: str
    description: str
    input_schema: dict
    run: ToolRun
    confirm: bool | Callable[[dict, Any], bool] = False
    subject_key: str | None = None


@dataclass
class Command:
    """A slash command such as ``/stats``.

    Attributes:
        run: Implementation, called as ``run(args, ctx)`` where ``args`` is
            the text after the command name.
        description: One-line summary shown by ``/help``.
        terminal: Needs the real terminal (the TUI suspends itself while it runs).
        builtin: One of minimal's own commands (:meth:`Registry.register_builtin_command`).
    """

    run: CommandRun
    description: str = ""
    terminal: bool = False
    builtin: bool = False


@dataclass
class WidgetSpec:
    """A visual component an extension adds to the terminal UI.

    Attributes:
        name: Unique name; registering the same name again replaces it.
        create: Called as ``create(ctx)`` with the :class:`~minimal.core.context.Context`;
            returns a ``textual.widget.Widget``.
        area: ``"top"``, ``"bottom"`` or ``"sidebar"``.
    """

    name: str
    create: Callable[[Any], Any]
    area: str = "sidebar"


@dataclass
class StatusSpec:
    """A segment an extension adds to the terminal UI's status line (see :meth:`Registry.register_status`).

    Attributes:
        name: Unique name; registering the same name again replaces it.
        render: Called as ``render(ctx)`` with the :class:`~minimal.core.context.Context`; returns
            a ``str``, a ``textual.content.Content`` or ``None`` (no segment this time).
        side: ``"left"`` (after the git branch) or ``"right"`` (before the model).
    """

    name: str
    render: Callable[[Any], Any]
    side: str = "right"


@dataclass
class ThemeSpec:
    """A colour theme an extension adds to the terminal UI (see :meth:`Registry.register_theme`).

    Attributes:
        name: Unique name; registering the same name again replaces it.
        dark: Whether it is a dark theme.
        colors: ``#rrggbb`` colours by name: ``background``, ``foreground``,
            ``accent`` and any of :data:`THEME_COLORS`.
    """

    name: str
    dark: bool
    colors: dict[str, str]


@dataclass
class LogoSpec:
    """Pixel art shown in the terminal UI's welcome header instead of the built-in wordmark (see :meth:`Registry.register_logo`).

    Attributes:
        art: Pixel rows of equal length, one character per pixel: a key of ``colors``,
            or ``" "``/``"."`` for a transparent pixel.
        colors: Colour of each pixel character: ``#rrggbb`` or a theme variable such as ``$accent``.
        align: Where the header is aligned: ``"left"``, ``"center"`` or ``"right"``.
    """

    art: list[str]
    colors: dict[str, str]
    align: str = "center"


@dataclass
class FrontendSpec:
    """A user interface minimal can run in, chosen with ``--ui`` or the ``ui`` setting (see :meth:`Registry.register_frontend`).

    Attributes:
        name: Unique name; registering the same name again replaces it.
        run: Called as ``run(host)`` with the :class:`~minimal.core.host.Host`; returns when the user quits.
        description: One line for ``minimal --help`` and the error that lists the frontends.
    """

    name: str
    run: Callable[[Any], None]
    description: str = ""


class Registry:
    """Collects tools, commands, providers, prompt additions, event handlers, widgets, status segments, themes, the logo and frontends.

    Attributes:
        tools: Tools offered to the model, by name, in registration order.
        commands: Slash commands added by extensions, by name.
        builtin_commands: minimal's own slash commands, by name; an extension command of the
            same name replaces one.
        providers: Model integrations, by name (see :mod:`minimal.providers`).
        prompt_parts: Extra paragraphs appended to the system prompt.
        widgets: Visual components for the terminal UI, by name.
        status: Status-line segments for the terminal UI, by name.
        themes: Colour themes for the terminal UI, by name.
        logo: Pixel art for the welcome header, or ``None`` for the built-in wordmark.
        frontends: User interfaces, by name (``tui`` and ``console`` are built in).
    """

    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}
        self.commands: dict[str, Command] = {}
        self.builtin_commands: dict[str, Command] = {}
        self.providers: dict[str, ProviderSpec] = {}
        self.prompt_parts: list[str] = []
        self.widgets: dict[str, WidgetSpec] = {}
        self.status: dict[str, StatusSpec] = {}
        self.themes: dict[str, ThemeSpec] = {}
        self.logo: LogoSpec | None = None
        self.frontends: dict[str, FrontendSpec] = {}
        self._handlers: dict[str, list[EventHandler]] = {}
        self._disposers: list[Callable[[], Any]] = []

    def reset(self) -> None:
        """Forget everything registered, keeping the same container objects.

        The containers are cleared in place because the agent loop holds this
        registry and re-reads ``tools`` on every model request: a reload during
        a turn offers the new tools on the next request.
        """
        self.tools.clear()
        self.commands.clear()
        self.builtin_commands.clear()
        self.providers.clear()
        self.prompt_parts.clear()
        self.widgets.clear()
        self.status.clear()
        self.themes.clear()
        self.logo = None
        self.frontends.clear()
        self._handlers.clear()
        self._disposers.clear()

    def add_tool(self, tool: Tool) -> None:
        """Register a ready-made ``Tool``, replacing any tool with the same name."""
        self.tools[tool.name] = tool

    def register_tool(
        self,
        name: str,
        run: ToolRun,
        description: str = "",
        input_schema: dict | None = None,
        confirm: bool | Callable[[dict, Any], bool] = False,
        subject_key: str | None = None,
    ) -> None:
        """Build and register a tool. ``input_schema`` defaults to an empty object.

        Raises:
            ValueError: if ``name`` is empty or ``run`` is not callable.
        """
        if not name or not callable(run):
            raise ValueError("register_tool: name and run are required")
        schema = input_schema or {"type": "object", "properties": {}}
        self.add_tool(Tool(name, description, schema, run, confirm, subject_key))

    def register_command(
        self, name: str, run: CommandRun, description: str = "", terminal: bool = False
    ) -> None:
        """Register a slash command, available as ``/name``.

        Set ``terminal`` when the command needs the real terminal (prompts,
        menus, a browser login): the TUI suspends itself while it runs.

        Raises:
            ValueError: if ``run`` is not callable.
        """
        if not callable(run):
            raise ValueError("register_command: run is required")
        self.commands[name] = Command(run, description, terminal)

    def register_builtin_command(
        self, name: str, run: CommandRun, description: str = "", terminal: bool = False
    ) -> None:
        """Register one of minimal's own slash commands (see :meth:`register_command`).

        Raises:
            ValueError: if ``run`` is not callable.
        """
        if not callable(run):
            raise ValueError("register_builtin_command: run is required")
        self.builtin_commands[name] = Command(run, description, terminal, builtin=True)

    def register_frontend(self, name: str, run: Callable[[Any], None], description: str = "") -> None:
        """Add a user interface, run as ``run(host)`` when ``--ui name`` (or the ``ui`` setting) picks it.

        ``run`` installs its :class:`~minimal.core.app.Frontend` with ``host.attach(frontend)`` and
        drives minimal through the :class:`~minimal.core.host.Host` until the user quits.

        Raises:
            ValueError: if ``name`` is empty or ``run`` is not callable.
        """
        if not name or not callable(run):
            raise ValueError("register_frontend: name and run are required")
        self.frontends[name] = FrontendSpec(name, run, description)

    def register_widget(
        self, name: str, create: Callable[[Any], Any], area: str = "sidebar"
    ) -> None:
        """Add a visual component to the terminal UI, replacing any widget with the same name.

        ``create(ctx)`` receives the :class:`~minimal.core.context.Context` and returns a
        ``textual.widget.Widget``, placed in ``area`` (``"top"``, ``"bottom"`` or
        ``"sidebar"``). Style it with its own ``DEFAULT_CSS``. The ``top`` and
        ``bottom`` areas size to their content, so give the widget a height and
        do not ``dock`` it (a docked widget leaves the area zero rows tall). If
        the widget has a method ``refresh_state(ctx)``, the TUI calls it after
        mount, after every turn, after every slash command and after every reload.
        If it has a method ``refresh_live(ctx, event)``, the TUI calls it on the UI
        thread when a turn or command starts (``{"kind": "start", "turn": bool}``),
        after each tool call is shown (``{"kind": "tool_call", "call": dict}``) and
        after each tool result is shown (``{"kind": "tool_result", "call": dict,
        "result": dict}``, where ``result`` is the ``tool_result`` block).

        Raises:
            ValueError: if ``name`` is empty, ``create`` is not callable or ``area`` is unknown.
        """
        if not name or not callable(create):
            raise ValueError("register_widget: name and create are required")
        if area not in WIDGET_AREAS:
            raise ValueError(
                "register_widget: area must be one of top, bottom, sidebar"
            )
        self.widgets[name] = WidgetSpec(name, create, area)

    def register_status(
        self, name: str, render: Callable[[Any], Any], side: str = "right"
    ) -> None:
        """Add a segment to the terminal UI's status line, replacing any segment with the same name.

        ``render(ctx)`` receives the :class:`~minimal.core.context.Context` and returns a ``str``,
        a ``textual.content.Content`` or ``None`` to show nothing this time. It is
        called whenever the status line is redrawn, so keep it cheap. ``side`` is
        ``"left"`` (after the git branch) or ``"right"`` (before the model).

        Raises:
            ValueError: if ``name`` is empty, ``render`` is not callable or ``side`` is unknown.
        """
        if not name or not callable(render):
            raise ValueError("register_status: name and render are required")
        if side not in STATUS_SIDES:
            raise ValueError("register_status: side must be left or right")
        self.status[name] = StatusSpec(name, render, side)

    def register_theme(
        self, name: str, *, dark: bool, background: str, foreground: str, accent: str, **colors: str
    ) -> None:
        """Add a colour theme to the terminal UI (pick it with ``/theme``), replacing any with the same name.

        Colours are ``#rrggbb`` strings. ``background``, ``foreground`` and
        ``accent`` are required; the optional ones (:data:`THEME_COLORS`) are
        derived from those when left out:

        - ``surface``, ``panel``: raised backgrounds (code blocks, dialogs; highlighted rows)
        - ``secondary``: file paths in tool calls
        - ``success``, ``warning``, ``error``: states, icons and the permission dialog
        - ``muted``, ``dim``: secondary and faint text
        - ``border``, ``focus``: separators, and the prompt's lines while it has focus
        - ``user_bg``, ``pending_bg``, ``success_bg``, ``error_bg``: user messages and tool calls by state
        - ``code``: inline code in replies
        - ``model``, ``path``, ``status_bg``, ``git_clean``, ``git_dirty``: the status line
        - ``selection``: selected text in inputs
        - ``diff_added``, ``diff_removed``: lines of an edit's diff

        The built-in ``minimal`` theme cannot be replaced.

        Raises:
            ValueError: if ``name`` is empty, a colour name is unknown or a value is not ``#rrggbb``.
        """
        if not name:
            raise ValueError("register_theme: name is required")
        unknown = sorted(set(colors) - set(THEME_COLORS))
        if unknown:
            raise ValueError(f"register_theme: unknown colour(s) {', '.join(unknown)}; use {', '.join(THEME_COLORS)}")
        palette = {"background": background, "foreground": foreground, "accent": accent, **colors}
        bad = [key for key, value in palette.items() if not (isinstance(value, str) and _HEX_COLOR.fullmatch(value))]
        if bad:
            raise ValueError(f"register_theme: not a #rrggbb colour: {', '.join(bad)}")
        self.themes[name] = ThemeSpec(name, bool(dark), palette)

    def register_logo(self, art: list[str], colors: dict[str, str], align: str = "center") -> None:
        """Replace the default wordmark in the terminal UI's welcome header with pixel art.

        ``art`` is a list of equal-length strings, one character per pixel;
        ``" "`` and ``"."`` are transparent. Two pixel rows make one terminal
        row (drawn with half blocks), so pixels are roughly square. ``colors``
        maps every other character to ``#rrggbb`` or a theme variable
        (``$accent``, ``$foreground``, ``$muted``, … see :mod:`minimal.ui.tui.theme`),
        so the art can follow ``/theme``. Lines wider than the terminal are clipped.
        ``align`` places the logo and the header text under it: ``"left"``,
        ``"center"`` (default) or ``"right"``.

        A later call replaces an earlier one: with several logo extensions the
        last one loaded wins (the project folder after the global one,
        alphabetical within a folder). ``python -m minimal.extensions.logo IMAGE`` turns an
        image into a logo extension.

        Raises:
            ValueError: if ``align`` is not ``"left"``, ``"center"`` or ``"right"``, the art
                is empty or ragged, a colour key is not a single drawable character, a colour
                is not ``#rrggbb`` or ``$variable``, or a pixel has no colour.
        """
        if align not in LOGO_ALIGN:
            raise ValueError(f"register_logo: align must be 'left', 'center' or 'right', not {align!r}")
        if not art or not all(isinstance(row, str) and row for row in art):
            raise ValueError("register_logo: art must be a non-empty list of non-empty strings")
        if len({len(row) for row in art}) != 1:
            raise ValueError("register_logo: art rows must all have the same length")
        bad = [key for key in colors if not (isinstance(key, str) and len(key) == 1) or key in LOGO_TRANSPARENT]
        if bad:
            raise ValueError(
                f"register_logo: colour keys must be single characters other than space and '.': {', '.join(map(repr, bad))}"
            )
        bad = [
            key
            for key, value in colors.items()
            if not (isinstance(value, str) and (_HEX_COLOR.fullmatch(value) or _THEME_VARIABLE.fullmatch(value)))
        ]
        if bad:
            raise ValueError(f"register_logo: not a #rrggbb colour or $variable: {', '.join(bad)}")
        missing = sorted(set("".join(art)) - set(colors) - set(LOGO_TRANSPARENT))
        if missing:
            raise ValueError(f"register_logo: no colour for pixel(s) {', '.join(map(repr, missing))}")
        self.logo = LogoSpec(list(art), dict(colors), align)

    def add_provider(self, spec: ProviderSpec) -> None:
        """Register a ready-made ``ProviderSpec``, replacing any with the same name."""
        self.providers[spec.name] = spec

    def register_provider(
        self,
        name: str,
        create: Callable[[], Provider],
        default_model: str,
        env_keys: tuple[str, ...] = (),
        description: str = "",
    ) -> None:
        """Register a model integration, selectable with ``--provider name`` or ``/provider name``.

        ``create()`` must return an object implementing
        :class:`~minimal.providers.base.Provider`.

        Raises:
            ValueError: if ``name`` or ``default_model`` is empty or ``create`` is not callable.
        """
        if not name or not default_model or not callable(create):
            raise ValueError(
                "register_provider: name, create and default_model are required"
            )
        self.add_provider(
            ProviderSpec(name, create, default_model, tuple(env_keys), description)
        )

    def on(self, event: str, handler: EventHandler) -> None:
        """Subscribe ``handler`` to ``event`` (see the module docstring for events).

        Raises:
            ValueError: if ``event`` is not one of :data:`EVENTS`.
        """
        check_event(event)
        self._handlers.setdefault(event, []).append(handler)

    def append_system_prompt(self, text: str) -> None:
        """Add a paragraph to the end of the system prompt."""
        self.prompt_parts.append(text)

    def emit(self, event: str, payload: dict, *, strict: bool = False) -> list:
        """Call every handler of ``event`` in order and return their non-``None`` results.

        A failing handler is reported on stderr and skipped, so one broken
        extension cannot break the agent loop, except :class:`TurnInterrupted`,
        which propagates to the caller.

        With ``strict``, a failing handler's exception propagates instead. The
        agent loop uses it for ``tool_call``: a guard that crashed has not
        approved the call, so the call must not run.
        """
        results = []
        for handler in self._handlers.get(event, []):
            try:
                result = handler(payload)
            except TurnInterrupted:
                raise
            except Exception as e:
                if strict:
                    raise
                print(f'[extension] error in "{event}" handler: {e}', file=sys.stderr)
                continue
            if result is not None:
                results.append(result)
        return results

    def on_dispose(self, fn: Callable[[], Any]) -> None:
        """Call ``fn()`` at the next :meth:`dispose`: before a reload and when minimal exits.

        Raises:
            ValueError: if ``fn`` is not callable.
        """
        if not callable(fn):
            raise ValueError("on_dispose: fn must be callable")
        self._disposers.append(fn)

    def dispose(self) -> None:
        """Call every :meth:`on_dispose` function, last registered first, then forget them.

        A failing one is reported on stderr and the rest still run.
        """
        disposers, self._disposers = self._disposers, []
        for fn in reversed(disposers):
            try:
                fn()
            except Exception as e:
                print(f"[extension] error in dispose: {e}", file=sys.stderr)
