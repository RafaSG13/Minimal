"""The symbols minimal draws with, for any frontend: plain strings, no UI toolkit.

Extensions get this module as ``minimal.api.theme`` (and ``minimal.api.glyphs``). The colour
themes themselves belong to the terminal UI (:mod:`minimal.ui.tui.theme`); its names that used to
be reachable as ``minimal.api.theme.<name>`` (``DEFAULT``, ``MINIMAL``, ``build``, ``available``)
still are, loaded from the terminal UI on first use.
"""

from __future__ import annotations

import importlib

# omp's unicode symbol preset.
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
UPPER_HALF = "▀"
LOWER_HALF = "▄"
FULL_BLOCK = "█"
HEADER_NODE = "─●─"
CURSOR = "▌"

_TUI_THEME = frozenset({"DEFAULT", "MINIMAL", "build", "available"})


def __getattr__(name: str) -> object:
    """The terminal UI's theme names (they need Textual), imported only when asked for."""
    if name in _TUI_THEME:
        return getattr(importlib.import_module("minimal.ui.tui.theme"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
