"""Conversation persistence.

Each session is a JSONL file in ``~/.minimal/sessions/<id>.jsonl``. Its first
line is a header, ``{"type": "session", "cwd": "<project directory>"}``, which
tells which project the conversation belongs to (plus ``title`` and ``parent``
when set); then comes one message per line. Files are only ever appended to
(only setting a title rewrites the header), so saving after each turn is cheap
and a crash loses at most the turn in progress, and at most leaves a cut-off
last line, which opening ignores and the next save removes.

A minimal that continues a session holds ``<id>.lock`` (:meth:`Session.lock`),
so a second one cannot write the same file and interleave two conversations.

Session ids are UTC timestamps (e.g. ``2026-10-28T21-19-43-183Z``), so sorting
them by name also sorts them by creation time.
"""

from __future__ import annotations

import fcntl
import itertools
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from minimal import config

SESSIONS_DIR = Path.home() / ".minimal" / "sessions"
"""Folder holding session files. Read at call time, so tests can patch it."""

ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
"""Valid session ids (always ``fullmatch``): a plain file name, so an id cannot point outside :data:`SESSIONS_DIR`."""

TORN_NOTICE = "the last line of the session file was cut off by an interrupted write: it was ignored."
"""What to tell the user when opening a session left a line out (:attr:`Session.torn_at` is set)."""


class SessionLocked(ValueError):
    """The session is open in another minimal process."""


def new_id() -> str:
    """Return a fresh, filename-safe session id based on the current UTC time."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z").replace(":", "-").replace(".", "-")


def project_dir(cwd: str) -> str:
    """The form of ``cwd`` stored in a session header and compared when filtering by project."""
    return os.path.realpath(cwd)


def _is_header(record: object) -> bool:
    return isinstance(record, dict) and record.get("type") == "session" and "role" not in record


@dataclass
class Session:
    """A conversation and the file it is saved to.

    Attributes:
        id: Session id (the file name without ``.jsonl``).
        file: Path of the JSONL file.
        messages: The conversation. Modify it freely, then call :meth:`save`.
        persisted: How many leading messages are already on disk.
        cwd: The project directory the session belongs to (``""`` if its file has no header).
        torn_at: Where a cut-off last line starts in the file, if opening it found one (``None`` otherwise).
            That line is not in :attr:`messages` and the next :meth:`save` removes it.
        title: A name given with :meth:`set_title` (``""`` if none).
        parent: The session this one continues, e.g. the one ``compact`` summarised (``""`` if none).
            Set it before the first :meth:`save`, which writes the header.
    """

    id: str
    file: Path
    messages: list[dict] = field(default_factory=list)
    persisted: int = 0
    cwd: str = ""
    torn_at: int | None = None
    title: str = ""
    parent: str = ""
    _lock_fd: int | None = field(default=None, repr=False, compare=False)

    def _header(self) -> str:
        header = {"type": "session", "cwd": self.cwd}
        header.update({k: v for k, v in (("title", self.title), ("parent", self.parent)) if v})
        return json.dumps(header, ensure_ascii=False) + "\n"

    def _drop_torn_line(self) -> None:
        if self.torn_at is not None:
            os.truncate(self.file, self.torn_at)
            self.torn_at = None

    def save(self) -> None:
        """Append the messages added since the last save to the file (a new file starts with its header).

        A cut-off last line (see :attr:`torn_at`) is removed first, so the file stays valid.
        """
        pending = self.messages[self.persisted :]
        if pending:
            self._drop_torn_line()
            new = not self.file.exists() or self.file.stat().st_size == 0
            fd = os.open(self.file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                if new:
                    f.write(self._header())
                for message in pending:
                    f.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.persisted = len(self.messages)

    def set_title(self, title: str) -> None:
        """Name the session; a saved file gets its header rewritten (atomically) with the title.

        Raises:
            ValueError: if ``title`` is empty or has more than one line.
        """
        title = title.strip()
        if not title or "\n" in title:
            raise ValueError("a session title is one non-empty line")
        self.title = title
        if not self.file.exists() or self.file.stat().st_size == 0:
            return  # the first save writes the header
        self._drop_torn_line()
        lines = self.file.read_bytes().splitlines(keepends=True)
        try:
            has_header = _is_header(json.loads(lines[0]))
        except ValueError:
            has_header = False
        body = b"".join(lines[1:] if has_header else lines)
        temp = self.file.with_name(f".{self.id}.title.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(self._header().encode("utf-8") + body)
        os.replace(temp, self.file)

    def lock(self) -> None:
        """Hold this session for this process until :meth:`unlock` (or the process ends).

        Raises:
            SessionLocked: if another minimal holds it.
        """
        if self._lock_fd is not None:
            return
        path = self.file.with_suffix(".lock")
        while True:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = os.read(fd, 32).decode("ascii", "replace").strip() or "?"
                os.close(fd)
                raise SessionLocked(
                    f"session {self.id} is open in another minimal (pid {holder}): continue it there, "
                    "or start a new session"
                ) from None
            # The holder may have unlocked and removed the file between our open and flock.
            try:
                if os.fstat(fd).st_ino == os.stat(path).st_ino:
                    break
            except FileNotFoundError:
                pass
            os.close(fd)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        self._lock_fd = fd

    def unlock(self) -> None:
        """Release :meth:`lock` (nothing happens if this process does not hold it)."""
        if self._lock_fd is None:
            return
        try:
            self.file.with_suffix(".lock").unlink(missing_ok=True)  # removed while still held: see lock()
        finally:
            os.close(self._lock_fd)
            self._lock_fd = None

    def take_lock(self, other: Session) -> None:
        """Take over ``other``'s lock: both are the same session file (e.g. it was opened again)."""
        self._lock_fd, other._lock_fd = other._lock_fd, None


def open_session(id: str | None = None, cwd: str | None = None) -> Session:
    """Open session ``id``, loading its messages if the file exists.

    A new id is generated when ``id`` is ``None``. The file itself is only
    created on the first :meth:`Session.save` that has something to write.

    Args:
        cwd: The project directory of a new session (default: the current directory). An
            existing session keeps the one in its header.

    Raises:
        ValueError: if ``id`` is not a plain name (see :data:`ID_RE`), or the file has a line that is
            not JSON and is not the last one. A last line that is not JSON is a save cut off by a
            crash: it is left out and :attr:`Session.torn_at` is set.
    """
    config.private_dir(SESSIONS_DIR)
    id = id or new_id()
    if not ID_RE.fullmatch(id):
        raise ValueError(f"invalid session id {id!r}: use letters, digits, '.', '_' and '-'")
    file = SESSIONS_DIR / f"{id}.jsonl"
    if not file.exists():
        return Session(id, file, cwd=project_dir(cwd or os.getcwd()))
    project = title = parent = ""
    messages: list[dict] = []
    torn_at = None
    lines = file.read_bytes().splitlines(keepends=True)
    offset = 0
    for number, raw in enumerate(lines):
        start, offset = offset, offset + len(raw)
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            if any(later.strip() for later in lines[number + 1 :]):
                raise  # not the last line: the file is corrupt, not just cut off
            torn_at = start
            break
        if _is_header(record) and not messages and not project:
            project = record["cwd"] if isinstance(record.get("cwd"), str) else ""
            title = record["title"] if isinstance(record.get("title"), str) else ""
            parent = record["parent"] if isinstance(record.get("parent"), str) else ""
        else:
            messages.append(record)
    return Session(
        id, file, messages, persisted=len(messages), cwd=project, torn_at=torn_at, title=title, parent=parent
    )


def session_header(id: str) -> dict:
    """The header of session ``id``'s file; ``{}`` if the file is missing, unreadable or has none."""
    try:
        with open(SESSIONS_DIR / f"{id}.jsonl", encoding="utf-8") as f:
            record = json.loads(f.readline())
    except (OSError, ValueError):
        return {}
    return record if _is_header(record) else {}


def session_project(id: str) -> str:
    """The project directory in session ``id``'s header; ``""`` if the file is missing, unreadable or has none."""
    cwd = session_header(id).get("cwd")
    return cwd if isinstance(cwd, str) else ""


def list_sessions(cwd: str | None = None) -> list[str]:
    """Return saved session ids, oldest first: only the sessions of project ``cwd`` if given, else all."""
    if not SESSIONS_DIR.is_dir():
        return []
    ids = sorted(p.stem for p in SESSIONS_DIR.glob("*.jsonl"))
    if cwd is None:
        return ids
    project = project_dir(cwd)
    return [i for i in ids if session_project(i) == project]


def last_session(cwd: str | None = None) -> str | None:
    """Return the id of the most recent session (of project ``cwd`` if given), or ``None`` if there are none."""
    sessions = list_sessions(cwd)
    return sessions[-1] if sessions else None


def preview(id: str) -> str:
    """Session ``id``'s title, else the start of its first message, on one line (60 characters at most), for pickers.

    Empty if the file is missing, empty or not valid JSON, or the message is not plain text.
    """
    content: object = None
    try:
        with open(SESSIONS_DIR / f"{id}.jsonl", encoding="utf-8") as f:
            records = (json.loads(line) for line in f if line.strip())
            first = next(records, None)
            title = first.get("title") if isinstance(first, dict) and _is_header(first) else None
            if isinstance(title, str) and title:
                content = title
            else:
                rest = itertools.chain([first], records)
                content = next((r.get("content") for r in rest if r is not None and not _is_header(r)), None)
    except (OSError, ValueError, AttributeError):
        return ""
    return " ".join(content.split())[:60] if isinstance(content, str) else ""
