"""OpenAI-compatible provider, using the ``openai`` SDK and the Chat Completions API.

Chat Completions is the API that OpenAI-compatible servers implement, so
``OPENAI_BASE_URL`` points this provider at OpenRouter, Ollama, LM Studio and
the like. Mapping between the canonical format and chat messages:

==============================  =============================================
canonical                       chat message
==============================  =============================================
system prompt                   ``system`` message, sent first
user text                       ``user`` message
assistant ``text`` blocks       ``content`` of one ``assistant`` message
assistant ``tool_use``          entry in that message's ``tool_calls``
                                (``id``, ``function.name``, ``function.arguments`` as JSON)
user ``tool_result``            ``tool`` message (``tool_call_id``, ``content``)
==============================  =============================================

No output-token limit is sent: the name of that parameter differs between
servers. Usage is requested in the stream (``stream_options``), which the
server must support.

Credentials: ``OPENAI_API_KEY`` (read by the SDK; local servers accept any value).
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from minimal.providers.base import ProviderSpec, Reply, TextCallback, Usage, result_text


class OpenAIProvider:
    """Streams replies from an OpenAI-compatible Chat Completions API."""

    name = "openai"

    def __init__(self, client: Any = None) -> None:
        if client is None:
            import openai

            client = openai.OpenAI()
        self.client = client

    def list_models(self) -> list[str]:
        """Model ids the server offers, as reported by the API."""
        return sorted(m.id for m in self.client.models.list())

    def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        on_text: TextCallback | None,
    ) -> Reply:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": to_chat_messages(system, messages),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:  # some servers reject an empty list
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
                }
                for t in tools
            ]
        with self.client.chat.completions.create(**kwargs) as chunks:
            return read_chunks(chunks, on_text)


# --- canonical history -> chat messages ----------------------------------------


def to_chat_messages(system: str, messages: list[dict]) -> list[dict]:
    """Convert the system prompt and the canonical history to Chat Completions messages."""
    chat: list[dict] = [{"role": "system", "content": system}]
    for message in messages:
        content = message["content"]
        if message["role"] == "assistant":
            chat.append(_assistant_message(content))
        elif isinstance(content, str):
            chat.append({"role": "user", "content": content})
        else:
            # Tool messages first: they must directly follow the assistant message that made the calls.
            chat.extend(
                {"role": "tool", "tool_call_id": b["tool_use_id"], "content": result_text(b.get("content", ""))}
                for b in content
                if b["type"] == "tool_result"
            )
            chat.extend({"role": "user", "content": b["text"]} for b in content if b["type"] == "text")
    return chat


def _assistant_message(blocks: list[dict]) -> dict:
    """One assistant chat message for an assistant turn: its text and its tool calls."""
    text = "".join(b["text"] for b in blocks if b["type"] == "text")
    calls = [
        {"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
        for b in blocks
        if b["type"] == "tool_use"
    ]
    if calls:
        return {"role": "assistant", "content": text or None, "tool_calls": calls}
    return {"role": "assistant", "content": text}


# --- streamed chunks -> reply ---------------------------------------------------


def read_chunks(chunks: Iterable[Any], on_text: TextCallback | None) -> Reply:
    """Consume a streamed chat completion and return the canonical :class:`Reply`.

    Tool calls arrive in pieces keyed by ``index``: the id and name once, the
    JSON arguments spread over several chunks. The usage comes in a last
    chunk that has no choices.

    Raises:
        json.JSONDecodeError: if a tool call's arguments are not valid JSON.
    """
    text = ""
    calls: dict[int, dict] = {}
    finish_reason = None
    usage = None
    for chunk in chunks:
        if chunk.usage:
            details = getattr(chunk.usage, "prompt_tokens_details", None)
            usage = Usage(
                input_tokens=chunk.usage.prompt_tokens or 0,
                output_tokens=chunk.usage.completion_tokens or 0,
                cache_read_tokens=getattr(details, "cached_tokens", None) or 0,
            )
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        if choice.delta.content:
            text += choice.delta.content
            if on_text:
                on_text(choice.delta.content)
        for piece in choice.delta.tool_calls or []:
            call = calls.setdefault(piece.index, {"id": None, "name": "", "arguments": ""})
            if piece.id:
                call["id"] = piece.id
            if piece.function is not None:
                call["name"] += piece.function.name or ""
                call["arguments"] += piece.function.arguments or ""
        if choice.finish_reason:
            finish_reason = choice.finish_reason

    content: list[dict] = [{"type": "text", "text": text}] if text else []
    for index in sorted(calls):
        call = calls[index]
        content.append(
            {
                "type": "tool_use",
                "id": call["id"] or f"call_{index}",
                "name": call["name"],
                "input": json.loads(call["arguments"]) if call["arguments"].strip() else {},
            }
        )

    if calls:
        stop_reason = "tool_use"
    elif finish_reason == "length":
        stop_reason = "max_tokens"
    else:
        stop_reason = "end_turn"
    return Reply(content=content, stop_reason=stop_reason, usage=usage)


SPEC = ProviderSpec(
    name="openai",
    create=OpenAIProvider,
    default_model="gpt-5.5",
    env_keys=("OPENAI_API_KEY",),
    description="OpenAI-compatible Chat Completions (OpenAI; OpenRouter, Ollama, LM Studio via OPENAI_BASE_URL)",
)
