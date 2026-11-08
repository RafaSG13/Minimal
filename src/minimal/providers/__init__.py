"""Model integrations.

Each integration is a module exposing a :class:`~minimal.providers.base.ProviderSpec`
named ``SPEC``. Built-in ones are registered at startup; extensions can add
more with ``pi.register_provider(...)``.

To add a built-in integration: write ``providers/<name>.py`` with a class that
implements :class:`~minimal.providers.base.Provider` (convert the canonical
history to the API's format, stream text through ``on_text``, return a
:class:`~minimal.providers.base.Reply`), define ``SPEC``, and list it in
``BUILTIN_PROVIDERS``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from minimal.providers import anthropic, claude, gemini

if TYPE_CHECKING:
    from minimal.registry import Registry

BUILTIN_PROVIDERS = (anthropic.SPEC, claude.SPEC, gemini.SPEC)


def register_builtin_providers(registry: Registry) -> None:
    """Add the built-in providers to ``registry``."""
    for spec in BUILTIN_PROVIDERS:
        registry.add_provider(spec)
