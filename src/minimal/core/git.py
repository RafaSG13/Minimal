"""The working directory's git state, polled by one background thread for every reader (``ctx.git()``).

Extensions and the terminal UI's own status line used to run their own ``git status``; :class:`GitPoller` runs it once
per interval and shares the result. Nothing here imports a UI toolkit.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

MAX_FILES = 50
"""Most changed files a :class:`GitInfo` lists (the counts cover all of them)."""

TIMEOUT = 5.0
"""Seconds one ``git status`` may take before the poll gives up and keeps the last result."""


@dataclass(frozen=True)
class GitInfo:
    """The state of the git repository containing the working directory (``ctx.git()``). API 2.3.

    Attributes:
        branch: The current branch; ``None`` on a detached HEAD.
        dirty: Whether any file is changed, staged or untracked.
        ahead: Commits the branch has that its upstream lacks (``0`` without an upstream).
        behind: Commits its upstream has that the branch lacks.
        staged: Files with a change in the index.
        unstaged: Files with a change in the working tree that is not staged.
        untracked: Files git does not track.
        files: ``(status, path)`` of each changed file, at most :data:`MAX_FILES`, in git's order; ``status`` is
            git's two-letter code (``" M"`` modified, ``"A "`` added, ``"??"`` untracked, ``"UU"`` conflicted, …).
    """

    branch: str | None
    dirty: bool = False
    ahead: int = 0
    behind: int = 0
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    files: tuple[tuple[str, str], ...] = ()


def _branch(header: str) -> tuple[str | None, int, int]:
    """``(branch, ahead, behind)`` from the ``## ...`` line of ``git status --porcelain=v1 --branch``."""
    head, _, tracking = header.removeprefix("## ").partition(" [")
    ahead = behind = 0
    for part in tracking.rstrip("]").split(", "):
        word, _, count = part.partition(" ")
        if word == "ahead" and count.isdigit():
            ahead = int(count)
        elif word == "behind" and count.isdigit():
            behind = int(count)
    name = head.removeprefix("No commits yet on ").removeprefix("Initial commit on ").split("...")[0]
    return (None if name.startswith("HEAD (no branch)") else name), ahead, behind


def parse_status(output: str) -> GitInfo | None:
    """The :class:`GitInfo` of ``git status --porcelain=v1 -z --branch`` output; ``None`` if it has no branch line."""
    records = output.split("\0")
    if not records or not records[0].startswith("## "):
        return None
    branch, ahead, behind = _branch(records[0])
    staged = unstaged = untracked = 0
    files: list[tuple[str, str]] = []
    changed = 0
    i = 1
    while i < len(records):
        record = records[i]
        i += 1
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        if code[0] in "RC":  # a rename or copy is followed by the path it came from
            i += 1
        if code == "!!":
            continue
        changed += 1
        if code == "??":
            untracked += 1
        else:
            staged += code[0] not in " ?"
            unstaged += code[1] not in " ?"
        if len(files) < MAX_FILES:
            files.append((code, path))
    return GitInfo(branch, changed > 0, ahead, behind, staged, unstaged, untracked, tuple(files))


def read_git(cwd: str, timeout: float = TIMEOUT) -> GitInfo | None:
    """The git state of the repository containing ``cwd``, or ``None`` outside one (or if ``git`` is missing or slow)."""
    try:
        result = subprocess.run(
            # core.fsmonitor=false: a repository's own config must not make git run a command
            ["git", "-c", "core.fsmonitor=false", "--no-optional-locks", "status", "--porcelain=v1", "-z", "--branch"],
            cwd=cwd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return parse_status(result.stdout)


class GitPoller:
    """One thread that runs :func:`read_git` every ``interval`` seconds and keeps the last result.

    Args:
        cwd: The working directory to ask about.
        interval: Seconds between polls.
        on_change: Called from the poller's thread with the new result each time it differs from the last one.
        read: How to read the state; :func:`read_git` by default.
    """

    def __init__(
        self,
        cwd: str,
        interval: float,
        on_change: Callable[[GitInfo | None], None],
        read: Callable[[str], GitInfo | None] | None = None,
    ) -> None:
        self._cwd = cwd
        self._interval = interval
        self._on_change = on_change
        self._read = read or read_git
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._info: GitInfo | None = None
        self._thread: threading.Thread | None = None

    @property
    def info(self) -> GitInfo | None:
        """The last result: ``None`` until the first poll ends, and outside a repository."""
        with self._lock:
            return self._info

    def start(self) -> None:
        """Start the thread (a second call does nothing)."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="minimal-git", daemon=True)
            self._thread.start()

    def poke(self) -> None:
        """Poll again now instead of waiting for the interval (something probably changed)."""
        self._wake.set()

    def stop(self) -> None:
        """Ask the thread to end; it does not wait for a ``git status`` in progress."""
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            info = self._read(self._cwd)
            if self._stop.is_set():
                return
            with self._lock:
                changed = info != self._info
                self._info = info
            if changed:
                try:
                    self._on_change(info)
                except Exception:  # a broken listener must not end the polling
                    pass
            self._wake.wait(self._interval)
            self._wake.clear()
