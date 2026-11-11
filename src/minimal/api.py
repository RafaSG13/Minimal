"""What extensions may import from minimal: these names stay put when internal modules move.

``register(minimal)`` receives an :class:`Extension`; tools, commands, events and
widgets receive a :class:`Context` as ``ctx``. Import anything else an extension
needs from here::

    from minimal.api import USER_DENIED, TurnInterrupted, theme

Internal code never imports this module.
"""

from minimal import __version__
from minimal.core.agent import USER_DENIED
from minimal.core.context import UI, Context
from minimal.extensions.facade import Extension, ToolCallEvent, ToolResultEvent, TurnEndEvent
from minimal.extensions.registry import EVENTS, STATUS_SIDES, THEME_COLORS, WIDGET_AREAS, LogoSpec, Tool, TurnInterrupted
from minimal.providers.base import Provider, ProviderSpec, Reply, Usage, result_text
from minimal.tools.search import glob_regex
from minimal.ui.tui import theme
from minimal.ui.tui.widgets import logo_content

__all__ = [
    "EVENTS", "STATUS_SIDES", "THEME_COLORS", "UI", "USER_DENIED", "WIDGET_AREAS", "Context", "Extension", "LogoSpec",
    "Provider", "ProviderSpec", "Reply", "Tool", "ToolCallEvent", "ToolResultEvent", "TurnEndEvent", "TurnInterrupted",
    "Usage", "__version__", "glob_regex", "logo_content", "result_text", "theme",
]  # fmt: skip
