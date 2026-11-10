"""Subagent tool: delegates a task to a fresh agent loop with its own history.

The subagent uses the current provider, model and tools (except this one, so
subagents do not start subagents) and starts from an empty history. Only its
last message returns to the caller, which keeps long searches and
investigations out of the main conversation. The user sees the subagent's
tool calls, is asked for the same confirmations, and can interrupt it.

Load it on demand with ``minimal --ext extensions/tools/subagent.py``, or copy
the file to ``~/.minimal/extensions/tools/`` to always have it.
"""

from minimal.agent import run_agent
from minimal.app import build_system_prompt
from minimal.registry import Registry

__version__ = "1.0"

NAME = "subagent"

SUBAGENT_NOTE = (
    "You are a subagent started by another agent. Do the task with the tools and finish with a concise report: "
    "the caller sees only your last message."
)


def _child_registry(parent):
    """A registry with ``parent``'s tools except this one, whose tool events go to ``parent``'s handlers."""
    child = Registry()
    for tool in parent.tools.values():
        if tool.name != NAME:
            child.add_tool(tool)

    def veto(event):
        for verdict in parent.emit("tool_call", event):
            if isinstance(verdict, dict) and verdict.get("block"):
                return verdict
        return None

    def rewrite(event):
        output = None
        for result in parent.emit("tool_result", event):
            if isinstance(result, dict) and isinstance(result.get("output"), str):
                output = result["output"]
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
