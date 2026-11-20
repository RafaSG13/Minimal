"""Colour themes without a UI toolkit: the built-in theme and the colours a theme leaves out.

The terminal UI builds its Textual themes from these (:mod:`minimal.ui.tui.theme`); extensions read the result
through ``ctx.theme`` (:class:`~minimal.extensions.registry.ThemeInfo`) and write ``$name`` in styled text.
"""

from __future__ import annotations

from collections.abc import Mapping

from minimal.extensions.registry import THEME_COLORS, ThemeInfo, ThemeSpec

DEFAULT = "minimal"
"""The built-in theme's name."""

_SEMANTIC = {
    True: {"success": "#89d281", "warning": "#e5c07b", "error": "#e06c75"},
    False: {"success": "#1a7f37", "warning": "#9a6700", "error": "#cf222e"},
}
"""Default state colours for dark (``True``) and light themes."""

BUILTIN = ThemeSpec(
    DEFAULT,
    True,
    {
        "background": "#000000",
        "foreground": "#d4d4d4",
        "accent": "#ffffff",
        "surface": "#111111",
        "panel": "#222222",
        "secondary": "#ffffff",
        "success": "#ffffff",
        "warning": "#ffffff",
        "error": "#ffffff",
        "muted": "#8a8a8a",
        "dim": "#5c5c5c",
        "border": "#2a2a2a",
        "focus": "#5c5c5c",
        "user_bg": "#161616",
        "pending_bg": "#0c0c0c",
        "success_bg": "#0c0c0c",
        "error_bg": "#1c1c1c",
        "code": "#ffffff",
        "model": "#d4d4d4",
        "path": "#8a8a8a",
        "status_bg": "#000000",
        "git_clean": "#8a8a8a",
        "git_dirty": "#d4d4d4",
        "selection": "#333333",
        "diff_added": "#ffffff",
        "diff_removed": "#6e6e6e",
    },
)
"""The built-in ``minimal`` theme: black and white, nothing else."""

def _channels(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def blend(start: str, end: str, factor: float) -> str:
    """``start`` moved ``factor`` (0 to 1) of the way to ``end``; both ``#rrggbb``."""
    r1, g1, b1 = _channels(start)
    r2, g2, b2 = _channels(end)
    return _hex(int(r1 + (r2 - r1) * factor), int(g1 + (g2 - g1) * factor), int(b1 + (b2 - b1) * factor))


def complete(dark: bool, colors: Mapping[str, str]) -> dict[str, str]:
    """``colors`` with every colour :meth:`Registry.register_theme` leaves optional derived, all as lowercase ``#rrggbb``."""
    c = {key: value.lower() for key, value in colors.items()}
    background, foreground = c["background"], c["foreground"]

    def mix(amount: float, toward: str | None = None) -> str:
        return blend(background, toward or foreground, amount)

    for key, value in _SEMANTIC[dark].items():
        c.setdefault(key, value)
    c.setdefault("surface", mix(0.05))
    c.setdefault("panel", mix(0.10))
    c.setdefault("secondary", c["accent"])
    c.setdefault("muted", mix(0.55))
    c.setdefault("dim", mix(0.38))
    c.setdefault("border", mix(0.15))
    c.setdefault("focus", c["accent"])
    c.setdefault("user_bg", mix(0.07))
    c.setdefault("pending_bg", c["surface"])
    c.setdefault("success_bg", c["surface"])
    c.setdefault("error_bg", mix(0.12, c["error"]))
    c.setdefault("code", c["accent"])
    c.setdefault("model", c["accent"])
    c.setdefault("path", c["secondary"])
    c.setdefault("status_bg", c["background"])
    c.setdefault("git_clean", c["success"])
    c.setdefault("git_dirty", c["warning"])
    c.setdefault("selection", mix(0.30, c["accent"]))
    c.setdefault("diff_added", c["success"])
    c.setdefault("diff_removed", c["error"])
    return c


def theme_info(spec: ThemeSpec) -> ThemeInfo:
    """What ``ctx.theme`` reports for ``spec``: every colour resolved (``background``, ``foreground``, ``accent`` and each of :data:`THEME_COLORS`)."""
    resolved = complete(spec.dark, spec.colors)
    keys = ("background", "foreground", "accent", *THEME_COLORS)
    return ThemeInfo(spec.name, spec.dark, {key: resolved[key] for key in keys})
