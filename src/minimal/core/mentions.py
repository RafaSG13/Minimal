"""``@file`` mentions in a message: finding project files to complete, and attaching the mentioned ones.

Typing ``@`` and some letters in the composer offers the project's files (:func:`project_files`, ranked by
:func:`match_files`). When the message is sent, :func:`expand_mentions` leaves each mention in the text as written and
appends the content of the files it names as ``<file path="...">`` blocks, so the model has them without a tool call.
Only regular text files inside the working directory are ever read.
"""

from __future__ import annotations

import codecs
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass, field

CANDIDATES = 50
"""Most files :func:`match_files` returns."""

DEFAULT_MAX_CHARS = 20_000
"""Most characters of one file attached when ``ui.mention_max_chars`` is not set."""

FILE_LIMIT = 20_000
"""Most files listed outside a git repository."""

_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})
_INDEX_TTL = 5.0  # seconds a file listing is reused
_BINARY_PROBE = 8192  # a NUL byte in the first bytes of a file marks it as binary
_TRAILING = ".,;:!?)]}'\""  # punctuation that can follow a mention in prose
_MENTION = re.compile(r"(?<!\S)@([^\s@]+)")
_BLOCK = r'<file path="[^"\n]*">\n.*?\n</file>\n?'
_ATTACHED = re.compile(rf"\n\n((?:{_BLOCK})+)", re.DOTALL)

_cache: dict[str, tuple[float, list[str]]] = {}


def _git_files(cwd: str) -> list[str] | None:
    """Tracked and untracked-but-not-ignored files under ``cwd`` (``git ls-files``), or ``None`` outside a repository."""
    try:
        proc = subprocess.run(
            # core.fsmonitor=false: a repository's own config must not make git run a command
            ["git", "-c", "core.fsmonitor=false", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=cwd,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return sorted({p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p})


def _walk_files(cwd: str) -> list[str]:
    """Files under ``cwd`` without ``.git``, ``node_modules``, ``.venv`` and ``__pycache__``; at most :data:`FILE_LIMIT`."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        base = os.path.relpath(dirpath, cwd)
        for name in sorted(filenames):
            found.append(name if base == "." else f"{base}/{name}")
            if len(found) >= FILE_LIMIT:
                return found
    return found


def project_files(cwd: str) -> list[str]:
    """The files of the project in ``cwd`` as paths relative to it, sorted: what ``@`` completes.

    A git repository's ``.gitignore`` is honoured; elsewhere a bounded walk is used. The listing is reused for a
    few seconds.
    """
    now = time.monotonic()
    cached = _cache.get(cwd)
    if cached is not None and now - cached[0] < _INDEX_TTL:
        return cached[1]
    files = _git_files(cwd)
    if files is None:
        files = _walk_files(cwd)
    _cache[cwd] = (now, files)
    return files


def _subsequence(query: str, text: str) -> bool:
    chars = iter(text)
    return all(c in chars for c in query)


def match_files(query: str, files: list[str], limit: int = CANDIDATES) -> list[str]:
    """The ``limit`` best of ``files`` for ``query``: a prefix of the path or of the file name, then a substring,
    then a subsequence of the letters (``rdm`` finds ``README.md``); shorter paths first. Case does not matter."""
    q = query.lower()
    if not q:
        return files[:limit]
    ranked: list[tuple[int, int, str]] = []
    for path in files:
        low = path.lower()
        name = low.rpartition("/")[2]
        if low.startswith(q):
            rank = 0
        elif name.startswith(q):
            rank = 1
        elif q in name:
            rank = 2
        elif q in low:
            rank = 3
        elif _subsequence(q, low):
            rank = 4
        else:
            continue
        ranked.append((rank, len(path), path))
    ranked.sort()
    return [path for _, _, path in ranked[:limit]]


@dataclass
class Expansion:
    """A message with its mentioned files attached.

    Attributes:
        text: The message to send: the original text, then one ``<file path="...">`` block per attached file.
        attached: ``(path relative to the working directory, characters attached, truncated)`` per file.
        notices: Mentions that were not attached and why (binary file, outside the working directory, unreadable).
    """

    text: str
    attached: list[tuple[str, int, bool]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


def _resolve(token: str, cwd_real: str) -> tuple[str, str | None]:
    """``(real path, problem)`` of mention ``token``; the path is ``""`` when it names no file (not a mention)."""
    path = token
    while path:
        try:
            real = os.path.realpath(os.path.join(cwd_real, path))
            mode = os.stat(real).st_mode
        except (OSError, ValueError):  # missing, too long, or an embedded NUL
            real, mode = "", 0
        if stat.S_ISREG(mode):
            if os.path.commonpath([real, cwd_real]) != cwd_real:
                return "", f"@{path}: outside the working directory, not attached"
            return real, None
        if path[-1] not in _TRAILING:
            break
        path = path[:-1]
    return "", None


def _read_text(real: str, max_chars: int) -> tuple[str, bool] | None:
    """The first ``max_chars`` characters of the UTF-8 file ``real`` and whether there is more; ``None`` if binary."""
    with open(real, "rb") as f:
        data = f.read(max_chars * 4 + 1)
    if b"\0" in data[:_BINARY_PROBE]:
        return None
    more = len(data) > max_chars * 4
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(data, final=not more)
    except UnicodeDecodeError:
        return None
    return text[:max_chars], more or len(text) > max_chars


def expand_mentions(text: str, cwd: str, max_chars: int = DEFAULT_MAX_CHARS) -> Expansion:
    """Attach the files ``text`` mentions with ``@path``, as :class:`Expansion` describes.

    A mention counts when the path (relative to ``cwd``, or absolute) names a regular file whose real path, after
    following links, is inside ``cwd``; trailing punctuation is ignored. Anything else is left alone, and a file
    outside ``cwd`` is never opened.
    """
    result = Expansion(text)
    cwd_real = os.path.realpath(cwd)
    blocks: list[str] = []
    seen: set[str] = set()
    for match in _MENTION.finditer(text):
        real, problem = _resolve(match.group(1), cwd_real)
        if problem:
            result.notices.append(problem)
        if not real or real in seen:
            continue
        seen.add(real)
        rel = os.path.relpath(real, cwd_real).replace(os.sep, "/")
        try:
            body = _read_text(real, max_chars)
        except OSError as e:
            result.notices.append(f"@{rel}: not attached ({e.strerror or e})")
            continue
        if body is None:
            result.notices.append(f"@{rel}: binary file, not attached")
            continue
        content, truncated = body
        if truncated:
            content += f"\n[truncated to the first {max_chars} characters]"
        content = content.replace("</file>", "<\\/file>")
        blocks.append(f'<file path="{rel.replace(chr(34), "&quot;")}">\n{content}\n</file>')
        result.attached.append((rel, len(content), truncated))
    if blocks:
        result.text = text + "\n\n" + "\n".join(blocks)
    return result


def split_attachments(text: str) -> tuple[str, list[str]]:
    """``(message, paths)``: ``text`` without the ``<file>`` blocks :func:`expand_mentions` appended, and their paths."""
    match = _ATTACHED.search(text)
    if match is None or match.end() != len(text):
        return text, []
    return text[: match.start()], re.findall(r'<file path="([^"\n]*)">', match.group(1))
