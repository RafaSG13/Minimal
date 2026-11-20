"""What ``/copy`` and ``/export`` take from a session: the assistant's texts, and the whole conversation as Markdown."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

from minimal.core.mentions import split_attachments
from minimal.providers.base import result_text

CALL_CHARS = 300
"""Characters of a tool call's input shown by :func:`session_markdown`."""

RESULT_CHARS = 800
"""Characters of a tool result shown by :func:`session_markdown`."""


def assistant_texts(messages: list[dict]) -> list[str]:
    """The text of each assistant message that has any, oldest first."""
    texts = []
    for message in messages:
        if message["role"] != "assistant":
            continue
        content = message["content"]
        text = content if isinstance(content, str) else "".join(
            b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
        if text.strip():
            texts.append(text)
    return texts


def _fence(text: str, info: str = "text") -> str:
    """``text`` in a code fence long enough not to be closed by backticks inside it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text}\n{fence}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}\n… [{len(text) - limit} more characters]"


def _block(block: Any, names: dict[str, str]) -> str:
    """One content block as Markdown; tool calls and results are summarised, images and thinking are placeholders."""
    if not isinstance(block, dict):
        return ""
    kind = block.get("type")
    if kind == "text":
        return block.get("text", "")
    if kind == "tool_use":
        names[block.get("id", "")] = block.get("name", "?")
        args = json.dumps(block.get("input", {}), ensure_ascii=False)
        return f"**Tool call** `{block.get('name', '?')}`:\n\n{_fence(_clip(args, CALL_CHARS), 'json')}"
    if kind == "tool_result":
        name = names.get(block.get("tool_use_id", ""), "?")
        content = block.get("content", "")
        blocks = [] if isinstance(content, str) else content
        images = sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "image")
        text = _clip(result_text(content), RESULT_CHARS)
        status = "error" if block.get("is_error") else "result"
        parts = [f"**Tool {status}** `{name}`:"]
        if text:
            parts.append(_fence(text))
        parts.extend("*[image]*" for _ in range(images))
        return "\n\n".join(parts)
    if kind == "image":
        return "*[image]*"
    if kind in ("thinking", "redacted_thinking"):
        return "*[thinking omitted]*"
    return f"*[{kind} block]*"


def session_markdown(messages: list[dict], session_id: str, title: str = "", cwd: str = "") -> str:
    """The conversation as a Markdown document: a heading per speaker, tool calls and results summarised
    (cut to :data:`CALL_CHARS` and :data:`RESULT_CHARS` characters), images and thinking as placeholders."""
    lines = [f"# {title or 'minimal session'}", "", f"- Session: `{session_id}`"]
    if cwd:
        lines.append(f"- Directory: `{cwd}`")
    lines += [f"- Messages: {len(messages)}", ""]
    names: dict[str, str] = {}
    speaker = ""
    for message in messages:
        content = message["content"]
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
        parts = []
        for block in blocks:
            if message["role"] == "user" and isinstance(block, dict) and block.get("type") == "text":
                body, paths = split_attachments(block.get("text", ""))
                parts.append(body + (f"\n\n*Attached: {', '.join(f'`{p}`' for p in paths)}*" if paths else ""))
            else:
                parts.append(_block(block, names))
        body = "\n\n".join(p for p in parts if p.strip())
        if not body:
            continue
        who = "Assistant" if message["role"] == "assistant" else "You"
        if message["role"] == "user" and not any(isinstance(b, dict) and b.get("type") == "text" for b in blocks):
            who = speaker or who  # only tool results: they belong to the assistant's turn
        if who != speaker:
            lines += [f"## {who}", ""]
            speaker = who
        lines += [body, ""]
    return "\n".join(lines)


def native_copy(text: str) -> str | None:
    """Put ``text`` on the system clipboard with ``pbcopy``, ``wl-copy``, ``xclip`` or ``xsel`` if one is installed.

    Returns:
        The name of the tool that did it, or ``None`` if none is available or it failed.
    """
    for argv in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        if shutil.which(argv[0]) is None:
            continue
        try:
            # DEVNULL, not pipes: wl-copy and xclip keep running in the background and would hold a pipe open.
            subprocess.run(
                argv,
                input=text.encode("utf-8"),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        return argv[0]
    return None
