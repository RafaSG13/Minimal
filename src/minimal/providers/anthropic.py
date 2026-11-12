"""Anthropic (Claude) provider, using the ``anthropic`` SDK.

The canonical message format is already the Anthropic one, so the only
conversion is dropping ``provider_data`` and ``usage`` (the API rejects unknown keys).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from minimal.providers.base import ProviderSpec, Reply, TextCallback, Usage

MAX_TOKENS = 8192


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
        request = [{"role": m["role"], "content": m["content"]} for m in messages]
        system_param: str | list[dict] = (
            system
            if self.identity is None
            else [{"type": "text", "text": self.identity}, {"type": "text", "text": system}]
        )
        extra = {"temperature": options["temperature"]} if options.get("temperature") is not None else {}
        with self.client.messages.stream(
            model=model,
            max_tokens=options.get("max_tokens", MAX_TOKENS),
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
