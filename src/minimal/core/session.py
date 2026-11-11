"""Conversation persistence.

Each session is a JSONL file in ``~/.minimal/sessions/<id>.jsonl``. Its first
line is a header, ``{"type": "session", "cwd": "<project directory>"}``, which
tells which project the conversation belongs to; then comes one message per
line. Files are only ever appended to, so saving after each turn is cheap and
a crash loses at most the turn in progress.

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
    """

    id: str
    file: Path
    messages: list[dict] = field(default_factory=list)
    persisted: int = 0
    cwd: str = ""

    def save(self) -> None:
        """Append the messages added since the last save to the file (a new file starts with its header)."""
        pending = self.messages[self.persisted :]
        if pending:
            new = not self.file.exists()
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
        ValueError: if ``id`` is not a plain name (see :data:`ID_RE`).
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
    with open(file, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            record = json.loads(line)
            if _is_header(record) and not messages and not project:
                project = record["cwd"] if isinstance(record.get("cwd"), str) else ""
            else:
                messages.append(record)
    return Session(id, file, messages, persisted=len(messages), cwd=project)


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
