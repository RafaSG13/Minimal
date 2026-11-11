"""The reload_extensions tool."""

from __future__ import annotations

from typing import Any

from minimal.extensions.loader import reload_summary
from minimal.extensions.registry import Tool


def reload_extensions(input: dict, ctx: Any) -> str:
    """Reload every extension; raise with the summary if any failed, so the model sees the errors."""
    loaded, errors = ctx.reload_extensions()
    summary = reload_summary(loaded, errors, ctx.untrusted_notice())
    if errors:
        raise RuntimeError(summary)
    return summary


RELOAD = Tool(
    name="reload_extensions",
    description=(
        "Reload every minimal extension from disk so new or edited tools, commands, hooks and UI widgets "
        "take effect now. Call it after writing or editing an extension."
    ),
    input_schema={"type": "object", "properties": {}},
    run=reload_extensions,
    confirm=True,
)
