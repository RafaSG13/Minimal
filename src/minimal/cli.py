"""Command-line entry point: parse options, build the :class:`~minimal.core.app.App`
and open the terminal UI. A turn can only be started from inside the UI: there is no
one-shot mode that runs the agent from the shell command line.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from minimal import config
from minimal.core.app import App, ConsoleFrontend
from minimal.core.host import Host
from minimal.core.session import TORN_NOTICE, Session, last_session, new_id, open_session
from minimal.extensions import marketplace
from minimal.extensions.loader import load_extensions
from minimal.extensions.registry import Registry
from minimal.providers import login_provider, register_builtin_providers
from minimal.tools.builtin import register_builtin_tools
from minimal.ui import setup
from minimal.ui.commands import register_builtin_commands

HELP = """minimal — minimal, extensible coding-agent harness

Usage:
  minimal [options]            the terminal UI (runs setup first if no provider is chosen yet)
  minimal --setup              choose the default provider, sign in or enter its key, choose the model
  minimal --login [provider]   sign in to a provider that uses a login (a subscription, an account)
  minimal --logout [provider]  forget a provider's login
  minimal ext check [--install-deps] <path>...
                               validate extension files, package folders or a marketplace checkout
                               (exit status 1 if any fails; runs without a terminal or a provider);
                               --install-deps first installs their missing __dependencies__
  minimal ext install [--project] [<category>/]<name>
                               install one item of the default marketplace (globally, after a scan you confirm,
                               or into this project); how to add a provider that is not built in

Options:
  -c, --continue        resume the last session of this directory
  -s, --session <id>    open (or create) a specific session
  --provider <name>     model integration: anthropic, gemini, openai, or one added by an extension
                        (default $MINIMAL_PROVIDER, else the settings' provider)
  -m, --model <name>    model (default $MINIMAL_MODEL, else the settings' model, else the provider's default)
  -e, --ext <path>      load an extension file or folder (repeatable)
  -y, --yes             do not ask for confirmation for write/edit/bash
  --trust-project       apply this project's extensions and trust-gated settings without /trust
  -h, --help            this help

Settings:
  ~/.minimal/settings.json         global: provider, model, theme, prompt, permissions, extensions, marketplaces
                                   (setup, /theme and /ext save there)
  .minimal/settings.json           this project's settings, shared with it
  .minimal/settings.local.json     this project's personal settings; both beat the global file
                                   (/settings shows the files and where each value comes from)

Environment:
  ANTHROPIC_API_KEY             for --provider anthropic
  GEMINI_API_KEY/GOOGLE_API_KEY for --provider gemini
  OPENAI_API_KEY                for --provider openai (OPENAI_BASE_URL points it at OpenRouter, Ollama, LM Studio)
                                (a key entered in setup is saved in ~/.minimal/keys.json and wins over these)
  (a provider added by an extension may use a login: minimal --login <provider>)
  MINIMAL_PROVIDER, MINIMAL_MODEL
  MINIMAL_MARKETPLACE           git URL or path of the extension marketplace used by the
                                model's tools and listed first by /ext
                                (default https://github.com/RafaSG13/minimal-marketplace.git)

Extensions: ~/.minimal/extensions/ and ./.minimal/extensions/ in a project."""


def main() -> None:
    """Entry point of the ``minimal`` command."""
    if sys.argv[1:2] == ["ext"]:
        ext_cli(sys.argv[2:])
    opts = parse_args(sys.argv[1:])
    if opts is None:
        return
    if opts.login is not None or opts.logout is not None:
        try:
            sign_in_or_out(opts)
        except KeyboardInterrupt:
            print("\nLogin cancelled.", file=sys.stderr)
            sys.exit(130)
        except Exception as e:
            print(e, file=sys.stderr)
            sys.exit(1)
        return

    if not setup.can_run():
        print(
            "minimal needs a terminal: start it there and type your prompt in the interface.",
            file=sys.stderr,
        )
        sys.exit(2)

    cwd = os.getcwd()
    try:
        session = open_session(opts.session or (last_session(cwd) if opts.cont else None) or new_id(), cwd)
        session.lock()
    except ValueError as e:  # a session id that is not a plain name, or one open in another minimal
        print(e, file=sys.stderr)
        sys.exit(2)
    try:
        _run(session, opts)
    finally:
        session.unlock()  # App.shutdown has released it, unless minimal stopped before the UI


def _run(session: Session, opts: argparse.Namespace) -> None:
    """Build the App on ``session``, start the provider and run the terminal UI until it exits."""
    if session.torn_at is not None:
        print(f"warning: {session.id}: {TORN_NOTICE}", file=sys.stderr)
    app = App(
        registry=Registry(),
        session=session,
        cwd=os.getcwd(),
        auto_approve=opts.yes,
        trust_project=opts.trust_project,
        extra_extensions=opts.ext,
        register_commands=register_builtin_commands,
    )
    app.reload_extensions()
    try:
        config.apply_keys()
        start_provider(app, opts)
    except KeyboardInterrupt:
        print("\nSetup cancelled.", file=sys.stderr)
        sys.exit(130)
    except (
        Exception
    ) as e:  # unknown provider, missing credentials, failed login, unreadable config
        print(e, file=sys.stderr)
        sys.exit(1)

    try:
        from minimal.ui.tui.app import run_tui  # imports Textual: only when the UI opens

        run_tui(Host(app))
    finally:
        app.shutdown()


def sign_in_or_out(opts: argparse.Namespace) -> None:
    """Run ``--logout`` then ``--login`` on the provider they name, or on the only one with a login.

    Providers added by the global extensions and ``--ext`` are known; the project's own are not run before ``/trust``.

    Raises:
        ValueError: if no provider, or more than one, could be meant, or it has no such action.
    """
    cwd = os.getcwd()
    registry = Registry()
    register_builtin_providers(registry)
    layers, _ = config.read_layers(cwd)
    load_extensions(registry, cwd, config.Settings(tuple(layers), False), opts.ext)
    for action, given in (("logout", opts.logout), ("login", opts.login)):
        if given is None:
            continue
        spec = login_provider(registry.providers, given or None)
        run = getattr(spec, action)
        if run is None:
            raise ValueError(f"{spec.name} has no {action}")
        run()


def ext_install(args: list[str]) -> None:
    """``minimal ext install [--project] [<category>/]<name>``: install one marketplace item and exit 0, or 1 on failure.

    A global install shows the static scan of the code and asks first, as ``/ext`` does, so it needs a terminal.
    """
    names = [a for a in args if not a.startswith("--")]
    flags = {a for a in args if a.startswith("--")}
    if len(names) != 1 or flags - {"--project"}:
        print("usage: minimal ext install [--project] [<category>/]<name>", file=sys.stderr)
        sys.exit(2)
    scope = "project" if flags else "global"
    category, _, name = names[0].rpartition("/")
    frontend = ConsoleFrontend()
    try:
        found = [r for r in marketplace.releases(name, category=category or None) if r.listing.name == name]
        if not found:
            raise ValueError(f"no item named {names[0]} in the marketplace (/ext lists them)")
        if len(found) > 1:
            options = ", ".join(f"{r.listing.category}/{name}" for r in found)
            raise ValueError(f"{name} is in several categories: {options}; name one of them")
        release = found[0]
        if scope == "global" and not marketplace.confirm_global([release], frontend.ask):
            print("Nothing installed.", file=sys.stderr)
            sys.exit(1)
        dest, updated = marketplace.install(name, scope, os.getcwd(), category=release.listing.category)
        print(f"{'Updated' if updated else 'Installed'} {dest}; minimal loads it when it starts.")
        message = marketplace.ensure_dependencies([release.listing], frontend.ask)
        if message:
            print(message)
    except (ValueError, RuntimeError) as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    sys.exit(0)


def ext_cli(args: list[str]) -> None:
    """``minimal ext check [--install-deps] <path>...``: print one line per extension checked and exit 0, or 1 if any failed.

    ``minimal ext install ...`` is :func:`ext_install`.
    """
    if args[:1] == ["install"]:
        ext_install(args[1:])
    install_deps = len(args) > 1 and args[1] == "--install-deps"
    paths = args[2:] if install_deps else args[1:]
    if not args or args[0] != "check" or not paths:
        print("usage: minimal ext check [--install-deps] <path>...\n       minimal ext install [--project] [<category>/]<name>", file=sys.stderr)
        sys.exit(2)

    def make_registry() -> Registry:
        # The surface App.reload_extensions gives extensions: they may wrap any of it at register time.
        registry = Registry()
        register_builtin_tools(registry)
        register_builtin_providers(registry)
        marketplace.register_marketplace_tools(registry)
        register_builtin_commands(registry)
        return registry

    lines, ok = marketplace.check([Path(p) for p in paths], make_registry, install_deps)
    print("\n".join(lines))
    sys.exit(0 if ok else 1)


def start_provider(app: App, opts: argparse.Namespace) -> None:
    """Start the provider: ``--provider``, else ``$MINIMAL_PROVIDER``, else the settings' provider.

    Setup runs first with ``--setup``, when no settings file names a provider, or when
    that provider cannot start (for example after ``minimal --logout``).
    Without a terminal, those cases are errors instead.
    """
    model = opts.model or os.environ.get("MINIMAL_MODEL")
    explicit = opts.provider or os.environ.get("MINIMAL_PROVIDER")
    if explicit and not opts.setup:
        same = app.settings.provider == explicit
        app.use_provider(explicit, model or (app.settings.model if same else None))
        return
    saved = None if opts.setup else app.settings.provider
    if saved is not None:
        try:
            app.use_provider(saved, model or app.settings.model)
            return
        except ValueError as e:
            if not setup.can_run():
                raise
            print(f"{e}\n", file=sys.stderr)
    elif not setup.can_run():
        raise ValueError(
            "No default provider yet: run `minimal --setup` in a terminal, or pass --provider."
        )
    name, saved_model = setup.run_setup(app.registry, ConsoleFrontend())
    app.use_provider(name, model or saved_model)


def parse_args(argv: list[str]) -> argparse.Namespace | None:
    """Parse command-line options.

    Returns ``None`` after printing the help for ``-h``. Exits with status 2
    after printing the help for unknown or malformed options.
    """
    parser = argparse.ArgumentParser(prog="minimal", add_help=False, allow_abbrev=False)
    parser.add_argument("-c", "--continue", dest="cont", action="store_true")
    parser.add_argument("-s", "--session")
    parser.add_argument("--provider")
    parser.add_argument("-m", "--model")
    parser.add_argument("-e", "--ext", action="append", default=[])
    parser.add_argument("-y", "--yes", action="store_true")
    parser.add_argument("--trust-project", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--login", nargs="?", const="", default=None, metavar="provider")
    parser.add_argument("--logout", nargs="?", const="", default=None, metavar="provider")
    help_text = HELP

    try:
        opts, unknown = parser.parse_known_args(argv)
    except SystemExit:  # malformed option, e.g. -s without a value
        print(help_text)
        sys.exit(2)
    if unknown:
        print(f"Unknown option: {unknown[0]}\n", file=sys.stderr)
        print(help_text)
        sys.exit(2)
    if opts.help:
        print(help_text)
        return None
    return opts


if __name__ == "__main__":
    main()
