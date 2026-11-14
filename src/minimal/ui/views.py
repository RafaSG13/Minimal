"""What extensions add to the interface, as styled lines for a ``minimal serve`` client.

Panels (``register_panel``), status-line segments (``register_status``) and block renderers
(``register_renderer``) are Python functions, so they run here; the client only draws the
``(text, style)`` lines they return.
"""

from __future__ import annotations

import json
import queue
import threading
import time

from minimal.extensions.registry import styled_line, styled_lines

TICK = 0.25
"""Seconds between looks at which panels are due for an ``interval`` redraw."""


class Views:
    """Runs the extensions' panels, status segments and renderers and sends what they draw."""

    def __init__(self, host, bridge) -> None:
        self.host = host
        self.bridge = bridge
        self.lock = threading.RLock()  # extensions' code is written for one UI thread: run it one at a time
        self.failed: set[str] = set()  # panels hidden after an error, until the next reload
        self.reported: set[str] = set()
        self.lines: dict[str, list] = {}  # each panel's last drawing
        self.ran: dict[str, float] = {}  # when each panel last rendered
        self.sent: dict[str, str] = {}
        self.requests: queue.Queue = queue.Queue()

    def start(self) -> None:
        threading.Thread(target=self.answer_renders, name="serve-renderers", daemon=True).start()
        threading.Thread(target=self.tick, name="serve-panels", daemon=True).start()

    # --- errors ------------------------------------------------------------------

    def report(self, what: str, error: Exception) -> None:
        """Tell the user once that ``what`` failed."""
        if what not in self.reported:
            self.reported.add(what)
            self.bridge.send(t="notify", text=f"[extension] {what}: {error}", kind="error")

    def reset(self) -> None:
        """A reload rebuilt the registry: panels get another chance."""
        with self.lock:
            self.failed.clear()
            self.reported.clear()
            self.lines.clear()
            self.ran.clear()
            self.sent.clear()

    # --- panels and status segments -----------------------------------------------

    def refresh(self, event: dict | None = None, due_only: bool = False) -> None:
        """Redraw every panel (calling its ``live`` with ``event`` first) and the status segments, and send what changed.

        With ``due_only``, only the panels whose ``interval`` has passed render again.
        """
        with self.lock:
            ctx = self.host.ctx
            now = time.monotonic()
            panels = []
            for name, spec in list(self.host.panels.items()):
                if name in self.failed:
                    continue
                if due_only and not (spec.interval and now - self.ran.get(name, 0.0) >= spec.interval) and name in self.lines:
                    lines = self.lines[name]
                else:
                    try:
                        if event is not None and spec.live is not None:
                            spec.live(ctx, event)
                        lines = styled_lines(spec.render(ctx)) or []
                    except Exception as e:
                        self.failed.add(name)
                        self.report(f"panel {name}", e)
                        continue
                    self.lines[name] = lines
                    self.ran[name] = now
                panels.append({"name": name, "area": spec.area, "lines": lines})
            segments = self.segments()
        self.push("panels", panels)
        self.push("segments", segments)

    def segments(self) -> dict:
        """The status line's extension segments: one line of pieces each, by side."""
        out: dict[str, list] = {"left": [], "right": []}
        for name, spec in list(self.host.status_segments.items()):
            try:
                value = spec.render(self.host.ctx)
                if value is not None:
                    out[spec.side].append(styled_line(value))
            except Exception as e:
                self.report(f"status {name}", e)
        return out

    def push(self, kind: str, value) -> None:
        """Send ``value`` unless it is what the client already has."""
        key = json.dumps(value, default=str, sort_keys=True)
        if self.sent.get(kind) != key:
            self.sent[kind] = key
            if kind == "panels":
                self.bridge.send(t="panels", panels=value)
            else:
                self.bridge.send(t="segments", **value)

    def tick(self) -> None:
        """Run in a thread: redraw the panels that ask for it every ``interval`` seconds."""
        while not self.bridge.closed.wait(TICK):
            now = time.monotonic()
            with self.lock:
                due = any(
                    spec.interval and spec.name not in self.failed and now - self.ran.get(spec.name, 0.0) >= spec.interval
                    for spec in list(self.host.panels.values())
                )
            if due:
                self.refresh(due_only=True)

    # --- renderers ----------------------------------------------------------------

    def kinds(self) -> list[str]:
        """The block kinds extensions registered a renderer for."""
        return sorted(self.host.renderers)

    def block(self, kind: str, block: dict) -> list | None:
        """The lines ``kind``'s renderer draws for ``block``; ``None`` for the default drawing (or if it failed)."""
        spec = self.host.renderers.get(kind)
        if spec is None:
            return None
        with self.lock:
            try:
                return styled_lines(spec.render(self.host.ctx, block))
            except Exception as e:
                self.report(f"renderer {kind}", e)
                return None

    def tool(self, call: dict, result: dict | None) -> list | None:
        kind = f"tool:{call['name']}"
        return self.block(kind, {"kind": kind, "call": call, "result": result})

    def bodies(self, messages: list[dict]) -> dict[str, list]:
        """Tool renderer output for the tool calls of a resumed session, by call id."""
        results: dict[str, dict] = {}
        for message in messages:
            if isinstance(message.get("content"), list):
                for part in message["content"]:
                    if part.get("type") == "tool_result":
                        results[part["tool_use_id"]] = part
        out: dict[str, list] = {}
        for message in messages:
            if isinstance(message.get("content"), list):
                for part in message["content"]:
                    if part.get("type") == "tool_use" and f"tool:{part['name']}" in self.host.renderers:
                        lines = self.tool(part, results.get(part["id"]))
                        if lines is not None:
                            out[part["id"]] = lines
        return out

    def request(self, message: dict) -> None:
        """Queue a ``render`` request from the client (a renderer may be slow: not on the reading thread)."""
        self.requests.put(message)

    def answer_renders(self) -> None:
        """Run in a thread: answer the client's render requests."""
        while not self.bridge.closed.is_set():
            try:
                message = self.requests.get(timeout=0.2)
            except queue.Empty:
                continue
            lines = self.block(str(message.get("kind")), message.get("block") or {})
            self.bridge.send(t="rendered", id=message.get("id"), lines=lines)
