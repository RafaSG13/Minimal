"""The agent loop: ask the model, run the tools it calls, send back the results, repeat.

One call to :func:`run_agent` handles one user turn:

1. Fire ``before_request`` (its handlers may replace the messages, system prompt or
   tools sent, for that request only) and stream a reply from the provider, offering
   every registered tool. If the request fails, ``provider_error`` handlers may retry
   it, after a delay or with another provider; otherwise the error propagates.
2. Append the reply to ``messages`` as an ``assistant`` message and fire ``after_response``.
3. If the reply contains no tool calls, fire ``turn_end`` and return it.
4. Otherwise run each call (see :func:`execute_tool_call`), append the results
   as a ``user`` message and go back to step 1. A run of consecutive calls to ``read_only`` tools runs
   concurrently (see :func:`run_agent`); every other call runs alone, in order, and the results are always
   appended in the order of the calls. If the reply was cut off by the
   output token limit, its calls may be incomplete: none runs, each is answered
   with an error asking for smaller steps, and the loop goes on.

If the loop ends with an exception (interruption, provider error) while the calls run, the results so far are
kept and every call not finished is answered with an error, so ``messages`` never ends with a ``tool_use`` that
has no result.

Each loop is one agent (see :mod:`minimal.core.agents`): it has an ``agent_id`` and a ``parent_id``, which
every event it fires carries and which the tool calls and results it hands to ``on_tool`` and
``on_tool_result`` carry too (as copies, so the history stays as the provider sent it).

The loop only sees the canonical message format described in
:mod:`minimal.providers.base`; each provider translates to and from its own API.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, cast

from minimal.core.agents import current_call
from minimal.config import DEFAULT_MAX_PARALLEL, DEFAULT_MAX_TURNS
from minimal.extensions.registry import Registry, Tool, TurnInterrupted
from minimal.providers.base import Provider, Reply, result_text
from minimal.tools.common import clip

USER_DENIED = "the user denied the execution"
"""Error of a tool call the user refused (the model sees ``"Error: " + USER_DENIED``)."""

TRUNCATED = (
    "this call was not run: the reply was cut off by the output token limit before it was complete. "
    "Try again with smaller steps (for example, write a large file in several parts)."
)
"""Error of a tool call the model's output limit cut off (the model sees ``"Error: " + TRUNCATED``)."""

UNFINISHED = "the turn ended before this call finished: it did not run, or ran only in part."
"""Error of a tool call left pending when the turn failed or was interrupted (the model sees ``"Error: " + UNFINISHED``)."""

Confirm = Callable[[dict], bool]
"""Asks whether a ``tool_use`` call may run."""

Permit = Callable[[Tool, dict], bool]
"""Permission rules: ``True`` runs a call without asking; raising denies it."""

ToolGate = Callable[[str], "str | None"]
"""Decides per tool name: ``None`` lets the model see and call it; a reason (the text of the error the model
gets) hides it from the tool list and refuses a call to it. Asked live, so it may change during a turn."""


class MaxTurnsExceeded(RuntimeError):
    """The model was still calling tools after the loop's ``max_turns`` requests."""


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
    on_reply: Callable[[Reply, str, str], Any] | None = None,
    confirm: Confirm | None = None,
    cancelled: threading.Event | None = None,
    permit: Permit | None = None,
    exclude_tools: frozenset[str] = frozenset(),
    gate: ToolGate | None = None,
    nested: bool = False,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    options: Mapping[str, Any] | None = None,
    switch_provider: Callable[[str, str | None], tuple[Provider, str]] | None = None,
    agent_id: str = "main",
    parent_id: str | None = None,
    dynamic_system: Callable[[], str] | None = None,
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
        on_tool: Called with each ``tool_use`` block before it runs (a copy with ``agent_id`` and ``parent_id`` added).
        on_tool_result: Called with each ``tool_use`` block and its ``tool_result`` block after it runs (copies with
            ``agent_id`` and ``parent_id`` added).
        on_reply: Called with each reply, the provider's name and the model that answered it, after the reply is
            appended to ``messages``.
        confirm: Asked before running a tool marked ``confirm`` that ``permit`` did not allow.
        cancelled: Setting it interrupts the turn: at the next text chunk,
            before the next model request or before the next tool.
        permit: Permission rules, consulted after the ``tool_call`` handlers.
        exclude_tools: Tools neither offered to the model nor run (a subagent's own tool).
        gate: Further restriction on tools, asked on every request and every call (the current mode, an allow-list).
        nested: This loop runs inside a tool (a subagent); passed on in ``turn_end``.
        max_turns: Most model requests allowed for this turn.
        max_parallel: Most calls to ``read_only`` tools run at once. When one reply holds several calls, the longest
            run of consecutive ones to ``read_only`` tools is run concurrently on worker threads; each call is still
            announced (``on_tool``), checked (``tool_call`` handlers, permissions, confirmation) one after the other in
            the order of the reply on the calling thread, before any of them starts, and its result is handed to the
            ``tool_result`` handlers and ``on_tool_result`` in that order as well. A call to any other tool is a
            barrier: it runs alone, after the calls before it have finished. ``1`` runs every call one by one.
        options: Request options for the provider (``max_tokens``, ``temperature``); ``before_request``
            handlers may add to them. Passed to ``provider.stream`` only when not empty.
        switch_provider: Switches to ``(provider name, model or None)`` and returns the new provider
            and model, for a ``provider_error`` handler that answers ``{"provider": ...}``; ``None``
            means such an answer re-raises the error.
        agent_id: This loop's id, carried by every event it fires.
        parent_id: The id of the agent whose tool started this loop; ``None`` for the user's turn.
        dynamic_system: Text added after ``system`` (blank line between) on every request, asked live: the current
            mode's note, which a tool may change in the middle of a turn.

    Returns:
        The final reply.

    Raises:
        MaxTurnsExceeded: (a ``RuntimeError``) if the model is still calling tools after ``max_turns`` requests.
        TurnInterrupted: if ``cancelled`` is set while the turn runs.
        Exception: a failed model request that no ``provider_error`` handler retried.
    """

    def stream_text(chunk: str) -> None:
        raise_if_cancelled(cancelled)
        if on_text:
            on_text(chunk)

    who = {"agent_id": agent_id, "parent_id": parent_id}

    for _ in range(max_turns):
        raise_if_cancelled(cancelled)
        extra = dynamic_system() if dynamic_system else ""
        request = _before_request(
            registry,
            ctx,
            messages,
            f"{system}\n\n{extra}" if system and extra else system or extra,
            tool_specs(registry, exclude_tools, gate),
            options or {},
            nested,
            who,
        )
        reply = None
        attempt = 0
        while reply is None:
            try:
                reply = provider.stream(model=model, on_text=stream_text, **request)
            except TurnInterrupted:
                raise
            except Exception as e:
                attempt += 1
                retry = _provider_error(
                    e, attempt, registry, ctx, provider, model, nested, switch_provider, cancelled, who
                )
                if retry is None:
                    raise
                provider, model = retry
                raise_if_cancelled(cancelled)
        messages.append(_assistant_message(provider, reply))
        if on_reply:
            on_reply(reply, provider.name, model)
        registry.emit(
            "after_response",
            {
                "ctx": ctx,
                "reply": reply,
                "usage": reply.usage,
                "nested": nested,
                "model": model,
                "provider": provider.name,
                "source": "loop",
                **who,
            },
        )

        calls = [block for block in reply.content if block["type"] == "tool_use"]
        if not calls or reply.stop_reason not in ("tool_use", "max_tokens"):
            registry.emit("turn_end", {"messages": messages, "ctx": ctx, "nested": nested, **who})
            return reply

        if reply.stop_reason == "max_tokens":
            # Every tool_use needs its tool_result or the next request is invalid, and the last call is incomplete.
            results: list[dict] = [
                {"type": "tool_result", "tool_use_id": call["id"], "content": "Error: " + TRUNCATED, "is_error": True}
                for call in calls
            ]
            messages.append({"role": "user", "content": results})
            continue

        results = []
        try:
            _run_calls(
                calls,
                results,
                registry,
                ctx,
                confirm=confirm,
                permit=permit,
                exclude=exclude_tools,
                gate=gate,
                who=who,
                on_tool=on_tool,
                on_tool_result=on_tool_result,
                cancelled=cancelled,
                max_parallel=max_parallel,
            )
        except BaseException:
            # Keep the results of the calls that finished and answer the rest, so the history stays valid.
            pending = [
                {"type": "tool_result", "tool_use_id": call["id"], "content": "Error: " + UNFINISHED, "is_error": True}
                for call in calls[len(results) :]
            ]
            messages.append({"role": "user", "content": [*results, *pending]})
            raise
        messages.append({"role": "user", "content": results})

    raise MaxTurnsExceeded(f"exceeded the maximum of {max_turns} turns")


def _run_calls(
    calls: list[dict],
    results: list[dict],
    registry: Registry,
    ctx: Any,
    *,
    confirm: Confirm | None,
    permit: Permit | None,
    exclude: frozenset[str],
    gate: ToolGate | None,
    who: dict[str, Any],
    on_tool: Callable[[dict], Any] | None,
    on_tool_result: Callable[[dict, dict], Any] | None,
    cancelled: threading.Event | None,
    max_parallel: int,
) -> None:
    """Run ``calls`` and append each ``tool_result`` to ``results``, in the order of ``calls``, as soon as it is final.

    A run of consecutive calls to ``read_only`` tools goes to :func:`_run_concurrently`; any other call runs alone.
    On an exception ``results`` holds exactly the results of the calls that finished, so the caller can answer the rest.
    """
    index = 0
    known: dict[int, bool] = {}  # a callable ``read_only`` is asked once per call

    def read_only(i: int) -> bool:
        if i not in known:
            known[i] = _is_read_only(calls[i], registry, exclude, ctx)
        return known[i]

    while index < len(calls):
        end = index + 1
        if max_parallel > 1 and read_only(index):
            while end < len(calls) and read_only(end):
                end += 1
        if end - index > 1:
            _run_concurrently(
                calls[index:end], results, registry, ctx, confirm, permit, exclude, gate, who,
                on_tool, on_tool_result, cancelled, max_parallel,
            )  # fmt: skip
        else:
            call = calls[index]
            raise_if_cancelled(cancelled)
            if on_tool:
                on_tool({**call, **who})
            result = execute_tool_call(
                call,
                registry,
                ctx,
                confirm=confirm,
                permit=permit,
                exclude=exclude,
                gate=gate,
                agent_id=who["agent_id"],
                parent_id=who["parent_id"],
            )
            if on_tool_result:
                on_tool_result({**call, **who}, {**result, **who})
            results.append(result)
        index = end


def _is_read_only(call: dict, registry: Registry, exclude: frozenset[str], ctx: Any) -> bool:
    """Whether ``call`` is to a registered, not excluded tool marked ``read_only`` (for this input, if it is a callable).

    A callable that raises counts as not read-only: the call then runs alone, in order.
    """
    tool = None if call["name"] in exclude else registry.tools.get(call["name"])
    if tool is None:
        return False
    if callable(tool.read_only):
        try:
            return bool(tool.read_only(call["input"], ctx))
        except Exception:
            return False
    return tool.read_only


def _run_concurrently(
    calls: list[dict],
    results: list[dict],
    registry: Registry,
    ctx: Any,
    confirm: Confirm | None,
    permit: Permit | None,
    exclude: frozenset[str],
    gate: ToolGate | None,
    who: dict[str, Any],
    on_tool: Callable[[dict], Any] | None,
    on_tool_result: Callable[[dict, dict], Any] | None,
    cancelled: threading.Event | None,
    max_parallel: int,
) -> None:
    """Run calls to ``read_only`` tools together; the rest of the contract is :func:`run_agent`'s ``max_parallel``.

    Every call is announced and vetted in order on this thread (so confirmations are asked one at a time, in
    order), then the approved ones run on at most ``max_parallel`` worker threads, and the results are finished
    (``tool_result`` handlers, ``on_tool_result``) in order on this thread as each one in turn is ready. An
    interruption stops waiting: calls not started never run, ones already running finish unobserved.
    """
    vetted: list[tuple[Tool | None, str | None]] = []  # (tool to run, or the error that stops the call)
    for call in calls:
        raise_if_cancelled(cancelled)
        if on_tool:
            on_tool({**call, **who})
        try:
            vetted.append((_vet_tool_call(call, registry, ctx, confirm, permit, exclude, gate, who), None))
        except Exception as e:
            vetted.append((None, f"Error: {e}"))
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(max_parallel, len(calls)), thread_name_prefix="minimal-tool"
    )
    try:
        running = [
            None if tool is None else pool.submit(_invoke, tool, call, ctx) for call, (tool, _) in zip(calls, vetted)
        ]
        for call, (_, error), future in zip(calls, vetted, running):
            output: str | list[dict]
            if future is None:
                output, is_error = cast(str, error), True
            else:
                while not concurrent.futures.wait([future], timeout=0.05).done:
                    raise_if_cancelled(cancelled)
                output, is_error = future.result()
            result = _finish_tool_call(call, output, is_error, registry, ctx, who)
            if on_tool_result:
                on_tool_result({**call, **who}, {**result, **who})
            results.append(result)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _invoke(tool: Tool, call: dict, ctx: Any) -> tuple[str | list[dict], bool]:
    """Run ``tool`` for ``call``: ``(output, is_error)``; an exception from the tool is the error text."""
    try:
        return _output(_run_tool(tool, call, ctx)), False
    except Exception as e:
        return f"Error: {e}", True


def _run_tool(tool: Tool, call: dict, ctx: Any) -> Any:
    """``tool.run`` for ``call``, with the call's progress slot as the one ``ctx.progress`` fills on this thread, and
    that slot cleared when it ends."""
    key = f"{getattr(ctx, 'agent_id', 'main')}/{call['id']}"
    previous = getattr(current_call, "key", None)
    current_call.key = key
    try:
        return tool.run(call["input"], ctx)
    finally:
        current_call.key = previous
        progress = getattr(ctx, "progress", None)
        if callable(progress):
            try:
                progress(None, key=key)
            except Exception:  # a ctx without keyed progress: nothing to clear
                pass


def _output(value: Any) -> str | list[dict]:
    """What a tool returned as the content of its result: a list of ``text``/``image`` blocks as it is, else ``str(value)``."""
    if (
        isinstance(value, list)
        and value
        and all(
            isinstance(b, dict)
            and (
                (b.get("type") == "text" and isinstance(b.get("text"), str))
                or (b.get("type") == "image" and isinstance(b.get("source"), dict))
            )
            for b in value
        )
    ):
        return value
    return str(value)


def execute_tool_call(
    call: dict,
    registry: Registry,
    ctx: Any,
    *,
    confirm: Confirm | None = None,
    permit: Permit | None = None,
    exclude: frozenset[str] = frozenset(),
    gate: ToolGate | None = None,
    agent_id: str = "main",
    parent_id: str | None = None,
) -> dict:
    """Run one ``tool_use`` block and return the matching ``tool_result`` block.

    Never raises: any error (unknown tool, a tool ``gate`` refuses (a mode), veto from a ``tool_call``
    handler, permission rule, user refusal, exception in the tool) becomes an ``is_error``
    result so the model can react to it. ``tool_result`` handlers see the whole
    output and may rewrite it; what the model gets is then clipped. ``agent_id`` and ``parent_id`` go
    into the payloads of ``tool_call`` and ``tool_result``. A tool may return a list of ``text`` and ``image``
    blocks: the result's ``content`` is then that list (its text clipped), and the handlers see its text.
    """
    who = {"agent_id": agent_id, "parent_id": parent_id}
    output: str | list[dict]
    try:
        tool = _vet_tool_call(call, registry, ctx, confirm, permit, exclude, gate, who)
        output = _output(_run_tool(tool, call, ctx))
        is_error = False
    except Exception as e:
        output = f"Error: {e}"
        is_error = True
    return _finish_tool_call(call, output, is_error, registry, ctx, who)


def _finish_tool_call(
    call: dict, output: str | list[dict], is_error: bool, registry: Registry, ctx: Any, who: dict[str, Any]
) -> dict:
    """Fire ``tool_result`` for ``call`` and build its ``tool_result`` block from ``output`` (clipped), as the handlers left it."""
    payload = {
        "name": call["name"],
        "input": call["input"],
        "output": output if isinstance(output, str) else result_text(output),
        "is_error": is_error,
        "ctx": ctx,
        **who,
    }
    for rewrite in registry.emit("tool_result", payload):
        if isinstance(rewrite, dict) and isinstance(rewrite.get("output"), str):
            output = rewrite["output"]

    content = (
        clip(output)
        if isinstance(output, str)
        else [{**b, "text": clip(b["text"])} if b["type"] == "text" else b for b in output]
    )
    return {"type": "tool_result", "tool_use_id": call["id"], "content": content, "is_error": is_error}


def _vet_tool_call(
    call: dict,
    registry: Registry,
    ctx: Any,
    confirm: Confirm | None,
    permit: Permit | None,
    exclude: frozenset[str],
    gate: ToolGate | None,
    who: dict[str, Any],
) -> Tool:
    """Look up the tool for ``call``, check it against the gate, ``tool_call`` handlers, permissions and the user; return it.

    Raises:
        Exception: for any reason the call must not run: the message is the error the model gets.
    """
    tool = None if call["name"] in exclude else registry.tools.get(call["name"])
    if tool is None:
        raise ValueError(f"unknown tool: {call['name']}")
    if gate is not None and (reason := gate(call["name"])) is not None:
        raise RuntimeError(reason)

    try:
        verdicts = registry.emit(
            "tool_call", {"name": call["name"], "input": call["input"], "ctx": ctx, **who}, strict=True
        )
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
    return tool


def tool_specs(registry: Registry, exclude: frozenset[str] = frozenset(), gate: ToolGate | None = None) -> list[dict]:
    """Describe every registered tool but ``exclude`` and the ones ``gate`` refuses in the provider-neutral form."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in list(registry.tools.values())
        if t.name not in exclude and (gate is None or gate(t.name) is None)
    ]


_REQUEST_TYPES: dict[str, type] = {"messages": list, "system": str, "tools": list, "options": dict}
"""What ``before_request`` handlers may return, and the type each must have."""


def _before_request(
    registry: Registry,
    ctx: Any,
    messages: list[dict],
    system: str,
    tools: list[dict],
    options: Mapping[str, Any],
    nested: bool,
    who: dict[str, Any],
) -> dict[str, Any]:
    """The keyword arguments of the next ``provider.stream`` call (but ``model``, ``on_text``), after the ``before_request`` handlers.

    ``messages``, ``system`` and ``tools`` from the handlers replace the ones given; ``options`` are
    merged into ``options``. Results are combined by key, the last handler wins; a value of the wrong
    type is ignored with a warning. ``options`` is left out when empty.
    """
    request: dict[str, Any] = {"messages": messages, "system": system, "tools": tools}
    opts = dict(options)
    for result in registry.emit("before_request", {"ctx": ctx, **request, "options": dict(opts), "nested": nested, **who}):
        if not isinstance(result, dict):
            continue
        for key, value in result.items():
            if key not in _REQUEST_TYPES:
                continue
            if not isinstance(value, _REQUEST_TYPES[key]):
                print(f'[extension] before_request: ignored "{key}" (wrong type)', file=sys.stderr)
            elif key == "options":
                opts.update(cast("dict[str, Any]", value))
            else:
                request[key] = value
    if opts:
        request["options"] = opts
    return request


def _is_delay(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _provider_error(
    error: Exception,
    attempt: int,
    registry: Registry,
    ctx: Any,
    provider: Provider,
    model: str,
    nested: bool,
    switch_provider: Callable[[str, str | None], tuple[Provider, str]] | None,
    cancelled: threading.Event | None,
    who: dict[str, Any],
) -> tuple[Provider, str] | None:
    """Ask the ``provider_error`` handlers what to do about a failed request; return the provider and model to retry with.

    The first result with ``retry_after`` (seconds, ``>= 0``) or ``provider`` (a name, with an optional
    ``model``) decides: switch provider if asked, wait, then retry. ``None`` if no handler retries
    (or one asks for another provider and there is no ``switch_provider``): the caller re-raises.

    Raises:
        ValueError: if switching provider fails (chained to ``error``).
        TurnInterrupted: if ``cancelled`` is set while waiting.
    """
    payload = {
        "ctx": ctx,
        "error": error,
        "attempt": attempt,
        "provider": provider.name,
        "model": model,
        "nested": nested,
        **who,
    }
    verdict = next(
        (
            v
            for v in registry.emit("provider_error", payload)
            if isinstance(v, dict) and (_is_delay(v.get("retry_after")) or isinstance(v.get("provider"), str))
        ),
        None,
    )
    if verdict is None:
        return None
    if isinstance(verdict.get("provider"), str):
        if switch_provider is None:
            return None
        model_name = verdict.get("model")
        try:
            provider, model = switch_provider(verdict["provider"], model_name if isinstance(model_name, str) else None)
        except ValueError as e:
            raise e from error
    delay = float(verdict["retry_after"]) if _is_delay(verdict.get("retry_after")) else 0.0
    if cancelled is not None:
        if cancelled.wait(delay):
            raise TurnInterrupted("interrupted by the user")
    elif delay:
        time.sleep(delay)
    return provider, model


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
