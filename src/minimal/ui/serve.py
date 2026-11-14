"""``minimal serve``: minimal without a user interface, driven by a frontend over JSON lines.

A frontend (a program written in any language) starts ``minimal serve`` and talks to it on its
standard input and output, one JSON object per line, with the kind of message in ``"t"``. minimal
sends what to show (text, tool calls, questions, panels, …); the frontend sends what the user does
(a message to submit, an interrupt, an answer). ``docs/serve.md`` lists every message and field.

Standard output carries only protocol lines: :func:`take_streams` moves the real stdin and stdout
to private descriptors and points file descriptors 0 and 1 at the null device and at stderr, so
whatever extensions, commands or child processes print goes to stderr instead.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import os
import queue
import sys
import threading
from collections.abc import Iterator
from typing import Any, TextIO

from minimal import __version__
from minimal.core.app import ConsoleFrontend, ExitRepl
from minimal.core.host import Host
from minimal.ui.views import Views

PROTOCOL = 2
"""Version of the JSON-lines protocol; a client refuses another one."""

REPLAY_LIMIT = 200
"""Most messages of a resumed session sent to be drawn."""

SUSPEND_WAIT = 5
"""Seconds to wait for the client's ``suspended`` before running a terminal command anyway."""

QUEUED = object()
"""Put in the inbox when an extension queues a user message, to wake the main loop."""

TOOLKIT_UI = "console"
"""The frontend :class:`~minimal.core.app.App` is told is running: its toolkit (none) decides which extensions load."""


def take_streams() -> tuple[TextIO, TextIO]:
    """Claim the process's stdin and stdout for the protocol and return them as ``(inp, out)``.

    Afterwards file descriptor 0 reads the null device and file descriptor 1 is stderr, so ``print``,
    ``input()`` and child processes cannot touch the protocol.
    """
    protocol_in, protocol_out = os.dup(0), os.dup(1)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    sys.stdout.flush()
    os.dup2(2, 1)
    inp = open(protocol_in, "r", encoding="utf-8", newline="\n")
    out = open(protocol_out, "w", encoding="utf-8", newline="\n", buffering=1)
    return inp, out


class Output:
    """File-like that forwards what commands and extensions print to the client."""

    def __init__(self, bridge: Bridge, stderr: bool) -> None:
        self.bridge = bridge
        self.stderr = stderr

    def write(self, text: str) -> int:
        if text:
            self.bridge.send(t="output", text=text, stderr=self.stderr)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


class Bridge:
    """The client as seen from minimal: lines out to ``out``, lines in from ``inp``."""

    def __init__(self, host: Host, inp: TextIO, out: TextIO) -> None:
        self.host = host
        self.inp = inp
        self.out = out
        self.write_lock = threading.Lock()
        self.inbox: queue.Queue = queue.Queue()
        self.answers: dict[str, tuple[threading.Event, list]] = {}
        self.ids = itertools.count(1)
        self.closed = threading.Event()
        self.suspended = threading.Event()
        self.views: Views  # set by run() before the reader starts

    def send(self, **message: Any) -> None:
        line = json.dumps(message, default=str) + "\n"
        with self.write_lock:
            try:
                self.out.write(line)
                self.out.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass  # the client is gone; the reader notices and ends the session

    def question(self, **message: Any) -> Any:
        """Send a question and wait for the client's answer; ``None`` if it was cancelled or the client quit."""
        id = str(next(self.ids))
        done = threading.Event()
        slot: list = [None]
        self.answers[id] = (done, slot)
        self.send(id=id, **message)
        while not done.wait(0.2):
            if self.closed.is_set():
                break
        self.answers.pop(id, None)
        return slot[0]

    def read(self) -> None:
        """Run in a thread: turn the client's lines into submits, interrupts and answers."""
        for line in self.inp:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("t")
            if kind == "submit":
                self.inbox.put(str(message.get("text", "")).strip())
            elif kind == "interrupt":
                self.host.interrupt()
            elif kind == "answer":
                entry = self.answers.get(str(message.get("id")))
                if entry is not None:
                    entry[1][0] = message.get("value")
                    entry[0].set()
            elif kind == "render":
                self.views.request(message)
            elif kind == "suspended":
                self.suspended.set()
            elif kind == "quit":
                break
        self.closed.set()
        self.inbox.put(None)
        for done, _ in list(self.answers.values()):
            done.set()


class BridgeFrontend:
    """Shows turns in the client."""

    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge

    def text(self, chunk: str) -> None:
        self.bridge.send(t="text", chunk=chunk)

    def tool_call(self, call: dict) -> None:
        views = self.bridge.views
        self.bridge.send(t="tool_call", call=call, body=views.tool(call, None))
        views.refresh({"kind": "tool_call", "call": call})

    def tool_result(self, call: dict, result: dict) -> None:
        views = self.bridge.views
        self.bridge.send(t="tool_result", call=call, result=result, body=views.tool(call, result))
        views.refresh({"kind": "tool_result", "call": call, "result": result})

    def turn_end(self, error: str | None) -> None:
        self.bridge.send(t="turn_end", error=error)

    def ask(self, question: str) -> bool:
        return self.bridge.question(t="ask", question=question) is True

    def choose(self, title: str, options: list[tuple[str, str]]) -> str | None:
        value = self.bridge.question(t="choose", title=title, options=[{"value": v, "label": label} for v, label in options])
        return value if isinstance(value, str) else None

    def choose_many(self, title: str, options: list[tuple[str, str, bool]]) -> list[str] | None:
        value = self.bridge.question(
            t="choose_many",
            title=title,
            options=[{"value": v, "label": label, "checked": checked} for v, label, checked in options],
        )
        if not isinstance(value, list):
            return None
        ticked = {str(v) for v in value}
        return [v for v, _, _ in options if v in ticked]

    def ask_text(self, title: str) -> str | None:
        value = self.bridge.question(t="ask_text", title=title)
        return value.strip() or None if isinstance(value, str) else None

    def reloaded(self) -> None:
        views = self.bridge.views
        views.reset()
        self.bridge.send(t="reloaded", renderers=views.kinds(), **problems(self.bridge.host))
        views.refresh({"kind": "end"})

    def widget(self, name: str) -> object | None:
        return None

    def notify(self, text: str, kind: str) -> None:
        self.bridge.send(t="notify", text=text, kind=kind)

    def queued(self) -> None:
        self.bridge.inbox.put(QUEUED)


def problems(host: Host) -> dict:
    """What the last reload reported: failed and skipped extensions, and the untrusted notice."""
    return {
        "errors": host.extension_errors,
        "skipped": host.extension_skipped,
        "notice": host.untrusted_notice(),
    }


def status(host: Host) -> dict:
    return dataclasses.asdict(host.status())


@contextlib.contextmanager
def printing_to(bridge: Bridge) -> Iterator[None]:
    """Show what is printed meanwhile (command output, extension messages) in the client."""
    with contextlib.redirect_stdout(Output(bridge, False)), contextlib.redirect_stderr(Output(bridge, True)):  # type: ignore[type-var]
        yield


@contextlib.contextmanager
def on_terminal() -> Iterator[None]:
    """Point stdin, stdout and stderr at the controlling terminal meanwhile.

    Raises:
        OSError: if there is no terminal to open.
    """
    names = ("CONIN$", "CONOUT$") if os.name == "nt" else ("/dev/tty", "/dev/tty")
    with open(names[0], "r", encoding="utf-8") as tty_in, open(names[1], "w", encoding="utf-8", buffering=1) as tty_out:
        saved = sys.stdin, sys.stdout, sys.stderr
        sys.stdin, sys.stdout, sys.stderr = tty_in, tty_out, tty_out
        try:
            yield
        finally:
            sys.stdin, sys.stdout, sys.stderr = saved


def execute(host: Host, bridge: Bridge, line: str) -> None:
    """Run one user line: a ``/command`` or a message to the model. Raises ``ExitRepl`` for ``/exit``."""
    if line.startswith("/"):
        command = host.command(line[1:].partition(" ")[0])
        if command is not None and command.terminal:
            run_in_terminal(host, bridge, line)
            return
        with printing_to(bridge):
            host.run_command(line)
    else:
        with printing_to(bridge):
            host.submit(line)


def run_in_terminal(host: Host, bridge: Bridge, line: str) -> None:
    """Run a command that reads the real terminal: the client gives it up meanwhile."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(on_terminal())
        except OSError as e:
            bridge.send(t="notify", text=f"{line.partition(' ')[0]} needs a terminal: {e}", kind="error")
            return
        bridge.suspended.clear()
        bridge.send(t="suspend")
        bridge.suspended.wait(SUSPEND_WAIT)
        host.attach(ConsoleFrontend())
        try:
            host.run_command(line)
        except KeyboardInterrupt:
            print("\nCancelled.", file=sys.stderr)
        finally:
            host.attach(BridgeFrontend(bridge))
    bridge.send(t="resume")


def run(host: Host, inp: TextIO, out: TextIO) -> None:
    """Serve ``host`` on ``inp`` and ``out`` until the client quits or a command exits."""
    bridge = Bridge(host, inp, out)
    bridge.views = Views(host, bridge)
    bridge.views.start()
    threading.Thread(target=bridge.read, name="serve-reader", daemon=True).start()
    host.attach(BridgeFrontend(bridge))
    messages = host.messages[-REPLAY_LIMIT:]
    bridge.send(
        t="hello",
        protocol=PROTOCOL,
        version=__version__,
        status=status(host),
        messages=messages,
        hidden=len(host.messages) - len(messages),
        renderers=bridge.views.kinds(),
        bodies=bridge.views.bodies(messages),
        **problems(host),
    )
    try:
        with printing_to(bridge):
            host.begin()
        bridge.views.refresh({"kind": "end"})
        loop(host, bridge)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.send(t="quit")
        bridge.closed.set()
        with contextlib.suppress(OSError, ValueError):
            out.close()  # the client sees the end of the stream


def loop(host: Host, bridge: Bridge) -> None:
    """Run what the user types, one line at a time, until the client quits or a command exits."""
    while True:
        line = host.next_queued()
        if line is None:
            item = bridge.inbox.get()
            if item is None:
                return
            if item is QUEUED or not item:
                continue
            line = item
        try:
            bridge.views.refresh({"kind": "start", "turn": not line.startswith("/")})
            execute(host, bridge, line)
        except ExitRepl:
            return
        bridge.views.refresh({"kind": "end"})
        bridge.send(t="idle", status=status(host))
