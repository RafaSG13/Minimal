"""Command-line entry point: parse options, build the :class:`~minimal.core.app.App`
and open an interactive frontend (``console``, or one an installed package or extension adds), or
serve one over JSON lines (``minimal serve``). A turn can only be started from inside a frontend:
there is no one-shot mode that runs the agent from the shell command line.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from minimal import config
from minimal.core.app import App
from minimal.core.host import Host
from minimal.core.session import TORN_NOTICE, Session, last_session, new_id, open_session
from minimal.extensions import marketplace, migrate
from minimal.extensions.loader import frontend_folder
from minimal.extensions.registry import Registry
from minimal.providers import claude_auth, register_builtin_providers
from minimal.tools.builtin import register_builtin_tools
from minimal.ui import serve, setup
from minimal.ui.commands import register_builtin_commands
from minimal.ui.frontends import EXTERNAL_FRONTENDS, exec_external, register_builtin_frontends, register_frontends, scan_frontends

HELP = """minimal — minimal, extensible coding-agent harness

Usage:
  minimal [options]            interactive interface in the chosen frontend (runs setup first if no provider is chosen yet)
  minimal serve [options]      no interface: serves minimal to a frontend over JSON lines on stdin/stdout
                               (see minimal serve --help and docs/serve.md)
  minimal --setup              choose the default provider, sign in or enter its key, choose the model
  minimal --login              sign in with a Claude Pro/Max subscription (browser)
  minimal --logout             forget the Claude login
  minimal ext check [--install-deps] <path>...
                               validate extension files, package folders or a marketplace checkout
                               (exit status 1 if any fails; runs without a terminal or a provider);
                               --install-deps first installs their missing __dependencies__

Options:
  -c, --continue        resume the last session of this directory
  -s, --session <id>    open (or create) a specific session
  --provider <name>     model integration: anthropic, claude, gemini, openai, or one added by an extension
                        (default $MINIMAL_PROVIDER, else the settings' provider)
  -m, --model <name>    model (default $MINIMAL_MODEL, else the settings' model, else the provider's default)
  -e, --ext <path>      load an extension file or folder (repeatable)
  --ui <name>           frontend: console (plain line-by-line), tui (minimal-textual), go (the minimal-go program),
                        or one added by another package or an extension. Default: the settings' ui; unset, the
                        installed one, or a menu to choose (and save) one when there are several
  -y, --yes             do not ask for confirmation for write/edit/bash
  --trust-project       apply this project's extensions and trust-gated settings without /trust
  -h, --help            this help

Settings:
  ~/.minimal/settings.json         global: provider, model, theme, ui, prompt, permissions, extensions, marketplaces
                                   (setup, /theme, /ext and the frontend menu save there)
  .minimal/settings.json           this project's settings, shared with it
  .minimal/settings.local.json     this project's personal settings; both beat the global file
                                   (/settings shows the files and where each value comes from)

Environment:
  ANTHROPIC_API_KEY             for --provider anthropic
  GEMINI_API_KEY/GOOGLE_API_KEY for --provider gemini
  OPENAI_API_KEY                for --provider openai (OPENAI_BASE_URL points it at OpenRouter, Ollama, LM Studio)
                                (a key entered in setup is saved in ~/.minimal/keys.json and wins over these)
  --provider claude uses the login from minimal --login
  MINIMAL_PROVIDER, MINIMAL_MODEL
  MINIMAL_MARKETPLACE           git URL or path of the extension marketplace used by the
                                model's tools and listed first by /ext
                                (default https://github.com/RafaSG13/minimal-marketplace.git)

Extensions are kept per frontend: ~/.minimal/extensions/<frontend>/ (console, textual, go) and
.minimal/extensions/<frontend>/ in a project; only the running frontend's folder loads."""

SERVE_HELP = """minimal serve — minimal without an interface, for a frontend that drives it over JSON lines

Usage:
  minimal serve [options]

Reads one JSON message per line on stdin and writes one per line on stdout (nothing else is
written there; anything extensions or commands print goes to stderr). Start it from the frontend
program, in the project directory. Protocol: docs/serve.md. A provider must already be chosen
(`minimal --setup`, or --provider); /setup and /login run on the controlling terminal.

Options:
  -c, --continue        resume the last session of this directory
  -s, --session <id>    open (or create) a specific session
  --provider <name>     model integration (default $MINIMAL_PROVIDER, else the settings' provider)
  -m, --model <name>    model (default $MINIMAL_MODEL, else the settings' model, else the provider's default)
  -e, --ext <path>      load an extension file or folder (repeatable)
  --ui <name>           the frontend being served (default console): its folder under ~/.minimal/extensions/ and
                        .minimal/extensions/ is the one loaded, and its marketplace folder the one /ext lists
  -y, --yes             do not ask for confirmation for write/edit/bash
  --trust-project       apply this project's extensions and trust-gated settings without /trust
  -h, --help            this help

Only the extensions of the served frontend's folder load (a __frontend__ = "textual" extension needs --ui tui).
The settings and the environment are the same as for minimal (see minimal --help)."""


def main() -> None:
    """Entry point of the ``minimal`` command."""
    if sys.argv[1:2] == ["ext"]:
        ext_cli(sys.argv[2:])
    argv = sys.argv[1:]
    serving = argv[:1] == ["serve"]
    opts = parse_args(argv[1:] if serving else argv, serving)
    if opts is None:
        return
    if opts.login or opts.logout:
        try:
            if opts.logout:
                claude_auth.logout()
            if opts.login:
                claude_auth.login()
        except KeyboardInterrupt:
            print("\nLogin cancelled.", file=sys.stderr)
            sys.exit(130)
        except Exception as e:
            print(e, file=sys.stderr)
            sys.exit(1)
        return

    if not serving and not setup.can_run():
        print(
            "minimal needs a terminal: start it there and type your prompt in the interface.",
            file=sys.stderr,
        )
        sys.exit(2)

    cwd = os.getcwd()
    migrate_extensions(cwd)
    if not serving:
        try:
            opts.ui = resolve_ui(opts.ui, cwd)
        except KeyboardInterrupt:
            print("\nCancelled.", file=sys.stderr)
            sys.exit(130)
        if opts.ui in EXTERNAL_FRONTENDS:
            start_external(opts, argv)
    try:
        session = open_session(opts.session or (last_session(cwd) if opts.cont else None) or new_id(), cwd)
        session.lock()
    except ValueError as e:  # a session id that is not a plain name, or one open in another minimal
        print(e, file=sys.stderr)
        sys.exit(2)
    try:
        _run(session, opts, serving)
    finally:
        session.unlock()  # App.shutdown has released it, unless minimal stopped before the UI


def _run(session: Session, opts: argparse.Namespace, serving: bool = False) -> None:
    """Build the App on ``session``, start the provider and run the chosen frontend (or serve one) until it exits."""
    if session.torn_at is not None:
        print(f"warning: {session.id}: {TORN_NOTICE}", file=sys.stderr)
    streams = serve.take_streams() if serving else None
    app = App(
        registry=Registry(),
        session=session,
        cwd=os.getcwd(),
        auto_approve=opts.yes,
        trust_project=opts.trust_project,
        extra_extensions=opts.ext,
        register_commands=register_builtin_commands,
        register_frontends=register_builtin_frontends if serving else register_frontends,
        ui=opts.ui,
    )
    app.reload_extensions()
    ui = app.ui
    frontend = app.registry.frontends.get(ui)
    if frontend is None and not serving:  # a served frontend is an external program: it need not be registered
        hint = "; install minimal-textual: https://github.com/RafaSG13/minimal-marketplace/tree/main/textual" if ui == "tui" else ""
        print(f"unknown frontend {ui!r}; available: {', '.join(sorted(app.registry.frontends))}{hint}", file=sys.stderr)
        sys.exit(2)
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
        if streams is None:
            assert frontend is not None  # checked above for everything but serving
            frontend.run(Host(app))
        else:
            serve.run(Host(app), *streams)
    finally:
        app.shutdown()


def ext_cli(args: list[str]) -> None:
    """``minimal ext check [--install-deps] <path>...``: print one line per extension checked and exit 0, or 1 if any failed."""
    install_deps = len(args) > 1 and args[1] == "--install-deps"
    paths = args[2:] if install_deps else args[1:]
    if not args or args[0] != "check" or not paths:
        print("usage: minimal ext check [--install-deps] <path>...", file=sys.stderr)
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


def migrate_extensions(cwd: str) -> None:
    """Move the old flat extension layout into per-frontend folders and tell the user what moved (:mod:`minimal.extensions.migrate`); a failure is a warning."""

    def folders() -> list[str]:
        registry = scan_frontends()
        return [frontend_folder(registry, name) for name in registry.frontends]

    try:
        notices = [migrate.migrate_global(folders), migrate.project_notice(cwd)]
    except OSError as e:
        print(f"warning: could not check the extension folders for the old layout: {e}", file=sys.stderr)
        return
    for notice in notices:
        if notice:
            print(notice, file=sys.stderr)


def resolve_ui(explicit: str | None, cwd: str) -> str:
    """The frontend to run: ``--ui`` (``explicit``), else the ``ui`` setting, else what is installed.

    Installed means ``console`` plus the frontends found by :func:`~minimal.ui.frontends.scan_frontends`. With one
    besides ``console`` that one is used; with several, a menu asks and the choice is saved as ``ui`` in the global
    settings; with none, ``console``.

    Raises:
        KeyboardInterrupt: if the user cancels the menu.
    """
    if explicit:
        return explicit
    saved = config.Settings(tuple(config.read_layers(cwd)[0])).ui
    if saved:
        return saved
    available = scan_frontends().frontends
    others = [name for name in available if name != "console"]
    if not others:
        return config.DEFAULT_UI
    if len(others) == 1:
        return others[0]
    choice = setup.choose_frontend({name: spec.description for name, spec in available.items()})
    try:
        config.update_global({"ui": choice})
        print(f"Saved {choice} as the default frontend (~/.minimal/settings.json). Change it with the `ui` setting or --ui.")
    except (OSError, ValueError) as e:
        print(f"warning: could not save the frontend choice ({e}); it will be asked again", file=sys.stderr)
    return choice


def passthrough_args(argv: list[str]) -> list[str]:
    """``argv`` without what an external frontend must not receive: ``--ui <name>`` (it is told with ``minimal serve --ui``) and ``--setup`` (done before)."""
    args: list[str] = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "--ui":
            skip = True
        elif not arg.startswith("--ui=") and arg != "--setup":
            args.append(arg)
    return args


def start_external(opts: argparse.Namespace, argv: list[str]) -> None:
    """Run setup if no provider is chosen yet, then replace this process with the external frontend (``opts.ui``) given the rest of ``argv``. Does not return.

    The frontend starts ``minimal serve --ui <name>`` itself, which locks the session: nothing is locked here.
    """
    try:
        config.apply_keys()
        ensure_provider(opts)
    except KeyboardInterrupt:
        print("\nSetup cancelled.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    binary = EXTERNAL_FRONTENDS[opts.ui]
    try:
        exec_external(opts.ui, passthrough_args(argv))
    except OSError as e:
        print(f"cannot start the {opts.ui} frontend: {binary}: {e}; install {binary} and put it on the PATH", file=sys.stderr)
        sys.exit(1)


def ensure_provider(opts: argparse.Namespace) -> None:
    """Run setup like :func:`start_provider` does when no provider is chosen yet (or ``--setup``, or the saved one cannot start); nothing else.

    Providers added by extensions are not known here: a provider that only an extension offers is left for ``minimal serve`` to start.
    """
    if (opts.provider or os.environ.get("MINIMAL_PROVIDER")) and not opts.setup:
        return
    registry = Registry()
    register_builtin_providers(registry)
    saved = None if opts.setup else config.Settings(tuple(config.read_layers(os.getcwd())[0])).provider
    if saved is not None:
        spec = registry.providers.get(saved)
        problem = spec.missing_credentials() if spec is not None else None
        if problem is None:
            return
        print(f"{problem}\n", file=sys.stderr)
    setup.run_setup(registry)


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
    name, saved_model = setup.run_setup(app.registry)
    app.use_provider(name, model or saved_model)


def parse_args(argv: list[str], serving: bool = False) -> argparse.Namespace | None:
    """Parse command-line options; ``serving`` for those after ``minimal serve``, which has no interface options.

    Returns ``None`` after printing the help for ``-h``. Exits with status 2
    after printing the help for unknown or malformed options.
    """
    parser = argparse.ArgumentParser(prog="minimal serve" if serving else "minimal", add_help=False, allow_abbrev=False)
    parser.add_argument("-c", "--continue", dest="cont", action="store_true")
    parser.add_argument("-s", "--session")
    parser.add_argument("--provider")
    parser.add_argument("-m", "--model")
    parser.add_argument("-e", "--ext", action="append", default=[])
    parser.add_argument("-y", "--yes", action="store_true")
    parser.add_argument("--trust-project", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("--ui")
    parser.set_defaults(setup=False, login=False, logout=False)
    if not serving:
        parser.add_argument("--setup", action="store_true")
        parser.add_argument("--login", action="store_true")
        parser.add_argument("--logout", action="store_true")
    help_text = SERVE_HELP if serving else HELP

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
    if serving:
        opts.ui = opts.ui or config.DEFAULT_UI
    return opts


if __name__ == "__main__":
    main()
