"""The agent loop: ask the model, run the tools it calls, send back the results, repeat.

One call to :func:`run_agent` handles one user turn:

1. Stream a reply from the provider, offering every registered tool.
2. Append the reply to ``messages`` as an ``assistant`` message.
3. If the reply contains no tool calls, fire ``turn_end`` and return it.
4. Otherwise run each call (see :func:`execute_tool_call`), append the results
   as a ``user`` message and go back to step 1.

The loop only sees the canonical message format described in
:mod:`minimal.providers.base`; each provider translates to and from its own API.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable
from typing import Any

from minimal.extensions.registry import Registry, Tool, TurnInterrupted
from minimal.providers.base import Provider, Reply
from minimal.tools.common import clip

USER_DENIED = "the user denied the execution"
"""Error of a tool call the user refused (the model sees ``"Error: " + USER_DENIED``)."""

Confirm = Callable[[dict], bool]
"""Asks whether a ``tool_use`` call may run."""

Permit = Callable[[Tool, dict], bool]
"""Permission rules: ``True`` runs a call without asking; raising denies it."""


def raise_if_cancelled(cancelled: threading.Event | None) -> None:
    """Raise :class:`TurnInterrupted` if ``cancelled`` is set."""
    if cancelled is not None and cancelled.is_set():
        raise TurnInterrupted("interrupted by the user")


def run_agent(
    *,
    provider: Provider,
    model: str,
    system: str,
    messages: list[dict],
    registry: Registry,
    ctx: Any,
    on_text: Callable[[str], Any] | None = None,
    on_tool: Callable[[dict], Any] | None = None,
    on_tool_result: Callable[[dict, dict], Any] | None = None,
    confirm: Confirm | None = None,
    cancelled: threading.Event | None = None,
    permit: Permit | None = None,
    exclude_tools: frozenset[str] = frozenset(),
    nested: bool = False,
    max_turns: int = 50,
) -> Reply:
    """Run the model/tool loop until the model answers without calling tools.

    Args:
        provider: The model API to talk to.
        model: Model name for that provider.
        system: System prompt.
        messages: Conversation so far, in canonical format. Modified in place:
            assistant replies and tool results are appended.
        registry: Source of tools and event handlers.
        ctx: The :class:`~minimal.core.context.Context` passed to every tool and event.
        on_text: Called with each streamed text chunk.
        on_tool: Called with each ``tool_use`` block before it runs.
        on_tool_result: Called with each ``tool_use`` block and its ``tool_result`` block after it runs.
        confirm: Asked before running a tool marked ``confirm`` that ``permit`` did not allow.
        cancelled: Setting it interrupts the turn: at the next text chunk,
            before the next model request or before the next tool.
        permit: Permission rules, consulted after the ``tool_call`` handlers.
        exclude_tools: Tools neither offered to the model nor run (a subagent's own tool).
        nested: This loop runs inside a tool (a subagent); passed on in ``turn_end``.
        max_turns: Most model requests allowed for this turn.

    Returns:
        The final reply.

    Raises:
        RuntimeError: if the model is still calling tools after ``max_turns`` requests.
        TurnInterrupted: if ``cancelled`` is set while the turn runs.
    """

    def stream_text(chunk: str) -> None:
        raise_if_cancelled(cancelled)
        if on_text:
            on_text(chunk)

    for _ in range(max_turns):
        raise_if_cancelled(cancelled)
        reply = provider.stream(
            model=model,
            system=system,
            messages=messages,
            tools=tool_specs(registry, exclude_tools),
            on_text=stream_text,
        )
        messages.append(_assistant_message(provider, reply))

        calls = [block for block in reply.content if block["type"] == "tool_use"]
        if reply.stop_reason != "tool_use" or not calls:
            registry.emit("turn_end", {"messages": messages, "ctx": ctx, "nested": nested})
            return reply

        results = []
        for call in calls:
            raise_if_cancelled(cancelled)
            if on_tool:
                on_tool(call)
            result = execute_tool_call(call, registry, ctx, confirm=confirm, permit=permit, exclude=exclude_tools)
            if on_tool_result:
                on_tool_result(call, result)
            results.append(result)
        messages.append({"role": "user", "content": results})

    raise RuntimeError(f"exceeded the maximum of {max_turns} turns")


def execute_tool_call(
    call: dict,
    registry: Registry,
    ctx: Any,
    *,
    confirm: Confirm | None = None,
    permit: Permit | None = None,
    exclude: frozenset[str] = frozenset(),
) -> dict:
    """Run one ``tool_use`` block and return the matching ``tool_result`` block.

    Never raises: any error (unknown tool, veto from a ``tool_call`` handler,
    permission rule, user refusal, exception in the tool) becomes an ``is_error``
    result so the model can react to it. ``tool_result`` handlers see the whole
    output and may rewrite it; what the model gets is then clipped.
    """
    try:
        output = _run_tool(call, registry, ctx, confirm, permit, exclude)
        is_error = False
    except Exception as e:
        output = f"Error: {e}"
        is_error = True

    payload = {
        "name": call["name"],
        "input": call["input"],
        "output": output,
        "is_error": is_error,
        "ctx": ctx,
    }
    for rewrite in registry.emit("tool_result", payload):
        if isinstance(rewrite, dict) and isinstance(rewrite.get("output"), str):
            output = rewrite["output"]

    return {
        "type": "tool_result",
        "tool_use_id": call["id"],
        "content": clip(output),
        "is_error": is_error,
    }


def _run_tool(
    call: dict, registry: Registry, ctx: Any, confirm: Confirm | None, permit: Permit | None, exclude: frozenset[str]
) -> str:
    """Look up, vet, check permissions, confirm and run the tool for ``call``. Raises on any failure."""
    tool = None if call["name"] in exclude else registry.tools.get(call["name"])
    if tool is None:
        raise ValueError(f"unknown tool: {call['name']}")

    try:
        verdicts = registry.emit("tool_call", {"name": call["name"], "input": call["input"], "ctx": ctx}, strict=True)
    except TurnInterrupted:
        raise
    except Exception as e:  # fail closed: a guard that crashed has not approved the call
        raise RuntimeError(f"blocked: a tool_call handler failed: {e}") from e
    for verdict in verdicts:
        if isinstance(verdict, dict) and verdict.get("block"):
            raise RuntimeError(verdict.get("reason") or "blocked by an extension")

    allowed = permit(tool, call["input"]) if permit else False  # raises PermissionDenied for a deny rule
    needed = tool.confirm(call["input"], ctx) if callable(tool.confirm) else tool.confirm
    if needed and not allowed and confirm is not None and not confirm(call):
        raise RuntimeError(USER_DENIED)

    return str(tool.run(call["input"], ctx))


def tool_specs(registry: Registry, exclude: frozenset[str] = frozenset()) -> list[dict]:
    """Describe every registered tool but ``exclude`` in the provider-neutral form."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in registry.tools.values()
        if t.name not in exclude
    ]


def _assistant_message(provider: Provider, reply: Reply) -> dict:
    """Build the canonical assistant message for ``reply``, tagging any provider data and adding its usage."""
    message: dict = {"role": "assistant", "content": reply.content}
    if reply.provider_data is not None:
        message["provider_data"] = {
            "provider": provider.name,
            "data": reply.provider_data,
        }
    if reply.usage is not None:
        message["usage"] = dataclasses.asdict(reply.usage)
    return message
