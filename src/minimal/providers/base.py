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
    {"role": "user", "content": [
        {"type": "text", "text": "what is in this picture?"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "<base64>"}},
    ]}
    {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "...", "signature": "..." or None},
        {"type": "text", "text": "..."},
        {"type": "tool_use", "id": "...", "name": "...", "input": {...}},
    ], "provider_data": {"provider": "gemini", "data": ...},      # optional
       "usage": {"input_tokens": ..., "output_tokens": ...,       # optional
                 "cache_read_tokens": ..., "cache_write_tokens": ...}}
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "...", "content": "...", "is_error": False},
    ]}

A ``tool_result``'s ``content`` is a string, or a list of ``text`` and ``image`` blocks (a tool that returns a
picture). ``image`` blocks (:data:`IMAGE_TYPES`, :func:`image_block`) may also appear in user messages. A
``thinking`` block is the model's reasoning, as far as the provider shows it: ``signature`` is what Anthropic needs to
accept the block again, ``None`` for reasoning that came without one (Gemini, OpenAI-compatible servers), which only
the UI reads. Anthropic may also reply with ``{"type": "redacted_thinking", "data": ...}``, kept as received. A provider
sends back only the blocks it understands and drops the others from the request, and anything that reads history skips
block types it does not know.

``provider_data`` holds whatever a provider needs to replay its own reply
exactly (for example Gemini's thought signatures). Only the provider named in
it reads it; every other provider rebuilds the turn from ``content``.

``usage`` holds the token counts of the request that produced the reply (see
:class:`Usage`), when the provider reports them. It is never sent to a model.
"""

from __future__ import annotations

import base64
import importlib.util
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

TextCallback = Callable[[str], Any]

IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
"""Media types of an ``image`` block that every provider can send."""


@dataclass
class Usage:
    """Token counts of one request. ``input_tokens`` is the whole prompt, cached parts included."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class Reply:
    """One model reply in canonical form.

    Attributes:
        content: Canonical content blocks (``text``, ``tool_use`` and, when the model's reasoning is shown, ``thinking``).
        stop_reason: ``"tool_use"`` when the model wants tools run; otherwise
            why it stopped (``"end_turn"``, ``"max_tokens"``, ...).
        provider_data: Opaque data stored with the message and handed back to
            the same provider on later requests; ``None`` if not needed.
        usage: Token counts of the request; ``None`` if the provider does not report them.
    """

    content: list[dict]
    stop_reason: str
    provider_data: Any = None
    usage: Usage | None = None


def image_block(data: str | bytes, media_type: str) -> dict:
    """An ``image`` block holding ``data`` (raw bytes, or already base64 text) of one of :data:`IMAGE_TYPES`.

    API 2.3.

    Raises:
        ValueError: if ``media_type`` is not one of :data:`IMAGE_TYPES`.
    """
    if media_type not in IMAGE_TYPES:
        raise ValueError(f"image_block: media_type must be one of {', '.join(IMAGE_TYPES)}, not {media_type!r}")
    encoded = base64.b64encode(data).decode("ascii") if isinstance(data, bytes) else data
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}}


def image_source(block: dict) -> dict:
    """The ``source`` of an ``image`` block; ``{}`` if the block has none (or not a dict)."""
    source = block.get("source")
    return source if isinstance(source, dict) else {}


def image_summary(block: dict) -> str:
    """``[image: media_type, N bytes]`` for an ``image`` block: what is shown or logged instead of its data."""
    source = image_source(block)
    data = source.get("data") if isinstance(source.get("data"), str) else ""
    size = len(data) * 3 // 4 - data[-2:].count("=") if data else 0
    return f"[image: {source.get('media_type', 'unknown')}, {size} bytes]"


def result_text(content: Any) -> str:
    """The text of a message's or ``tool_result`` block's content: a string, or the ``text`` blocks of a list.

    An ``image`` block counts as :func:`image_summary`, never as its data; other block types count as nothing.
    """
    if isinstance(content, str):
        return content
    return "".join(
        b.get("text", "") if b.get("type") != "image" else image_summary(b) for b in content if isinstance(b, dict)
    )


class Provider(Protocol):
    """A model API. Implementations live in ``minimal.providers.<name>``."""

    name: str
    """Registry name, also stored in ``provider_data`` to recognise its own data."""

    def list_models(self) -> list[str]:
        """Optional: model names this provider can use right now (queried from its API).

        ``/model`` lists them when present; providers without it fall back to the default model.
        """
        ...

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
        """Send one request and return the reply.

        Args:
            model: Model name for this provider.
            system: System prompt.
            messages: Canonical history (see module docstring). Must not be modified.
            tools: ``{"name", "description", "input_schema"}`` for each tool.
            on_text: Called with each text chunk as it streams in.
            options: Request options: ``max_tokens`` (int, the output limit), ``temperature``
                (float) and ``reasoning_effort`` (``"low"``, ``"medium"`` or ``"high"``: how much the model
                should think, which a provider maps to its own setting or ignores). A provider ignores keys it does not know. minimal passes it only when it is
                not empty, so a provider written before it existed keeps working.
        """
        ...


@dataclass
class ProviderSpec:
    """How to build a provider; registered in the :class:`~minimal.extensions.registry.Registry`.

    Attributes:
        name: Name used by ``--provider`` and ``/provider``.
        create: Builds the provider. Should import its SDK lazily.
        default_model: Model used when none is given.
        env_keys: Environment variables holding the credential; at least one must be set.
            Setup saves a key entered by the user under the first one, so list the
            name the SDK prefers first.
        description: One line shown by ``/provider``.
        sdk: Top-level module of the SDK :attr:`create` imports, when it is an optional dependency
            (``"openai"``). Empty if there is none to check.
        install: What the user installs to get :attr:`sdk` (``"mnml-agent[openai]"``); shown when it is missing.
        login: Signs the user in (a browser flow, a device code…), blocking and printing its instructions to the
            terminal; raises ``ValueError`` (a message for the user) if it fails. Providers authenticated by an
            ``env_keys`` key leave it ``None``. With it, ``minimal --login [name]``, ``/login`` and setup offer to sign in.
        logout: Forgets the sign-in (``minimal --logout [name]``, ``/logout``).
        account: Who is signed in: an email or any short text, or ``None`` if nobody is. Setup shows it.
        context_windows: Context size in tokens by model-name prefix (``{"my-model-": 32768}``; the longest matching prefix
            wins), what ``ctx.context_window`` reports for this provider's models. Built-in providers' models are known
            already (:mod:`minimal.providers.windows`); the ``context_window`` setting beats both. API 2.3.
    """

    name: str
    create: Callable[[], Provider]
    default_model: str
    env_keys: tuple[str, ...] = ()
    description: str = ""
    sdk: str = ""
    install: str = ""
    login: Callable[[], None] | None = None
    logout: Callable[[], None] | None = None
    account: Callable[[], str | None] | None = None
    context_windows: Mapping[str, int] = field(default_factory=dict)

    def missing_sdk(self) -> str | None:
        """Return an error message if the optional SDK (:attr:`sdk`) is not installed, else ``None``."""
        if not self.sdk:
            return None
        try:
            if importlib.util.find_spec(self.sdk) is not None:
                return None
        except (ImportError, ValueError):  # a dotted name whose parent package is missing
            pass
        hint = self.install or self.sdk
        return (
            f"The {self.name} provider needs the {self.sdk} package, which is not installed: run "
            f"`uv tool install --force '{hint}'` (or `pip install '{hint}'`)."
        )

    def missing_credentials(self) -> str | None:
        """Return an error message if no credential variable is set, else ``None``."""
        if not self.env_keys or any(os.environ.get(k) for k in self.env_keys):
            return None
        return f"{' or '.join(self.env_keys)} is not set in the environment."
