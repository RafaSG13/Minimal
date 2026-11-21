"""Conversation persistence.

Each session is a JSONL file in ``~/.minimal/sessions/<id>.jsonl``. Its first
line is a header, ``{"type": "session", "cwd": "<project directory>"}``, which
tells which project the conversation belongs to (plus ``title`` and ``parent``
when set); then comes one message per line. Files are only ever appended to
(only setting a title or cutting the history rewrites the file, atomically), so saving after each turn is cheap
and a crash loses at most the turn in progress, and at most leaves a cut-off
last line, which opening ignores and the next save removes.

A minimal that continues a session holds ``<id>.lock`` (:meth:`Session.lock`),
so a second one cannot write the same file and interleave two conversations.

Session ids are UTC timestamps (e.g. ``2026-10-28T21-19-43-183Z``), so sorting
them by name also sorts them by creation time.
"""

from __future__ import annotations

import contextlib
import fcntl
import itertools
import json
import os
import re
import tempfile
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


def _has_tool_use(message: dict) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "assistant"
        and isinstance(content, list)
        and any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
    )


def _only_tool_results(message: dict) -> bool:
    content = message.get("content")
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


def user_prompts(messages: list[dict]) -> list[int]:
    """Indexes of the messages the user wrote (not the ``tool_result`` messages that carry tool output), oldest first."""
    return [i for i, m in enumerate(messages) if m.get("role") == "user" and not _only_tool_results(m)]


def prompt_text(message: dict) -> str:
    """The text of a user message, for showing it back to the user: ``""`` if it has none."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def check_cut(messages: list[dict], at_index: int) -> None:
    """Check that ``messages[:at_index]`` is a history the model accepts: ``at_index`` is in range and the cut
    does not leave an assistant message's ``tool_use`` without the ``tool_result`` that follows it.

    Raises:
        ValueError: with a message for the user if not.
    """
    total = len(messages)
    if isinstance(at_index, bool) or not isinstance(at_index, int) or not 0 <= at_index <= total:
        raise ValueError(f"index {at_index!r} is out of range: this session has {total} messages (0 to {total})")
    if 0 < at_index < total and _has_tool_use(messages[at_index - 1]):
        raise ValueError(
            f"cannot cut at {at_index}: message {at_index - 1} calls tools and their results are the next message; "
            f"cut at {at_index - 1} (before the call) or at {at_index + 1} (after the results)"
        )


@dataclass(frozen=True)
class SessionInfo:
    """What a session's file says about it, without its messages (:meth:`Context.session_info <minimal.core.context.Context.session_info>`). API 2.4.

    Attributes:
        id: Session id.
        title: The name given with ``/title`` (``""`` if none).
        parent: The session this one continues or was forked from (``""`` if none).
        cwd: The project directory in its header (``""`` if the file has none).
        preview: The start of the first message the user wrote, on one line, at most 200 characters (``""`` if the
            session has none or it is not plain text).
        mtime: When the file was last written, in seconds since the epoch (that of now for a current session that is
            not saved yet).
        n_messages: How many messages the session holds.
        created: When the session started, in seconds since the epoch, read from its id (they are timestamps);
            ``None`` for an id that is not one.
        locked: Whether a minimal process (this one included) holds the session open.
    """

    id: str
    title: str
    parent: str
    cwd: str
    preview: str
    mtime: float
    n_messages: int
    created: float | None
    locked: bool


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

    def _rewrite(self) -> None:
        """Replace the file (atomically) with the header and every message."""
        fd, temp = tempfile.mkstemp(dir=self.file.parent, prefix=f".{self.id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self._header())
                for message in self.messages:
                    f.write(json.dumps(message, ensure_ascii=False) + "\n")
            os.replace(temp, self.file)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise
        self.torn_at = None
        self.persisted = len(self.messages)

    def truncate(self, at_index: int) -> int:
        """Drop the messages from ``at_index`` on and rewrite the file (atomically) without them; return how many.

        Messages not saved yet are saved with the rewrite. Nothing changes if ``at_index`` is the length.

        Raises:
            ValueError: if ``at_index`` is out of range or the cut would leave a ``tool_use`` without its
                ``tool_result`` (see :func:`check_cut`).
        """
        check_cut(self.messages, at_index)
        dropped = len(self.messages) - at_index
        if not dropped:
            return 0
        del self.messages[at_index:]
        if self.file.exists() and self.file.stat().st_size:
            self._rewrite()
        else:
            self.persisted = min(self.persisted, len(self.messages))
        return dropped

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


_PREVIEW_CHARS = 200
_PREVIEW_SCAN_LINES = 100  # how far into a file the first user message is looked for


def created_at(id: str) -> float | None:
    """The time encoded in a :func:`new_id` id, or ``None``."""
    try:
        return datetime.strptime(id, "%Y-%m-%dT%H-%M-%S-%fZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _held(file: Path) -> bool:
    """Whether some process holds ``file``'s ``.lock`` (see :meth:`Session.lock`); never creates it."""
    try:
        fd = os.open(file.with_suffix(".lock"), os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


def _scan(file: Path) -> tuple[dict, str, int]:
    """``(header, preview, n_messages)`` of ``file`` without parsing it all: the first line, the first user message
    among the first lines, and a count of the other lines. Anything unreadable gives empty values."""
    header: dict = {}
    preview = ""
    count = 0
    last = b""
    with open(file, "rb") as f:
        for number, raw in enumerate(f):
            if not raw.strip():
                continue
            last = raw
            record = None
            if number == 0 or (not preview and number < _PREVIEW_SCAN_LINES):
                with contextlib.suppress(ValueError):
                    record = json.loads(raw)
            if number == 0 and _is_header(record):
                header = record  # type: ignore[assignment]
                continue
            count += 1
            if not preview and isinstance(record, dict) and record.get("role") == "user" and not _only_tool_results(record):
                preview = " ".join(prompt_text(record).split())[:_PREVIEW_CHARS]
    if last and not last.endswith(b"\n"):  # a cut-off last line is not a message
        try:
            json.loads(last)
        except ValueError:
            count -= 1
    return header, preview, count


def read_session_info(id: str) -> SessionInfo:
    """The :class:`SessionInfo` of saved session ``id``, read from its file's first lines and a count of the rest.

    A file that is not valid (no header, lines that are not JSON) gives what could be read: empty strings and counts of
    its non-blank lines, not an error.

    Raises:
        ValueError: if ``id`` is not a valid session id or there is no such session (also when the file is unreadable).
    """
    if not ID_RE.fullmatch(id):
        raise ValueError(f"invalid session id {id!r}")
    file = SESSIONS_DIR / f"{id}.jsonl"
    try:
        mtime = file.stat().st_mtime
        header, preview, count = _scan(file)
    except OSError:
        raise ValueError(f"no session {id!r}") from None

    def text(key: str) -> str:
        value = header.get(key)
        return value if isinstance(value, str) else ""

    return SessionInfo(id, text("title"), text("parent"), text("cwd"), preview, mtime, count, created_at(id), _held(file))


def read_session_infos(cwd: str | None = None, limit: int | None = None) -> list[SessionInfo]:
    """:class:`SessionInfo` of the saved sessions (of project ``cwd`` if given), most recently written first.

    Files are ordered by modification time first and read until ``limit`` matched, so asking for a few of many does not
    read them all. A file that vanishes meanwhile is left out.
    """
    if not SESSIONS_DIR.is_dir() or (limit is not None and limit < 1):
        return []
    stamped = []
    for path in SESSIONS_DIR.glob("*.jsonl"):
        try:
            stamped.append((path.stat().st_mtime, path.stem))
        except OSError:
            continue
    project = None if cwd is None else project_dir(cwd)
    found: list[SessionInfo] = []
    for _, id in sorted(stamped, reverse=True):
        if project is not None and session_project(id) != project:
            continue
        try:
            found.append(read_session_info(id))
        except ValueError:
            continue
        if limit is not None and len(found) >= limit:
            break
    return found
