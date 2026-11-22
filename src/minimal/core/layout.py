"""The terminal UI's layout and key map, resolved from what extensions registered and what the user set.

Extensions place widgets and panels in areas (:data:`~minimal.extensions.registry.WIDGET_AREAS`), add status-line
segments and bind keys; the user overrides any of that without writing code: ``ui.widgets``, ``ui.status`` and
``ui.keys`` in the settings (see :mod:`minimal.config`), or ``/ui``. :func:`resolve` and :func:`resolve_keys` merge
the two into what the UI draws and binds, and report what the user asked for that cannot be honoured. Nothing
here imports a UI toolkit: the terminal UI and ``/ui`` both read these results.

Widgets and panels of an area, and the segments of a status side, are stacked by ``order`` (lower first), then in
registration order. minimal's own status segments (:data:`BUILTIN_SEGMENTS`) take part like extension segments: they
can be hidden, moved or reordered, and an extension segment of the same name replaces one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from minimal import config
from minimal.extensions.registry import (
    KEY_ACTIONS,
    RESERVED_KEYS,
    KeybindingSpec,
    Registry,
    normalize_key,
)

BUILTIN_SEGMENTS: dict[str, tuple[str, int, str]] = {
    "model": ("left", -40, "the model and its provider"),
    "path": ("left", -30, "the working directory"),
    "git": ("left", -20, "the git branch"),
    "mode": ("left", -10, "the current mode"),
    "context": ("right", 100, "tokens in the context"),
    "extensions": ("right", 105, "how many extensions are loaded"),
    "session": ("right", 110, "the session id"),
}
"""minimal's own status-line segments: ``name -> (side, order, what it shows)``."""

KEY_DESCRIPTIONS = {
    "interrupt": "interrupt the turn in progress",
    "toggle_sidebar": "show or hide the sidebars",
    "cycle_mode": "go to the next mode",
}
"""What each of :data:`~minimal.extensions.registry.KEY_ACTIONS` does."""


@dataclass(frozen=True)
class Placed:
    """A widget, panel or status segment as it will be drawn.

    Attributes:
        kind: ``"widget"``, ``"panel"`` or ``"status"``.
        name: Its name.
        place: Its area (``"top"``, ``"sidebar"``, …), or the side of the status line.
        order: Its position among the others of its place, lower first.
        enabled: ``False`` if the user hid it (or ``ui.sidebar`` is ``hidden`` and it is in the sidebar).
        source: ``"extension"`` or ``"built-in"``.
        overridden: Which of ``area``/``side``, ``order``, ``enabled``, ``title``, ``collapsed`` and ``height`` the user's settings changed.
        spec: The extension's spec; ``None`` for a built-in segment.
        note: Why it is not shown, besides the user hiding it.
        seq: Registration sequence, breaking ties of ``order``.
        title: The heading drawn above a widget or panel (the user's ``ui.widgets`` ``title`` over the extension's); ``None`` or
            ``""`` for none. Always ``None`` for a status segment.
        collapsible: Whether a click or Enter on the heading collapses it (the extension's ``collapsible``).
        collapsed: Whether it starts collapsed to its heading (the user's ``collapsed``).
        height: Rows the user gave it (``ui.widgets`` ``height``); ``None`` keeps the widget's own height. API 2.4.
    """

    kind: str
    name: str
    place: str
    order: int
    enabled: bool
    source: str
    overridden: tuple[str, ...] = ()
    spec: Any = None
    note: str = ""
    seq: int = 0
    title: str | None = None
    collapsible: bool = False
    collapsed: bool = False
    height: int | None = None


@dataclass(frozen=True)
class Layout:
    """What :func:`resolve` found: every placement, and the problems with the user's overrides."""

    placements: tuple[Placed, ...]
    problems: tuple[str, ...]
    sidebar: str
    """``ui.sidebar``: ``right``, ``left`` or ``hidden``."""

    def in_area(self, area: str) -> list[Placed]:
        """The enabled widgets and panels of ``area``, in the order they are stacked."""
        return self._sorted(p for p in self.placements if p.kind != "status" and p.place == area and p.enabled)

    def segments(self, side: str) -> list[Placed]:
        """The enabled status segments of ``side``, in order."""
        return self._sorted(p for p in self.placements if p.kind == "status" and p.place == side and p.enabled)

    @staticmethod
    def _sorted(placements: Any) -> list[Placed]:
        return sorted(placements, key=lambda p: (p.order, p.seq))


def _override_problems(key: str, overrides: dict[str, Any], known: Any, what: str) -> list[str]:
    return [
        f"{key}: no {what} called {name!r} (known: {', '.join(sorted(known)) or 'none'})"
        for name in overrides
        if name not in known
    ]


def resolve(registry: Registry, settings: config.Settings) -> Layout:
    """The layout of the widgets, panels and status segments in ``registry`` with the user's ``ui.*`` overrides applied."""
    widget_over = settings.ui_map("ui.widgets")
    status_over = settings.ui_map("ui.status")
    sidebar = settings.ui_sidebar
    placements: list[Placed] = []
    for kind, specs in (("widget", registry.widgets), ("panel", registry.panels)):
        for name, spec in specs.items():
            over = widget_over.get(name, {})
            area = over.get("area", spec.area)
            enabled, note = over.get("enabled", True), ""
            if enabled and area == "sidebar" and sidebar == "hidden":
                enabled, note = False, "ui.sidebar is hidden"
            placements.append(
                Placed(
                    kind, name, area, over.get("order", spec.order), enabled, "extension",
                    tuple(f for f in ("area", "order", "enabled", "title", "collapsed", "height") if f in over), spec, note,
                    registry.placement_index(kind, name),
                    over["title"] if "title" in over else spec.title, spec.collapsible, over.get("collapsed", False),
                    over.get("height"),
                )
            )  # fmt: skip
    for name, (side, order, _) in BUILTIN_SEGMENTS.items():
        if name in registry.status:
            continue
        over = status_over.get(name, {})
        placements.append(
            Placed(
                "status", name, over.get("side", side), over.get("order", order), over.get("enabled", True),
                "built-in", tuple(f for f in ("side", "order", "enabled") if f in over), None, "", -1,
            )
        )  # fmt: skip
    for name, status in registry.status.items():
        over = status_over.get(name, {})
        placements.append(
            Placed(
                "status", name, over.get("side", status.side), over.get("order", status.order), over.get("enabled", True),
                "extension", tuple(f for f in ("side", "order", "enabled") if f in over), status, "",
                registry.placement_index("status", name),
            )
        )  # fmt: skip
    problems = _override_problems("ui.widgets", widget_over, {*registry.widgets, *registry.panels}, "widget or panel")
    problems += _override_problems("ui.status", status_over, {*registry.status, *BUILTIN_SEGMENTS}, "status segment")
    return Layout(tuple(placements), tuple(problems), sidebar)


@dataclass(frozen=True)
class Bound:
    """A key action or extension keybinding with the key it ends up on.

    Attributes:
        id: The action name (``cycle_mode``), or ``<extension id>:<name>`` for an extension's keybinding; what
            ``ui.keys`` calls it.
        key: The effective key; ``None`` if unbound (by the user, or its key could not be used).
        default: The key it has without ``ui.keys``.
        description: What it does.
        spec: The extension's binding; ``None`` for a built-in action.
        overridden: Whether ``ui.keys`` set its key.
    """

    id: str
    key: str | None
    default: str
    description: str
    spec: KeybindingSpec | None = None
    overridden: bool = False

    @property
    def builtin(self) -> bool:
        """Whether it is one of minimal's own actions (:data:`~minimal.extensions.registry.KEY_ACTIONS`)."""
        return self.spec is None


def _user_key(id: str, raw: str, default: str, builtin: bool) -> str | None:
    """The key ``ui.keys`` asks for ``id``: ``None`` for ``""`` (unbound). Raises ``ValueError`` if it cannot be used."""
    what = f"ui.keys[{id}]"
    if not raw.strip():
        return None
    key = normalize_key(raw, what) if raw.strip().lower() != default else default
    if key in RESERVED_KEYS and not (builtin and key == default):
        raise ValueError(f"{what}: {key} is reserved for minimal; choose another key")
    return key


def resolve_keys(registry: Registry, settings: config.Settings) -> tuple[list[Bound], list[str]]:
    """``(bindings, problems)``: minimal's key actions, then the extensions' keybindings, each on its effective key.

    ``ui.keys`` re-keys any of them by id. A key the user asks for that is malformed, reserved or already taken
    is a problem, and the binding keeps its default key (an extension's is unbound if that is taken too). Keys
    that bindings have by default are claimed first, minimal's own actions before extensions', then the keys
    the user asked for, so a re-keying never takes a key from a binding that has it by default.
    """
    user = settings.ui_map("ui.keys")
    problems: list[str] = []
    entries: list[tuple[str, str, str, KeybindingSpec | None]] = [
        (action, default, KEY_DESCRIPTIONS[action], None) for action, default in KEY_ACTIONS.items()
    ]
    entries += [(spec.id, spec.key, spec.description or spec.name, spec) for spec in registry.keybindings.values()]
    asked: dict[str, str | None] = {}
    for id, default, _, spec in entries:
        if id in user:
            try:
                asked[id] = _user_key(id, user[id], default, spec is None)
            except ValueError as e:
                problems.append(str(e))
    taken: dict[str, str] = {}
    keys: dict[str, str | None] = {}
    for id, default, _, _ in entries:
        if id not in asked:
            if default in taken:
                problems.append(f"{id}: key {default} is already used by {taken[default]}; it is unbound")
                keys[id] = None
            else:
                keys[id] = default
                taken[default] = id
    for id, default, _, _ in entries:
        if id not in asked:
            continue
        key = asked[id]
        if key is not None and key in taken:
            problems.append(f"{id}: key {key} is already used by {taken[key]}")
            key = default if key != default and default not in taken else None
            del asked[id]
        keys[id] = key
        if key is not None:
            taken[key] = id
    bound = [Bound(id, keys[id], default, description, spec, id in asked) for id, default, description, spec in entries]
    problems += _override_problems("ui.keys", user, {b.id for b in bound}, "key action or extension keybinding")
    return bound, problems
