"""Slash commands: the built-ins and the dispatcher.

Every command is called as ``run(args, app)``, where ``args`` is the text after
the command name and ``app`` is the :class:`~minimal.app.App`. Commands
registered by extensions take precedence over built-ins with the same name.
Commands print their output; the terminal UI shows it in the transcript.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from minimal import config, extensions, marketplace, setup
from minimal.app import App, reload_summary
from minimal.providers import claude_auth
from minimal.registry import Command
from minimal.session import list_sessions, new_id, open_session


class ExitRepl(Exception):
    """Raised by ``/exit`` to quit the interactive UI."""


def _help(args: str, app: App) -> None:
    for name, cmd in {**BUILTIN_COMMANDS, **app.registry.commands}.items():
        print(f"  /{name:<10} {cmd.description}")


def _new(args: str, app: App) -> None:
    app.session = open_session(new_id())
    print(f"New session: {app.session.id}")


def _sessions(args: str, app: App) -> None:
    print("\n".join(list_sessions()) or "(none)")


def _available_models(app: App) -> list[str]:
    """Models the current provider offers: queried from its API, else just the default."""
    lister = getattr(app.provider, "list_models", None)
    spec = app.registry.providers.get(app.provider_name)
    found: list[str] = []
    if lister is not None:
        try:
            found = list(lister())
        except Exception as e:
            print(f"(could not fetch the model list: {e})", file=sys.stderr)
    if not found and spec is not None:
        found = [spec.default_model]
    if app.model not in found:
        found.insert(0, app.model)
    return found


def _model(args: str, app: App) -> None:
    models = _available_models(app) if (not args or args.isdigit()) else []
    if args.isdigit():
        index = int(args)
        if not 1 <= index <= len(models):
            raise ValueError(f"no model number {index}; /model lists 1-{len(models)}")
        app.model = models[index - 1]
    elif args:
        app.model = args
    else:
        print(f"Models for {app.provider_name} (* = current):")
        for i, name in enumerate(models, 1):
            print(f"{'*' if name == app.model else ' '} {i:>2}. {name}")
        print("Change with /model <number or name>. Other providers: /provider")
        return
    print(f"Model: {app.model}")


def _provider(args: str, app: App) -> None:
    if args:
        name, _, model = args.partition(" ")
        app.use_provider(name, model.strip() or None)
        print(f"Provider: {app.provider_name} · model {app.model}")
        return
    for name, spec in app.registry.providers.items():
        mark = "*" if name == app.provider_name else " "
        print(f"{mark} {name:<10} {spec.default_model:<24} {spec.description}")


def _tools(args: str, app: App) -> None:
    print(", ".join(app.registry.tools))


def _login(args: str, app: App) -> None:
    try:
        claude_auth.login()
    except KeyboardInterrupt:  # run_command only catches Exception; keep the UI alive
        print("\nLogin cancelled.")
        return
    keep_model = app.provider_name in ("claude", "anthropic")  # same model names; gemini's would be invalid
    app.use_provider("claude", app.model if keep_model else None)
    print(f"Provider: {app.provider_name} · model {app.model}")


def _logout(args: str, app: App) -> None:
    claude_auth.logout()


def _setup(args: str, app: App) -> None:
    if not setup.can_run():
        raise ValueError("setup needs a terminal")
    try:
        name, model = setup.run_setup(app.registry)
    except KeyboardInterrupt:  # run_command only catches Exception; keep the UI alive
        print("\nSetup cancelled.")
        return
    app.use_provider(name, model)
    print(f"Provider: {app.provider_name} · model {app.model}")


def _exit(args: str, app: App) -> None:
    raise ExitRepl


def _reload(args: str, app: App) -> None:
    print(reload_summary(*app.reload_extensions()))


EXT_USAGE = "/ext [list|publish <path> [name]]"

_ADD = "\x00add"
_REMOVE = "\x00remove"
"""Marketplace-picker option values that cannot be URLs."""


def _ext(args: str, app: App) -> None:
    words = shlex.split(args)
    if not words:
        _ext_picker(app)
    elif words == ["list"]:
        rows = marketplace.installed(app.cwd)
        for where, path in rows:
            print(f"{where:<8} {path.stem:<20} {path}")
        if not rows:
            print("(none)")
    elif words[0] == "publish" and 2 <= len(words) <= 3:
        print(marketplace.publish(Path(app.cwd, words[1]), words[2] if len(words) > 2 else None))
    else:
        raise ValueError(f"usage: {EXT_USAGE}")


def _marketplace_label(url: str) -> str:
    label = url
    if url == marketplace.DEFAULT_URL:
        label += " (default)"
    if url == os.environ.get("MINIMAL_MARKETPLACE"):
        label += " ($MINIMAL_MARKETPLACE)"
    return label


def _pick_marketplace(app: App) -> str | None:
    """Ask for a marketplace, letting the user add or remove saved ones; ``None`` if cancelled."""
    while True:
        saved = config.load_marketplaces()
        options = [(url, _marketplace_label(url)) for url in marketplace.marketplaces()]
        options.append((_ADD, "+ Add a marketplace…"))
        if saved:
            options.append((_REMOVE, "- Remove a saved marketplace…"))
        choice = app.frontend.choose("Marketplace", options)
        if choice == _ADD:
            url = app.frontend.ask_text("Add a marketplace (git URL or local path)")
            if url is None:
                continue
            try:
                return marketplace.add_marketplace(url)
            except (ValueError, RuntimeError) as e:
                print(f"Could not add {url}: {e}", file=sys.stderr)
        elif choice == _REMOVE:
            url = app.frontend.choose("Remove a saved marketplace", [(u, u) for u in saved])
            if url is not None:
                marketplace.remove_marketplace(url)
                print(f"Removed marketplace {url}")
        else:
            return choice


def _entry_label(entry: marketplace.Entry) -> str:
    label = marketplace.format_listing(entry.listing)
    if entry.outdated:
        label += " · update available"
        old, new = entry.installed_version, entry.listing.version
        if old and new and old != new:
            label += f" ({old} → {new})"
    elif entry.installed:
        label += f" · installed ({', '.join(entry.installed)})"
    return label


def _ext_picker(app: App) -> None:
    """Pick a marketplace, then tick its extensions: ticking installs, unticking uninstalls.

    Ticked extensions whose installed copy differs from the marketplace are
    offered for update. Nothing changes on disk until every question is answered.
    """
    url = _pick_marketplace(app)
    if url is None:
        print("Cancelled.")
        return
    entries = marketplace.catalog(url, app.cwd)
    if not entries:
        print(f"No extensions in {url}.")
        return
    ticked = app.frontend.choose_many(
        f"Extensions · {url}", [(e.listing.name, _entry_label(e), bool(e.installed)) for e in entries]
    )
    if ticked is None:
        print("Cancelled.")
        return
    to_install = [e for e in entries if not e.installed and e.listing.name in ticked]
    to_remove = [e for e in entries if e.installed and e.listing.name not in ticked]
    outdated = [e for e in entries if e.outdated and e.listing.name in ticked]
    scope = "global"
    if to_install:
        scope = app.frontend.choose(
            f"Install {len(to_install)} extension(s) into",
            [
                ("global", f"Global: {extensions.GLOBAL_EXTENSIONS_DIR}"),
                ("project", f"This project: {extensions.project_extensions_dir(app.cwd)}"),
            ],
        )
        if scope is None:
            print("Cancelled.")
            return
    to_update = []
    if outdated:
        picked = app.frontend.choose_many(
            "Update these extensions?", [(e.listing.name, _entry_label(e), True) for e in outdated]
        )
        if picked is None:
            print("Cancelled.")
            return
        to_update = [e for e in outdated if e.listing.name in picked]
    if not (to_install or to_remove or to_update):
        print("No changes.")
        return
    for e in to_install:
        dest, _ = marketplace.install_from(e.listing.path, scope, app.cwd)
        print(f"Installed {e.listing.name} at {dest}")
    for e in to_update:
        for s in e.outdated:
            dest, _ = marketplace.install_from(e.listing.path, s, app.cwd)
            print(f"Updated {e.listing.name} at {dest}")
    for e in to_remove:
        for s in e.installed:
            print(f"Removed {marketplace.uninstall(e.listing.name, s, app.cwd)}")
    print(reload_summary(*app.reload_extensions()))


BUILTIN_COMMANDS: dict[str, Command] = {
    "help": Command(_help, "show the commands"),
    "new": Command(_new, "start a new session"),
    "sessions": Command(_sessions, "list saved sessions"),
    "model": Command(_model, "list models, or change: /model [number|name]"),
    "provider": Command(_provider, "list providers, or switch: /provider <name> [model]"),
    "tools": Command(_tools, "list the tools"),
    "reload": Command(_reload, "reload extensions from disk"),
    "ext": Command(_ext, f"pick extensions to install, update or remove; {EXT_USAGE}"),
    "setup": Command(_setup, "choose and save the default provider, its login or key, and the model", terminal=True),
    "login": Command(_login, "sign in with a Claude Pro/Max subscription in the browser", terminal=True),
    "logout": Command(_logout, "forget the Claude login"),
    "exit": Command(_exit, "quit"),
}


def find_command(name: str, app: App) -> Command | None:
    """The command called ``name``: an extension's if registered, else the built-in."""
    return app.registry.commands.get(name) or BUILTIN_COMMANDS.get(name)


def run_command(line: str, app: App) -> None:
    """Parse and run a ``/name args`` line.

    Unknown commands and errors raised by a command are reported, not raised.

    Raises:
        ExitRepl: when the command asks to quit.
    """
    name, _, args = line[1:].partition(" ")
    cmd = find_command(name, app)
    if cmd is None:
        print(f"Unknown command: /{name}")
        return
    try:
        cmd.run(args.strip(), app)
    except ExitRepl:
        raise
    except Exception as e:
        print(f"Error in /{name}: {e}", file=sys.stderr)
