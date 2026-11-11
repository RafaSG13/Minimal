"""The bash tool."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Any

from minimal import config
from minimal.extensions.registry import Tool


def _command_env() -> dict[str, str]:
    """The environment for ``bash`` commands: ours without the API keys saved by setup (``~/.minimal/keys.json``).

    Those keys are for the providers; a command the model runs has no use for
    them and could send them anywhere. Excluded are the names listed in the
    file now plus every name this process ever copied into ``os.environ``, so
    deleting or rewriting the file mid-session does not expose them. Variables
    exported in the shell under other names are kept.

    Raises:
        ValueError: if the key store exists but cannot be read: the keys can't
            be told apart from other variables, so no command runs.
    """
    saved = config.injected_key_names() | config.load_keys().keys()
    return {k: v for k, v in os.environ.items() if k not in saved}


MAX_CAPTURE = 1_000_000
"""Bytes of each of a command's streams kept by :func:`bash`; the rest is read and discarded."""


def _drain(pipe: Any, chunks: list[bytes]) -> None:
    """Read ``pipe`` to EOF, keeping its first :data:`MAX_CAPTURE` bytes in ``chunks``."""
    kept = 0
    with pipe:
        # read1 returns whatever is available: data must not sit in the buffer while a child keeps the pipe open
        for chunk in iter(lambda: pipe.read1(65536), b""):
            if kept < MAX_CAPTURE:
                chunk = chunk[: MAX_CAPTURE - kept]
                chunks.append(chunk)
                kept += len(chunk)


def _decode_output(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def bash(input: dict, ctx: Any) -> str:
    """Run a command with ``/bin/bash -c`` and report stdout, stderr and the exit code.

    On timeout the whole process group is killed, so child processes die too,
    and the result ends with ``[exit 1 (timeout)]``. The same happens, with
    ``[exit 1 (interrupted)]``, when ``ctx.cancelled`` turns true while the command runs.

    The command counts as running until it has exited and closed both pipes
    (a background child that keeps them open runs into the timeout). Only the
    first :data:`MAX_CAPTURE` bytes of each stream are kept; the agent loop clips
    what the model sees after the ``tool_result`` handlers saw all of it.
    """
    timeout_s = input.get("timeout_ms", 120_000) / 1000
    proc = subprocess.Popen(
        ["/bin/bash", "-c", input["command"]],
        cwd=ctx.cwd,
        env=_command_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,  # own process group, so a timeout can kill all of it
    )
    stdout: list[bytes] = []
    stderr: list[bytes] = []
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, stdout), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout_s
    while True:
        slice_end = min(time.monotonic() + 0.2, deadline)
        for reader in readers:
            reader.join(max(0.0, slice_end - time.monotonic()))
        try:
            proc.wait(max(0.0, slice_end - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
        if proc.returncode is not None and not any(reader.is_alive() for reader in readers):
            status = f"exit {proc.returncode}"
            break
        timed_out = time.monotonic() >= deadline
        if timed_out or ctx.cancelled:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            for reader in readers:  # a child that left the group may still hold a pipe: don't wait for it
                reader.join(1)
            status = "exit 1 (timeout)" if timed_out else "exit 1 (interrupted)"
            break

    out = _decode_output(stdout)
    err = _decode_output(stderr)
    if err:
        out += f"\n[stderr]\n{err}"
    out += f"\n[{status}]"
    return out.strip()


BASH = Tool(
    name="bash",
    description=(
        "Run a shell command in the working directory. Returns stdout, stderr and the exit code. "
        "API keys saved by minimal's setup are not in its environment."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to run"},
            "timeout_ms": {"type": "integer", "description": "Maximum time in milliseconds (default 120000)"},
        },
        "required": ["command"],
    },
    run=bash,
    confirm=True,
    subject_key="command",
)
