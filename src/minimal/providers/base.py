"""The provider interface: how the agent loop talks to a model API.

A provider turns one request (system prompt, history, tools) into one
:class:`Reply`, streaming text as it arrives. Everything provider-specific
(SDKs, wire formats, auth) stays behind this interface, so adding a new
integration means writing one class and registering one :class:`ProviderSpec`.

Canonical message format
------------------------
History is stored in one provider-neutral format (shaped like the Anthropic
Messages API), so sessions can be resumed and providers switched mid-session::

    {"role": "user", "content": "text"}
    {"role": "assistant", "content": [
        {"type": "text", "text": "..."},
        {"type": "tool_use", "id": "...", "name": "...", "input": {...}},
    ], "provider_data": {"provider": "gemini", "data": ...}}      # optional
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": False},
    ]}

``provider_data`` holds whatever a provider needs to replay its own reply
exactly (for example Gemini's thought signatures). Only the provider named in
it reads it; every other provider rebuilds the turn from ``content``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Protocol

TextCallback = Callable[[str], Any]


@dataclass
class Reply:
    """One model reply in canonical form.

    Attributes:
        content: Canonical content blocks (``text`` and ``tool_use``).
        stop_reason: ``"tool_use"`` when the model wants tools run; otherwise
            why it stopped (``"end_turn"``, ``"max_tokens"``, ...).
        provider_data: Opaque data stored with the message and handed back to
            the same provider on later requests; ``None`` if not needed.
    """

    content: list[dict]
    stop_reason: str
    provider_data: Any = None


class Provider(Protocol):
    """A model API. Implementations live in ``minimal.providers.<name>``."""

    name: str
    """Registry name, also stored in ``provider_data`` to recognise its own data."""

    def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        on_text: TextCallback | None,
    ) -> Reply:
        """Send one request and return the reply.

        Args:
            model: Model name for this provider.
            system: System prompt.
            messages: Canonical history (see module docstring). Must not be modified.
            tools: ``{"name", "description", "input_schema"}`` for each tool.
            on_text: Called with each text chunk as it streams in.
        """
        ...


@dataclass
class ProviderSpec:
    """How to build a provider; registered in the :class:`~minimal.registry.Registry`.

    Attributes:
        name: Name used by ``--provider`` and ``/provider``.
        create: Builds the provider. Should import its SDK lazily.
        default_model: Model used when none is given.
        env_keys: Environment variables holding the credential; at least one must be set.
            Setup saves a key entered by the user under the first one, so list the
            name the SDK prefers first.
        description: One line shown by ``/provider``.
    """

    name: str
    create: Callable[[], Provider]
    default_model: str
    env_keys: tuple[str, ...] = ()
    description: str = ""

    def missing_credentials(self) -> str | None:
        """Return an error message if no credential variable is set, else ``None``."""
        if not self.env_keys or any(os.environ.get(k) for k in self.env_keys):
            return None
        return f"{' or '.join(self.env_keys)} is not set in the environment."
