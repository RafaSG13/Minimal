"""What extensions may import from minimal: these names stay put when internal modules move.

``register(minimal)`` receives an :class:`Extension`; tools, commands, events and
widgets receive a :class:`Context` as ``ctx``. Import anything else an extension
needs from here::

    from minimal.api import USER_DENIED, TurnInterrupted, theme

``theme`` holds the glyphs (:mod:`minimal.glyphs`). Importing this module does not import any UI toolkit.

This module is versioned by :data:`API_VERSION`, which every extension declares with
``__api__`` (see :mod:`minimal.extensions.metadata`). Internal code never imports this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minimal import API_VERSION, __version__
from minimal.config import REASONING_EFFORTS
from minimal.core.agent import USER_DENIED
from minimal.core.agents import AGENT_STATUSES, MAIN_AGENT, AgentInfo, AgentResult, UsageTotals
from minimal.core.context import UI, Context
from minimal.core.git import GitInfo
from minimal.extensions.facade import (
    AfterResponseEvent,
    AgentEndEvent,
    AgentStartEvent,
    BeforeRequestEvent,
    Extension,
    ModeChangeEvent,
    ProviderErrorEvent,
    SessionStartEvent,
    SessionSwitchEvent,
    ShutdownEvent,
    ThemeChangeEvent,
    ToolCallEvent,
    ToolResultEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from minimal.extensions.registry import (
    CSS_VARIABLES,
    EVENTS,
    KEY_ACTIONS,
    PROMPT_SECTIONS,
    RESERVED_KEYS,
    SETTING_SCHEMA_KEYS,
    STATUS_SIDES,
    STYLES,
    THEME_COLORS,
    WIDGET_AREAS,
    CssSpec,
    KeyInfo,
    KeybindingSpec,
    LogoSpec,
    ModeSpec,
    PanelSpec,
    PromptText,
    RendererSpec,
    SettingsSpec,
    ThemeInfo,
    Tool,
    TurnInterrupted,
    styled_line,
    styled_lines,
)
from minimal.providers.anthropic import AnthropicProvider
from minimal.providers.base import IMAGE_TYPES, Provider, ProviderSpec, Reply, Usage, image_block, result_text
from minimal.providers.openai import openai_compatible
from minimal.tools.search import glob_regex

from minimal import glyphs
from minimal import glyphs as theme

if TYPE_CHECKING:
    from textual.content import Content

__all__ = [
    "AGENT_STATUSES", "API_VERSION", "CSS_VARIABLES", "EVENTS", "IMAGE_TYPES", "KEY_ACTIONS", "MAIN_AGENT",
    "PROMPT_SECTIONS", "REASONING_EFFORTS", "RESERVED_KEYS", "SETTING_SCHEMA_KEYS", "STATUS_SIDES", "STYLES",
    "THEME_COLORS", "UI", "USER_DENIED", "WIDGET_AREAS",
    "AfterResponseEvent", "AgentEndEvent", "AgentInfo", "AgentResult", "AgentStartEvent", "AnthropicProvider",
    "BeforeRequestEvent", "Context", "CssSpec", "Extension", "GitInfo", "KeyInfo", "KeybindingSpec", "LogoSpec",
    "ModeChangeEvent", "ModeSpec", "PanelSpec", "PromptText", "Provider",
    "ProviderErrorEvent", "ProviderSpec", "RendererSpec", "Reply", "SessionStartEvent", "SessionSwitchEvent",
    "SettingsSpec", "ShutdownEvent", "ThemeChangeEvent", "ThemeInfo", "Tool",
    "ToolCallEvent", "ToolResultEvent", "TurnEndEvent", "TurnInterrupted", "TurnStartEvent", "Usage", "UsageTotals",
    "__version__",
    "glob_regex", "glyphs", "image_block", "logo_content", "openai_compatible", "result_text", "styled_line", "styled_lines", "theme",
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
    **{
        f"theme.{name}": ("2.0", f"minimal.ui.tui.theme.{name} (internal to the terminal UI, not part of the extension API)")
        for name in ("DEFAULT", "MINIMAL", "build", "available")
    },
}
"""Names this module no longer has: ``name -> (API version that removed it, what to use instead)``.

``theme.<name>`` is an attribute of :data:`theme` (:mod:`minimal.glyphs`)."""


def logo_content(spec: LogoSpec) -> Content:
    """The pixel-art logo ``spec`` as Textual content (two art rows per row of half blocks)."""
    from minimal.ui.tui.widgets import logo_content as render

    return render(spec)


def __getattr__(name: str) -> object:
    """Explain an import of a removed name."""
    if name in REMOVED:
        version, instead = REMOVED[name]
        raise ImportError(
            f"minimal.api.{name} was removed in minimal API {version} (this minimal has API {API_VERSION}); "
            f"use {instead}",
            name=name,
        )
    raise AttributeError(f"module 'minimal.api' has no attribute {name!r}")
