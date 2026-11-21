"""Agent identity and bookkeeping: who is running, under whom, and how it ended.

Every loop that talks to the model is an agent: the user's turn is ``"main"``, and each ``ctx.run_agent`` a tool
starts is another one, with its own id, its ``parent_id`` (the agent whose tool started it) and its own
cancel flag. :class:`Agents` keeps one record per agent for the session; :meth:`Agents.snapshot` is what
``ctx.agents()`` returns.

Each flag is a :class:`CancelEvent`: setting it sets those of the agents started under it, so the user's
interrupt reaches every agent, and cancelling one agent reaches only it and its children.

:class:`Agents` is guarded by a lock: agents run in worker threads while the UI thread reads and cancels.
"""

from __future__ import annotations

import dataclasses
import secrets
import threading
import time
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from minimal.providers.base import Usage

MAIN_AGENT = "main"
"""The id of the agent that answers the user's messages (its ``parent_id`` is ``None``)."""

AGENT_STATUSES = ("running", "done", "interrupted", "error", "max_turns")
"""How an agent is doing: ``running``, or how it ended (``max_turns``: it kept calling tools until its turn limit)."""


current_call = threading.local()
"""``current_call.key`` is, on a thread running a tool call, that call's progress slot (``"<agent id>/<call id>"``)."""


class CancelEvent(threading.Event):
    """A cancel flag that also sets the flags of the agents started under it (:meth:`link`)."""

    def __init__(self) -> None:
        super().__init__()
        self._children: list[CancelEvent] = []
        self._children_lock = threading.Lock()

    def set(self) -> None:
        """Set this flag and every linked one."""
        with self._children_lock:
            super().set()
            children = list(self._children)
        for child in children:
            child.set()

    def link(self, child: CancelEvent) -> None:
        """Set ``child`` whenever this flag is set (at once if it already is)."""
        with self._children_lock:
            if not self.is_set():
                self._children.append(child)
                return
        child.set()

    def unlink(self, child: CancelEvent) -> None:
        """Stop setting ``child`` along with this flag."""
        with self._children_lock:
            if child in self._children:
                self._children.remove(child)


@dataclass(frozen=True)
class AgentInfo:
    """A snapshot of one agent (see :meth:`Context.agents <minimal.core.context.Context.agents>`).

    Attributes:
        id: ``"main"`` for the user's turn, else a short unique id.
        parent_id: The id of the agent whose tool started this one; ``None`` for ``"main"``.
        name: The ``name`` it was started with (``"agent"`` if none; ``"main"`` for the user's turn).
        status: One of :data:`AGENT_STATUSES`. ``"main"`` is ``running`` during a turn and keeps how the last one ended.
        model: The model it talks to.
        provider: The provider's name.
        prompt: The message it was started with (for ``"main"``, the user's latest message).
        started: When it started, Unix seconds (``"main"``: when its first turn started).
        finished: When it ended, Unix seconds; ``None`` while it runs.
        tool_calls: How many tools it has called.
        usage: Token counts summed over its replies so far (for ``"main"``, over the session's turns).
        error: Why it failed (``status`` ``error`` or ``max_turns``); else ``None``.
    """

    id: str
    parent_id: str | None
    name: str
    status: str
    model: str
    provider: str
    prompt: str
    started: float
    finished: float | None
    tool_calls: int
    usage: Usage
    error: str | None = None


@dataclass(frozen=True)
class AgentResult:
    """How an agent started with ``ctx.run_agent_result`` ended.

    Attributes:
        agent_id: Its id (as in ``agent_start``, ``tool_call``... and ``ctx.agents()``).
        text: The text of its last reply; if it did not finish, of the last reply it got to (else ``""``).
        usage: Token counts summed over its replies.
        status: ``"done"``; ``"interrupted"`` (the user, or ``ctx.cancel_agent``); ``"error"`` (the model request
            failed, no ``provider_error`` handler retried it); ``"max_turns"`` (still calling tools after its turn limit).
        error: The message of the failure (``error``, ``max_turns``); else ``None``.
        messages: The agent's own transcript in the canonical format, starting with its prompt. Every ``tool_use`` has
            its ``tool_result``, even when it was interrupted.
    """

    agent_id: str
    text: str
    usage: Usage
    status: str
    error: str | None
    messages: list[dict]


@dataclass
class AgentRun:
    """The live record of one agent; mutated only through :class:`Agents`."""

    id: str
    parent_id: str | None
    name: str
    prompt: str
    model: str
    provider: str
    cancel: CancelEvent
    started: float
    status: str = "running"
    finished: float | None = None
    error: str | None = None
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    parent_cancel: CancelEvent | None = None
    reasoning_effort: str | None = None
    """The effort ``run_agent(reasoning_effort=...)`` gave this agent (inherited by the agents it starts); ``None``: the app's."""
    cwd: str | None = None
    """The working directory ``run_agent(cwd=...)`` gave this agent (inherited by the agents it starts); ``None``: the app's."""


def add_usage(total: Usage, usage: Usage | None) -> None:
    """Add ``usage`` (``None`` counts nothing) to ``total`` in place."""
    if usage is not None:
        for f in dataclasses.fields(Usage):
            setattr(total, f.name, getattr(total, f.name) + getattr(usage, f.name))


@dataclass(frozen=True)
class UsageTotals:
    """Tokens and requests a minimal process has spent so far (:attr:`~minimal.core.context.Context.usage`).

    API 2.3. Counts every model request the process made: the user's turns, the agents they start, and
    ``ctx.complete``. A provider that reports no usage adds to ``requests`` only.

    Attributes:
        input_tokens: Prompt tokens, cached ones included (as :class:`~minimal.providers.base.Usage`).
        output_tokens: Tokens the model wrote.
        cache_read_tokens: Prompt tokens read from the provider's cache.
        cache_write_tokens: Prompt tokens written to the provider's cache.
        requests: How many model requests were made.
        by_model: The same counts per model, keyed ``"provider/model"`` (API 2.4): each value is a
            :class:`UsageTotals` with an empty ``by_model``, and the values sum to the totals above. Multiply them by
            your own price table to get a cost; minimal knows no prices.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0
    by_model: Mapping[str, UsageTotals] = field(default_factory=dict, compare=True, hash=False)


class UsageMeter:
    """The running :class:`UsageTotals` of a process; guarded by a lock because agents reply from worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._totals = UsageTotals()
        self._models: dict[str, UsageTotals] = {}

    def add(self, usage: Usage | None, model: str = "", provider: str = "") -> None:
        """Count one request to ``provider``/``model``, and its ``usage`` if the provider reported it."""
        u = usage or Usage()
        key = f"{provider}/{model}"
        with self._lock:
            self._models[key] = self._bump(self._models.get(key, UsageTotals()), u)
            t = self._bump(self._totals, u)
            self._totals = dataclasses.replace(t, by_model=types.MappingProxyType(dict(self._models)))

    @staticmethod
    def _bump(t: UsageTotals, u: Usage) -> UsageTotals:
        return UsageTotals(
            t.input_tokens + u.input_tokens,
            t.output_tokens + u.output_tokens,
            t.cache_read_tokens + u.cache_read_tokens,
            t.cache_write_tokens + u.cache_write_tokens,
            t.requests + 1,
        )

    @property
    def totals(self) -> UsageTotals:
        """The totals now (a snapshot: later requests do not change it)."""
        with self._lock:
            return self._totals


class ProgressSlots:
    """The ``ctx.progress`` notes of the tool calls running now, one slot per key; shows the most recently set one.

    The frontend gets one string: the newest live slot's text, followed by ``" (+n more)"`` when ``n`` other slots are
    live; ``None`` when none is.

    Args:
        show: Called with that string (or ``None``) whenever a slot is set, or a live one cleared, under the lock, so
            calls arrive in order.
    """

    def __init__(self, show: Callable[[str | None], None]) -> None:
        self._show = show
        self._lock = threading.RLock()
        self._slots: dict[str, str] = {}

    def set(self, key: str, text: str | None) -> None:
        """Set slot ``key`` to ``text`` (making it the newest), or clear it with ``None`` (nothing happens if it is empty)."""
        with self._lock:
            had = self._slots.pop(key, None) is not None
            if text is not None:
                self._slots[key] = text
            if text is not None or had:
                self._refresh()

    def clear(self, prefix: str | None = None) -> None:
        """Clear every slot, or only those whose key is ``prefix`` or starts with ``prefix + "/"``."""
        with self._lock:
            gone = [k for k in self._slots if prefix is None or k == prefix or k.startswith(prefix + "/")]
            for key in gone:
                del self._slots[key]
            if gone:
                self._refresh()

    def _refresh(self) -> None:
        if not self._slots:
            shown = None
        else:
            newest = next(reversed(self._slots.values()))
            more = len(self._slots) - 1
            shown = f"{newest} (+{more} more)" if more else newest
        self._show(shown)


class Agents:
    """The agents of one session: ids, parents, status, counters and cancel flags.

    Args:
        cancelled: The app's cancel flag, the parent of every agent's own.
    """

    def __init__(self, cancelled: CancelEvent) -> None:
        self._app_cancel = cancelled
        self._lock = threading.Lock()
        self._runs: dict[str, AgentRun] = {}

    def begin_main(self, prompt: str, model: str, provider: str) -> AgentRun:
        """Mark the user's turn as running and return its record (one record serves every turn)."""
        with self._lock:
            run = self._runs.get(MAIN_AGENT)
            if run is None:
                run = self._runs[MAIN_AGENT] = AgentRun(
                    MAIN_AGENT, None, MAIN_AGENT, prompt, model, provider, self._app_cancel, time.time()
                )
            run.prompt, run.model, run.provider = prompt, model, provider
            run.status, run.finished, run.error = "running", None, None
            return run

    def begin(
        self,
        *,
        name: str,
        parent_id: str | None,
        prompt: str,
        model: str,
        provider: str,
        cwd: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AgentRun:
        """Register a new agent under ``parent_id`` and return its record.

        Its cancel flag is linked to its parent's while the parent runs, else to the app's.
        """
        parent_id = parent_id or MAIN_AGENT
        with self._lock:
            parent = self._runs.get(parent_id)
            parent_cancel = parent.cancel if parent is not None and parent.status == "running" else self._app_cancel
            agent_id = secrets.token_hex(4)
            while agent_id in self._runs or agent_id == MAIN_AGENT:
                agent_id = secrets.token_hex(4)
            run = AgentRun(agent_id, parent_id, name, prompt, model, provider, CancelEvent(), time.time())
            run.parent_cancel = parent_cancel
            run.cwd, run.reasoning_effort = cwd, reasoning_effort
            self._runs[agent_id] = run
        parent_cancel.link(run.cancel)
        return run

    def get(self, agent_id: str) -> AgentRun | None:
        """The record of agent ``agent_id``; ``None`` if there is none."""
        with self._lock:
            return self._runs.get(agent_id)

    def tool_called(self, run: AgentRun) -> None:
        """Count one more tool call of ``run``."""
        with self._lock:
            run.tool_calls += 1

    def replied(self, run: AgentRun, usage: Usage | None) -> None:
        """Add a reply's ``usage`` to ``run``'s."""
        with self._lock:
            add_usage(run.usage, usage)

    def finish(self, run: AgentRun, status: str, error: str | None) -> Usage:
        """Record how ``run`` ended and return its total usage."""
        with self._lock:
            run.status, run.error, run.finished = status, error, time.time()
            usage = dataclasses.replace(run.usage)
        if run.parent_cancel is not None:
            run.parent_cancel.unlink(run.cancel)
        return usage

    def snapshot(self) -> list[AgentInfo]:
        """Every agent of the session, in the order they started, as it is now."""
        with self._lock:
            return [
                AgentInfo(
                    r.id, r.parent_id, r.name, r.status, r.model, r.provider, r.prompt, r.started,
                    r.finished, r.tool_calls, dataclasses.replace(r.usage), r.error,
                )
                for r in self._runs.values()
            ]

    def main_running(self) -> bool:
        """Whether the user's turn (agent ``"main"``) is running."""
        with self._lock:
            run = self._runs.get(MAIN_AGENT)
            return run is not None and run.status == "running"

    def cancel(self, agent_id: str) -> bool:
        """Interrupt agent ``agent_id`` and the agents started under it; ``False`` if there is none running."""
        with self._lock:
            run = self._runs.get(agent_id)
            if run is None or run.status != "running":
                return False
            cancel = run.cancel
        cancel.set()
        return True
