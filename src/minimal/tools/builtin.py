"""Built-in tools: ``read``, ``write``, ``edit``, ``bash``, ``grep``, ``glob`` and ``reload_extensions``.

Each tool is a function ``(input: dict, ctx) -> str`` followed by its
:class:`~minimal.extensions.registry.Tool` definition (description and JSON schema shown
to the model). Paths are resolved against ``ctx.cwd``. Tools raise on failure;
the agent loop sends the error back to the model as an ``is_error`` result.
"""

from __future__ import annotations

from minimal.extensions.registry import Registry, Tool
from minimal.tools.files import EDIT, READ, WRITE
from minimal.tools.reload import RELOAD
from minimal.tools.search import GLOB, GREP
from minimal.tools.shell import BASH

BUILTIN_TOOLS: tuple[Tool, ...] = (READ, WRITE, EDIT, BASH, GREP, GLOB, RELOAD)


def register_builtin_tools(registry: Registry) -> None:
    """Add the built-in tools to ``registry``."""
    for tool in BUILTIN_TOOLS:
        registry.add_tool(tool)
