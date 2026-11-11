"""The read, write and edit tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from minimal.extensions.registry import Tool
from minimal.tools.common import MAX_CHARS, outside_workspace, resolve_path


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
    """Return lines ``offset .. offset+limit-1`` of a file, numbered ``N<TAB>line``.

    The file is read line by line: only the window is decoded and at most about
    :data:`MAX_CHARS` of it kept, the other lines are only counted. Lines are
    split on ``\\n``; a ``\\r`` before it is kept.

    Raises:
        ValueError: if the path is a directory, device, FIFO or socket.
        UnicodeDecodeError: if a line in the window is not UTF-8.
    """
    path = resolve_path(ctx, input["path"])
    if path.exists() and not path.is_file():
        raise ValueError(f"not a regular file: {path}")
    start = max(1, input.get("offset", 1)) - 1
    stop = start + max(0, input.get("limit", 2000))

    kept: list[str] = []
    kept_chars = 0  # length of "\n".join(kept), plus one
    chars = 0  # the same for the whole window
    total = 0  # lines seen
    ends_with_newline = True  # an empty file is one empty line

    def take(raw: bytes) -> None:
        nonlocal kept_chars, chars
        line = f"{total + 1}\t{raw.decode('utf-8')}"
        chars += len(line) + 1
        if kept_chars <= MAX_CHARS:  # enough is kept to cut at MAX_CHARS
            kept.append(line)
            kept_chars += len(line) + 1

    with open(path, "rb") as f:
        for raw in f:
            if start <= total < stop:
                take(raw.removesuffix(b"\n"))
            total += 1
            ends_with_newline = raw.endswith(b"\n")
    if ends_with_newline:  # the empty text after the last newline is a line too
        if start <= total < stop:
            take(b"")
        total += 1

    remaining = total - stop
    footer = f"\n... [{remaining} more lines]" if remaining > 0 else ""
    full = max(0, chars - 1) + len(footer)
    result = "\n".join(kept) + footer
    if full <= MAX_CHARS:
        return result
    return result[:MAX_CHARS] + f"\n... [truncated: {full - MAX_CHARS} more characters]"


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
    confirm=outside_workspace,
    subject_key="path",
)


# --- write -------------------------------------------------------------------


def write(input: dict, ctx: Any) -> str:
    """Create or overwrite a file, creating parent directories as needed."""
    path = resolve_path(ctx, input["path"])
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
    subject_key="path",
)


# --- edit --------------------------------------------------------------------


def edit(input: dict, ctx: Any) -> str:
    """Replace the single occurrence of ``old_string`` with ``new_string``.

    Raises:
        ValueError: if ``old_string`` is missing or appears more than once.
    """
    path = resolve_path(ctx, input["path"])
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
    subject_key="path",
)
