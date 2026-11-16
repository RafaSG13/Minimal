"""Model integrations.

Each integration is a module exposing a :class:`~minimal.providers.base.ProviderSpec`
named ``SPEC``. Built-in ones are registered at startup; extensions can add
more with ``minimal.register_provider(...)``.

To add a built-in integration: write ``providers/<name>.py`` with a class that
implements :class:`~minimal.providers.base.Provider` (convert the canonical
history to the API's format, stream text through ``on_text``, return a
:class:`~minimal.providers.base.Reply`), define ``SPEC``, and list it in
``BUILTIN_PROVIDERS``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from minimal.providers import anthropic, claude, gemini, openai
from minimal.providers.base import ProviderSpec

if TYPE_CHECKING:
    from minimal.extensions.registry import Registry

BUILTIN_PROVIDERS = (anthropic.SPEC, claude.SPEC, gemini.SPEC, openai.SPEC)


def register_builtin_providers(registry: Registry) -> None:
    """Add the built-in providers to ``registry``."""
    for spec in BUILTIN_PROVIDERS:
        registry.add_provider(spec)


def login_provider(
    providers: Mapping[str, ProviderSpec], name: str | None = None, current: str | None = None
) -> ProviderSpec:
    """The provider whose sign-in ``--login``, ``/login`` and ``/logout`` act on.

    That is ``name`` if given, else ``current`` if it has a login, else the only provider that has one.

    Raises:
        ValueError: if ``name`` is unknown or has no login, or none or several providers could be meant.
    """
    with_login = {n: spec for n, spec in providers.items() if spec.login is not None}
    if name:
        spec = providers.get(name)
        if spec is None:
            raise ValueError(f"unknown provider: {name} (available: {', '.join(providers)})")
        if spec.login is None:
            raise ValueError(f"{name} signs in with a key, not a login (logins: {', '.join(with_login) or 'none'})")
        return spec
    if current in with_login:
        return with_login[current]
    if len(with_login) == 1:
        return next(iter(with_login.values()))
    if not with_login:
        raise ValueError(
            "no installed provider offers a login: install an extension that adds one (/ext, or `minimal ext install <name>`)"
        )
    raise ValueError(f"which provider? Name one: {', '.join(with_login)}")
