"""Look of the terminal UI: the colour themes and the glyphs.

The built-in theme is ``minimal`` (:data:`DEFAULT`): black and white, nothing
else. Extensions add more with ``minimal.register_theme`` (see
:meth:`~minimal.extensions.registry.Registry.register_theme`); the marketplace keeps them
in its ``themes/`` folder. Pick one with ``/theme`` or ctrl+p → Theme; the
choice is saved as ``theme`` in ``~/.minimal/settings.json``.

:func:`build` turns a theme's colours into a Textual theme: the standard
variables (``$primary``, ``$success``, …) plus custom ones (``$muted``,
``$user-message-bg``, ``$tool-success-bg``, …), which extension widgets can use
too. The UI only paints with these variables, so switching theme recolours
everything.
"""

from __future__ import annotations

from collections.abc import Mapping

from textual.color import Color
from textual.theme import Theme

from minimal.extensions.registry import ThemeSpec
from minimal.glyphs import (  # noqa: F401 (the UI modules use them as theme.<name>)
    BRANCH,
    CURSOR,
    DOT,
    ERROR,
    FULL_BLOCK,
    HEADER_NODE,
    LOWER_HALF,
    MODEL,
    PROMPT,
    RUNNING,
    SEPARATOR,
    SPINNER,
    SUCCESS,
    UPPER_HALF,
    WARNING,
)

DEFAULT = "minimal"

_SEMANTIC = {
    True: {"success": "#89d281", "warning": "#e5c07b", "error": "#e06c75"},
    False: {"success": "#1a7f37", "warning": "#9a6700", "error": "#cf222e"},
}
"""Default state colours for dark (``True``) and light themes."""


def _complete(dark: bool, colors: dict[str, str]) -> dict[str, str]:
    """``colors`` with every colour :meth:`Registry.register_theme` leaves optional filled in."""
    c = dict(colors)
    background, foreground = Color.parse(c["background"]), Color.parse(c["foreground"])

    def mix(amount: float, toward: str | None = None) -> str:
        return background.blend(Color.parse(toward) if toward else foreground, amount).hex

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


def build(spec: ThemeSpec) -> Theme:
    """The Textual theme for ``spec``, with the custom variables the UI's CSS and text use."""
    c = _complete(spec.dark, spec.colors)
    background = c["background"]
    headings = {
        f"markdown-h{n}-{key}": value for n in range(1, 5) for key, value in (("color", c["accent"]), ("text-style", "bold"))
    }
    return Theme(
        name=spec.name,
        primary=c["accent"],
        secondary=c["secondary"],
        accent=c["accent"],
        warning=c["warning"],
        error=c["error"],
        success=c["success"],
        foreground=c["foreground"],
        background=background,
        surface=c["surface"],
        panel=c["panel"],
        dark=spec.dark,
        variables={
            "muted": c["muted"],
            "dim": c["dim"],
            # Behind modals: a 30% wash of the background on dark themes, a faint shade on light ones.
            "modal-overlay": f"{background}4d" if spec.dark else f"{c['foreground']}1f",
            "border-accent": c["secondary"],
            "border-blue": c["focus"],
            "border-muted": c["border"],
            "user-message-bg": c["user_bg"],
            "tool-pending-bg": c["pending_bg"],
            "tool-success-bg": c["success_bg"],
            "tool-error-bg": c["error_bg"],
            "diff-added": c["diff_added"],
            "diff-removed": c["diff_removed"],
            "md-code": c["code"],
            "status-model": c["model"],
            "status-path": c["path"],
            "status-bg": c["status_bg"],
            "status-git-clean": c["git_clean"],
            "status-git-dirty": c["git_dirty"],
            **headings,
            "scrollbar": c["border"],
            "scrollbar-hover": c["muted"],
            "scrollbar-active": c["accent"],
            "scrollbar-background": background,
            "scrollbar-background-hover": background,
            "scrollbar-background-active": background,
            "scrollbar-corner-color": background,
            "input-selection-background": c["selection"],
        },
    )


MINIMAL = build(
    ThemeSpec(
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
)


def available(themes: Mapping[str, ThemeSpec]) -> dict[str, Theme]:
    """The themes to offer: the built-in one first, then ``themes`` (extension themes by name) except one named like it."""
    return {DEFAULT: MINIMAL, **{name: build(spec) for name, spec in themes.items() if name != DEFAULT}}

