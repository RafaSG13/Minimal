"""The reload_extensions tool."""

from __future__ import annotations

from typing import Any

from minimal.extensions.registry import Tool


def reload_extensions(input: dict, ctx: Any) -> str:
    """Reload every extension; raise with the summary if any failed, so the model sees the errors as an error."""
    summary = ctx.reload()
    if "\nFailed: " in summary:  # reload_summary's line per error
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
