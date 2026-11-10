"""Built-in tools: ``read``, ``write``, ``edit``, ``bash``, ``grep``, ``glob`` and ``reload_extensions``.

Each tool is a function ``(input: dict, ctx) -> str`` followed by its
:class:`~minimal.registry.Tool` definition (description and JSON schema shown
to the model). Paths are resolved against ``ctx.cwd``. Tools raise on failure;
the agent loop sends the error back to the model as an ``is_error`` result.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
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


GREP_LIMIT = 200
"""Most matching lines :func:`grep` returns."""
GLOB_LIMIT = 500
"""Most paths :func:`glob` returns."""
_LINE_CHARS = 300  # grep output lines are cut to this length (minified files)
_SKIP_DIRS = frozenset({"node_modules", "__pycache__"})  # pruned outside git, besides hidden dirs


def _display(path: Path, ctx: Any) -> str:
    """``path`` relative to the working directory when inside it, so it can be passed to ``read``."""
    try:
        return path.relative_to(Path(ctx.cwd).resolve()).as_posix()
    except ValueError:
        return str(path)


def _list_files(root: Path) -> list[Path]:
    """Sorted files under ``root``: ``git ls-files`` in a repo (honours .gitignore), else a walk."""
    if not root.exists():
        raise FileNotFoundError(f"No such file or directory: {root}")
    if root.is_file():
        return [root]
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is not None and proc.returncode == 0:
        names = proc.stdout.decode("utf-8", "surrogateescape").split("\0")
        # is_file drops deleted-but-tracked files and submodule directories.
        files = [f for f in (root / p for p in names if p) if f.is_file()]
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS]
            files.extend(Path(dirpath, name) for name in filenames)
    return sorted(files)


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob (``**``, ``*``, ``?``, ``[...]``, ``{a,b}``) into a regex for ``fullmatch``."""
    parts: list[str] = []
    depth = 0
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            parts.append(".*")
            i += 2
            continue
        if c == "*":
            parts.append("[^/]*")
        elif c == "?":
            parts.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 2)
            if j == -1:
                parts.append(re.escape("["))
            else:
                body = pattern[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                parts.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        elif c == "{":
            parts.append("(?:")
            depth += 1
        elif c == "}" and depth > 0:
            parts.append(")")
            depth -= 1
        elif c == "," and depth > 0:
            parts.append("|")
        else:
            parts.append(re.escape(c))
        i += 1
    return re.compile("".join(parts))


def _filter_glob(files: list[Path], root: Path, pattern: str) -> list[Path]:
    """Files matching ``pattern``: by name at any depth if it has no ``/``, else by path relative to ``root``."""
    pattern = pattern.removeprefix("./")
    regex = _glob_regex(pattern)
    if "/" not in pattern:
        return [f for f in files if regex.fullmatch(f.name)]
    return [f for f in files if regex.fullmatch(f.name if f == root else f.relative_to(root).as_posix())]


def _searchable_text(path: Path) -> str | None:
    """File contents as text, or ``None`` if unreadable or binary (NUL byte in the first 8 KiB)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


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
    and the result ends with ``[exit 1 (timeout)]``. The same happens, with
    ``[exit 1 (interrupted)]``, when ``ctx.cancelled`` (a ``threading.Event``,
    if ``ctx`` has one) is set while the command runs.
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
    deadline = time.monotonic() + timeout_s
    cancelled = getattr(ctx, "cancelled", None)
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=min(0.2, max(0.0, deadline - time.monotonic())))
            status = f"exit {proc.returncode}"
            break
        except subprocess.TimeoutExpired:
            timed_out = time.monotonic() >= deadline
            if not timed_out and not (cancelled is not None and cancelled.is_set()):
                continue
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate()
            status = "exit 1 (timeout)" if timed_out else "exit 1 (interrupted)"
            break

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


# --- grep --------------------------------------------------------------------


def grep(input: dict, ctx: Any) -> str:
    """Return ``path:line:text`` for each line matching the regex ``pattern``, at most ``GREP_LIMIT``."""
    regex = re.compile(input["pattern"], re.IGNORECASE if input.get("ignore_case") else 0)
    root = _resolve(ctx, input.get("path", "."))
    files = _list_files(root)
    if input.get("glob"):
        files = _filter_glob(files, root, input["glob"])

    out: list[str] = []
    for file in files:
        text = _searchable_text(file)
        if text is None:
            continue
        shown = _display(file, ctx)
        for n, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                out.append(f"{shown}:{n}:{line[:_LINE_CHARS]}")
                if len(out) == GREP_LIMIT:
                    out.append(f"... [stopped after {GREP_LIMIT} matches; narrow pattern, path or glob]")
                    return clip("\n".join(out))
    if not out:
        return "No matches"
    return clip("\n".join(out))


GREP = Tool(
    name="grep",
    description=(
        "Search file contents with a Python regular expression. Returns path:line:text for each matching line "
        f"(at most {GREP_LIMIT}). Skips binary files and, in a git repo, files ignored by .gitignore. "
        "Use glob to filter files, e.g. '*.py' (no '/': matches file names at any depth) or 'src/**/*.ts'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression (Python re syntax)"},
            "path": {"type": "string", "description": "File or directory to search (default: working directory)"},
            "glob": {
                "type": "string",
                "description": "Only search files matching this glob, e.g. '*.py' or 'src/**/*.{ts,tsx}'",
            },
            "ignore_case": {"type": "boolean", "description": "Case-insensitive match"},
        },
        "required": ["pattern"],
    },
    run=grep,
)


# --- glob --------------------------------------------------------------------


def glob(input: dict, ctx: Any) -> str:
    """List files under ``path`` matching ``pattern``, sorted by path, at most ``GLOB_LIMIT``."""
    root = _resolve(ctx, input.get("path", "."))
    files = _filter_glob(_list_files(root), root, input["pattern"])
    if not files:
        return "No files found"
    out = [_display(f, ctx) for f in files[:GLOB_LIMIT]]
    if len(files) > GLOB_LIMIT:
        out.append(f"... [{len(files) - GLOB_LIMIT} more files]")
    return clip("\n".join(out))


GLOB = Tool(
    name="glob",
    description=(
        "Find files by path pattern. '**' matches any number of directories, '*' and '?' stay within one, "
        "'{a,b}' alternatives and '[abc]' classes work. A pattern without '/' matches file names at any depth "
        "('*.py'); with '/' it is matched against the path relative to path ('src/**/*.py'). "
        f"Returns at most {GLOB_LIMIT} paths. In a git repo, files ignored by .gitignore are skipped."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py' or '*.md'"},
            "path": {"type": "string", "description": "Directory to search (default: working directory)"},
        },
        "required": ["pattern"],
    },
    run=glob,
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

BUILTIN_TOOLS: tuple[Tool, ...] = (READ, WRITE, EDIT, BASH, GREP, GLOB, RELOAD)


def register_builtin_tools(registry: Registry) -> None:
    """Add the built-in tools to ``registry``."""
    for tool in BUILTIN_TOOLS:
        registry.add_tool(tool)
