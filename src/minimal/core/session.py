"""Conversation persistence.

Each session is a JSONL file in ``~/.minimal/sessions/<id>.jsonl``. Its first
line is a header, ``{"type": "session", "cwd": "<project directory>"}``, which
tells which project the conversation belongs to; then comes one message per
line. Files are only ever appended to, so saving after each turn is cheap and
a crash loses at most the turn in progress, and at most leaves a cut-off last line, which
opening ignores and the next save removes.

Session ids are UTC timestamps (e.g. ``2026-10-28T21-19-43-183Z``), so sorting
them by name also sorts them by creation time.
"""

from __future__ import annotations

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
    """

    id: str
    file: Path
    messages: list[dict] = field(default_factory=list)
    persisted: int = 0
    cwd: str = ""
    torn_at: int | None = None

    def save(self) -> None:
        """Append the messages added since the last save to the file (a new file starts with its header).

        A cut-off last line (see :attr:`torn_at`) is removed first, so the file stays valid.
        """
        pending = self.messages[self.persisted :]
        if pending:
            if self.torn_at is not None:
                os.truncate(self.file, self.torn_at)
                self.torn_at = None
            new = not self.file.exists() or self.file.stat().st_size == 0
            fd = os.open(self.file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                if new:
                    f.write(json.dumps({"type": "session", "cwd": self.cwd}, ensure_ascii=False) + "\n")
                for message in pending:
                    f.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.persisted = len(self.messages)


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
    project = ""
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
        else:
            messages.append(record)
    return Session(id, file, messages, persisted=len(messages), cwd=project, torn_at=torn_at)


def session_project(id: str) -> str:
    """The project directory in session ``id``'s header; ``""`` if the file is missing, unreadable or has none."""
    try:
        with open(SESSIONS_DIR / f"{id}.jsonl", encoding="utf-8") as f:
            record = json.loads(f.readline())
    except (OSError, ValueError):
        return ""
    return record["cwd"] if _is_header(record) and isinstance(record.get("cwd"), str) else ""


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
    """The start of session ``id``'s first message on one line (60 characters at most), for pickers.

    Empty if the file is missing, empty or not valid JSON, or the message is not plain text.
    """
    try:
        with open(SESSIONS_DIR / f"{id}.jsonl", encoding="utf-8") as f:
            records = (json.loads(line) for line in f if line.strip())
            content = next((r.get("content") for r in records if not _is_header(r)), None)
    except (OSError, ValueError, AttributeError):
        return ""
    return " ".join(content.split())[:60] if isinstance(content, str) else ""
