"""Settings and saved state in ``~/.minimal`` and in a project's ``.minimal`` folder.

Settings are JSON files read as layers, lowest first (see :func:`read_layers`):

- ``~/.minimal/settings.json`` (``global``): written by setup, ``/theme`` and
  the ``/ext`` picker too (:func:`update_global`).
- ``<project>/.minimal/settings.json`` (``project``): shared with the project.
- ``<project>/.minimal/settings.local.json`` (``local``): personal, not committed.

``$MINIMAL_PROVIDER``/``$MINIMAL_MODEL`` and the command line beat every layer.
What each key means and how layers merge is :class:`Settings`. A project's
``permissions.allow`` and ``extensions.settings`` apply only once the user
trusted the project (``/trust``); every other project key always applies.

Secrets and state live in their own files:

- ``keys.json`` (mode 0600): API keys entered during setup, stored under their
  environment-variable names. :func:`apply_keys` copies them into
  ``os.environ`` at startup, replacing variables from the shell, so every
  provider (built-in or from an extension) reads them the same way.
- ``trusted.json``: project folders the user agreed to run, as
  ``{"projects": {folder: digest}}`` (see :class:`minimal.extensions.loader.ProjectSnapshot`).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

HOME_DIR = Path.home() / ".minimal"
SETTINGS_FILE = HOME_DIR / "settings.json"
KEYS_FILE = HOME_DIR / "keys.json"
TRUST_FILE = HOME_DIR / "trusted.json"

LEGACY_FILES = {
    "config": HOME_DIR / "config.json",
    "theme": HOME_DIR / "theme.json",
    "marketplaces": HOME_DIR / "marketplaces.json",
    "prompt": HOME_DIR / "prompt.md",
}
"""Files of minimal 0.1 that :func:`migrate_legacy` folds into :data:`SETTINGS_FILE`."""

SCOPES = ("global", "project", "local")
"""Settings layers, lowest first."""

RULE_RE = re.compile(r"^([A-Za-z0-9_\-]+)(?:\((.*)\))?$", re.DOTALL)
"""A permission rule: ``name`` or ``name(pattern)`` (see :mod:`minimal.core.permissions`)."""

GLOBAL_ONLY = ("marketplaces", "prompt.base")
"""Keys read only from :data:`SETTINGS_FILE`: a project must not choose where code or the base prompt comes from."""

RISKY = ("permissions.allow", "extensions.settings")
"""Project keys that apply only once the project is trusted."""


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _is_settings_map(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(v, dict) for v in value.values())


_TYPES = {
    "provider": (lambda v: isinstance(v, str), "a string"),
    "model": (lambda v: isinstance(v, str), "a string"),
    "theme": (lambda v: isinstance(v, str), "a string"),
    "marketplaces": (_is_str_list, "a list of strings"),
    "prompt.base": (lambda v: isinstance(v, str), "a string"),
    "prompt.append": (lambda v: isinstance(v, str) or _is_str_list(v), "a string or a list of strings"),
    "prompt.files": (_is_str_list, "a list of strings"),
    "permissions.allow": (_is_str_list, "a list of strings"),
    "permissions.deny": (_is_str_list, "a list of strings"),
    "extensions.disabled": (_is_str_list, "a list of strings"),
    "extensions.settings": (_is_settings_map, "an object mapping extension ids to objects"),
}
"""Every setting as a dotted key: ``(type check, type name for errors)``."""

_GROUPS = ("prompt", "permissions", "extensions")
"""Top-level keys holding an object of settings."""


def project_settings_files(cwd: str) -> dict[str, Path]:
    """The project and local settings files of ``cwd``, by scope."""
    folder = Path(cwd) / ".minimal"
    return {"project": folder / "settings.json", "local": folder / "settings.local.json"}


def settings_files(cwd: str) -> dict[str, Path]:
    """Every settings file read for ``cwd``, by scope, lowest layer first."""
    return {"global": SETTINGS_FILE, **project_settings_files(cwd)}


@dataclass(frozen=True)
class Layer:
    """One settings file as read: ``data`` maps dotted keys (``prompt.append``) to validated values."""

    scope: str
    path: Path
    data: dict


def read_layers(cwd: str) -> tuple[list[Layer], list[str]]:
    """Read the global, project and local settings files, skipping missing ones.

    Returns:
        ``(layers, errors)``. A file that is not a JSON object is left out with one
        error; a key that is unknown, has the wrong type, or is global-only in a
        project file is dropped with one error each.
    """
    layers, errors = [], []
    for scope, path in settings_files(cwd).items():
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as e:
            errors.append(f"{path}: not valid JSON ({e})")
            continue
        if not isinstance(raw, dict):
            errors.append(f"{path}: not valid JSON (the top level must be an object)")
            continue
        data, problems = _validate(raw, scope)
        errors.extend(f"{path}: {p}" for p in problems)
        layers.append(Layer(scope, path, data))
    return layers, errors


def _validate(raw: dict, scope: str) -> tuple[dict, list[str]]:
    """Flatten ``raw`` to dotted keys, keeping the valid ones; return them and one problem per dropped key."""
    flat, problems = {}, []
    for key, value in raw.items():
        if key == "$schema":
            continue
        if key in _GROUPS:
            if not isinstance(value, dict):
                problems.append(f"{key} must be an object")
                continue
            flat.update({f"{key}.{sub}": v for sub, v in value.items()})
        else:
            flat[key] = value
    data = {}
    for key, value in flat.items():
        check = _TYPES.get(key)
        if check is None:
            problems.append(f"unknown setting '{key}'")
        elif not check[0](value):
            problems.append(f"{key} must be {check[1]}")
        elif key in GLOBAL_ONLY and scope != "global":
            problems.append(f"{key} is only read from ~/.minimal/settings.json")
        else:
            if key == "prompt.append" and isinstance(value, str):
                value = [value]
            if key.startswith("permissions."):
                bad = [rule for rule in value if not RULE_RE.fullmatch(rule)]
                problems.extend(f"invalid permission rule {rule!r}: use name or name(pattern)" for rule in bad)
                value = [rule for rule in value if rule not in bad]
            data[key] = value
    return data, problems


@dataclass(frozen=True)
class Settings:
    """The effective settings: ``layers`` merged, with project trust applied.

    Args:
        layers: From :func:`read_layers`, lowest first.
        trusted: Whether the project's ``permissions.allow`` and ``extensions.settings`` apply.
    """

    layers: tuple[Layer, ...] = ()
    trusted: bool = False

    def scalar(self, key: str) -> tuple[object, str | None]:
        """``(value, scope)`` of ``key`` from the highest layer that sets it; ``(None, None)`` if none does."""
        for layer in reversed(self.layers):
            if key in layer.data:
                return layer.data[key], layer.scope
        return None, None

    @property
    def provider(self) -> str | None:
        return self.scalar("provider")[0]

    @property
    def model(self) -> str | None:
        return self.scalar("model")[0]

    @property
    def theme(self) -> str | None:
        return self.scalar("theme")[0]

    def _applies(self, layer: Layer, key: str) -> bool:
        return self.trusted or layer.scope == "global" or key not in RISKY

    def _concat(self, key: str) -> list[tuple[object, str]]:
        return [
            (value, layer.scope)
            for layer in self.layers
            if self._applies(layer, key)
            for value in layer.data.get(key, [])
        ]

    @property
    def marketplaces(self) -> list[str]:
        return list(self._global().get("marketplaces", []))

    @property
    def prompt_base(self) -> Path | None:
        base = self._global().get("prompt.base")
        return None if base is None else Path(base).expanduser()

    @property
    def prompt_append(self) -> list[str]:
        return [text for text, _ in self._concat("prompt.append")]

    @property
    def prompt_files(self) -> tuple[list[str], str]:
        """``(files, scope)``; ``(["AGENTS.md"], "default")`` when no layer sets them."""
        files, scope = self.scalar("prompt.files")
        return (list(files), scope) if scope else (["AGENTS.md"], "default")

    @property
    def allow(self) -> list[tuple[str, str]]:
        """``(rule, scope)`` of every allow rule that applies (a project's only once trusted)."""
        return self._concat("permissions.allow")

    @property
    def deny(self) -> list[tuple[str, str]]:
        return self._concat("permissions.deny")

    @property
    def disabled(self) -> frozenset[str]:
        return frozenset(ext for ext, _ in self._concat("extensions.disabled"))

    def extension_settings(self, ext_id: str) -> dict:
        """The user's settings for extension ``ext_id``: each layer's object updating the lower ones (a project's only once trusted)."""
        merged: dict = {}
        for layer in self.layers:
            if self._applies(layer, "extensions.settings"):
                merged.update(layer.data.get("extensions.settings", {}).get(ext_id, {}))
        return merged

    @property
    def untrusted_risky(self) -> bool:
        """Whether project or local files set trust-gated keys that do not apply because the project is not trusted."""
        return not self.trusted and bool(risky_subset(self.layers))

    def _global(self) -> dict:
        return next((layer.data for layer in self.layers if layer.scope == "global"), {})


def risky_subset(layers) -> dict:
    """The trust-gated keys set by project and local layers: ``{scope: {key: value}}``; ``{}`` if none."""
    subset = {}
    for layer in layers:
        if layer.scope == "global":
            continue
        found = {key: layer.data[key] for key in RISKY if key in layer.data}
        if found:
            subset[layer.scope] = found
    return subset


def load_global() -> dict:
    """The validated global settings (dotted keys); ``{}`` if the file is missing or not a JSON object."""
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    return _validate(raw, "global")[0] if isinstance(raw, dict) else {}


def update_global(changes: dict) -> None:
    """Set top-level keys of :data:`SETTINGS_FILE` (``None`` deletes one), keeping everything else.

    Raises:
        ValueError: if the file exists but is not a JSON object: it is left untouched for the user to fix.
    """
    data = {}
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ValueError(f"{SETTINGS_FILE} is not valid JSON ({e}); fix it first") from None
        if not isinstance(data, dict):
            raise ValueError(f"{SETTINGS_FILE} is not a JSON object; fix it first")
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    write_private(SETTINGS_FILE, data)


def migrate_legacy() -> str | None:
    """Fold minimal 0.1's ``config.json``, ``theme.json``, ``marketplaces.json`` and ``prompt.md`` into :data:`SETTINGS_FILE`.

    Runs only when the settings file does not exist yet and a legacy file does.
    The JSON files are deleted once moved; ``prompt.md`` stays and is referenced
    as ``prompt.base``. An unreadable file is left in place and reported.

    Returns:
        What happened, for the user; ``None`` if there was nothing to do.
    """
    if SETTINGS_FILE.exists() or not any(path.exists() for path in LEGACY_FILES.values()):
        return None
    data: dict = {}
    moved: list[Path] = []
    problems: list[str] = []
    wanted = {
        "config": {"provider": str, "model": str},
        "theme": {"theme": str},
        "marketplaces": {"marketplaces": list},
    }
    for name, keys in wanted.items():
        path = LEGACY_FILES[name]
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as e:
            problems.append(f"left {path.name} in place: not valid JSON ({e})")
            continue
        if not (isinstance(raw, dict) and all(isinstance(raw.get(k), t) for k, t in keys.items())):
            problems.append(f"left {path.name} in place: unexpected content")
            continue
        if name == "marketplaces" and not _is_str_list(raw["marketplaces"]):
            problems.append(f"left {path.name} in place: unexpected content")
            continue
        data.update({k: raw[k] for k in keys})
        moved.append(path)
    if LEGACY_FILES["prompt"].exists():
        data["prompt"] = {"base": "~/.minimal/prompt.md"}
    if not data:
        return "Settings not migrated: " + "; ".join(problems)
    write_private(SETTINGS_FILE, data)
    for path in moved:
        path.unlink()
    parts = []
    if moved:
        parts.append(f"Moved {', '.join(p.name for p in moved)} into ~/.minimal/settings.json")
    if "prompt" in data:
        parts.append("prompt.md is kept and set as prompt.base in ~/.minimal/settings.json")
    return "; ".join(parts + problems)


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


def load_trusted() -> dict[str, str]:
    """Return the trusted project folders: resolved path -> digest of what the user accepted.

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
    """Remember that the user trusted what hashes to ``digest`` in project folder ``directory``."""
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
