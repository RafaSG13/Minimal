"""Shared by the built-in tools: output clipping and which paths are read without asking."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import minimal
from minimal.extensions import loader

MAX_CHARS = 20_000
"""Longest tool output sent to the model; anything beyond is truncated by :func:`clip`."""


def clip(text: str) -> str:
    """Truncate ``text`` to ``MAX_CHARS`` and say how much was cut."""
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f"\n... [truncated: {len(text) - MAX_CHARS} more characters]"


def resolve_path(ctx: Any, path: str) -> Path:
    """Resolve ``path`` against the working directory (absolute paths are kept)."""
    return Path(ctx.cwd, path).resolve()


def readable_without_asking(path: Path, ctx: Any) -> bool:
    """Whether resolved ``path`` is in the working directory, minimal's own source or the global extensions folder."""
    roots = (Path(ctx.cwd).resolve(), Path(minimal.__file__).resolve().parent, loader.GLOBAL_EXTENSIONS_DIR.resolve())
    return any(path.is_relative_to(root) for root in roots)


def outside_workspace(input: dict, ctx: Any) -> bool:
    """Whether ``input["path"]`` (default ``.``) resolves outside the working directory, minimal's
    own source (the system prompt points the model at extensions/registry.py) and the global extensions folder.

    The ``confirm`` of the reading tools: those places are read freely, anything
    else (``~/.ssh``, ``~/.minimal/keys.json``, a symlink leading there) asks first.
    """
    return not readable_without_asking(resolve_path(ctx, input.get("path", ".")), ctx)
