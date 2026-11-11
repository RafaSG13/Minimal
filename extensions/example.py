"""Example extension: a tool, a command, a guardrail with a setting, a prompt note and a sidebar widget.

Copy it to ~/.minimal/extensions/ or ./.minimal/extensions/ to always load it,
or load it on demand with:  minimal --ext extensions/example.py

Its guardrail patterns are a setting: replace them in settings.json with
{"extensions": {"settings": {"example": {"blocked": ["..."]}}}}.
"""

import re
from datetime import datetime

from textual.widgets import Static

__version__ = "1.0"
__requires__ = ">=0.2"


class SessionPanel(Static):
    """Sidebar panel showing the session size, provider and model."""

    DEFAULT_CSS = "SessionPanel { padding: 0 1; }"

    def refresh_state(self, ctx):
        self.update(f"{len(ctx.messages)} messages\n{ctx.provider_name} · {ctx.model}")


def register(minimal):
    settings = minimal.declare_settings({"blocked": [r"\brm\s+-rf\s+(/|~)(\s|$)"]})

    # 1) A new tool for the model: the name is the function's, the description its docstring.
    @minimal.tool(input_schema={"type": "object", "properties": {}})
    def now(input, ctx):
        """Return the current system date and time."""
        return datetime.now().astimezone().isoformat(timespec="seconds")

    # 2) A slash command for the user: /stats
    @minimal.command(description="show how many messages the session has")
    def stats(args, ctx):
        print(f"{len(ctx.messages)} messages in the session · model {ctx.model}")

    # 3) A guardrail: block commands matching the "blocked" setting (rm -rf on / or ~ by default).
    @minimal.on("tool_call")
    def guard(event):
        command = event["input"].get("command", "")
        if event["name"] == "bash" and any(re.search(p, command) for p in settings["blocked"]):
            return {"block": True, "reason": "this command is blocked by the example extension"}

    # 4) An extra note in the system prompt.
    minimal.append_system_prompt("Always reply in the same language the user writes in.")

    # 5) A visual component: a sidebar panel, refreshed after every turn.
    minimal.register_widget("session", lambda ctx: SessionPanel(markup=False), area="sidebar")
