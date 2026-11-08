"""Example extension: a tool, a command, a guardrail, a prompt note and a sidebar widget.

Copy it to ~/.minimal/extensions/ or ./.minimal/extensions/ to always load it,
or load it on demand with:  minimal --ext extensions/example.py
"""

import re
from datetime import datetime

from textual.widgets import Static


class SessionPanel(Static):
    """Sidebar panel showing the session size, provider and model."""

    DEFAULT_CSS = "SessionPanel { padding: 0 1; }"

    def refresh_state(self, ctx):
        self.update(f"{len(ctx.messages)} messages\n{ctx.provider_name} · {ctx.model}")


def register(pi):
    # 1) A new tool for the model.
    pi.register_tool(
        name="now",
        description="Return the current system date and time.",
        input_schema={"type": "object", "properties": {}},
        run=lambda input, ctx: datetime.now().astimezone().isoformat(timespec="seconds"),
    )

    # 2) A slash command for the user: /stats
    def stats(args, ctx):
        print(f"{len(ctx.messages)} messages in the session · model {ctx.model}")

    pi.register_command("stats", stats, description="show how many messages the session has")

    # 3) A guardrail: block clearly destructive commands.
    def guard(event):
        if event["name"] == "bash" and re.search(r"\brm\s+-rf\s+(/|~)(\s|$)", event["input"].get("command", "")):
            return {"block": True, "reason": "rm -rf on / or ~ is blocked by the example extension"}

    pi.on("tool_call", guard)

    # 4) An extra note in the system prompt.
    pi.append_system_prompt("Always reply in the same language the user writes in.")

    # 5) A visual component: a sidebar panel, refreshed after every turn.
    pi.register_widget("session", lambda ctx: SessionPanel(markup=False), area="sidebar")
