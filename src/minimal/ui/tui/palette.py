"""The slash commands in Textual's command palette (``Ctrl+P``)."""

from __future__ import annotations

import re
from functools import partial
from typing import TYPE_CHECKING, cast

from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.markup import escape

if TYPE_CHECKING:
    from minimal.ui.tui.app import MinimalTui


def needs_args(name: str, description: str) -> bool:
    """Whether ``/name`` cannot run without arguments: its description's usage shows a required ``<arg>`` right after it."""
    return re.search(rf"/{re.escape(name)}\s+<", description) is not None


class SlashCommands(Provider):
    """Lists every slash command, built-in and from extensions, and runs the chosen one through
    :meth:`~minimal.ui.tui.app.MinimalTui.run_slash_command`."""

    @property
    def tui(self) -> MinimalTui:
        return cast("MinimalTui", self.app)

    async def discover(self) -> Hits:
        for name, command in sorted(self.tui.host.commands().items()):
            yield DiscoveryHit(f"/{name}", partial(self.tui.run_slash_command, name), help=escape(command.description))

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, command in sorted(self.tui.host.commands().items()):
            score = matcher.match(f"/{name}")
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(f"/{name}"),
                    partial(self.tui.run_slash_command, name),
                    help=escape(command.description),
                )
