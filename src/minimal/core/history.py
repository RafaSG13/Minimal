"""The prompt history of a project: what the user sent, recalled with Up and Down in the composer.

It is stored per project, next to the extensions' project data, in ``~/.minimal/data/projects/<key>/history.json``
(:func:`~minimal.config.project_data_dir`), private to the owner (mode 0600).
"""

from __future__ import annotations

import json
from pathlib import Path

from minimal import config

DEFAULT_SIZE = 500
"""Entries kept when ``ui.history_size`` is not set."""


def history_path(cwd: str) -> Path:
    """The history file of the project in ``cwd``."""
    return config.project_data_dir(cwd) / "history.json"


class PromptHistory:
    """The messages and commands sent in one project, oldest first.

    Args:
        path: The file the history is kept in.
        size: Most entries kept (the newest); ``0`` keeps none.

    Attributes:
        entries: The history, oldest first; consecutive duplicates are collapsed.
    """

    def __init__(self, path: Path, size: int = DEFAULT_SIZE) -> None:
        self.path = path
        self.size = size
        self.entries: list[str] = self._read()

    def _read(self) -> list[str]:
        """The entries on disk; none if the file is missing or not what :meth:`add` wrote."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return []
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, str) and e][-self.size :] if self.size else []

    def add(self, text: str) -> None:
        """Remember ``text`` as the newest entry and save.

        Entries other minimal processes in the same project saved meanwhile are kept.

        Raises:
            OSError: if the file cannot be written; the entry is still kept in memory.
        """
        text = text.strip()
        if not text or not self.size:
            return
        entries = self._read() or self.entries
        if not entries or entries[-1] != text:
            entries = [*entries, text]
        self.entries = entries[-self.size :]
        config.write_private(self.path, {"entries": self.entries})
