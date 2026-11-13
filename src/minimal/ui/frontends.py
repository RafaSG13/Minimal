"""The built-in frontends: ``tui`` (the Textual terminal UI) and ``console`` (a plain line-by-line REPL).

Both are registered like an extension's frontend (:meth:`~minimal.extensions.registry.Registry.register_frontend`),
so ``--ui`` or the ``ui`` setting picks one, and an extension can register another or replace
these by name. Textual is only imported when the ``tui`` frontend starts.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable

import minimal
from minimal import glyphs
from minimal.core.app import ConsoleFrontend, ExitRepl
from minimal.core.host import Host
from minimal.extensions.registry import Registry

DEFAULT_UI = "tui"
"""The frontend used when neither ``--ui`` nor the ``ui`` setting names one."""


def register_builtin_frontends(registry: Registry) -> None:
    """Register ``tui`` and ``console``."""
    registry.register_frontend("tui", _run_tui, "the terminal UI: transcript, widgets, themes (default)")
    registry.register_frontend("console", run_console, "a plain console: one line per message, no colours or widgets")


def _run_tui(host: Host) -> None:
    from minimal.ui.tui.app import run_tui

    run_tui(host)


def run_console(host: Host) -> None:
    """Read lines until EOF: a ``/command`` runs it, anything else is sent to the model.

    Ctrl-C interrupts the turn in progress (or clears the line being typed); Ctrl-D or ``/exit`` quits.
    Messages extensions queue are sent between turns.
    """
    try:
        import readline  # noqa: F401 (line editing and history for input())
    except ImportError:
        pass
    host.attach(ConsoleFrontend())
    status = host.status()
    print(f"minimal {minimal.__version__}{glyphs.DOT}{status.provider} {status.model}{glyphs.DOT}session {status.session_id}")
    print("Type a message, or /help. Ctrl-C interrupts a turn, Ctrl-D quits.")
    for error in host.extension_errors:
        print(f"[extension] could not load {error}", file=sys.stderr)
    if notice := host.untrusted_notice():
        print(f"{glyphs.WARNING} {notice}", file=sys.stderr)
    host.begin()
    while True:
        line = host.next_queued()
        if line is None:
            try:
                line = input(f"\n{glyphs.PROMPT} ").strip()
            except EOFError:
                print()
                return
            except KeyboardInterrupt:
                print()
                continue
        if not line:
            continue
        try:
            if line.startswith("/"):
                command = host.command(line[1:].partition(" ")[0])
                if command is not None and command.terminal:
                    host.run_command(line)  # in this thread: it reads the terminal itself
                else:
                    _interruptible(host, lambda: host.run_command(line))
            else:
                _interruptible(host, lambda: host.submit(line))
        except ExitRepl:
            return
        except KeyboardInterrupt:
            print("\nCancelled.", file=sys.stderr)


def _interruptible(host: Host, action: Callable[[], None]) -> None:
    """Run ``action`` in a worker thread; Ctrl-C meanwhile interrupts the turn instead of killing minimal.

    Raises:
        Whatever ``action`` raised (``ExitRepl`` from ``/exit``).
    """
    raised: list[BaseException] = []
    done = threading.Event()

    def work() -> None:
        try:
            action()
        except BaseException as e:  # handed to the main thread
            raised.append(e)
        finally:
            done.set()

    threading.Thread(target=work, name="minimal-console", daemon=True).start()
    # Not Thread.join: a KeyboardInterrupt inside join() can leave is_alive() False while the thread still runs.
    while not done.is_set():
        try:
            done.wait(0.1)
        except KeyboardInterrupt:
            if not host.interrupting:
                host.interrupt()
                print("\nInterrupting…", file=sys.stderr)
    if raised:
        raise raised[0]
