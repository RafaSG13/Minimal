"""Onboarding: pick the default provider, sign in or enter its API key, pick
the model, and save the choice with :mod:`minimal.config`.

Runs on the first start (no saved provider yet), when the saved provider can
no longer start (for example after ``minimal --logout``), and on demand with
``minimal --setup`` or ``/setup``. The menus need a terminal.
"""

from __future__ import annotations

import os
import sys

import questionary
from questionary import Choice

from minimal import config
from minimal.providers import claude, claude_auth
from minimal.providers.base import ProviderSpec
from minimal.registry import Registry


def can_run() -> bool:
    """Whether the menus can be shown: stdin and stdout are terminals."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def run_setup(registry: Registry) -> tuple[str, str]:
    """Ask for the provider, its credentials and the model; save and return ``(provider, model)``.

    Raises:
        KeyboardInterrupt: if the user cancels with Ctrl-C.
    """
    try:
        current = config.load_config() or {}
    except ValueError:
        current = {}
    login = _stored_login()
    names = list(registry.providers)
    if current.get("provider") in names:
        default = current["provider"]
    elif login and claude.SPEC.name in names:
        default = claude.SPEC.name
    else:
        default = names[0]

    print("minimal setup: choose the default model provider.\n")
    status = {n: _status(spec, login) for n, spec in registry.providers.items()}
    name_width, status_width = max(map(len, names)), max(map(len, status.values()))
    name = questionary.select(
        "Provider",
        choices=[
            Choice(f"{n:<{name_width}}  {status[n]:<{status_width}}  {spec.description}", value=n)
            for n, spec in registry.providers.items()
        ],
        default=default,
    ).unsafe_ask()
    spec = registry.providers[name]
    if name == claude.SPEC.name:
        _ensure_login(login)
    else:
        _ensure_key(spec)
    model = questionary.text(
        "Model",
        default=current["model"] if current.get("provider") == name else spec.default_model,
        validate=lambda text: bool(text.strip()) or "Enter a model name",
    ).unsafe_ask().strip()
    config.save_config(name, model)
    print(f"Saved {name} · {model} as the default ({config.CONFIG_FILE}). Change it with `minimal --setup` or /setup.")
    return name, model


def _stored_login() -> claude_auth.Credentials | None:
    try:
        return claude_auth.load()
    except ValueError:  # corrupt file: treat as logged out, a new login replaces it
        return None


def _status(spec: ProviderSpec, login: claude_auth.Credentials | None) -> str:
    """Short credential state shown next to each provider."""
    if spec.name == claude.SPEC.name:
        return "logged in" if login else "needs login"
    if not spec.env_keys:
        return "ready"
    if any(k in config.load_keys() for k in spec.env_keys):
        return "key saved"
    env = next((k for k in spec.env_keys if os.environ.get(k)), None)
    return f"${env} set" if env else "needs API key"


def _ensure_login(login: claude_auth.Credentials | None) -> None:
    if login:
        who = f" ({login.email})" if login.email else ""
        keep = questionary.select(
            "Claude login",
            choices=[Choice(f"Keep the current login{who}", value=True), Choice("Log in again", value=False)],
        ).unsafe_ask()
        if keep:
            return
    claude_auth.login()


def _ensure_key(spec: ProviderSpec) -> None:
    if not spec.env_keys:
        return
    if any(k in config.load_keys() for k in spec.env_keys):
        keep_label = "Keep the saved key"
    else:
        env = next((k for k in spec.env_keys if os.environ.get(k)), None)
        keep_label = f"Use ${env} from the environment" if env else None
    if keep_label:
        keep = questionary.select(
            "API key",
            choices=[Choice(keep_label, value=True), Choice("Enter a new key", value=False)],
        ).unsafe_ask()
        if keep:
            return
    name = spec.env_keys[0]  # the name the SDK prefers when several are set
    key = questionary.password(
        name,
        validate=lambda text: bool(text.strip()) or "Paste the key",
    ).unsafe_ask().strip()
    config.save_key(name, key)
    print(f"Key saved to {config.KEYS_FILE}; it is used instead of the environment variables.")
