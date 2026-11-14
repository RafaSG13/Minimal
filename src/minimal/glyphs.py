"""The symbols minimal draws with, for any frontend: plain strings, no UI toolkit.

Extensions get this module as ``minimal.api.theme`` (and ``minimal.api.glyphs``). The colour
themes belong to the Textual UI, the ``minimal-textual`` package (``minimal_textual.theme``); their
names that used to be reachable here (``DEFAULT``, ``MINIMAL``, ``build``, ``available``) were removed
in API 2.0.
"""

from __future__ import annotations

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


def __getattr__(name: str) -> object:
    """Explain an access to a theme name that moved to ``minimal_textual.theme``."""
    from minimal.api import API_VERSION, REMOVED

    if f"theme.{name}" in REMOVED:
        version, instead = REMOVED[f"theme.{name}"]
        raise AttributeError(
            f"minimal.api.theme.{name} was removed in minimal API {version} (this minimal has API {API_VERSION}); "
            f"use {instead}"
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
