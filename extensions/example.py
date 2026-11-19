"""Example extension: a tool, a command, a guardrail with a setting, a prompt note, a sidebar panel and a key.

Copy it to ~/.minimal/extensions/ or ./.minimal/extensions/ to always load it,
or load it on demand with:  minimal --ext extensions/example.py

Its guardrail patterns are a setting: replace them in settings.json with
{"extensions": {"settings": {"example": {"blocked": ["..."]}}}}.
"""

import re
from datetime import datetime

__version__ = "1.2"
__api__ = "2"


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

    # 5) A visual component any frontend can draw: a sidebar panel, redrawn after every turn. `order` puts it
    #    before the other widgets of its area; the user can move, reorder or hide it without touching this file:
    #    /ui move session footer, /ui hide session, or "ui": {"widgets": {"session": {...}}} in settings.json.
    minimal.register_panel(
        "session", lambda ctx: f"{len(ctx.messages)} messages\n{ctx.provider_name} · {ctx.model}", order=-10
    )

    # 6) A global key: ctrl+o runs like a slash command (it may print, or ask the user). The user can give it
    #    another key with {"ui": {"keys": {"example:stats": "alt+s"}}} in settings.json.
    @minimal.keybinding("ctrl+o", name="stats", description="show session stats")
    def show_stats(ctx):
        stats("", ctx)
