"""Look of the terminal UI: the colour theme and the glyphs.

The palette is omp's (oh-my-pi) ``dark`` theme. :data:`THEME` exposes it to
Textual CSS: the standard variables (``$primary``, ``$success``, …) plus the
custom ones listed in its ``variables`` (``$user-message-bg``,
``$tool-success-bg``, …), which extension widgets can use too.
"""

from __future__ import annotations

from textual.theme import Theme

ACCENT = "#febc38"
BLUE = "#178fb9"
CYAN = "#0088fa"
GREEN = "#89d281"
RED = "#fc3a4b"
YELLOW = "#e4c00f"
GRAY = "#777d88"
DIM_GRAY = "#5f6673"
DARK_GRAY = "#3d424a"
STATUS_MODEL = "#d787af"
STATUS_PATH = "#00afaf"
STATUS_GIT_CLEAN = "#5faf5f"
STATUS_GIT_DIRTY = "#d7af5f"
BACKGROUND = "#0f0f14"

THEME = Theme(
    name="minimal-dark",
    primary=ACCENT,
    secondary=CYAN,
    accent=ACCENT,
    warning=YELLOW,
    error=RED,
    success=GREEN,
    foreground="#cccccc",
    background=BACKGROUND,
    surface="#1a1a22",
    panel="#25252f",
    dark=True,
    variables={
        "muted": GRAY,
        "dim": DIM_GRAY,
        "border-accent": CYAN,
        "border-blue": BLUE,
        "border-muted": DARK_GRAY,
        "user-message-bg": "#221d1a",
        "tool-pending-bg": "#1d2129",
        "tool-success-bg": "#161a1f",
        "tool-error-bg": "#291d1d",
        "md-code": "#e5c1ff",
        "status-model": STATUS_MODEL,
        "status-path": STATUS_PATH,
        "status-bg": "#16161c",
        "status-git-clean": STATUS_GIT_CLEAN,
        "status-git-dirty": STATUS_GIT_DIRTY,
        "markdown-h1-color": ACCENT,
        "markdown-h1-text-style": "bold",
        "markdown-h2-color": ACCENT,
        "markdown-h2-text-style": "bold",
        "markdown-h3-color": ACCENT,
        "markdown-h3-text-style": "bold",
        "markdown-h4-color": ACCENT,
        "markdown-h4-text-style": "bold",
        "scrollbar": DARK_GRAY,
        "scrollbar-hover": GRAY,
        "scrollbar-active": ACCENT,
        "scrollbar-background": BACKGROUND,
        "scrollbar-background-hover": BACKGROUND,
        "scrollbar-background-active": BACKGROUND,
        "scrollbar-corner-color": BACKGROUND,
        "input-selection-background": "#31363f",
    },
)

# Glyphs (omp's unicode symbol preset).
SUCCESS = "✔"
ERROR = "✘"
WARNING = "⚠"
RUNNING = "⟳"
PROMPT = "❯"
MODEL = "⬢"
BRANCH = "⎇"
SEPARATOR = "▕"
DOT = " · "
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
