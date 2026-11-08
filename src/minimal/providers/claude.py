"""Claude through a Claude Pro/Max subscription, using the browser login
from :mod:`minimal.providers.claude_auth` instead of an API key.
"""

from __future__ import annotations

from typing import Any

from minimal.providers import claude_auth
from minimal.providers.anthropic import AnthropicProvider
from minimal.providers.base import ProviderSpec

CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."


class ClaudeProvider(AnthropicProvider):
    """Anthropic provider authenticated with the stored subscription login."""

    name = "claude"
    identity = CLAUDE_CODE_IDENTITY

    def __init__(self, client: Any = None) -> None:
        if client is None:
            if claude_auth.load() is None:
                raise ValueError(claude_auth.NOT_LOGGED_IN)
            import anthropic

            client = anthropic.Anthropic(credentials=claude_auth.TokenProvider())
        super().__init__(client)


SPEC = ProviderSpec(
    name="claude",
    create=ClaudeProvider,
    default_model="claude-sonnet-5-5",
    env_keys=(),
    description="Claude Pro/Max subscription (minimal --login)",
)
