"""Session outline: your prompts in this session, numbered, one line each, newest last."""

from __future__ import annotations

__version__ = "1.0"
__api__ = "2"

ROWS = 10
"""Most prompts listed: the latest ones."""

WIDTH = 31
"""Columns of a row, number included; the row fills the sidebar panel."""


def _prompt_text(message: dict) -> str | None:
    """The text a user message carries, or None for one holding only tool results."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        return "\n".join(texts) if texts else None
    return None


def _prompts(messages: list[dict]) -> list[str]:
    """Each prompt of the user, as its first non-empty line with runs of whitespace collapsed."""
    prompts = []
    for message in messages:
        if message.get("role") != "user":
            continue
        text = _prompt_text(message)
        if not text or not text.strip():
            continue
        first = next(line for line in text.splitlines() if line.strip())
        prompts.append(" ".join(first.split()))
    return prompts


def render(ctx):
    prompts = _prompts(ctx.messages)
    if not prompts:
        return None
    lines = [[("PROMPTS", "bold muted"), (f"  {len(prompts)}", "dim")]]
    start = max(0, len(prompts) - ROWS)
    if start:
        lines.append([(f"… {start} earlier", "dim")])
    digits = len(str(len(prompts)))
    for number, text in enumerate(prompts[start:], start + 1):
        room = WIDTH - digits - 1
        if len(text) > room:
            text = text[: room - 1] + "…"
        style = "foreground" if number == len(prompts) else "muted"
        lines.append([(str(number).rjust(digits), "accent"), " ", (text, style)])
    return lines


def register(minimal):
    minimal.register_panel("session_outline", render, area="sidebar")
