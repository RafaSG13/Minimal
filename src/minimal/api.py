"""What extensions may import from minimal: these names stay put when internal modules move.

``register(minimal)`` receives the Registry; import anything else an extension needs from here::

    from minimal.api import open_session, run_agent, theme

Internal code never imports this module.
"""

from minimal.core.agent import raise_if_cancelled, run_agent, tool_specs
from minimal.core.app import build_system_prompt
from minimal.core.session import ID_RE, SESSIONS_DIR, new_id, open_session
from minimal.extensions.loader import reload_summary
from minimal.extensions.registry import LogoSpec, Registry, TurnInterrupted
from minimal.providers.base import result_text
from minimal.tools.search import glob_regex
from minimal.ui.commands import BUILTIN_COMMANDS
from minimal.ui.tui import theme
from minimal.ui.tui.widgets import logo_content

__all__ = [
    "BUILTIN_COMMANDS", "ID_RE", "SESSIONS_DIR", "LogoSpec", "Registry", "TurnInterrupted",
    "build_system_prompt", "glob_regex", "logo_content", "new_id", "open_session", "raise_if_cancelled",
    "reload_summary", "result_text", "run_agent", "theme", "tool_specs",
]
