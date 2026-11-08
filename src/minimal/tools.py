"""Built-in tools: ``read``, ``write``, ``edit``, ``bash`` and ``reload_extensions``.

Each tool is a function ``(input: dict, ctx) -> str`` followed by its
:class:`~minimal.registry.Tool` definition (description and JSON schema shown
to the model). Paths are resolved against ``ctx.cwd``. Tools raise on failure;
the agent loop sends the error back to the model as an ``is_error`` result.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from minimal.registry import Registry, Tool

MAX_CHARS = 20_000
"""Longest tool output sent to the model; anything beyond is truncated by :func:`clip`."""


def clip(text: str) -> str:
    """Truncate ``text`` to ``MAX_CHARS`` and say how much was cut."""
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f"\n... [truncated: {len(text) - MAX_CHARS} more characters]"


# --- helpers -----------------------------------------------------------------


def _resolve(ctx: Any, path: str) -> Path:
    """Resolve ``path`` against the working directory (absolute paths are kept)."""
    return Path(ctx.cwd, path).resolve()


def _read_text(path: Path) -> str:
    # newline="" keeps line endings exactly as they are on disk.
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _write_text(path: Path, content: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


_PATH = {"type": "string", "description": "File path"}


# --- read --------------------------------------------------------------------


def read(input: dict, ctx: Any) -> str:
    """Return lines ``offset .. offset+limit-1`` of a file, numbered ``N<TAB>line``."""
    lines = _read_text(_resolve(ctx, input["path"])).split("\n")
    offset = input.get("offset", 1)
    limit = input.get("limit", 2000)

    start = max(1, offset) - 1
    window = lines[start : start + limit]
    out = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(window))

    remaining = len(lines) - start - limit
    if remaining > 0:
        out += f"\n... [{remaining} more lines]"
    return clip(out)


READ = Tool(
    name="read",
    description="Read a text file and return its lines numbered. Use offset (1-based start line) and limit for large files.",
    input_schema={
        "type": "object",
        "properties": {
            "path": _PATH,
            "offset": {"type": "integer", "description": "Start line (1-based)"},
            "limit": {"type": "integer", "description": "Maximum number of lines (default 2000)"},
        },
        "required": ["path"],
    },
    run=read,
)


# --- write -------------------------------------------------------------------


def write(input: dict, ctx: Any) -> str:
    """Create or overwrite a file, creating parent directories as needed."""
    path = _resolve(ctx, input["path"])
    content = input["content"]
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, content)
    return f"Wrote {path} ({len(content)} characters)"


WRITE = Tool(
    name="write",
    description="Create or overwrite a file with the given content. Creates missing directories.",
    input_schema={
        "type": "object",
        "properties": {
            "path": _PATH,
            "content": {"type": "string", "description": "Full file content"},
        },
        "required": ["path", "content"],
    },
    run=write,
    confirm=True,
)


# --- edit --------------------------------------------------------------------


def edit(input: dict, ctx: Any) -> str:
    """Replace the single occurrence of ``old_string`` with ``new_string``.

    Raises:
        ValueError: if ``old_string`` is missing or appears more than once.
    """
    path = _resolve(ctx, input["path"])
    old, new = input["old_string"], input["new_string"]
    text = _read_text(path)

    count = text.count(old)
    if count == 0:
        raise ValueError("old_string not found in the file")
    if count > 1:
        raise ValueError(f"old_string appears {count} times; add more context so it is unique")

    _write_text(path, text.replace(old, new, 1))
    return f"Edited {path}"


EDIT = Tool(
    name="edit",
    description="Replace an exact piece of text in a file. old_string must appear exactly once; otherwise the call fails.",
    input_schema={
        "type": "object",
        "properties": {
            "path": _PATH,
            "old_string": {"type": "string", "description": "Exact text to replace"},
            "new_string": {"type": "string", "description": "New text"},
        },
        "required": ["path", "old_string", "new_string"],
    },
    run=edit,
    confirm=True,
)


# --- bash --------------------------------------------------------------------


def bash(input: dict, ctx: Any) -> str:
    """Run a command with ``/bin/bash -c`` and report stdout, stderr and the exit code.

    On timeout the whole process group is killed, so child processes die too,
    and the result ends with ``[exit 1 (timeout)]``.
    """
    timeout_s = input.get("timeout_ms", 120_000) / 1000
    proc = subprocess.Popen(
        ["/bin/bash", "-c", input["command"]],
        cwd=ctx.cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,  # own process group, so a timeout can kill all of it
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        status = f"exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        status = "exit 1 (timeout)"

    out = stdout
    if stderr:
        out += f"\n[stderr]\n{stderr}"
    out += f"\n[{status}]"
    return clip(out.strip())


BASH = Tool(
    name="bash",
    description="Run a shell command in the working directory. Returns stdout, stderr and the exit code.",
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to run"},
            "timeout_ms": {"type": "integer", "description": "Maximum time in milliseconds (default 120000)"},
        },
        "required": ["command"],
    },
    run=bash,
    confirm=True,
)


# --- reload_extensions -------------------------------------------------------


def reload_extensions(input: dict, ctx: Any) -> str:
    """Reload every extension; raise with the summary if any failed, so the model sees the errors."""
    from minimal.app import reload_summary  # app imports this module

    loaded, errors = ctx.reload_extensions()
    summary = reload_summary(loaded, errors)
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


# --- registration ------------------------------------------------------------

BUILTIN_TOOLS: tuple[Tool, ...] = (READ, WRITE, EDIT, BASH, RELOAD)


def register_builtin_tools(registry: Registry) -> None:
    """Add the built-in tools to ``registry``."""
    for tool in BUILTIN_TOOLS:
        registry.add_tool(tool)
