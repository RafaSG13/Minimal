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
user or tool ``image`` block    ``image_url`` part (a ``data:`` URL) of a ``user`` message
assistant ``thinking`` block    left out of the request
==============================  =============================================

A tool message holds text only, so the pictures a tool returned follow the tool messages in one ``user`` message.
A server or model without vision rejects images; that is its error to report. The reasoning some servers stream
(``reasoning_content`` or ``reasoning`` in the delta: DeepSeek, OpenRouter, vLLM...) becomes a ``thinking`` block
without a signature in the reply; OpenAI's own Chat Completions API does not stream it.

``reasoning_effort`` (a request option) is sent as is to models that take it (names starting with ``o1``... or
``gpt-5``, also behind a ``vendor/`` prefix) and left out for every other model.

The output-token limit is sent only when the request options set one (``max_tokens``), as
``max_completion_tokens`` to OpenAI itself and as ``max_tokens`` when ``OPENAI_BASE_URL`` (or the
``base_url`` of :func:`openai_compatible`) points elsewhere: the name of that parameter differs between
servers. Usage is requested in the stream (``stream_options``), which the server must support.

Credentials: ``OPENAI_API_KEY`` (read by the SDK; local servers accept any value). A provider made by
:func:`openai_compatible` reads its own variable, or needs none.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any, Iterable

from minimal.providers.base import ProviderSpec, Reply, TextCallback, Usage, image_source, image_summary, result_text


_REASONING_MODEL = re.compile(r"(^|/)(o\d|gpt-5)")
"""Models that accept ``reasoning_effort``."""


class OpenAIProvider:
    """Streams replies from an OpenAI-compatible Chat Completions API.

    Args:
        client: An ``openai.OpenAI``-like client; default: one built from the arguments below.
        name: The provider's name.
        base_url: The server; default: ``$OPENAI_BASE_URL``, else OpenAI.
        api_key: The key; default: ``$OPENAI_API_KEY``.
    """

    def __init__(
        self, client: Any = None, *, name: str = "openai", base_url: str | None = None, api_key: str | None = None
    ) -> None:
        self.name = name
        self.base_url = base_url
        if client is None:
            import openai

            kwargs: dict[str, Any] = {k: v for k, v in (("base_url", base_url), ("api_key", api_key)) if v is not None}
            client = openai.OpenAI(**kwargs)
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
        options: Mapping[str, Any] | None = None,
    ) -> Reply:
        options = options or {}
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
        if options.get("max_tokens") is not None:
            official = not (self.base_url or os.environ.get("OPENAI_BASE_URL"))
            kwargs["max_completion_tokens" if official else "max_tokens"] = options["max_tokens"]
        if options.get("temperature") is not None:
            kwargs["temperature"] = options["temperature"]
        if options.get("reasoning_effort") is not None and _REASONING_MODEL.search(model):
            kwargs["reasoning_effort"] = options["reasoning_effort"]
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
            pictures: list[dict] = []  # what the tools returned: a tool message cannot hold an image
            for b in content:
                if b["type"] == "tool_result":
                    chat.append(_tool_message(b, pictures))
            own = [b for b in content if b["type"] in ("text", "image")]
            if pictures or any(b["type"] == "image" for b in own):
                parts = pictures + [
                    {"type": "text", "text": b["text"]} if b["type"] == "text" else _image_part(b) for b in own
                ]
                chat.append({"role": "user", "content": parts})
            else:
                chat.extend({"role": "user", "content": b["text"]} for b in own)
    return chat


def _tool_message(block: dict, pictures: list[dict]) -> dict:
    """The ``tool`` message for a ``tool_result`` block; the parts for its images are appended to ``pictures``."""
    result = block.get("content", "")
    images = [i for i in result if isinstance(i, dict) and i.get("type") == "image"] if isinstance(result, list) else []
    if not images:
        return {"role": "tool", "tool_call_id": block["tool_use_id"], "content": result_text(result)}
    text = "".join(i.get("text", "") for i in result if i.get("type") == "text")
    text += ("\n" if text else "") + f"[{len(images)} image(s): shown in the next user message]"
    pictures.append({"type": "text", "text": f"Image(s) returned by tool call {block['tool_use_id']}:"})
    pictures.extend(_image_part(i) for i in images)
    return {"role": "tool", "tool_call_id": block["tool_use_id"], "content": text}


def _image_part(block: dict) -> dict:
    """A user-message content part for an ``image`` block: its data as a ``data:`` URL.

    An image that has no base64 data (a shape this provider does not know) becomes text saying what it was.
    """
    source = image_source(block)
    if source.get("type") == "base64" and isinstance(source.get("data"), str):
        return {"type": "image_url", "image_url": {"url": f"data:{source.get('media_type')};base64,{source['data']}"}}
    return {"type": "text", "text": image_summary(block)}


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

    A reply cut off by the output limit (``finish_reason == "length"``), or whose tool call
    arguments are not valid JSON, has ``stop_reason == "max_tokens"`` even when it has tool calls;
    the agent loop does not run those. A call with invalid arguments keeps ``{}`` as its input. Reasoning the server
    streams (see the module docstring) comes first, as a ``thinking`` block.
    """
    text = ""
    reasoning = ""
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
        thought = getattr(choice.delta, "reasoning_content", None) or getattr(choice.delta, "reasoning", None)
        if isinstance(thought, str):
            reasoning += thought
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

    truncated = finish_reason == "length"
    content: list[dict] = [{"type": "thinking", "thinking": reasoning, "signature": None}] if reasoning else []
    if text:
        content.append({"type": "text", "text": text})
    for index in sorted(calls):
        call = calls[index]
        try:
            arguments = json.loads(call["arguments"]) if call["arguments"].strip() else {}
        except ValueError:
            arguments, truncated = {}, True  # cut off in the middle of the arguments: the call cannot run
        content.append(
            {"type": "tool_use", "id": call["id"] or f"call_{index}", "name": call["name"], "input": arguments}
        )

    if truncated:
        stop_reason = "max_tokens"
    elif calls:
        stop_reason = "tool_use"
    else:
        stop_reason = "end_turn"
    return Reply(content=content, stop_reason=stop_reason, usage=usage)


SPEC = ProviderSpec(
    name="openai",
    create=OpenAIProvider,
    default_model="gpt-5.5",
    env_keys=("OPENAI_API_KEY",),
    description="OpenAI-compatible Chat Completions (OpenAI; OpenRouter, Ollama, LM Studio via OPENAI_BASE_URL)",
    sdk="openai",
    install="mnml-agent[openai]",
)


def openai_compatible(
    name: str,
    base_url: str,
    default_model: str,
    *,
    key_env: str | None = None,
    description: str = "",
    context_windows: Mapping[str, int] | None = None,
) -> ProviderSpec:
    """A :class:`ProviderSpec` for one more OpenAI-compatible server, next to ``openai`` and to each other.

    Register it with ``minimal.add_provider(openai_compatible(...))``. Unlike ``openai``, which reads
    ``OPENAI_BASE_URL`` and ``OPENAI_API_KEY`` once per process, each one has its own server and key.

    Args:
        name: Name for ``--provider`` and ``/provider``.
        base_url: The server's API root, e.g. ``"http://localhost:11434/v1"`` (Ollama).
        default_model: Model used when none is given.
        key_env: Environment variable holding the key. ``None``: the server needs none (a local one).
        description: One line shown by ``/provider``.
        context_windows: Context size in tokens by model-name prefix (API 2.3), for ``ctx.context_window``.

    Raises:
        ValueError: if ``name``, ``base_url`` or ``default_model`` is empty.
    """
    if not (name and base_url and default_model):
        raise ValueError("openai_compatible: name, base_url and default_model are required")

    def create() -> OpenAIProvider:
        # The SDK refuses an empty key, but a local server ignores whatever it is sent.
        key = os.environ.get(key_env, "") if key_env else "not-needed"
        return OpenAIProvider(name=name, base_url=base_url, api_key=key)

    return ProviderSpec(
        name=name,
        create=create,
        default_model=default_model,
        env_keys=(key_env,) if key_env else (),
        description=description or f"OpenAI-compatible server at {base_url}",
        sdk="openai",
        install="mnml-agent[openai]",
        context_windows=dict(context_windows or {}),
    )
