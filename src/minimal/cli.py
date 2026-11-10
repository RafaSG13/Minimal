"""Command-line entry point: parse options, build the :class:`~minimal.app.App`
and open the interactive terminal UI. A turn can only be started from inside it:
there is no one-shot mode that runs the agent from the shell command line.
"""

from __future__ import annotations

import argparse
import os
import sys

from minimal import config, setup
from minimal.app import App
from minimal.providers import claude_auth
from minimal.registry import Registry
from minimal.session import last_session, new_id, open_session

HELP = """minimal — minimal, extensible coding-agent harness

Usage:
  minimal [options]            interactive terminal UI (runs setup first if no provider is chosen yet)
  minimal --setup              choose the default provider, sign in or enter its key, choose the model
  minimal --login              sign in with a Claude Pro/Max subscription (browser)
  minimal --logout             forget the Claude login

Options:
  -c, --continue        resume the last session
  -s, --session <id>    open (or create) a specific session
  --provider <name>     model integration: anthropic, claude, gemini, openai, or one added by an extension
                        (default $MINIMAL_PROVIDER, else the one chosen in setup)
  -m, --model <name>    model (default $MINIMAL_MODEL, else the one chosen in setup, else the provider's default)
  -e, --ext <path>      load an extension file or folder (repeatable)
  -y, --yes             do not ask for confirmation for write/edit/bash
  --trust-project       load this project's .minimal/extensions without asking
  -h, --help            this help

Environment:
  ANTHROPIC_API_KEY             for --provider anthropic
  GEMINI_API_KEY/GOOGLE_API_KEY for --provider gemini
  OPENAI_API_KEY                for --provider openai (OPENAI_BASE_URL points it at OpenRouter, Ollama, LM Studio)
                                (a key entered in setup is saved in ~/.minimal/keys.json and wins over these)
  --provider claude uses the login from minimal --login
  MINIMAL_PROVIDER, MINIMAL_MODEL
  MINIMAL_MARKETPLACE           git URL or path of the extension marketplace used by the
                                model's tools and listed first by /ext
                                (default https://github.com/RafaSG13/minimal-marketplace.git)"""


def main() -> None:
    """Entry point of the ``minimal`` command."""
    opts = parse_args(sys.argv[1:])
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

    if not setup.can_run():
        print("minimal needs a terminal: start it there and type your prompt in the interface.", file=sys.stderr)
        sys.exit(2)

    try:
        session = open_session(opts.session or (last_session() if opts.cont else None) or new_id())
    except ValueError as e:  # a session id that is not a plain name
        print(e, file=sys.stderr)
        sys.exit(2)
    app = App(
        registry=Registry(),
        session=session,
        cwd=os.getcwd(),
        auto_approve=opts.yes,
        trust_project=opts.trust_project,
        extra_extensions=opts.ext,
    )
    app.reload_extensions()
    try:
        config.apply_keys()
        start_provider(app, opts)
    except KeyboardInterrupt:
        print("\nSetup cancelled.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:  # unknown provider, missing credentials, failed login, unreadable config
        print(e, file=sys.stderr)
        sys.exit(1)

    from minimal.tui import run_tui

    run_tui(app)


def start_provider(app: App, opts: argparse.Namespace) -> None:
    """Start the provider: ``--provider``, else ``$MINIMAL_PROVIDER``, else the one saved by setup.

    Setup runs first with ``--setup``, when nothing is saved yet, or when the
    saved provider cannot start (for example after ``minimal --logout``).
    Without a terminal, those cases are errors instead.
    """
    model = opts.model or os.environ.get("MINIMAL_MODEL")
    saved = None if opts.setup else config.load_config()
    explicit = opts.provider or os.environ.get("MINIMAL_PROVIDER")
    if explicit and not opts.setup:
        same = saved is not None and saved["provider"] == explicit
        app.use_provider(explicit, model or (saved["model"] if same else None))
        return
    if saved is not None:
        try:
            app.use_provider(saved["provider"], model or saved["model"])
            return
        except ValueError as e:
            if not setup.can_run():
                raise
            print(f"{e}\n", file=sys.stderr)
    elif not setup.can_run():
        raise ValueError("No default provider yet: run `minimal --setup` in a terminal, or pass --provider.")
    name, saved_model = setup.run_setup(app.registry)
    app.use_provider(name, model or saved_model)


def parse_args(argv: list[str]) -> argparse.Namespace | None:
    """Parse command-line options.

    Returns ``None`` after printing the help for ``-h``. Exits with status 2
    after printing the help for unknown or malformed options.
    """
    parser = argparse.ArgumentParser(prog="minimal", add_help=False)
    parser.add_argument("-c", "--continue", dest="cont", action="store_true")
    parser.add_argument("-s", "--session")
    parser.add_argument("--provider")
    parser.add_argument("-m", "--model")
    parser.add_argument("-e", "--ext", action="append", default=[])
    parser.add_argument("-y", "--yes", action="store_true")
    parser.add_argument("--trust-project", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--logout", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")

    try:
        opts, unknown = parser.parse_known_args(argv)
    except SystemExit:  # malformed option, e.g. -s without a value
        print(HELP)
        sys.exit(2)
    if unknown:
        if unknown[0] in ("-p", "--print"):
            print("-p was removed: start minimal and type your prompt in its interface.\n", file=sys.stderr)
        else:
            print(f"Unknown option: {unknown[0]}\n", file=sys.stderr)
        print(HELP)
        sys.exit(2)
    if opts.help:
        print(HELP)
        return None
    return opts


if __name__ == "__main__":
    main()
