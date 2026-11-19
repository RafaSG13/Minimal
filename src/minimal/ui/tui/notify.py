"""The terminal notification the UI emits when a long turn ends while the window is in the background (``ui.notify``)."""

from __future__ import annotations

NOTIFY_MODES = ("off", "bell", "osc9")
"""Values of the ``ui.notify`` setting."""

DEFAULT_AFTER = 10.0
"""Seconds a turn must have taken to notify when ``ui.notify_after`` is not set."""


def sequence(mode: str, elapsed: float) -> str:
    """The characters that make the terminal notify: a bell, or an OSC 9 desktop notification (iTerm2, WezTerm,
    Kitty, Ghostty and others). Empty for ``"off"``."""
    if mode == "bell":
        return "\a"
    if mode == "osc9":
        return f"\x1b]9;minimal: finished after {elapsed:.0f}s\x07"
    return ""
