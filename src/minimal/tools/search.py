"""The grep and glob tools, and the glob-to-regex translation they share."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterator

from minimal.extensions.registry import Tool, TurnInterrupted
from minimal.tools.common import clip, outside_workspace, readable_without_asking, resolve_path

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
            # core.fsmonitor=false: a repository's own config must not make git run a command
            ["git", "-c", "core.fsmonitor=false", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
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
            # is_file drops devices, FIFOs, sockets and dangling links: reading them could block forever.
            files.extend(f for f in (Path(dirpath, name) for name in filenames) if f.is_file())
    return sorted(files)


def glob_regex(pattern: str) -> re.Pattern[str]:
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
    regex = glob_regex(pattern)
    if "/" not in pattern:
        return [f for f in files if regex.fullmatch(f.name)]
    return [f for f in files if regex.fullmatch(f.name if f == root else f.relative_to(root).as_posix())]


def _search_lines(path: Path) -> Iterator[tuple[int, str]]:
    """``(number, text)`` of each line of a file, read as it goes; nothing if unreadable or binary (NUL byte in the first 8 KiB).

    Lines are split on ``\\n`` (as :func:`~minimal.tools.files.read` numbers them) and lose their
    ``\\n`` or ``\\r\\n``; invalid UTF-8 is replaced.
    """
    try:
        with open(path, "rb") as f:
            if b"\0" in f.read(8192):
                return
            f.seek(0)
            for n, raw in enumerate(f, 1):
                yield n, raw.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8", errors="replace")
    except OSError:
        return


# --- grep --------------------------------------------------------------------


def grep(input: dict, ctx: Any) -> str:
    """Return ``path:line:text`` for each line matching the regex ``pattern``, at most ``GREP_LIMIT``.

    Files are read line by line. A file that is a symlink to somewhere outside
    the places read freely (see :func:`~minimal.tools.common.outside_workspace`) and outside the
    searched ``path`` itself is skipped: ``path`` was approved, not its links.
    Setting ``ctx.cancelled`` (if ``ctx`` has it)
    stops the search before the next file with :class:`TurnInterrupted`.
    """
    cancelled = getattr(ctx, "cancelled", None)
    regex = re.compile(input["pattern"], re.IGNORECASE if input.get("ignore_case") else 0)
    root = resolve_path(ctx, input.get("path", "."))
    files = _list_files(root)
    if input.get("glob"):
        files = _filter_glob(files, root, input["glob"])

    out: list[str] = []
    for file in files:
        if cancelled is not None and cancelled.is_set():
            raise TurnInterrupted("interrupted by the user")
        # The search root was approved, not what a link inside it points at: open only the resolved target.
        target = file.resolve()
        if not readable_without_asking(target, ctx) and not target.is_relative_to(root):
            continue
        shown = _display(file, ctx)
        for n, line in _search_lines(target):
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
        f"(at most {GREP_LIMIT}). Skips binary files, symlinks that lead outside the workspace and the searched path, "
        "and, in a git repo, files ignored by .gitignore. "
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
    confirm=outside_workspace,
)


# --- glob --------------------------------------------------------------------


def glob(input: dict, ctx: Any) -> str:
    """List files under ``path`` matching ``pattern``, sorted by path, at most ``GLOB_LIMIT``."""
    root = resolve_path(ctx, input.get("path", "."))
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
    confirm=outside_workspace,
)
