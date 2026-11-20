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

from textual.theme import Theme

from minimal.extensions import palette
from minimal.extensions.registry import ThemeSpec
from minimal.glyphs import (  # noqa: F401 (the UI modules use them as theme.<name>)
    COLLAPSED,
    EXPANDED,
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

DEFAULT = palette.DEFAULT


def build(spec: ThemeSpec) -> Theme:
    """The Textual theme for ``spec``, with the custom variables the UI's CSS and text use."""
    c = palette.complete(spec.dark, spec.colors)
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


MINIMAL = build(palette.BUILTIN)


def available(themes: Mapping[str, ThemeSpec]) -> dict[str, Theme]:
    """The themes to offer: the built-in one first, then ``themes`` (extension themes by name) except one named like it."""
    return {DEFAULT: MINIMAL, **{name: build(spec) for name, spec in themes.items() if name != DEFAULT}}

