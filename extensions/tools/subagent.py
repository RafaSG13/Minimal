"""Subagent tool: delegates a task to a fresh agent loop with its own history.

The subagent uses the current provider, model and tools (except this one, so
subagents do not start subagents; tools reloaded with reload_extensions take
effect on its next request) and starts from an empty history. Only its
last message returns to the caller, which keeps long searches and
investigations out of the main conversation. The user sees the subagent's
tool calls, is asked for the same confirmations, and can interrupt it.

Load it on demand with ``minimal --ext extensions/tools/subagent.py``, or copy
the file to ``~/.minimal/extensions/tools/`` to always have it.
"""

from minimal.api import Registry, build_system_prompt, run_agent

__version__ = "1.0"

NAME = "subagent"

SUBAGENT_NOTE = (
    "You are a subagent started by another agent. Do the task with the tools and finish with a concise report: "
    "the caller sees only your last message."
)


def _sync_tools(parent, child):
    """Make ``child.tools`` hold ``parent``'s current tools except this one, in place."""
    child.tools.clear()
    child.tools.update((name, tool) for name, tool in parent.tools.items() if name != NAME)


def _child_registry(parent):
    """A registry that mirrors ``parent``'s tools except this one (re-synced after every tool call, so reloads
    reach it) and sends tool events to ``parent``'s handlers."""
    child = Registry()
    _sync_tools(parent, child)

    def veto(event):
        for verdict in parent.emit("tool_call", event, strict=True):  # a failing parent guard blocks too
            if isinstance(verdict, dict) and verdict.get("block"):
                return verdict
        return None

    def rewrite(event):
        output = None
        for result in parent.emit("tool_result", event):
            if isinstance(result, dict) and isinstance(result.get("output"), str):
                output = result["output"]
        # a tool (reload_extensions) may have rebuilt the parent's registry
        _sync_tools(parent, child)
        return None if output is None else {"output": output}

    child.on("tool_call", veto)
    child.on("tool_result", rewrite)
    return child


def run_subagent(input, ctx):
    """Run ``input["prompt"]`` as its own turn and return the text of the final reply."""
    reply = run_agent(
        provider=ctx.provider,
        model=ctx.model,
        system=build_system_prompt(ctx.registry, ctx.cwd) + "\n\n" + SUBAGENT_NOTE,
        messages=[{"role": "user", "content": input["prompt"]}],
        registry=_child_registry(ctx.registry),
        ctx=ctx,
        on_tool=ctx.frontend.tool_call,
        on_tool_result=ctx.frontend.tool_result,
    )
    text = "".join(block["text"] for block in reply.content if block["type"] == "text")
    return text or "(the subagent returned no text)"


def register(minimal):
    minimal.register_tool(
        name=NAME,
        run=run_subagent,
        description=(
            "Delegate a self-contained task to a subagent with the same tools and an empty history. "
            "Only its final message comes back, so the prompt must contain everything it needs. "
            "Use it for searches and investigations whose intermediate output is not worth keeping "
            "in this conversation."
        ),
        input_schema={
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "Complete instructions for the subagent"}},
            "required": ["prompt"],
        },
    )
