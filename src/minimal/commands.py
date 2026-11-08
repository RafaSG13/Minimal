"""Slash commands: the built-ins and the dispatcher.

Every command is called as ``run(args, app)``, where ``args`` is the text after
the command name and ``app`` is the :class:`~minimal.app.App`. Commands
registered by extensions take precedence over built-ins with the same name.
Commands print their output; the terminal UI shows it in the transcript.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from minimal import marketplace, setup
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


def _model(args: str, app: App) -> None:
    if args:
        app.model = args
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


EXT_USAGE = "/ext list|search [q]|install <name> [--project]|uninstall <name> [--project]|publish <path> [name]"


def _ext(args: str, app: App) -> None:
    words = shlex.split(args)
    sub, rest = (words[0], words[1:]) if words else ("list", [])
    names = [w for w in rest if w != "--project"]
    scope = "project" if "--project" in rest else "global"
    if sub == "list":
        rows = marketplace.installed(app.cwd)
        for where, path in rows:
            print(f"{where:<8} {path.stem:<20} {path}")
        if not rows:
            print("(none)")
    elif sub == "search":
        listings = marketplace.search(" ".join(rest))
        print("\n".join(marketplace.format_listing(x) for x in listings) or "No extensions found.")
    elif sub == "install" and names:
        dest, updated = marketplace.install(names[0], scope, app.cwd)
        print(f"{'Updated' if updated else 'Installed'} {names[0]} at {dest}")
        print(reload_summary(*app.reload_extensions()))
    elif sub == "uninstall" and names:
        print(f"Removed {marketplace.uninstall(names[0], scope, app.cwd)}")
        print(reload_summary(*app.reload_extensions()))
    elif sub == "publish" and rest:
        print(marketplace.publish(Path(app.cwd, rest[0]), rest[1] if len(rest) > 1 else None))
    else:
        raise ValueError(f"usage: {EXT_USAGE}")


BUILTIN_COMMANDS: dict[str, Command] = {
    "help": Command(_help, "show the commands"),
    "new": Command(_new, "start a new session"),
    "sessions": Command(_sessions, "list saved sessions"),
    "model": Command(_model, "show or change the model: /model <name>"),
    "provider": Command(_provider, "list providers, or switch: /provider <name> [model]"),
    "tools": Command(_tools, "list the tools"),
    "reload": Command(_reload, "reload extensions from disk"),
    "ext": Command(_ext, f"extensions: {EXT_USAGE}"),
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
