"""The built-in ``console`` frontend (a plain line-by-line REPL) and the discovery of installed ones.

A frontend is registered like an extension's (:meth:`~minimal.extensions.registry.Registry.register_frontend`),
so ``--ui`` or the ``ui`` setting picks one, and an extension can register another or replace one by name.
A package ships a frontend by declaring an entry point in the group :data:`ENTRY_POINT_GROUP`::

    [project.entry-points."minimal.frontends"]
    tui = "minimal_textual:register"

``register`` is called as ``register(registry)`` with the :class:`~minimal.extensions.registry.Registry`
on every reload and calls ``registry.register_frontend(name, run, description, toolkit=...)``.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from importlib.metadata import entry_points

import minimal
from minimal import glyphs
from minimal.core.app import ConsoleFrontend, ExitRepl
from minimal.core.host import Host
from minimal.extensions.registry import Registry

ENTRY_POINT_GROUP = "minimal.frontends"
"""Entry-point group of installed packages that provide frontends."""


def register_builtin_frontends(registry: Registry) -> None:
    """Register ``console``."""
    registry.register_frontend("console", run_console, "a plain console: one line per message, no colours or widgets")


def register_installed_frontends(registry: Registry) -> list[str]:
    """Let every ``minimal.frontends`` entry point register its frontends; return one error line per one that failed."""
    errors = []
    for entry in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name):
        try:
            entry.load()(registry)
        except Exception as e:
            errors.append(f"{ENTRY_POINT_GROUP} {entry.name} ({entry.value}): {type(e).__name__}: {e}")
    return errors


def register_frontends(registry: Registry) -> list[str]:
    """Register ``console`` and then the installed frontends; the errors of the latter, as from :func:`register_installed_frontends`."""
    register_builtin_frontends(registry)
    return register_installed_frontends(registry)


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
    for skipped in host.extension_skipped:
        print(f"[extension] skipped {skipped}", file=sys.stderr)
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
