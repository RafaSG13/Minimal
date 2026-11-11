"""Compacts the history into a summary when the context grows, and adds /compact.

Compacting asks the model for a summary of the conversation and continues in
a new session holding that summary and the last exchange. The old session
stays on disk untouched: ``/resume <id>`` brings the full history back.

It runs by itself at the end of a turn whose context reached ``compact_at``
tokens (providers that report no usage never trigger it), and on demand with
``/compact``. Set the threshold in settings.json::

    {"extensions": {"settings": {"compact": {"compact_at": 150000}}}}

Load it on demand with ``minimal --ext extensions/compact.py``, or copy the
file to ``~/.minimal/extensions/`` to always have it.
"""

import sys

__version__ = "2.0"
__requires__ = ">=0.2"

COMPACT_AT = 100_000
"""Default context tokens (input + output of the last reply) that trigger a compaction."""

SUMMARY_REQUEST = (
    "Stop here and do not call any tools. Write a summary of this whole conversation that will replace it as your "
    "only memory: the user's goals and instructions, decisions made, files read or changed (with paths), commands "
    "run and their relevant results, the current state, and what remains to be done. Be specific and complete; "
    "plain text."
)

SUMMARY_PREFIX = "Summary of the earlier conversation (compacted):\n\n"

ACK = "Understood. I will continue from this summary."


def _text(blocks):
    return "".join(block["text"] for block in blocks if block["type"] == "text")


def compact(ctx):
    """Summarise ``ctx.messages`` and switch to a new session with the summary and the last exchange.

    The summary request sends the same system prompt, tools and history plus
    one message, so it reads the provider's prompt cache. Nothing changes
    unless the model returns a summary.

    Returns:
        A line telling the user what happened.

    Raises:
        ValueError: if there is no finished exchange to compact, or the model returned no summary.
        TurnInterrupted: if the user interrupts before the session is switched; nothing is saved or changed then.
    """
    messages = ctx.messages
    asked = [i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], str)]
    if not asked or messages[-1]["role"] != "assistant":
        raise ValueError("nothing to compact")

    ctx.check_cancelled()
    reply = ctx.complete([*messages, {"role": "user", "content": SUMMARY_REQUEST}], tools=True)
    summary = _text(reply.content)
    if not summary or any(block["type"] == "tool_use" for block in reply.content):
        raise ValueError("the model did not return a summary")

    answer = [b for b in messages[-1]["content"] if b["type"] == "text"]
    kept = [
        {"role": "user", "content": SUMMARY_PREFIX + summary},
        {"role": "assistant", "content": [{"type": "text", "text": ACK}]},
    ]
    if answer:  # an assistant message without content would be rejected by the API
        kept += [
            {"role": "user", "content": messages[asked[-1]]["content"]},
            {"role": "assistant", "content": answer},
        ]

    ctx.check_cancelled()  # before anything is saved: the interrupted turn is dropped from history
    old = ctx.new_session(kept)
    return (
        f"[compact] {len(messages)} messages summarised into session {ctx.session_id}; "
        f"the full history stays in session {old}"
    )


def register(minimal):
    settings = minimal.declare_settings({"compact_at": COMPACT_AT})

    @minimal.on("turn_end")
    def compact_when_full(event):
        """Compact when the turn that just ended left the session's context at ``compact_at`` tokens or more."""
        if event["nested"]:
            return  # not the session's own history: a subagent's loop
        usage = event["messages"][-1].get("usage")
        if not usage or usage["input_tokens"] + usage["output_tokens"] < settings["compact_at"]:
            return
        try:
            print(compact(event["ctx"]))
        except ValueError as e:
            print(f"[compact] skipped: {e}", file=sys.stderr)

    @minimal.command("compact", description="summarise the conversation into a new session")
    def compact_command(args, ctx):
        print(compact(ctx))
