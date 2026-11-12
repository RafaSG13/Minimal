"""The read, write and edit tools."""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from minimal.extensions.registry import Tool
from minimal.tools.common import MAX_CHARS, clip, outside_workspace, resolve_path


def _read_text(path: Path) -> str:
    """The whole file as text, line endings exactly as on disk.

    Raises:
        ValueError: if it is not UTF-8.
    """
    try:
        # newline="" keeps line endings exactly as they are on disk.
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not UTF-8 text: edit only changes UTF-8 files") from None


def _write_text(path: Path, content: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


_PATH = {"type": "string", "description": "File path"}


# --- changes made behind the model's back --------------------------------------

_seen: dict[tuple[str | None, Path], tuple[int, int]] = {}
"""``(session id, resolved path)`` -> ``(st_mtime_ns, st_size)`` when the model last read, wrote or edited it."""


def _stamp(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def _key(ctx: Any, path: Path) -> tuple[str | None, Path]:
    return getattr(ctx, "session_id", None), path


def _remember(ctx: Any, path: Path, stamp: tuple[int, int] | None = None) -> None:
    """Record ``path`` as the model now knows it: ``stamp``, or the file's current one."""
    try:
        _seen[_key(ctx, path)] = stamp or _stamp(path)
    except OSError:
        pass


def _check_unchanged(ctx: Any, path: Path) -> None:
    """Refuse to change a file that changed on disk since the model last read, wrote or edited it.

    A file the model has not seen in this session, or that no longer exists, passes.

    Raises:
        ValueError: if the file's modification time or size differ from the recorded ones.
    """
    seen = _seen.get(_key(ctx, path))
    if seen is None or not path.exists() or _stamp(path) == seen:
        return
    raise ValueError(
        f"{path} changed on disk since you last read it (the user, a command or another program "
        "modified it): read it again before changing it"
    )


# --- read --------------------------------------------------------------------

BINARY_PROBE = 8192
"""How many leading bytes are checked for a NUL byte, which marks a file as binary."""


def read(input: dict, ctx: Any) -> str:
    """Return lines ``offset .. offset+limit-1`` of a file, numbered ``N<TAB>line``.

    The file is read line by line: only the window is decoded and at most about
    :data:`MAX_CHARS` of it kept, the other lines are only counted. Lines are
    split on ``\\n``; a ``\\r`` before it is kept. Bytes that are not UTF-8 are
    shown as U+FFFD, with a note; a binary file (a NUL byte in its first
    :data:`BINARY_PROBE` bytes) is not shown at all.

    Raises:
        ValueError: if the path is a directory, device, FIFO or socket.
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
    replaced = False  # a line in the window was not UTF-8

    def take(raw: bytes) -> None:
        nonlocal kept_chars, chars, replaced
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            replaced = True
        line = f"{total + 1}\t{text}"
        chars += len(line) + 1
        if kept_chars <= MAX_CHARS:  # enough is kept to cut at MAX_CHARS
            kept.append(line)
            kept_chars += len(line) + 1

    stamp = _stamp(path)
    with open(path, "rb") as f:
        if b"\0" in f.read(BINARY_PROBE):
            return f"{path} is a binary file ({stamp[1]} bytes): not shown"
        f.seek(0)
        for raw in f:
            if start <= total < stop:
                take(raw.removesuffix(b"\n"))
            total += 1
            ends_with_newline = raw.endswith(b"\n")
    if ends_with_newline:  # the empty text after the last newline is a line too
        if start <= total < stop:
            take(b"")
        total += 1
    _remember(ctx, path, stamp)

    remaining = total - stop
    footer = f"\n... [{remaining} more lines]" if remaining > 0 else ""
    if replaced:
        footer += "\n... [not valid UTF-8: the bytes that are not are shown as \ufffd]"
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
    """Create or overwrite a file, creating parent directories as needed.

    Raises:
        ValueError: if the file changed on disk since the model last read, wrote or edited it.
    """
    path = resolve_path(ctx, input["path"])
    content = input["content"]
    _check_unchanged(ctx, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, content)
    _remember(ctx, path)
    return f"Wrote {path} ({len(content)} characters)"


WRITE = Tool(
    name="write",
    description=(
        "Create or overwrite a file with the given content. Creates missing directories. "
        "Fails if the file changed on disk since you last read it."
    ),
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

SNIPPET_CONTEXT = 2
"""Unchanged lines shown around each changed line in the result of ``edit``."""

SNIPPET_LINES = 40
"""Most lines of the edited file shown in the result of ``edit``."""


def _edits(input: dict) -> list[dict]:
    """The replacements asked for: ``edits``, or the single ``old_string``/``new_string``/``replace_all``.

    Raises:
        ValueError: if both forms or neither are given, or a replacement is malformed.
    """
    single = "old_string" in input or "new_string" in input
    if "edits" in input:
        if single:
            raise ValueError("give either old_string/new_string or edits, not both")
        edits = input["edits"]
        if not isinstance(edits, list) or not edits:
            raise ValueError("edits must be a non-empty list of {old_string, new_string, replace_all}")
    elif single:
        edits = [input]
    else:
        raise ValueError("old_string and new_string are required (or edits)")
    for i, e in enumerate(edits, 1):
        where = f"edit {i}: " if len(edits) > 1 else ""
        if not isinstance(e, dict) or not isinstance(e.get("old_string"), str) or not isinstance(e.get("new_string"), str):
            raise ValueError(f"{where}old_string and new_string must be strings")
        if not e["old_string"]:
            raise ValueError(f"{where}old_string is empty; use write to create or replace a whole file")
        if e["old_string"] == e["new_string"]:
            raise ValueError(f"{where}old_string and new_string are the same")
    return edits


def _snippet(before: str, after: str) -> str:
    """The changed lines of ``after`` with :data:`SNIPPET_CONTEXT` lines around them, numbered like ``read``."""
    old, new = before.split("\n"), after.split("\n")
    shown: set[int] = set()
    for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag != "equal":
            # A pure deletion (j1 == j2) still shows the lines around where it was.
            shown.update(range(max(0, j1 - SNIPPET_CONTEXT), min(len(new), max(j2, j1 + 1) + SNIPPET_CONTEXT)))
    lines: list[str] = []
    previous = None
    for n in sorted(shown)[:SNIPPET_LINES]:
        if previous is not None and n != previous + 1:
            lines.append("...")
        lines.append(f"{n + 1}\t{new[n].removesuffix(chr(13))}")
        previous = n
    if len(shown) > SNIPPET_LINES:
        lines.append(f"... [{len(shown) - SNIPPET_LINES} more changed or context lines]")
    return "\n".join(lines)


def edit(input: dict, ctx: Any) -> str:
    """Apply one replacement, or several in order (``edits``), and return the changed lines.

    Each replacement changes the single occurrence of ``old_string``, or every one with
    ``replace_all``. Either all of them apply or the file is left untouched.

    Raises:
        ValueError: if a replacement is malformed, its ``old_string`` is missing or (without
            ``replace_all``) appears more than once, the file is not UTF-8, or it changed on disk
            since the model last read, wrote or edited it.
    """
    path = resolve_path(ctx, input["path"])
    edits = _edits(input)
    _check_unchanged(ctx, path)
    before = text = _read_text(path)

    replacements = 0
    for i, e in enumerate(edits, 1):
        where = f"edit {i}: " if len(edits) > 1 else ""
        old, new = e["old_string"], e["new_string"]
        count = text.count(old)
        if count == 0:
            later = " after the previous edits" if i > 1 else ""
            raise ValueError(f"{where}old_string not found in the file{later}")
        if count > 1 and not e.get("replace_all"):
            raise ValueError(
                f"{where}old_string appears {count} times; add more context so it is unique, "
                "or set replace_all to change every occurrence"
            )
        text = text.replace(old, new) if e.get("replace_all") else text.replace(old, new, 1)
        replacements += count if e.get("replace_all") else 1

    _write_text(path, text)
    _remember(ctx, path)
    plural = "" if replacements == 1 else "s"
    return clip(f"Edited {path} ({replacements} replacement{plural}):\n{_snippet(before, text)}")


_REPLACEMENT = {
    "old_string": {"type": "string", "description": "Exact text to replace"},
    "new_string": {"type": "string", "description": "New text"},
    "replace_all": {"type": "boolean", "description": "Replace every occurrence (default: old_string must be unique)"},
}

EDIT = Tool(
    name="edit",
    description=(
        "Replace exact text in a file and return the changed lines. old_string must appear exactly once unless "
        "replace_all is set. For several changes to one file, pass edits (applied in order, all or none) instead "
        "of old_string/new_string. Fails if the file changed on disk since you last read it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": _PATH,
            **_REPLACEMENT,
            "edits": {
                "type": "array",
                "description": "Several replacements, applied in order",
                "items": {"type": "object", "properties": _REPLACEMENT, "required": ["old_string", "new_string"]},
            },
        },
        "required": ["path"],
    },
    run=edit,
    confirm=True,
    subject_key="path",
)
