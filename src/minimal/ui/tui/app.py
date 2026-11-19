"""The interactive terminal UI, built with Textual.

Layout, top to bottom: the ``top`` extension area, the transcript (a welcome
header (the built-in wordmark or an extension's pixel-art logo), then user messages, assistant
replies, tool calls and command output), the ``bottom`` extension area, the
working indicator, the prompt, the status line (model, provider, directory,
git branch, and extension segments) and the ``footer`` area. The ``sidebar`` area is a column on the right (or the
left, or hidden: ``ui.sidebar``) and ``left`` another on the left; they hold extension widgets and panels (see
:meth:`~minimal.extensions.registry.Registry.register_widget` and ``register_panel``), stacked by their ``order``.
The ``ui.*`` settings and ``/ui`` move, reorder and hide widgets and status segments, set the density and the
prompt's glyph and placeholder, rebind keys and add a stylesheet (:meth:`MinimalTui.apply_ui`); a reload applies
them. Colours come from the
active theme's CSS variables and glyphs from :mod:`minimal.ui.tui.theme`.

Turns and slash commands run in a worker thread so the UI stays responsive;
:class:`~minimal.ui.tui.frontend.TuiFrontend` hands their output and confirmations back to the UI
thread. Esc interrupts the turn in progress; ``shift+tab`` cycles the mode (``ui.keys`` rebinds these and the
extensions' keybindings, :meth:`MinimalTui.apply_keys`). Anything printed (slash-command
output, extension messages) is shown in the transcript. Commands marked
``terminal`` run with the UI suspended.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from textual import events, work
from textual.app import App as TextualApp
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.css.stylesheet import Stylesheet, StylesheetParseError
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widget import Widget
from textual.widgets import Input, Markdown, Static

from minimal import config
from minimal.core.app import ExitRepl
from minimal.core.host import Host, HostStatus
from minimal.core.layout import Layout, Placed
from minimal.extensions.registry import KEY_ACTIONS, KeybindingSpec, StyledLine, styled_line, styled_lines
from minimal.ui.tui import theme
from minimal.ui.tui.frontend import TuiFrontend, theme_command
from minimal.ui.tui.markdown import RenderedFence, TranscriptMarkdown
from minimal.ui.tui.widgets import Header, PanelView, ToolBlock, to_content

REPLAY_LIMIT = 200
"""Most messages of a resumed session drawn in the transcript."""

PLACEHOLDER = "Message, or / for commands"
"""The prompt's hint (``ui.placeholder`` replaces it)."""

PRIORITY_ACTIONS = frozenset({"cycle_mode"})
"""Key actions that fire even while the prompt has the focus. ``shift+tab`` moves the focus (the screen's own
binding) unless it is priority; ``cycle_mode`` still lets the key move the focus when it does not apply
(:meth:`MinimalTui.check_action`)."""

EXT_KEY_ID = "ext-key:"
"""Prefix of the ids of the Textual bindings that run extension keybindings (:meth:`MinimalTui.apply_keys`)."""


def _css_problem(error: Exception) -> str:
    """A one-line reason a stylesheet did not parse: the first error's line and message."""
    if isinstance(error, StylesheetParseError):
        for rule in error.errors.rules:
            for token, message in rule.errors:
                line = (token.referenced_by or token).location[0] + 1
                return f"line {line}: {getattr(message, 'summary', message)}"
    return str(error) or type(error).__name__


@contextmanager
def _outside_event_loop() -> Iterator[None]:
    """Hide Textual's running loop from synchronous code run on the UI thread.

    A terminal command (``terminal=True``, such as ``/setup``) may use a prompt toolkit that calls ``asyncio.run``,
    which refuses to start while a loop is running in the thread. The UI loop
    is blocked for the whole command anyway, so a nested loop is safe; running
    the command on the UI thread also keeps Ctrl-C working for it.
    """
    loop = asyncio.get_running_loop()
    asyncio.events._set_running_loop(None)
    try:
        yield
    finally:
        asyncio.events._set_running_loop(loop)


def git_status(cwd: str) -> tuple[str, bool] | None:
    """``(branch, dirty)`` for the repository containing ``cwd``, or ``None`` outside one."""
    try:
        result = subprocess.run(
            # core.fsmonitor=false: a repository's own config must not make git run a command
            ["git", "-c", "core.fsmonitor=false", "--no-optional-locks", "status", "--porcelain=v1", "--branch"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = result.stdout.splitlines()
    if result.returncode != 0 or not lines or not lines[0].startswith("## "):
        return None
    head = lines[0][3:].removeprefix("No commits yet on ")
    return head.split("...")[0], len(lines) > 1


def _short_path(path: str) -> str:
    """``path`` with the home directory written as ``~``."""
    home = str(Path.home())
    return (
        "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path
    )


class CommandSuggester(Suggester):
    """Completes a slash command's name in the prompt (→ accepts), built-in and extension commands alike."""

    def __init__(self, host: Host) -> None:
        super().__init__(use_cache=False, case_sensitive=True)  # extension commands change on reload
        self.host = host

    async def get_suggestion(self, value: str) -> str | None:
        return next((line for line in self.host.complete(value) if line != value), None)


class MinimalTui(TextualApp):
    """The terminal UI, driven through a :class:`~minimal.core.host.Host`."""

    TITLE = "minimal"

    BINDINGS = [
        Binding("escape", "interrupt", "Interrupt turn", show=False, id="interrupt"),
        Binding("ctrl+b", "toggle_sidebar", "Toggle sidebar", show=False, id="toggle_sidebar"),
        Binding("shift+tab", "cycle_mode", "Cycle mode", show=False, priority=True, id="cycle_mode"),
    ]

    CSS = """
    #body { height: 1fr; }
    #main { width: 1fr; }
    #transcript { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
    #top, #bottom, #footer { height: auto; }
    #left, #sidebar { width: 36; padding: 0 1; display: none; scrollbar-size-vertical: 1; }
    #left { border-right: solid $border-muted; }
    #sidebar { border-left: solid $border-muted; }
    #sidebar.-left { border-left: none; border-right: solid $border-muted; }
    #left.-has-widgets, #sidebar.-has-widgets { display: block; }
    #left.-collapsed, #sidebar.-collapsed, #sidebar.-hidden { display: none; }

    Header { height: auto; padding: 1 1 0 1; }
    Header.-align-center Static { text-align: center; }
    Header.-align-right Static { text-align: right; }
    Header .logo, Header .wordmark { text-wrap: nowrap; text-overflow: clip; }
    Header .wordmark { margin-bottom: 1; }
    .user { background: $user-message-bg; padding: 1 2; margin-top: 1; }
    Markdown.assistant { padding: 0 1; margin-top: 1; }
    Markdown.assistant MarkdownBlock:dark > .code_inline,
    Markdown.assistant MarkdownBlock:light > .code_inline { background: $surface; color: $md-code; }
    Markdown.assistant MarkdownFence { background: $surface; }
    Markdown.assistant MarkdownBullet { color: $accent; }
    .tool { padding: 1 2; margin-top: 1; }
    .tool.-pending { background: $tool-pending-bg; }
    .tool.-success { background: $tool-success-bg; }
    .tool.-error { background: $tool-error-bg; }
    .output { color: $foreground 80%; padding: 0 1; margin-top: 1; }
    .error { color: $error; background: $tool-error-bg; padding: 0 2; margin-top: 1; }

    #working { height: 1; padding: 0 2; margin-top: 1; display: none; }
    #working.-busy { display: block; }
    #composer {
        height: auto; margin-top: 1;
        border-top: solid $border-muted; border-bottom: solid $border-muted;
    }
    #composer:focus-within { border-top: solid $border-blue; border-bottom: solid $border-blue; }
    #prompt-glyph { width: 3; padding: 0 1; color: $accent; text-style: bold; }
    #prompt { border: none; height: 1; padding: 0; background: transparent; }
    #prompt:focus { border: none; }
    #prompt > .input--placeholder { color: $dim; }
    #statusline { height: 1; background: $status-bg; }
    #status-left { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    #status-right { width: auto; color: $dim; padding: 0 1 0 2; }

    MinimalTui.-compact Header { padding: 0 1; }
    MinimalTui.-compact Header .wordmark { margin-bottom: 0; }
    MinimalTui.-compact .user { padding: 0 2; margin-top: 0; }
    MinimalTui.-compact Markdown.assistant { margin-top: 0; }
    MinimalTui.-compact .tool { padding: 0 2; margin-top: 0; }
    MinimalTui.-compact .output, MinimalTui.-compact .error { margin-top: 0; }
    MinimalTui.-compact #working, MinimalTui.-compact #composer { margin-top: 0; }
    """

    def __init__(self, host: Host, theme_name: str | None = None) -> None:
        super().__init__()
        self.host = host
        self.sync_themes(theme_name)
        self.ui_thread: int | None = None
        self.pending_modal: threading.Event | None = None
        self.assistant: Markdown | None = None
        self.tool_blocks: dict[str, ToolBlock] = {}
        self.output_block: Static | None = None
        self.output_buffer: list[str] = []
        self.output_stderr = False
        self.ext_widgets: list[tuple[str, Widget]] = []
        self.shown_session = host.status().session_id
        self.busy_since = 0.0
        self.in_turn = False  # a model turn, not a slash command, is running: esc interrupts it
        self.git: tuple[str, bool] | None = None  # last git_status, refreshed off the UI thread
        self.status_errors: set[str] = set()  # status segments whose error was already shown
        self.renderer_errors: set[str] = set()  # renderer kinds whose error was already shown
        self.busy = False  # a turn or command is running: the prompt is locked
        self.ext_keys: list[KeybindingSpec] = []  # the extension keybindings apply_keys bound, by action index
        self.user_css: tuple[str, str] | None = None  # where the stylesheet of ui.css was added, if one was

    @property
    def cwd(self) -> str:
        """The working directory (tool paths and git status are relative to it)."""
        return self.host.status().cwd

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield VerticalScroll(id="left")
            with Vertical(id="main"):
                yield Vertical(id="top")
                with VerticalScroll(id="transcript"):
                    yield Header(self.host)
                yield Vertical(id="bottom")
                yield Static(id="working")
                with Horizontal(id="composer"):
                    yield Static(theme.PROMPT, id="prompt-glyph")
                    yield Input(
                        placeholder="Message, or / for commands",
                        id="prompt",
                        suggester=CommandSuggester(self.host),
                    )
                with Horizontal(id="statusline"):
                    yield Static(id="status-left")
                    yield Static(id="status-right")
                yield Vertical(id="footer")
            yield VerticalScroll(id="sidebar")

    async def on_mount(self) -> None:
        self.ui_thread = threading.get_ident()
        self.begin_capture_print(self, stdout=True, stderr=True)
        frontend = TuiFrontend(self)
        self.host.attach(frontend)
        self.host.register_command(
            "theme", theme_command(frontend), "pick the colour theme, or switch: /theme <name>"
        )
        self.spinner = self.set_interval(0.08, self.refresh_working, pause=True)
        problems = self.apply_ui()
        self.refresh_css(animate=False)
        await self.remount_widgets()
        pending = self.replay_history()
        if pending is not None:
            await pending
        for e in self.host.extension_errors:
            self.show_error(f"[extension] could not load {e}")
        for problem in problems:
            self.show_error(f"[ui] {problem}")
        if notice := self.host.untrusted_notice():
            self._add(Static(Content.assemble((theme.WARNING, "$warning"), f" {notice}"), classes="output"))
        self.refresh_status()
        self.query_one("#prompt", Input).focus()
        self.host.begin()

    def on_unmount(self) -> None:
        if self.pending_modal is not None:  # release a worker waiting for an answer
            self.pending_modal.set()

    # --- transcript ----------------------------------------------------------

    def _add(self, widget: Widget) -> Any:
        """Append ``widget`` to the transcript; returns the mount awaitable."""
        self.call_after_refresh(self._follow)
        return self.query_one("#transcript", VerticalScroll).mount(widget)

    def _follow(self) -> None:
        """Keep the transcript scrolled to the bottom once it overflows.

        Anchoring earlier would bottom-align short content; until it fills the
        screen, the conversation grows from the top.
        """
        transcript = self.query_one("#transcript", VerticalScroll)
        if not transcript.is_anchored and transcript.max_scroll_y > 0:
            transcript.anchor()

    def replay_history(self) -> Any:
        """Draw the current session's last :data:`REPLAY_LIMIT` messages in the transcript.

        Returns the mount awaitable, or ``None`` if there is nothing to draw.
        """
        messages = self.host.messages
        hidden = max(0, len(messages) - REPLAY_LIMIT)
        widgets: list[Widget] = []
        if hidden:
            widgets.append(Static(f"… {hidden} earlier messages not shown", classes="output"))
        blocks: dict[str, ToolBlock] = {}
        for message in messages[hidden:]:
            content = message["content"]
            if isinstance(content, str):
                widgets.append(Static(content, markup=False, classes="user"))
                continue
            for block in content:
                kind = block.get("type")
                if kind == "text" and message["role"] == "assistant":
                    widgets.append(TranscriptMarkdown(block["text"], classes="assistant"))
                elif kind == "tool_use":
                    blocks[block["id"]] = ToolBlock(block, self.cwd, self._tool_renderer(block))
                    widgets.append(blocks[block["id"]])
                elif kind == "tool_result" and block["tool_use_id"] in blocks:
                    blocks[block["tool_use_id"]].finish(block)
        if not widgets:
            return None
        self.call_after_refresh(self._follow)
        return self.query_one("#transcript", VerticalScroll).mount(*widgets)

    def show_error(self, text: str) -> Any:
        return self._add(Static(Content(f"{theme.ERROR} {text}"), classes="error"))

    def on_print(self, event: events.Print) -> None:
        """Show printed text (command output, extension messages) as an output block."""
        transcript = self.query_one("#transcript", VerticalScroll)
        last = transcript.children[-1] if transcript.children else None
        if (
            self.output_block is None
            or last is not self.output_block
            or self.output_stderr != event.stderr
        ):
            self.output_block = Static(
                "", markup=False, classes="error" if event.stderr else "output"
            )
            self.output_buffer = []
            self.output_stderr = event.stderr
            self._add(self.output_block)
        self.output_buffer.append(event.text)
        self.output_block.update("".join(self.output_buffer).rstrip("\n"))

    async def append_text(self, chunk: str) -> None:
        # Mount first: Markdown resets its source on mount, which would drop an earlier append.
        if self.assistant is None:
            self.assistant = TranscriptMarkdown("", classes="assistant")
            await self._add(self.assistant)
        await self.assistant.append(chunk)
        self.call_after_refresh(self._follow)

    def show_tool_call(self, call: dict) -> Any:
        self.assistant = None
        block = ToolBlock(call, self.cwd, self._tool_renderer(call))
        self.tool_blocks[call["id"]] = block
        mounted = self._add(block)
        self.refresh_live({"kind": "tool_call", "call": call})
        return mounted

    def show_tool_result(self, call: dict, result: dict) -> None:
        block = self.tool_blocks.pop(call["id"], None)
        if block is not None:
            block.finish(result)
            self.call_after_refresh(self._follow)
        self.refresh_live({"kind": "tool_result", "call": call, "result": result})

    def end_turn(self, error: str | None) -> Any:
        self.assistant = None
        self.tool_blocks.clear()
        if error:
            return self.show_error(f"Error: {error}")
        return None

    # --- extension themes and widgets ----------------------------------------

    def sync_themes(self, prefer: str | None = None) -> None:
        """Offer the built-in theme plus the ones extensions registered, and nothing else.

        Textual's own themes are dropped: they lack the UI's CSS variables.
        Switches to ``prefer`` if offered, and to the default if the current theme is gone.
        """
        wanted = theme.available(self.host.themes)
        for t in wanted.values():
            self.register_theme(t)
        if prefer in wanted:
            self.theme = prefer
        elif self.theme not in wanted:
            self.theme = theme.DEFAULT
        for name in set(self.available_themes) - set(wanted):
            self.unregister_theme(name)

    async def extensions_reloaded(self) -> None:
        """Pick up the reloaded registry and settings: themes (an edited one repaints), the ``ui.*`` settings, the
        widgets and the header (logo and extensions list)."""
        self.sync_themes()
        problems = self.apply_ui()
        self.refresh_css(animate=False)
        await self.remount_widgets()
        self._render_status()
        for header in self.query(Header):
            await header.recompose()
        for problem in problems:
            self.show_error(f"[ui] {problem}")

    def apply_ui(self) -> list[str]:
        """Apply the ``ui.*`` settings of the last reload except the widget layout (:meth:`remount_widgets`):
        density, sidebar side and width, prompt glyph and placeholder, keys and the user stylesheet.

        Returns:
            What the user asked for that could not be done, one line each.
        """
        settings = self.host.settings
        self.set_class(settings.ui_density == "compact", "-compact")
        body, main = self.query_one("#body"), self.query_one("#main")
        left, sidebar = self.query_one("#left"), self.query_one("#sidebar")
        sidebar.set_class(settings.ui_sidebar == "left", "-left")
        sidebar.set_class(settings.ui_sidebar == "hidden", "-hidden")
        order = list(body.children)
        if settings.ui_sidebar == "left" and order.index(sidebar) > order.index(main):
            body.move_child(sidebar, before=main)
        elif settings.ui_sidebar != "left" and order.index(sidebar) < order.index(main):
            body.move_child(sidebar, after=main)
        for bar in (left, sidebar):
            if settings.ui_sidebar_width is None:
                bar.styles.clear_rule("width")
            else:
                bar.styles.width = settings.ui_sidebar_width
        glyph = self.query_one("#prompt-glyph", Static)
        custom = settings.ui_prompt_glyph
        glyph.update(Content(custom or theme.PROMPT))
        if custom:
            glyph.styles.width = len(custom) + 2
        else:
            glyph.styles.clear_rule("width")
        self.query_one("#prompt").placeholder = settings.ui_placeholder or PLACEHOLDER  # type: ignore[attr-defined]
        return [*self.host.layout().problems, *self.apply_keys(), *self.apply_css()]

    def apply_keys(self) -> list[str]:
        """Bind the built-in actions and the extensions' keybindings to the keys :meth:`Host.key_map` gives them,
        dropping the previous ones. Returns the key problems."""
        bound, problems = self.host.key_map()
        for key, bindings in list(self._bindings.key_to_bindings.items()):
            kept = [b for b in bindings if b.id not in KEY_ACTIONS and not (b.id or "").startswith(EXT_KEY_ID)]
            if kept:
                self._bindings.key_to_bindings[key] = kept
            else:
                del self._bindings.key_to_bindings[key]
        self.ext_keys = []
        for item in bound:
            if item.key is None:
                continue
            if item.spec is None:
                binding = Binding(
                    item.key, item.id, item.description, show=False, priority=item.id in PRIORITY_ACTIONS, id=item.id
                )
            else:
                binding = Binding(
                    item.key, f"ext_key({len(self.ext_keys)})", item.description or item.spec.label,
                    show=item.spec.show, priority=True, id=f"{EXT_KEY_ID}{len(self.ext_keys)}",
                )  # fmt: skip
                self.ext_keys.append(item.spec)
            self._bindings._add_binding(binding)
        self.refresh_bindings()
        return problems

    def apply_css(self) -> list[str]:
        """Use the stylesheet ``ui.css`` names (or stop using the one used before). A file that cannot be read or
        parsed is reported and ignored: the UI keeps its own styles. The caller refreshes the CSS."""
        path = self.host.settings.ui_css
        if self.user_css is not None:
            self.stylesheet.source.pop(self.user_css, None)
            self.user_css = None
        if path is None:
            return []
        try:
            css = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return [f"ui.css: cannot read {path}: {e}"]
        location = (str(path), "")
        try:
            probe = Stylesheet(variables=self.get_css_variables())
            probe.add_source(css, read_from=location)
            probe.parse()
        except Exception as e:
            return [f"ui.css: {path} was not applied: {_css_problem(e)}"]
        self.stylesheet.add_source(css, read_from=location)
        self.user_css = location
        return []

    async def remount_widgets(self) -> None:
        """Replace every extension widget and panel with fresh ones, placed as :meth:`Host.layout` says."""
        layout = self.host.layout()
        for area in config.UI_AREAS:
            await self.query_one(f"#{area}").remove_children()
        self.ext_widgets = []
        self.status_errors.clear()
        self.renderer_errors.clear()
        for area in config.UI_AREAS:
            container = self.query_one(f"#{area}")
            for placed in layout.in_area(area):
                try:
                    if placed.kind == "widget":
                        widget = placed.spec.create(self.host.ctx)
                    else:
                        widget = PanelView(placed.spec, self.host.ctx, self.show_error)
                    await container.mount(widget)
                    self.ext_widgets.append((placed.name, widget))
                except Exception as e:
                    self.show_error(f"[extension] {placed.kind} {placed.name}: {e}")
        for area in ("sidebar", "left"):
            self.query_one(f"#{area}").set_class(bool(layout.in_area(area)), "-has-widgets")
        self.refresh_widgets()

    def render_block(self, kind: str, block: dict) -> list[StyledLine] | None:
        """The lines an extension's renderer of ``kind`` draws for ``block``; ``None`` for the default drawing.

        A renderer that raises is reported once per kind (until the next reload).
        """
        spec = self.host.renderers.get(kind)
        if spec is None:
            return None
        try:
            return styled_lines(spec.render(self.host.ctx, block))
        except Exception as e:
            if kind not in self.renderer_errors:
                self.renderer_errors.add(kind)
                # Later: this may run while another widget composes, when mounting is not allowed.
                self.call_later(self.show_error, f"[extension] renderer {kind}: {e}")
            return None

    def _tool_renderer(self, call: dict) -> Callable[[dict | None], list[StyledLine] | None] | None:
        """The ``tool:<name>`` renderer for ``call`` as a function of its result (``None`` while pending), if any."""
        kind = f"tool:{call['name']}"
        if kind not in self.host.renderers:
            return None
        return lambda result: self.render_block(kind, {"kind": kind, "call": call, "result": result})

    def _call_widgets(self, method: str, *args: Any) -> None:
        """Call ``method(*args)`` on every extension widget that has it, showing any error."""
        for name, widget in self.ext_widgets:
            fn = getattr(widget, method, None)
            if not callable(fn):
                continue
            try:
                fn(*args)
            except Exception as e:
                self.show_error(f"[extension] widget {name}: {e}")

    def refresh_widgets(self) -> None:
        """Call ``refresh_state(ctx)`` on every extension widget that has it."""
        self._call_widgets("refresh_state", self.host.ctx)

    def refresh_live(self, event: dict) -> None:
        """Call ``refresh_live(ctx, event)`` on every extension widget that has it, then redraw the status line."""
        self._call_widgets("refresh_live", self.host.ctx, event)
        self._render_status()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Let the ``cycle_mode`` key cycle modes only when one is registered, no dialog is open and no turn is
        running (otherwise it still moves the focus), and extension keybindings run only when no dialog is open and
        nothing is running."""
        if action == "cycle_mode":
            return bool(self.host.modes()) and not isinstance(self.screen, ModalScreen) and not self.busy
        if action == "ext_key":
            return not isinstance(self.screen, ModalScreen) and not self.busy
        return True

    @work(thread=True, exclusive=True, group="agent")
    def action_cycle_mode(self) -> None:
        """Go to the next mode (:meth:`~minimal.core.host.Host.cycle_mode`) and show which one is current.

        It runs in a worker like a command, so a ``mode_change`` handler may ask the user a question."""
        mode = self.host.cycle_mode()
        self.call_from_thread(self.notify, f"Mode: {mode or 'none'}", timeout=2, markup=False)

    def action_toggle_sidebar(self) -> None:
        """Hide or show the sidebars (``sidebar`` and ``left`` areas)."""
        self.query_one("#sidebar").toggle_class("-collapsed")
        self.query_one("#left").toggle_class("-collapsed")

    def action_ext_key(self, index: int) -> None:
        """Run the extension keybinding ``index`` (of :attr:`ext_keys`) like a slash command: the prompt is locked and
        the function runs on a worker, so it may ask the user questions."""
        if index >= len(self.ext_keys):
            return
        self.in_turn = False
        self.set_busy(True)
        self.refresh_live({"kind": "start", "turn": False})
        self.run_key_worker(self.ext_keys[index])

    @work(thread=True, exclusive=True, group="agent")
    def run_key_worker(self, spec: KeybindingSpec) -> None:
        try:
            spec.run(self.host.ctx)
        except ExitRepl:
            self.call_from_thread(self.exit)
            return
        except Exception as e:
            self.call_from_thread(self.show_error, f"[extension] key {spec.key} ({spec.id}): {e}")
        self.call_from_thread(self.after_action)

    # --- status line and working indicator -----------------------------------

    def refresh_status(self) -> None:
        """Redraw the status line now, then update its git part in the background."""
        self._render_status()
        self.refresh_git()

    @work(thread=True, exclusive=True, group="git", exit_on_error=False)
    def refresh_git(self) -> None:
        """Run ``git status`` in a worker thread (it can take up to a second) and show the result."""
        git = git_status(self.cwd)
        try:
            self.call_from_thread(self._set_git, git)
        except RuntimeError:  # the app closed meanwhile
            pass

    def _set_git(self, git: tuple[str, bool] | None) -> None:
        if git != self.git:
            self.git = git
            self._render_status()

    def _report_status_error(self, name: str, error: Exception) -> None:
        if name not in self.status_errors:
            self.status_errors.add(name)
            self.show_error(f"[extension] status {name}: {error}")

    def _status_segment(self, placed: Placed, status: HostStatus) -> Content | None:
        """What one status segment shows now, or ``None``; a failing extension segment is skipped and reported once."""
        spec = placed.spec
        if spec is None:
            return self._builtin_segment(placed.name, status)
        try:
            value = spec.render(self.host.ctx)
        except Exception as e:
            self._report_status_error(placed.name, e)
            return None
        if value is None:
            return None
        try:
            return value if isinstance(value, Content) else to_content(styled_line(value))
        except ValueError as e:
            self._report_status_error(placed.name, e)
            return None

    def _builtin_segment(self, name: str, status: HostStatus) -> Content | None:
        """minimal's own status segment ``name`` (:data:`~minimal.core.layout.BUILTIN_SEGMENTS`), or ``None`` if it has nothing to show."""
        if name == "model":
            return Content.assemble(
                (f"{theme.MODEL} {status.model}", "$status-model"), (f"{theme.DOT}{status.provider}", "$muted")
            )
        if name == "path":
            return Content.assemble((_short_path(status.cwd), "$status-path"))
        if name == "git":
            if self.git is None:
                return None
            branch, dirty = self.git
            color = "$status-git-dirty" if dirty else "$status-git-clean"
            return Content.assemble((f"{theme.BRANCH} {branch}{' *' if dirty else ''}", color))
        if name == "mode":
            return Content.assemble((f"mode {status.mode}", "$accent")) if status.mode else None
        if name == "context":
            tokens = status.context_tokens
            return Content(f"{tokens / 1000:.1f}k ctx") if tokens is not None else None
        if name == "extensions":
            return Content(f"{status.extensions} ext")
        return Content(f"session {status.session_id}")

    def _status_segments(self, layout: Layout, side: str, status: HostStatus) -> list[Content]:
        """The segments of ``side`` of the status line in order: minimal's and the extensions', less the hidden ones."""
        pieces: list[tuple[int, int, Content]] = []
        for placed in layout.segments(side):
            if (content := self._status_segment(placed, status)) is not None:
                pieces.append((placed.order, placed.seq, content))
        if side == "right" and status.untrusted:  # not a segment of its own: it must not be hidden
            warning = Content.assemble((f"{theme.WARNING} {status.untrusted} untrusted", "$warning"))
            pieces.append((99, -2, warning))
        return [content for _, _, content in sorted(pieces, key=lambda p: p[:2])]

    def _render_status(self) -> None:
        status = self.host.status()
        layout = self.host.layout()
        sep = (f" {theme.SEPARATOR} ", "$border-muted")
        parts: list[str | tuple[str, str] | Content] = [" "]
        for i, segment in enumerate(self._status_segments(layout, "left", status)):
            parts += [sep, segment] if i else [segment]
        self.query_one("#status-left", Static).update(Content.assemble(*parts))
        self.query_one("#status-right", Static).update(
            Content(theme.DOT).join(self._status_segments(layout, "right", status))
        )

    def set_busy(self, busy: bool) -> None:
        """Lock the prompt and show the working indicator while a turn or command runs."""
        self.busy = busy
        self.query_one("#prompt", Input).disabled = busy
        self.query_one("#working").set_class(busy, "-busy")
        if busy:
            self.busy_since = time.monotonic()
            self.refresh_working()
            self.spinner.resume()
        else:
            self.spinner.pause()

    def refresh_working(self) -> None:
        elapsed = time.monotonic() - self.busy_since
        frame = theme.SPINNER[int(elapsed / 0.08) % len(theme.SPINNER)]
        parts: list[str | tuple[str, str]] = [(frame, "$accent"), " "]
        if self.in_turn and self.host.interrupting:
            parts.append(("Interrupting…", "$muted"))
        else:
            parts += [("Working…", "$muted"), (f" {elapsed:.0f}s", "$dim")]
            if self.in_turn:
                parts.append((" · esc interrupts", "$dim"))
        self.query_one("#working", Static).update(Content.assemble(*parts))

    def action_interrupt(self) -> None:
        """Ask the turn in progress to stop (see :class:`~minimal.extensions.registry.TurnInterrupted`)."""
        if self.in_turn and not self.host.interrupting:
            self.host.interrupt()
            self.refresh_working()

    # --- input ---------------------------------------------------------------

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.input.clear()
        self.submit(event.value.strip())

    def submit(self, line: str) -> None:
        """Send ``line`` as if the user typed it: a ``/command`` or a message to the model."""
        if not line:
            return
        self.in_turn = not line.startswith("/")
        self.set_busy(True)
        self.refresh_live({"kind": "start", "turn": self.in_turn})
        if line.startswith("/"):
            cmd = self.host.command(line[1:].partition(" ")[0])
            if cmd is not None and cmd.terminal:
                try:
                    with self.suspend(), _outside_event_loop():
                        self.host.run_command(line)
                except ExitRepl:
                    self.exit()
                    return
                self.after_action()
            else:
                self.run_command_worker(line)
            return
        self._add(Static(line, markup=False, classes="user"))
        self.assistant = None
        self.run_turn_worker(line)

    @work(thread=True, exclusive=True, group="agent")
    def run_turn_worker(self, line: str) -> None:
        self.host.submit(line)
        self.call_from_thread(self.after_action)

    @work(thread=True, exclusive=True, group="agent")
    def run_command_worker(self, line: str) -> None:
        try:
            self.host.run_command(line)
        except ExitRepl:
            self.call_from_thread(self.exit)
            return
        self.call_from_thread(self.after_action)

    def after_action(self) -> None:
        """After a turn or command: follow a session switch, refresh the status and widgets, re-enable input."""
        if self.host.status().session_id != self.shown_session:
            transcript = self.query_one("#transcript", VerticalScroll)
            transcript.remove_children()
            transcript.anchor(False)
            transcript.mount(Header(self.host))
            self.replay_history()
            self.shown_session = self.host.status().session_id
            self.assistant = None
        self.refresh_status()
        for fence in self.query(RenderedFence):  # a reply that left a fence open: draw it closed
            fence.finish()
        self.refresh_widgets()
        self.in_turn = False
        self.set_busy(False)
        self.query_one("#prompt", Input).focus()
        self.submit_queued()

    def submit_queued(self) -> None:
        """Send the next message an extension queued (``ctx.queue_user_message``), unless a turn or command is running."""
        if self.query_one("#prompt", Input).disabled:
            return  # after_action sends it
        if (queued := self.host.next_queued()) is not None:
            self.submit(queued)


def run_tui(host: Host) -> None:
    """Run the terminal UI until the user quits; remember the theme picked with /theme or ctrl+p in ~/.minimal/settings.json."""
    tui = MinimalTui(host, host.settings.theme)
    started = tui.theme
    tui.run()
    if tui.theme != started:
        try:
            config.update_global({"theme": tui.theme})
        except (OSError, ValueError) as e:
            print(f"Theme not saved: {e}", file=sys.stderr)
