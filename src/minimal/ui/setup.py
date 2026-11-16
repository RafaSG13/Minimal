"""Onboarding: pick the default provider, sign in or enter its API key, pick
the model, and save the choice in ``~/.minimal/settings.json`` (:func:`minimal.config.update_global`).

Runs on the first start (no saved provider yet), when the saved provider can
no longer start (for example after ``minimal --logout``), and on demand with
``minimal --setup`` or ``/setup``. It asks through a
:class:`~minimal.core.app.Frontend` (numbered lists on a console), so it needs
no menu toolkit; the key itself is read hidden with :mod:`getpass`, which
needs a terminal.
"""

from __future__ import annotations

import getpass
import os
import sys

from minimal import config
from minimal.core.app import Frontend
from minimal.extensions.registry import Registry
from minimal.providers.base import ProviderSpec


def can_run() -> bool:
    """Whether the questions can be asked: stdin and stdout are terminals."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def run_setup(registry: Registry, frontend: Frontend) -> tuple[str, str]:
    """Ask for the provider, its credentials and the model; save and return ``(provider, model)``.

    Raises:
        KeyboardInterrupt: if the user cancels (Ctrl-C, or an empty or invalid answer to a list).
    """
    current = config.load_global()
    names = list(registry.providers)
    if current.get("provider") in names:
        default = current["provider"]
    else:
        default = next((n for n, spec in registry.providers.items() if _account(spec)), names[0])

    print("minimal setup: choose the default model provider.")
    while True:
        status = {n: _status(spec) for n, spec in registry.providers.items()}
        name_width, status_width = max(map(len, names)), max(map(len, status.values()))
        name = _answer(
            frontend.choose(
                "Provider",
                [
                    (n, f"{n:<{name_width}}  {status[n]:<{status_width}}  {spec.description}"
                     + (" (current)" if n == default else ""))
                    for n, spec in registry.providers.items()
                ],
            )
        )
        spec = registry.providers[name]
        problem = spec.missing_sdk()
        if problem is None:
            break
        print(f"{problem}\n")
    if spec.login is not None:
        _ensure_login(spec, frontend)
    else:
        _ensure_key(spec, frontend)
    fallback = current.get("model", spec.default_model) if current.get("provider") == name else spec.default_model
    model = frontend.ask_text(f"Model (Enter for {fallback})") or fallback
    config.update_global({"provider": name, "model": model})
    print(f"Saved {name} · {model} as the default (~/.minimal/settings.json). Change it with `minimal --setup` or /setup.")
    for layer in config.read_layers(os.getcwd())[0]:
        if layer.scope != "global" and "provider" in layer.data:
            print(f"Note: {layer.path} sets provider {layer.data['provider']} for this project.")
    return name, model


def _answer(value: str | None) -> str:
    """``value``, or ``KeyboardInterrupt`` if the user cancelled the question."""
    if value is None:
        raise KeyboardInterrupt
    return value


def _account(spec: ProviderSpec) -> str | None:
    """Who is signed in to ``spec``; ``None`` if nobody, or it has no login."""
    if spec.account is None:
        return None
    try:
        return spec.account()
    except ValueError:  # an unreadable login counts as signed out: a new login replaces it
        return None


def _status(spec: ProviderSpec) -> str:
    """Short credential state shown next to each provider."""
    if spec.missing_sdk():
        return f"needs {spec.install or spec.sdk}"
    if spec.login is not None:
        return "logged in" if _account(spec) else "needs login"
    if not spec.env_keys:
        return "ready"
    if any(k in config.load_keys() for k in spec.env_keys):
        return "key saved"
    env = next((k for k in spec.env_keys if os.environ.get(k)), None)
    return f"${env} set" if env else "needs API key"


def _ensure_login(spec: ProviderSpec, frontend: Frontend) -> None:
    assert spec.login is not None
    who = _account(spec)
    if who:
        keep = _answer(
            frontend.choose(
                f"{spec.name} login",
                [("keep", f"Keep the current login ({who})"), ("again", "Log in again")],
            )
        )
        if keep == "keep":
            return
    spec.login()


def _ensure_key(spec: ProviderSpec, frontend: Frontend) -> None:
    if not spec.env_keys:
        return
    keep_label: str | None
    if any(k in config.load_keys() for k in spec.env_keys):
        keep_label = "Keep the saved key"
    else:
        env = next((k for k in spec.env_keys if os.environ.get(k)), None)
        keep_label = f"Use ${env} from the environment" if env else None
    if keep_label:
        keep = _answer(frontend.choose("API key", [("keep", keep_label), ("new", "Enter a new key")]))
        if keep == "keep":
            return
    name = spec.env_keys[0]  # the name the SDK prefers when several are set
    key = ""
    while not key:
        try:
            key = getpass.getpass(f"{name} (hidden; paste the key): ").strip()
        except EOFError:
            raise KeyboardInterrupt from None
    config.save_key(name, key)
    print(f"Key saved to {config.KEYS_FILE}; it is used instead of the environment variables.")
