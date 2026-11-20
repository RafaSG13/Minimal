"""``/ext settings``: list, show and edit the settings extensions declared with ``declare_settings``.

``/ext settings`` lists every loaded extension's settings with the value in effect, the default and the description;
``/ext settings <id>`` details one extension; ``/ext settings <id> <key> <value>`` validates the value against the
extension's defaults and schema, writes it to the global settings file (``extensions.settings.<id>.<key>``) and reloads;
``/ext settings <id> <key> reset`` removes it again.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from minimal import config
from minimal.core.app import App
from minimal.extensions.registry import SettingsSpec, check_setting

USAGE = "/ext settings [<id> [<key> <value>|<key> reset]]"


def _shown(value: Any) -> str:
    return value if isinstance(value, str) and value and " " not in value else json.dumps(value, ensure_ascii=False)


def _declared(app: App) -> dict[str, SettingsSpec]:
    """Every loaded extension that declared settings, by id, sorted."""
    return dict(sorted(app.registry.declared_settings.items()))


def _constraints(spec: SettingsSpec, key: str) -> str:
    """What the schema says about ``key``'s values, e.g. ``int, one of 1, 2`` or ``float, 0 to 1``."""
    entry = spec.schema.get(key, {})
    parts = [type(spec.defaults[key]).__name__]
    if "enum" in entry:
        parts.append("one of " + ", ".join(_shown(v) for v in entry["enum"]))
    if "min" in entry and "max" in entry:
        parts.append(f"{entry['min']} to {entry['max']}")
    elif "min" in entry:
        parts.append(f"at least {entry['min']}")
    elif "max" in entry:
        parts.append(f"at most {entry['max']}")
    return ", ".join(parts)


def _row(spec: SettingsSpec, key: str) -> str:
    value, default = spec.values[key], spec.defaults[key]
    mark = "*" if value != default else " "
    note = spec.schema.get(key, {}).get("description", "")
    return f"  {mark}{key:<18} {_shown(value):<14} default {_shown(default):<12} {note}".rstrip()


def _where(app: App, ext: str, key: str) -> str | None:
    """The scope of the highest settings layer that sets ``key`` of ``ext`` and applies; ``None`` if none does."""
    for layer in reversed(app.settings.layers):
        if layer.scope != "global" and not app.settings.trusted:
            continue
        if key in layer.data.get("extensions.settings", {}).get(ext, {}):
            return layer.scope
    return None


def _listing(app: App) -> None:
    declared = _declared(app)
    if not declared:
        print("No loaded extension declares settings.")
        return
    print("Extension settings (* = changed from the default); /ext settings <id> <key> <value> changes one:")
    for ext, spec in declared.items():
        print(ext)
        for key in spec.defaults:
            print(_row(spec, key))


def _detail(app: App, spec: SettingsSpec) -> None:
    print(f"{spec.ext}: settings in effect (* = changed from the default)")
    for key in spec.defaults:
        print(_row(spec, key))
        where = _where(app, spec.ext, key)
        print(f"      {_constraints(spec, key)}" + (f"; set in the {where} settings" if where else ""))
    print(f"Change one with /ext settings {spec.ext} <key> <value> (JSON, or a bare string); <key> reset goes back to the default.")


def _parse(text: str, default: Any) -> Any:
    """``text`` as JSON, or as the bare string it is: a string setting takes whatever is typed, quotes optional."""
    try:
        value = json.loads(text)
    except ValueError:
        return text
    if isinstance(default, str) and not isinstance(value, str):
        return text
    return value


def _save(app: App, spec: SettingsSpec, key: str, value: Any) -> None:
    path = ("extensions", "settings", spec.ext, key)
    try:
        config.update_global({path: value})
    except (OSError, ValueError) as e:
        raise ValueError(f"not saved: {e}") from None
    app.reload_extensions()


def _project_note(app: App, ext: str, key: str) -> None:
    scope = _where(app, ext, key)
    if scope is not None and scope != "global":
        print(f"The {scope} settings also set it and win over the global file; edit that one to change it.")


def run(args: str, app: App) -> None:
    """The ``/ext settings`` command with ``args`` the text after ``settings``.

    Raises:
        ValueError: for an unknown extension or setting, an invalid value or a wrong form.
    """
    words = args.split(None, 2)
    declared = _declared(app)
    if not words:
        _listing(app)
        return
    spec = declared.get(words[0])
    if spec is None:
        raise ValueError(
            f"no loaded extension {words[0]!r} declares settings; "
            f"with settings: {', '.join(declared) or 'none'} (usage: {USAGE})"
        )
    if len(words) == 1:
        _detail(app, spec)
        return
    key = words[1]
    if key not in spec.defaults:
        raise ValueError(f"{spec.ext} has no setting {key!r} (settings: {', '.join(spec.defaults)})")
    if len(words) == 2:
        raise ValueError(f"usage: {USAGE}")
    text = words[2].strip()
    if text == "reset":
        _save(app, spec, key, None)
        print(f"Reset extensions.settings.{spec.ext}.{key} to its default {_shown(spec.defaults[key])} (saved in {config.SETTINGS_FILE})")
        _project_note(app, spec.ext, key)
        return
    value = _parse(text, spec.defaults[key])
    problem = check_setting(spec.defaults[key], value, spec.schema.get(key))
    if problem:
        raise ValueError(f"extensions.settings.{spec.ext}.{key} {problem}, got {_shown(value)}")
    _save(app, spec, key, value)
    print(f"Set extensions.settings.{spec.ext}.{key} = {_shown(value)} (saved in {config.SETTINGS_FILE})")
    _project_note(app, spec.ext, key)


def _complete_word(args: str, options: Iterable[str]) -> list[str]:
    head, sep, last = args.rpartition(" ")
    return [f"{head}{sep}{o}" for o in options if o.startswith(last)]


def complete(args: str, app: App) -> list[str]:
    """Completions of the text typed after ``/ext settings``: extension ids, then the extension's keys, then ``reset``
    and the values the setting allows (its ``enum``, or ``true``/``false``)."""
    declared = _declared(app)
    parts = args.split(" ")
    if len(parts) == 1:
        return _complete_word(args, declared)
    spec = declared.get(parts[0])
    if spec is None:
        return []
    if len(parts) == 2:
        return _complete_word(args, spec.defaults)
    key = parts[1]
    if len(parts) != 3 or key not in spec.defaults:
        return []
    enum = spec.schema.get(key, {}).get("enum")
    values = [_shown(v) for v in enum] if enum else (["true", "false"] if isinstance(spec.defaults[key], bool) else [])
    return _complete_word(args, [*values, "reset"])
