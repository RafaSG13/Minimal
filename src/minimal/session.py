"""Conversation persistence.

Each session is a JSONL file in ``~/.minimal/sessions/<id>.jsonl`` with one
message per line. Files are only ever appended to, so saving after each
turn is cheap and a crash loses at most the turn in progress.

Session ids are UTC timestamps (e.g. ``2026-10-28T21-19-43-183Z``), so sorting
them by name also sorts them by creation time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SESSIONS_DIR = Path.home() / ".minimal" / "sessions"
"""Folder holding session files. Read at call time, so tests can patch it."""


def new_id() -> str:
    """Return a fresh, filename-safe session id based on the current UTC time."""
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return stamp.replace("+00:00", "Z").replace(":", "-").replace(".", "-")


@dataclass
class Session:
    """A conversation and the file it is saved to.

    Attributes:
        id: Session id (the file name without ``.jsonl``).
        file: Path of the JSONL file.
        messages: The conversation. Modify it freely, then call :meth:`save`.
        persisted: How many leading messages are already on disk.
    """

    id: str
    file: Path
    messages: list[dict] = field(default_factory=list)
    persisted: int = 0

    def save(self) -> None:
        """Append the messages added since the last save to the file."""
        pending = self.messages[self.persisted :]
        if pending:
            with open(self.file, "a", encoding="utf-8") as f:
                for message in pending:
                    f.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.persisted = len(self.messages)


def open_session(id: str | None = None) -> Session:
    """Open session ``id``, loading its messages if the file exists.

    A new id is generated when ``id`` is ``None``. The file itself is only
    created on the first :meth:`Session.save` that has something to write.
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    id = id or new_id()
    file = SESSIONS_DIR / f"{id}.jsonl"
    messages: list[dict] = []
    if file.exists():
        with open(file, encoding="utf-8") as f:
            messages = [json.loads(line) for line in f if line.strip()]
    return Session(id, file, messages, persisted=len(messages))


def list_sessions() -> list[str]:
    """Return all saved session ids, oldest first."""
    if not SESSIONS_DIR.is_dir():
        return []
    return sorted(p.stem for p in SESSIONS_DIR.glob("*.jsonl"))


def last_session() -> str | None:
    """Return the id of the most recent session, or ``None`` if there are none."""
    sessions = list_sessions()
    return sessions[-1] if sessions else None
