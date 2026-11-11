"""Settings saved by setup (:mod:`minimal.ui.setup`) in ``~/.minimal``.

- ``config.json``: the default provider and model.
- ``keys.json`` (mode 0600): API keys entered during setup, stored under their
  environment-variable names. :func:`apply_keys` copies them into
  ``os.environ`` at startup, replacing variables from the shell, so every
  provider (built-in or from an extension) reads them the same way.
- ``marketplaces.json``: extension marketplaces added from the ``/ext``
  picker (git URLs or local paths), as ``{"marketplaces": [...]}``.
- ``theme.json``: the terminal UI's colour theme, as ``{"theme": name}``.
- ``trusted.json``: project extension folders the user agreed to run, as
  ``{"projects": {folder: digest}}`` (see :func:`minimal.extensions.loader.files_digest`).
- ``prompt.md``: optional Markdown text that replaces the packaged base prompt (see :func:`minimal.core.app.base_prompt`).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

CONFIG_FILE = Path.home() / ".minimal" / "config.json"
KEYS_FILE = Path.home() / ".minimal" / "keys.json"
MARKETPLACES_FILE = Path.home() / ".minimal" / "marketplaces.json"
THEME_FILE = Path.home() / ".minimal" / "theme.json"
TRUST_FILE = Path.home() / ".minimal" / "trusted.json"
PROMPT_FILE = Path.home() / ".minimal" / "prompt.md"


def load_config() -> dict | None:
    """Return ``{"provider": str, "model": str}``, or ``None`` before the first setup.

    Raises:
        ValueError: if the file exists but cannot be parsed.
    """
    data = _read_json(CONFIG_FILE)
    if data is not None and not (isinstance(data.get("provider"), str) and isinstance(data.get("model"), str)):
        raise ValueError(f"{CONFIG_FILE} is unreadable; run `minimal --setup` again.")
    return data


def save_config(provider: str, model: str) -> None:
    """Save the default provider and model."""
    write_private(CONFIG_FILE, {"provider": provider, "model": model})


def load_keys() -> dict[str, str]:
    """Return the saved API keys by environment-variable name.

    Raises:
        ValueError: if the file exists but cannot be parsed.
    """
    return _read_json(KEYS_FILE) or {}


_injected_keys: set[str] = set()
"""Names this process copied into ``os.environ`` from the key store; kept even if the file later changes."""


def injected_key_names() -> frozenset[str]:
    """Names of the saved keys this process put in ``os.environ`` (by :func:`apply_keys` or :func:`save_key`)."""
    return frozenset(_injected_keys)


def save_key(name: str, value: str) -> None:
    """Save API key ``value`` under environment-variable ``name`` and set it in ``os.environ``."""
    keys = load_keys()
    keys[name] = value
    write_private(KEYS_FILE, keys)
    os.environ[name] = value
    _injected_keys.add(name)


def apply_keys() -> None:
    """Copy the saved API keys into ``os.environ``, overriding the shell's values."""
    keys = load_keys()
    os.environ.update(keys)
    _injected_keys.update(keys)


def load_marketplaces() -> list[str]:
    """Return the saved marketplace URLs, in the order they were added.

    Raises:
        ValueError: if the file exists but cannot be parsed.
    """
    data = _read_json(MARKETPLACES_FILE) or {}
    urls = data.get("marketplaces", [])
    if not (isinstance(urls, list) and all(isinstance(u, str) for u in urls)):
        raise ValueError(f"{MARKETPLACES_FILE} is unreadable; fix or delete it.")
    return urls


def save_marketplaces(urls: list[str]) -> None:
    """Replace the saved marketplace URLs with ``urls``."""
    write_private(MARKETPLACES_FILE, {"marketplaces": urls})


def load_theme() -> str | None:
    """Return the saved theme name, or ``None`` if none was saved or the file is unreadable."""
    try:
        data = _read_json(THEME_FILE)
    except ValueError:
        return None
    name = data.get("theme") if data else None
    return name if isinstance(name, str) else None


def save_theme(name: str) -> None:
    """Save the terminal UI's colour theme."""
    write_private(THEME_FILE, {"theme": name})


def load_trusted() -> dict[str, str]:
    """Return the trusted project extension folders: resolved path -> digest of the files the user accepted.

    Empty if nothing was trusted yet or the file is unreadable (an unreadable store trusts nothing).
    """
    try:
        data = _read_json(TRUST_FILE) or {}
    except ValueError:
        return {}
    projects = data.get("projects")
    if not (isinstance(projects, dict) and all(isinstance(v, str) for v in projects.values())):
        return {}
    return projects


def save_trusted(directory: str, digest: str) -> None:
    """Remember that the user trusted the extension files with ``digest`` in project folder ``directory``."""
    write_private(TRUST_FILE, {"projects": {**load_trusted(), directory: digest}})


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ValueError(f"{path} is unreadable; run `minimal --setup` again.")
    return data


def private_dir(path: Path) -> None:
    """Create folder ``path`` if missing and make it accessible to the owner only (mode 0700).

    Missing parents are created with mode 0700 too (``~/.minimal`` before
    ``~/.minimal/sessions``); existing parents are left as they are.
    """
    if not path.parent.exists():
        private_dir(path.parent)
    path.mkdir(exist_ok=True, mode=0o700)
    if path.stat().st_mode & 0o077:
        os.chmod(path, 0o700)


def write_private(path: Path, data: dict) -> None:
    """Write ``data`` as JSON atomically with mode 0600, in a folder with mode 0700.

    The temporary file has a fresh random name and is created exclusively, so a
    file or symlink planted next to ``path`` is never written through.
    """
    private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
