"""How many tokens of context each provider's models take, by model-name prefix.

The table is approximate and small on purpose: it covers the built-in providers' model families, so ``ctx.context_window``
has an answer for them. A model it does not know gives ``None``; the ``context_window`` setting and a provider's own
:attr:`~minimal.providers.base.ProviderSpec.context_windows` override it.
"""

from __future__ import annotations

from collections.abc import Mapping

CONTEXT_WINDOWS: dict[str, dict[str, int]] = {
    "anthropic": {"claude-": 200_000},
    "gemini": {"gemini-": 1_048_576},
    "openai": {"gpt-5": 400_000, "gpt-4.1": 1_047_576, "gpt-4o": 128_000, "o3": 200_000, "o4": 200_000},
}
"""``provider name -> {model-name prefix: context window in tokens}``; the longest matching prefix wins."""


def lookup(windows: Mapping[str, int], model: str) -> int | None:
    """The window of the longest prefix of ``model`` that ``windows`` has; ``None`` if none matches."""
    best = max((prefix for prefix in windows if model.startswith(prefix)), key=len, default=None)
    return None if best is None else windows[best]


def context_window(provider: str, model: str, spec_windows: Mapping[str, int] | None = None) -> int | None:
    """The context window of ``model`` of ``provider``: the provider's own ``spec_windows`` first, then the built-in
    table; ``None`` if neither knows the model."""
    return lookup(spec_windows or {}, model) or lookup(CONTEXT_WINDOWS.get(provider, {}), model)
