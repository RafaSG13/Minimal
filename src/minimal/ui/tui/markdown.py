"""The transcript's Markdown, with the blocks extensions can draw themselves.

:class:`TranscriptMarkdown` swaps two Markdown blocks for ones that ask the extensions' renderers
(:meth:`~minimal.extensions.registry.Registry.register_renderer`) first: a code fence
(``fence:<language>``) and a blockquote that opens with a GitHub alert marker (``blockquote:alert``).
Without a renderer for a block, or when it returns ``None``, textual's own drawing is used.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, cast

from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.content import Content
from textual.widgets import Label, Static
from textual.widgets._markdown import MarkdownBlock, MarkdownBlockQuote, MarkdownParagraph
from textual.widgets.markdown import Markdown, MarkdownFence

from minimal.ui.tui.widgets import lines_content

if TYPE_CHECKING:
    from minimal.ui.tui.app import MinimalTui

ALERT = re.compile(r"^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*", re.I)
"""The alert marker that opens the first paragraph of an alert blockquote."""


def _tui(widget: Any) -> MinimalTui:
    return cast("MinimalTui", widget.app)


def _width(widget: Any, markdown: Markdown) -> int:
    """Columns a renderer may use: the Markdown's width less the block's horizontal padding."""
    return max((markdown.content_region.width or widget.app.size.width) - 4, 20)


class TranscriptMarkdown(Markdown):
    """:class:`~textual.widgets.Markdown` whose fences and alert blockquotes go through extension renderers."""

    def get_block_class(self, block_name: str) -> type[MarkdownBlock]:
        cls = super().get_block_class(block_name)
        # Only replace the stock classes: an extension that swapped Markdown.BLOCKS itself keeps its own.
        if block_name == "fence" and cls is MarkdownFence:
            return RenderedFence
        if block_name == "blockquote_open" and cls is MarkdownBlockQuote:
            return RenderedQuote
        return cls


class RenderedFence(MarkdownFence):
    """A code fence drawn by the ``fence:<language>`` renderer, if there is one; else as textual draws it."""

    def __init__(self, markdown: Markdown, token: Any, code: str) -> None:
        super().__init__(markdown, token, code)
        raw = token.content
        self.language = (token.info.split() or [""])[0].lower()
        self.kind = f"fence:{self.language}"
        # markdown-it maps a closed fence to its content lines plus the two fence lines.
        self.closed = (
            token.map is not None and raw.endswith("\n") and token.map[1] - token.map[0] == raw.count("\n") + 2
        )
        # While streaming, only complete lines are passed on.
        self.body = raw if self.closed else raw[: raw.rfind("\n") + 1]
        self._key: tuple[str, bool, int] | None = None
        self._lines: list[list[tuple[str, str]]] | None = None

    def _view(self) -> Content:
        if self.kind not in _tui(self).host.renderers or not self.body.strip():
            return self._highlighted_code
        width = _width(self, self._markdown)
        key = (self.body, self.closed, width)
        if key != self._key:
            self._key = key
            block = {
                "kind": self.kind,
                "language": self.language,
                "source": self.body,
                "closed": self.closed,
                "width": width,
            }
            self._lines = _tui(self).render_block(self.kind, block)
        return self._highlighted_code if self._lines is None else lines_content(self._lines)

    def compose(self) -> ComposeResult:
        yield Label(self._view(), id="code-content", expand=True)

    def set_content(self, content: Content) -> None:
        self._content = content
        try:
            self.query_one("#code-content", Label).update(self._view())
        except NoMatches:
            pass

    async def _update_from_block(self, block: MarkdownBlock) -> None:
        if isinstance(block, RenderedFence):
            self.language = block.language
            self.kind = block.kind
            self.closed = block.closed
            self.body = block.body
        await super()._update_from_block(block)

    def finish(self) -> None:
        """Draw a fence the reply left unclosed as if it were closed."""
        if not self.closed and self.kind in _tui(self).host.renderers:
            self.closed = True
            self.body = self.code + "\n"
            self.set_content(self._content)


class RenderedQuote(MarkdownBlockQuote):
    """A blockquote that opens with an alert marker; the ``blockquote:alert`` renderer draws the alert's heading and first paragraph."""

    DEFAULT_CSS = """
    RenderedQuote.-alert.-note { border-left: outer $accent; background: $accent 10%; }
    RenderedQuote.-alert.-tip { border-left: outer $success; background: $success 10%; }
    RenderedQuote.-alert.-important { border-left: outer $secondary; background: $secondary 10%; }
    RenderedQuote.-alert.-warning { border-left: outer $warning; background: $warning 10%; }
    RenderedQuote.-alert.-caution { border-left: outer $error; background: $error 10%; }
    """

    def compose(self) -> ComposeResult:
        first = self._blocks[0] if self._blocks else None
        if "blockquote:alert" in _tui(self).host.renderers and isinstance(first, MarkdownParagraph):
            plain = first._content.plain
            if m := ALERT.match(plain):
                alert = m.group(1).lower()
                block = {
                    "kind": "blockquote:alert",
                    "alert": alert,
                    "text": plain[m.end() :],
                    "width": _width(self, self._markdown),
                }
                lines = _tui(self).render_block("blockquote:alert", block)
                if lines is not None:
                    self._blocks.pop(0)
                    self.add_class("-alert", f"-{alert}")
                    yield Static(lines_content(lines), classes="alert")
        yield from super().compose()

