"""What extensions may import from minimal: these names stay put when internal modules move.

``register(minimal)`` receives an :class:`Extension`; tools, commands, events and
widgets receive a :class:`Context` as ``ctx``. Import anything else an extension
needs from here::

    from minimal.api import USER_DENIED, TurnInterrupted, theme

This module is versioned by :data:`API_VERSION`, which every extension declares with
``__api__`` (see :mod:`minimal.extensions.metadata`). Internal code never imports this module.
"""

from minimal import API_VERSION, __version__
from minimal.core.agent import USER_DENIED
from minimal.core.context import UI, Context
from minimal.extensions.facade import (
    AfterResponseEvent,
    BeforeRequestEvent,
    Extension,
    SessionStartEvent,
    SessionSwitchEvent,
    ShutdownEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from minimal.extensions.registry import EVENTS, STATUS_SIDES, THEME_COLORS, WIDGET_AREAS, LogoSpec, Tool, TurnInterrupted
from minimal.providers.base import Provider, ProviderSpec, Reply, Usage, result_text
from minimal.tools.search import glob_regex
from minimal.ui.tui import theme
from minimal.ui.tui.widgets import logo_content

__all__ = [
    "API_VERSION", "EVENTS", "STATUS_SIDES", "THEME_COLORS", "UI", "USER_DENIED", "WIDGET_AREAS",
    "AfterResponseEvent", "BeforeRequestEvent", "Context", "Extension", "LogoSpec", "Provider", "ProviderSpec",
    "Reply", "SessionStartEvent", "SessionSwitchEvent", "ShutdownEvent", "Tool", "ToolCallEvent", "ToolResultEvent",
    "TurnEndEvent", "TurnInterrupted", "TurnStartEvent", "Usage", "__version__", "glob_regex", "logo_content",
    "result_text", "theme",
]  # fmt: skip

_SESSIONS = "ctx.sessions(), ctx.read_session() and ctx.new_session()"
REMOVED: dict[str, tuple[str, str]] = {
    "BUILTIN_COMMANDS": ("1.0", "ctx.commands() and ctx.run_command()"),
    "ID_RE": ("1.0", _SESSIONS),
    "SESSIONS_DIR": ("1.0", _SESSIONS),
    "new_id": ("1.0", _SESSIONS),
    "open_session": ("1.0", _SESSIONS),
    "Registry": ("1.0", "the Extension that register(minimal) receives"),
    "build_system_prompt": ("1.0", "ctx.system_prompt()"),
    "raise_if_cancelled": ("1.0", "ctx.check_cancelled()"),
    "reload_summary": ("1.0", "ctx.reload()"),
    "run_agent": ("1.0", "ctx.run_agent()"),
    "tool_specs": ("1.0", "ctx.tools"),
}
"""Names this module no longer has: ``name -> (API version that removed it, what to use instead)``."""


def __getattr__(name: str) -> object:
    """Explain an import of a removed name instead of a bare ``cannot import name``."""
    if name in REMOVED:
        version, instead = REMOVED[name]
        raise ImportError(
            f"minimal.api.{name} was removed in minimal API {version} (this minimal has API {API_VERSION}); "
            f"use {instead}",
            name=name,
        )
    raise AttributeError(f"module 'minimal.api' has no attribute {name!r}")
