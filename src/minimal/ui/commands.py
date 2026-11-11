"""Slash commands: the built-ins (:data:`BUILTIN_COMMANDS`), handed to the :class:`~minimal.core.app.App` that runs them.

Built-in commands are called as ``run(args, app)``, where ``args`` is the text
after the command name and ``app`` is the :class:`~minimal.core.app.App`;
extension commands get ``ctx``, the app's :class:`~minimal.core.context.Context`.
Commands registered by extensions take precedence over built-ins with the same name
(:meth:`~minimal.core.app.App.run_command`). Commands print their output; the terminal UI shows it in the transcript.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

from minimal import config
from minimal.core.app import App, ExitRepl
from minimal.core.session import TORN_NOTICE, list_sessions, new_id, open_session, preview, project_dir, session_project
from minimal.extensions import audit, loader, marketplace
from minimal.extensions.loader import reload_summary
from minimal.extensions.registry import Command
from minimal.providers import claude_auth
from minimal.ui import setup


def _help(args: str, app: App) -> None:
    for name, cmd in app.commands().items():
        print(f"  /{name:<10} {cmd.description}")


def _new(args: str, app: App) -> None:
    app.session = open_session(new_id(), app.cwd)
    print(f"New session: {app.session.id}")


def _split_all(args: str) -> tuple[bool, list[str]]:
    """Whether ``--all`` is among the words of ``args``, and the other words."""
    words = args.split()
    rest = [w for w in words if w != "--all"]
    return len(rest) != len(words), rest


def _session_label(id: str, all_projects: bool) -> str:
    where = f"  {session_project(id) or '?'}" if all_projects else ""
    return f"{id}{where}  {preview(id)}".rstrip()


def _sessions(args: str, app: App) -> None:
    all_projects, rest = _split_all(args)
    if rest:
        raise ValueError("usage: /sessions [--all]")
    ids = list_sessions(None if all_projects else app.cwd)
    print("\n".join(_session_label(i, all_projects) for i in ids) or "(none)")


RESUME_CHOICES = 30
"""Most sessions offered by the ``/resume`` picker: the newest ones."""


def _resume(args: str, app: App) -> None:
    all_projects, rest = _split_all(args)
    if len(rest) > 1:
        raise ValueError("usage: /resume [--all] [id]")
    if rest:
        id = rest[0]
        if id not in list_sessions():  # open_session would quietly start a new session under that id
            raise ValueError(f"no session {id!r}; /sessions --all lists every project's")
    else:
        ids = list_sessions(None if all_projects else app.cwd)
        recent = [i for i in reversed(ids) if i != app.session.id][:RESUME_CHOICES]
        if not recent:
            print("(none)")
            return
        id = app.frontend.choose("Resume session", [(i, _session_label(i, all_projects)) for i in recent])
        if id is None:
            print("Cancelled.")
            return
    app.session = open_session(id)
    elsewhere = f", from {app.session.cwd}" if app.session.cwd and app.session.cwd != project_dir(app.cwd) else ""
    print(f"Resumed session {id} ({len(app.messages)} messages{elsewhere})")
    if app.session.torn_at is not None:
        print(f"warning: {TORN_NOTICE}")


def _model(args: str, app: App) -> None:
    models: list[str] = []
    if not args or args.isdigit():
        models, problem = app.available_models()
        if problem:
            print(f"(could not fetch the model list: {problem})", file=sys.stderr)
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


def _theme(args: str, app: App) -> None:
    names, current = app.frontend.themes()
    if not names:
        print("Colour themes only apply to the terminal UI.")
        return
    if not args and len(names) == 1:
        print(f"Only the built-in {names[0]} theme is installed; get more from /ext → Themes.")
        return
    options = [(n, f"{n} (current)" if n == current else n) for n in names]
    name = args or app.frontend.choose_theme("Theme · more in /ext → Themes", options)
    if name is None:
        return
    if name not in names:
        raise ValueError(f"no theme {name!r}; installed: {', '.join(names)} (more in /ext → Themes)")
    app.frontend.set_theme(name)
    print(f"Theme: {name}")


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
    print(reload_summary(*app.reload_extensions(), app.untrusted_notice()))


def _trust(args: str, app: App) -> None:
    """Show a static scan of the project's extensions and its trust-gated settings, and ask once whether to trust them.

    Trust is saved for the digest of what was reported, so files changed after the
    report stay untrusted: the reload that follows reads them again and skips them.
    """
    if args:
        raise ValueError("usage: /trust")
    project = loader.read_project_snapshot(app.cwd, config.risky_subset(config.read_layers(app.cwd)[0]))
    if project is None:
        print(f"No project extensions or trust-gated settings in {Path(app.cwd, '.minimal')}.")
        return
    if app.is_trusted(project):
        print(f"The project extensions and settings in {project.directory.parent} are already trusted.")
        if app.untrusted is not None:  # trusted elsewhere since the last reload
            print(reload_summary(*app.reload_extensions(), app.untrusted_notice()))
        return
    print(audit.format_report(project))
    if not app.frontend.ask(
        "Trust and apply these project extensions/settings? Review the scan above: extensions run with your permissions."
    ):
        print("Not trusted: the project extensions and settings stay unapplied.")
        return
    config.save_trusted(project.key, project.digest)
    print(reload_summary(*app.reload_extensions(), app.untrusted_notice()))


_SCALARS = ("provider", "model", "theme", "prompt.base", "prompt.files")
"""Settings where the highest layer wins; the others are concatenated, merged or global-only lists."""


def _settings(args: str, app: App) -> None:
    """Show the settings files, the effective settings with the scope each comes from, and the reload's errors."""
    if args:
        raise ValueError("usage: /settings")
    settings = app.settings
    print("Files:")
    for scope, path in config.settings_files(app.cwd).items():
        print(f"  {scope:<8} {path}{'' if path.exists() else ' (not found)'}")
    lines = []
    for key in _SCALARS:
        value, scope = settings.scalar(key)
        if scope is not None:
            lines.append(f"{key}  {value if isinstance(value, str) else json.dumps(value)}  ({scope})")
    for layer in settings.layers:
        for key in ("marketplaces", "prompt.append", "permissions.allow", "permissions.deny", "extensions.disabled"):
            for item in layer.data.get(key, []):
                lines.append(f"{key}  {item}  ({layer.scope}){_untrusted_mark(settings, layer, key)}")
        for ext_id, values in layer.data.get("extensions.settings", {}).items():
            mark = _untrusted_mark(settings, layer, "extensions.settings")
            lines.append(f"extensions.settings  {ext_id} {json.dumps(values, separators=(',', ':'))}  ({layer.scope}){mark}")
    print("Effective:")
    print("\n".join(f"  {line}" for line in lines) or "  (defaults only)")
    if app.setting_errors:
        print("Errors:")
        print("\n".join(f"  {e}" for e in app.setting_errors))


def _untrusted_mark(settings: config.Settings, layer: config.Layer, key: str) -> str:
    return " (not trusted: /trust)" if key in config.RISKY and layer.scope != "global" and not settings.trusted else ""


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
        for where, category, path in rows:
            disabled = "disabled" if loader.ext_id(path) in app.settings.disabled else ""
            print(f"{where:<8} {category:<10} {path.stem:<20} {path} {disabled}".rstrip())
        if not rows:
            print("(none)")
    elif words[0] == "publish" and 2 <= len(words) <= 3:
        print(marketplace.publish(Path(app.cwd, words[1]), words[2] if len(words) > 2 else None))
    else:
        raise ValueError(f"usage: {EXT_USAGE}")


CATEGORY_LABELS = {"extensions": "Extensions", "themes": "Themes", "tools": "Tools", "widgets": "TUI components"}
"""How the ``/ext`` picker names each marketplace category."""


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
        saved = config.load_global().get("marketplaces", [])
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
    if not entry.listing.compatible:
        label += f" · not for this minimal (API {entry.listing.api})"
    return label


def _category_label(category: str, entries: list[marketplace.Entry]) -> str:
    installed = sum(1 for e in entries if e.installed)
    label = f"{CATEGORY_LABELS[category]} ({len(entries)}"
    return label + (f", {installed} installed)" if installed else ")")


def _ext_picker(app: App) -> None:
    """Pick a marketplace and a category, then tick its extensions: ticking installs, unticking uninstalls.

    Ticked extensions whose installed copy differs from the marketplace are
    offered for update. Nothing changes on disk until every question is answered.
    """
    url = _pick_marketplace(app)
    if url is None:
        print("Cancelled.")
        return
    by_category: dict[str, list[marketplace.Entry]] = {}
    for entry in marketplace.catalog(url, app.cwd):
        by_category.setdefault(entry.listing.category, []).append(entry)
    if not by_category:
        print(f"No extensions in {url}.")
        return
    category = app.frontend.choose(
        f"Category · {url}", [(c, _category_label(c, entries)) for c, entries in by_category.items()]
    )
    if category is None:
        print("Cancelled.")
        return
    entries = by_category[category]
    ticked = app.frontend.choose_many(
        f"{CATEGORY_LABELS[category]} · {url}", [(e.listing.name, _entry_label(e), bool(e.installed)) for e in entries]
    )
    if ticked is None:
        print("Cancelled.")
        return
    to_install = [e for e in entries if not e.installed and e.listing.name in ticked]
    for e in to_install:
        if not e.listing.compatible:
            print(f"Skipped {e.listing.name}: {e.listing.problem}")
    to_install = [e for e in to_install if e.listing.compatible]
    to_remove = [e for e in entries if e.installed and e.listing.name not in ticked]
    outdated = [e for e in entries if e.outdated and e.listing.compatible and e.listing.name in ticked]
    scope = "global"
    if to_install:
        scope = app.frontend.choose(
            f"Install {len(to_install)} item(s) into",
            [
                ("global", f"Global: {loader.category_dir(loader.GLOBAL_EXTENSIONS_DIR, category)}"),
                (
                    "project",
                    f"This project: {loader.category_dir(loader.project_extensions_dir(app.cwd), category)}",
                ),
            ],
        )
        if scope is None:
            print("Cancelled.")
            return
    to_update = []
    if outdated:
        picked = app.frontend.choose_many("Update these?", [(e.listing.name, _entry_label(e), True) for e in outdated])
        if picked is None:
            print("Cancelled.")
            return
        to_update = [e for e in outdated if e.listing.name in picked]
    if not (to_install or to_remove or to_update):
        print("No changes.")
        return
    to_review = [e.listing.path for e in to_install if scope == "global"]
    to_review += [e.listing.path for e in to_update if "global" in e.outdated]
    if to_review and not marketplace.confirm_global(to_review, app.frontend.ask):
        print("Cancelled.")
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
            print(f"Removed {marketplace.uninstall(e.listing.name, s, app.cwd, category)}")
    print(reload_summary(*app.reload_extensions(), app.untrusted_notice()))


BUILTIN_COMMANDS: dict[str, Command] = {
    "help": Command(_help, "show the commands"),
    "new": Command(_new, "start a new session"),
    "sessions": Command(_sessions, "list this project's saved sessions: /sessions [--all]"),
    "resume": Command(_resume, "resume a saved session of this project: /resume [--all] [id]"),
    "model": Command(_model, "list models, or change: /model [number|name]"),
    "provider": Command(_provider, "list providers, or switch: /provider <name> [model]"),
    "tools": Command(_tools, "list the tools"),
    "theme": Command(_theme, "pick the colour theme, or switch: /theme <name>"),
    "reload": Command(_reload, "reload extensions from disk"),
    "settings": Command(_settings, "show the settings files and the effective settings with where each comes from"),
    "trust": Command(_trust, "review this project's extensions and choose whether to trust and load them"),
    "ext": Command(_ext, f"pick extensions to install, update or remove; {EXT_USAGE}"),
    "extensions": Command(_ext, f"pick extensions to install, update or remove; {EXT_USAGE}"),
    "quit": Command(_exit, "quit"),
    "setup": Command(_setup, "choose and save the default provider, its login or key, and the model", terminal=True),
    "login": Command(_login, "sign in with a Claude Pro/Max subscription in the browser", terminal=True),
    "logout": Command(_logout, "forget the Claude login"),
    "exit": Command(_exit, "quit"),
}
