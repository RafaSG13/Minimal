"""Settings saved by setup (:mod:`minimal.setup`) in ``~/.minimal``.

- ``config.json``: the default provider and model.
- ``keys.json`` (mode 0600): API keys entered during setup, stored under their
  environment-variable names. :func:`apply_keys` copies them into
  ``os.environ`` at startup, replacing variables from the shell, so every
  provider (built-in or from an extension) reads them the same way.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_FILE = Path.home() / ".minimal" / "config.json"
KEYS_FILE = Path.home() / ".minimal" / "keys.json"


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
    _write_private(CONFIG_FILE, {"provider": provider, "model": model})


def load_keys() -> dict[str, str]:
    """Return the saved API keys by environment-variable name.

    Raises:
        ValueError: if the file exists but cannot be parsed.
    """
    return _read_json(KEYS_FILE) or {}


def save_key(name: str, value: str) -> None:
    """Save API key ``value`` under environment-variable ``name`` and set it in ``os.environ``."""
    keys = load_keys()
    keys[name] = value
    _write_private(KEYS_FILE, keys)
    os.environ[name] = value


def apply_keys() -> None:
    """Copy the saved API keys into ``os.environ``, overriding the shell's values."""
    os.environ.update(load_keys())


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


def _write_private(path: Path, data: dict) -> None:
    """Write ``data`` as JSON atomically with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=2))
    os.replace(tmp, path)
