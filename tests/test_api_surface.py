"""The public surface of :mod:`minimal.api` is pinned: the extension API is frozen at 2.5 until minimal 1.0.0.

After an approved API change, regenerate the snapshot:
``PINNED_API_WRITE=1 uv run python -m unittest tests.test_api_surface``.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
import os
import re
import typing
import unittest
from collections.abc import Mapping
from pathlib import Path

from minimal import api

GOLDEN = Path(__file__).with_name("golden") / "api_surface.txt"
"""One line per public name of ``minimal.api`` and per public member of the classes and modules it exports."""

FROZEN = (
    "minimal.api is frozen at API 2.5 until minimal 1.0.0 (docs/extensions.md, 'Changing the API'). "
    "If this change was approved, regenerate tests/golden/api_surface.txt with PINNED_API_WRITE=1."
)

_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")
_INIT_ATTR = re.compile(r"^\s+self\.([a-z][a-z0-9_]*)\s*(?::[^=\n]+)?=(?!=)", re.MULTILINE)


def _value(value: object) -> str:
    """``repr`` of ``value`` with sets and mappings in a stable order and no memory addresses."""
    if isinstance(value, (set, frozenset)):
        text = repr(sorted(value, key=repr))
    elif isinstance(value, Mapping):
        text = repr(sorted(value.items(), key=lambda item: repr(item[0])))
    else:
        text = repr(value)
    return _ADDRESS.sub("", text)


def _signature(obj: object) -> str:
    try:
        return _ADDRESS.sub("", str(inspect.signature(obj)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "(...)"


def _annotation(kind: object) -> str:
    if isinstance(kind, typing.ForwardRef):
        return kind.__forward_arg__
    return kind if isinstance(kind, str) else inspect.formatannotation(kind)


def _member(value: object) -> str:
    if isinstance(value, (property, functools.cached_property)):
        return "property"
    if isinstance(value, staticmethod):
        return f"staticmethod{_signature(value.__func__)}"
    if isinstance(value, classmethod):
        return f"classmethod{_signature(value.__func__)}"
    if inspect.isfunction(value):
        return f"def{_signature(value)}"
    return f"= {_value(value)}"


def _class_lines(name: str, cls: type) -> list[str]:
    lines = [f"{name} class({', '.join(base.__qualname__ for base in cls.__bases__)})"]
    if typing.is_typeddict(cls):
        required = cls.__required_keys__  # type: ignore[attr-defined]
        for key, kind in cls.__annotations__.items():
            lines.append(f"{name}.{key}: {_annotation(kind)}{'' if key in required else ' (optional)'}")
        return lines
    seen: set[str] = set()
    if dataclasses.is_dataclass(cls):
        for field in dataclasses.fields(cls):
            seen.add(field.name)
            if field.name.startswith("_"):
                continue
            default = ""
            if field.default is not dataclasses.MISSING:
                default = f" = {_value(field.default)}"
            elif field.default_factory is not dataclasses.MISSING:
                default = " = <factory>"
            lines.append(f"{name}.{field.name}: {_annotation(field.type)}{default}")
    attributes: set[str] = set()
    for klass in cls.__mro__:
        if not klass.__module__.startswith("minimal."):
            continue
        for member, value in sorted(vars(klass).items()):
            if member.startswith("_") or member in seen:
                continue
            seen.add(member)
            lines.append(f"{name}.{member} {_member(value)}")
        init = vars(klass).get("__init__")
        if init is not None:
            try:
                attributes.update(_INIT_ATTR.findall(inspect.getsource(init)))
            except (OSError, TypeError):
                pass
    lines += [f"{name}.{attr} attribute" for attr in sorted(attributes - seen)]
    return lines


def _module_lines(name: str, module: object) -> list[str]:
    lines = [f"{name} module {module.__name__}"]  # type: ignore[attr-defined]
    for attr, value in sorted(vars(module).items()):
        if attr.startswith("_") or attr == "annotations" or inspect.ismodule(value):
            continue
        if callable(value) and getattr(value, "__module__", None) != module.__name__:  # type: ignore[attr-defined]
            continue
        lines.append(f"{name}.{attr} {_member(value)}")
    return lines


def describe() -> str:
    """The public surface of ``minimal.api`` as sorted, stable text."""
    lines: list[str] = []
    for name in sorted(api.__all__):
        obj = getattr(api, name)
        if name == "__version__":
            lines.append("__version__ str")
        elif inspect.ismodule(obj):
            lines += _module_lines(name, obj)
        elif inspect.isclass(obj):
            lines += _class_lines(name, obj)
        elif callable(obj):
            lines.append(f"{name} def{_signature(obj)}")
        else:
            lines.append(f"{name} = {_value(obj)}")
    lines += [f"REMOVED {key} {version}" for key, (version, _) in sorted(api.REMOVED.items())]
    return "\n".join(lines) + "\n"


class ApiSurfaceTest(unittest.TestCase):
    """minimal.api does not change while the API is frozen."""

    def test_public_api_matches_the_frozen_snapshot(self):
        surface = describe()
        if os.environ.get("PINNED_API_WRITE"):
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(surface, encoding="utf-8")
        self.maxDiff = None
        self.assertEqual(surface, GOLDEN.read_text(encoding="utf-8"), FROZEN)

    def test_every_exported_name_resolves_and_none_was_removed(self):
        for name in api.__all__:
            self.assertTrue(hasattr(api, name), name)
        self.assertTrue(set(api.__all__).isdisjoint(api.REMOVED))
