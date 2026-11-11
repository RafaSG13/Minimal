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

__version__ = "2.0"
__api__ = "1"

NAME = "subagent"

SUBAGENT_NOTE = (
    "You are a subagent started by another agent. Do the task with the tools and finish with a concise report: "
    "the caller sees only your last message."
)


def run_subagent(input, ctx):
    """Run ``input["prompt"]`` as its own turn and return the text of the final reply."""
    return ctx.run_agent(input["prompt"], system_note=SUBAGENT_NOTE, exclude_tools={NAME}) or (
        "(the subagent returned no text)"
    )


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
