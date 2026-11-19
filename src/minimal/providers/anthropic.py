"""Anthropic (Claude) provider, using the ``anthropic`` SDK.

The canonical message format is already the Anthropic one, so the conversion (:func:`to_request_messages`) is
dropping ``provider_data`` and ``usage`` (the API rejects unknown keys) and the ``thinking`` blocks the request
cannot carry: Anthropic wants back, exactly as received, the signed thinking of the replies it made, and only when
the request has extended thinking on. ``reasoning_effort`` (a request option) turns extended thinking on, with a token
budget of :data:`THINKING_BUDGETS` ``[effort]``; thinking is left off when ``max_tokens`` is too small for the
smallest budget (1024).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from minimal.providers.base import ProviderSpec, Reply, TextCallback, Usage

MAX_TOKENS = 8192

THINKING_BUDGETS = {"low": 1024, "medium": 4096, "high": 16000}
"""Extended-thinking token budget for each ``reasoning_effort``."""

MIN_THINKING_BUDGET = 1024
"""The smallest ``budget_tokens`` the API accepts."""


def thinking_request(effort: object, limit: int | None) -> tuple[dict | None, int]:
    """The ``thinking`` parameter for ``effort`` and the ``max_tokens`` to send, given the output limit ``limit`` asked for.

    Thinking tokens count toward ``max_tokens`` and the budget must stay below it. Without a limit of the user's, the
    budget comes on top of :data:`MAX_TOKENS`; with one, the budget is at most half of it. The parameter is ``None`` (no
    thinking) for an unknown ``effort`` or when half the limit is under :data:`MIN_THINKING_BUDGET`.
    """
    budget = THINKING_BUDGETS.get(effort) if isinstance(effort, str) else None
    if budget is None:
        return None, limit or MAX_TOKENS
    if limit is None:
        return {"type": "enabled", "budget_tokens": budget}, MAX_TOKENS + budget
    budget = min(budget, limit // 2)
    if budget < MIN_THINKING_BUDGET:
        return None, limit
    return {"type": "enabled", "budget_tokens": budget}, limit


def to_request_messages(messages: list[dict], thinking: bool) -> list[dict]:
    """The canonical history as Messages API messages, without modifying it.

    ``provider_data`` and ``usage`` are left out. ``thinking`` blocks go only if ``thinking`` is true (the request has
    extended thinking on) and the block has a signature (reasoning another provider showed has none); a
    ``redacted_thinking`` block goes with them. A message left with no content is left out.
    """
    request = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            content = [
                b
                for b in content
                if b.get("type") not in ("thinking", "redacted_thinking")
                or (thinking and (b.get("signature") if b["type"] == "thinking" else b.get("data")))
            ]
            if not content:
                continue
        request.append({"role": message["role"], "content": content})
    return request


class AnthropicProvider:
    """Streams replies from the Anthropic Messages API.

    Credentials and endpoint come from the SDK's own environment variables
    (``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL``). Every request asks for
    prompt caching, so the tools, the system prompt and the history sent again
    on the next request are read from the cache.
    """

    name = "anthropic"
    identity: str | None = None
    """If set, sent as its own first system block (Claude subscription tokens require it)."""

    def __init__(self, client: Any = None) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client

    def list_models(self) -> list[str]:
        """Model ids the account can use, newest first, as reported by the API."""
        return [m.id for m in self.client.models.list(limit=100)]

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
        thinking, max_tokens = thinking_request(options.get("reasoning_effort"), options.get("max_tokens"))
        request = to_request_messages(messages, thinking is not None)
        system_param: str | list[dict] = (
            system
            if self.identity is None
            else [{"type": "text", "text": self.identity}, {"type": "text", "text": system}]
        )
        extra: dict[str, Any] = {}
        if thinking is not None:
            extra["thinking"] = thinking  # the API allows no temperature with extended thinking
        elif options.get("temperature") is not None:
            extra["temperature"] = options["temperature"]
        with self.client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            **extra,
            system=system_param,
            messages=request,
            tools=tools,
            cache_control={"type": "ephemeral"},  # automatic: the cache point is the last cacheable block
        ) as stream:
            if on_text:
                for chunk in stream.text_stream:
                    on_text(chunk)
            message = stream.get_final_message()
        content = [block.model_dump(exclude_none=True) for block in message.content]
        usage = None
        u = getattr(message, "usage", None)
        if u is not None:
            # The API leaves cached tokens out of input_tokens; Usage.input_tokens is the whole prompt.
            read = u.cache_read_input_tokens or 0
            write = u.cache_creation_input_tokens or 0
            usage = Usage(
                input_tokens=u.input_tokens + read + write,
                output_tokens=u.output_tokens,
                cache_read_tokens=read,
                cache_write_tokens=write,
            )
        return Reply(content=content, stop_reason=message.stop_reason or "end_turn", usage=usage)


SPEC = ProviderSpec(
    name="anthropic",
    create=AnthropicProvider,
    default_model="claude-sonnet-5-5",
    env_keys=("ANTHROPIC_API_KEY",),
    description="Claude models via the Anthropic API",
)
