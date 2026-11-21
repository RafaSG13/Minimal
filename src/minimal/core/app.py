"""Runtime state of a ``minimal`` process, and how one user turn is executed.

:class:`App` is internal: extensions see it through :attr:`App.context`, the
:class:`~minimal.core.context.Context` passed as ``ctx`` to their tools, slash
commands, events and widgets. Built-in slash commands get the same ``ctx``.

What the user sees goes through a :class:`Frontend`: the terminal UI's, or
:class:`ConsoleFrontend` (plain stdout, the default of an :class:`App` built without one).
"""

from __future__ import annotations

import collections
import copy
import json
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

import minimal.core.context
import minimal.extensions.facade
import minimal.extensions.loader
from minimal import config
from minimal.core.agent import MaxTurnsExceeded, run_agent
from minimal.core.agents import AgentResult, Agents, AgentRun, CancelEvent, UsageMeter
from minimal.core.context import Context
from minimal.core.git import GitInfo, GitPoller
from minimal.core.permissions import Permissions, suggest_rule
from minimal.core.session import Session, check_cut, new_id, open_session
from minimal.extensions.loader import (
    ProjectSnapshot,
    global_extensions_dir,
    load_extensions,
    project_extensions_dir,
    read_project_snapshot,
)
from minimal.extensions import palette
from minimal.extensions.registry import (
    PROMPT_SECTIONS,
    Command,
    CommandComplete,
    CommandRun,
    Registry,
    ThemeInfo,
    Tool,
    TurnInterrupted,
    apply_prompt_edits,
)
from minimal.providers import register_builtin_providers, windows
from minimal.providers.base import Provider, Usage, result_text
from minimal.tools.builtin import register_builtin_tools

DEFAULT_PROMPT_FILE = Path(__file__).with_name("prompt.md")


def base_prompt(settings: config.Settings) -> str:
    """The base prompt: the ``prompt.base`` file of ``settings`` if set, else the ``prompt.md`` shipped in the package.

    The user's file replaces the shipped one entirely; a blank file means no base prompt.
    It is read on every turn, so edits apply from the next one without ``/reload``.

    Raises:
        ValueError: if the configured file is missing or cannot be read as UTF-8 text.
    """
    path = settings.prompt_base or DEFAULT_PROMPT_FILE
    if settings.prompt_base is not None and not path.exists():
        raise ValueError(f"{path} (prompt.base) not found")
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"{path} is unreadable ({e}); fix it or delete it to use the default prompt") from e


SELF_EXTEND_PROMPT = (
    "You can extend minimal, the harness you are running in. An extension is one Python file, or a folder with an "
    "__init__.py (one extension that may hold helper modules), defining register(minimal); minimal is an Extension "
    "(API and docstrings: {facade_path}) and every ctx is a Context ({context_path}). Import only from minimal.api; "
    "never import other minimal modules, they move between releases. Start the file with a docstring (its first line "
    'describes it), set __version__ = "1.0" and __api__ = "{api_major}", the minimal API it is written for (without '
    "it, minimal does not load the file). "
    "Save new extensions to {global_dir}/<name>.py, or {project_dir}/<name>.py when the user wants it only for this "
    "project; themes, tools and UI widgets go in the themes/, tools/ or widgets/ subfolder, as the marketplace groups "
    "them. "
    "@minimal.tool(description=..., input_schema=..., confirm=...) on run(input, ctx) adds a tool (name and "
    "description default to the function's name and docstring); @minimal.command() on run(args, ctx) adds a slash "
    'command; @minimal.on(event) on handler(event) hooks events: "tool_call", "tool_result", "turn_end", '
    '"session_start", "session_switch", "turn_start", "before_request", "after_response", "shutdown", '
    '"provider_error", "agent_start", "agent_end" or "mode_change" (event["ctx"] is the Context; turn_end, '
    'before_request and after_response have event["nested"] for a subagent\'s loop, and the events of a loop have '
    'event["agent_id"] and event["parent_id"]; turn_start may return {{"text": ...}} to rewrite the user\'s message, before_request '
    '{{"messages", "system", "tools"}} to send instead for that request only, and provider_error '
    '{{"retry_after": seconds}} or {{"provider": name, "model": name}} to retry a failed request); the same exist as '
    "minimal.register_tool, "
    "register_command and on(event, handler). minimal.register_provider(...) adds a model; "
    "minimal.append_system_prompt(text) adds instructions, and minimal.register_prompt_section(name, text, "
    "replace=None) / remove_prompt_section(name) add, replace or remove a named section of this prompt (ctx.prompt_sections() "
    "lists them); minimal.register_mode(name, description=..., tools=None, exclude_tools=(), system_note='', "
    "read_only=False) adds a mode that restricts the tools the model may use (the user picks it with /mode or "
    "shift+tab; ctx.mode, ctx.modes(), ctx.set_mode(name), ctx.cycle_mode()); minimal.project_trusted and "
    "ctx.project_trusted say whether the user trusted this project; minimal.on_dispose(fn) runs fn before a reload and at "
    "exit: stop threads, timers and subprocesses there; minimal.data_dir(scope='project'|'global') is a private "
    "folder for the extension's own files, outside the repository; settings = minimal.declare_settings({{...defaults}}) "
    "declares settings the user sets under extensions.settings.<id> in ~/.minimal/settings.json (id is the name, or "
    "category/name in a subfolder). minimal.register_widget(name, create, area) adds a visual "
    'component to the terminal UI: create(ctx) returns a textual Widget for area "top", "bottom" or "sidebar", '
    "styled with its DEFAULT_CSS, and an optional refresh_state(ctx) method is called after every turn; "
    "an optional refresh_live(ctx, event) method is called live during a turn (event kind 'start', 'tool_call' or "
    "'tool_result'); "
    "minimal.register_panel(name, render, area='sidebar', live=None, interval=0) adds a panel of styled text "
    "(prefer it to register_widget): render(ctx) returns None to hide it, a str, or a list of lines, each a str or a "
    "list of str and (text, style) pieces, where style is space-separated names from minimal.api.STYLES such as "
    "'bold muted'; live(ctx, event) gets event kind 'start', 'tool_call', 'tool_result' or 'end', and interval > 0 "
    "redraws every interval seconds. "
    "minimal.register_status(name, render, side='right') adds a status-line segment: render(ctx) "
    "returns None, a str or a list of str and (text, style) pieces. "
    "minimal.register_renderer(kind, render) draws 'fence:<language>' code blocks, 'blockquote:alert' GitHub alerts "
    "or 'tool:<name>' tool-call bodies: render(ctx, block) returns lines like a panel, or None for the default "
    "drawing. "
    "minimal.register_theme(name, dark=..., background=..., foreground=..., accent=..., **more #rrggbb colours) "
    "adds a colour theme the user picks with /theme. "
    "minimal.register_logo(art, colors, align='center') replaces the default wordmark in the welcome header with "
    "pixel art, aligned 'left', 'center' or 'right': art is a list of "
    "equal-length strings, one character per pixel (' ' or '.' transparent), two pixel rows per terminal row; colors "
    "maps every other character to #rrggbb or a theme variable such as $accent. "
    "In ctx: ctx.ui.ask/choose/choose_many/ask_text question the user; ctx.complete(messages) makes one model "
    "request and ctx.run_agent(prompt) runs a nested agent loop (keywords name, system, tools, model, provider, "
    "max_turns; ctx.run_agent_result returns an AgentResult instead of raising; ctx.agents() lists the agents and "
    "ctx.cancel_agent(id) stops one); ctx.new_session(messages) switches to a new session; "
    "ctx.commands() lists the slash commands and ctx.run_command(line, builtin=False) runs one (builtin=True reaches "
    "a built-in the extension replaced); ctx.models() and ctx.set_model(name) list and switch models; "
    "ctx.widget(name) is the widget shown for register_widget(name, ...); ctx.notify(text, kind='info'|'warning'|"
    "'error') shows a notice; ctx.use_provider(name, model=None) switches provider; ctx.queue_user_message(text) "
    "sends text as the user's next message once the current turn ends; "
    "ctx.cancelled and ctx.check_cancelled() notice an interrupt. "
    "After writing or editing an extension, call reload_extensions and fix any reported errors."
)


def _join(*parts: str) -> str:
    """The non-empty ``parts`` separated by blank lines."""
    return "\n\n".join(p for p in parts if p)


def _builtin_sections(cwd: str, settings: config.Settings) -> list[tuple[str, Callable[[Any], str]]]:
    """The built-in sections of the system prompt (:data:`~minimal.extensions.registry.PROMPT_SECTIONS`) as ``(name, text)``.

    ``text(ctx)`` is only called if the section survives the extensions' edits.
    """

    def environment(ctx: Any) -> str:
        return f"Working directory: {cwd}\nDate: {datetime.now(timezone.utc).date().isoformat()}"

    def extending(ctx: Any) -> str:
        return SELF_EXTEND_PROMPT.format(
            facade_path=Path(minimal.extensions.facade.__file__),
            context_path=Path(minimal.core.context.__file__),
            global_dir=global_extensions_dir(),
            project_dir=project_extensions_dir(cwd),
            api_major=minimal.API_VERSION.split(".")[0],
        )

    def instructions(ctx: Any) -> str:
        files, scope = settings.prompt_files
        root = Path(cwd).resolve()
        parts = []
        for name in files:
            path = (root / Path(name).expanduser()).resolve()
            if scope in ("project", "local") and not path.is_relative_to(root):
                continue
            if path.is_file():
                parts.append(f"Project instructions ({name}):\n{path.read_text(encoding='utf-8')}")
        return _join(*parts)

    texts = {
        "base": lambda ctx: base_prompt(settings),
        "environment": environment,
        "extending": extending,
        "instructions": instructions,
        "append": lambda ctx: _join(*settings.prompt_append),
    }
    return [(name, texts[name]) for name in PROMPT_SECTIONS]


def build_system_prompt(registry: Registry, cwd: str, settings: config.Settings, ctx: Context | None = None) -> str:
    """Assemble the system prompt.

    Sections, separated by blank lines, in this order (the names extensions use to replace or remove one):
    ``base`` (:func:`base_prompt`), ``environment`` (the working directory and today's date), ``extending``
    (how to extend minimal), ``instructions`` (each existing ``prompt.files`` file, default ``AGENTS.md``,
    resolved against ``cwd``), ``append`` (each ``prompt.append`` text), then the sections extensions added
    (``append_system_prompt`` paragraphs and ``register_prompt_section``), after any replacing or removing
    they did (:func:`~minimal.extensions.registry.apply_prompt_edits`). An empty section is left out; an extension's
    text function that raises is reported on stderr and its section skipped.

    Files listed by a project or local settings file must be inside ``cwd``:
    a project cannot pull ``~/.ssh/id_rsa`` into the prompt.

    Args:
        ctx: Passed to the functions that give the text of an extension's section.

    Raises:
        ValueError: if the ``base`` section is not replaced and the ``prompt.base`` file is missing or unreadable.
    """
    builtin = _builtin_sections(cwd, settings)
    own = {id(text) for _, text in builtin}  # these raise ValueError for the user to fix; an extension's are reported and skipped
    parts = []
    for name, text in apply_prompt_edits(builtin, registry.prompt_edits):
        if not callable(text):
            parts.append(text or "")
        elif id(text) in own:
            parts.append(text(ctx))
        else:
            try:
                parts.append(text(ctx) or "")
            except Exception as e:
                print(f'[extension] prompt section "{name}": {e}', file=sys.stderr)
    return _join(*parts)



_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _visible(text: str) -> str:
    """``text`` with control characters (all but tab and newline) written as ``\\xNN``.

    Text from the model or from a tool must not reach the terminal raw: escape
    sequences can move the cursor and rewrite what is on screen, including the
    text of a confirmation prompt.
    """
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def _failure_status(error: BaseException) -> str:
    """The ``agent_end`` status of a loop that ended with ``error``."""
    if isinstance(error, TurnInterrupted):
        return "interrupted"
    return "max_turns" if isinstance(error, MaxTurnsExceeded) else "error"


def _last_text(messages: list[dict]) -> str:
    """The text of the last assistant message of ``messages`` that has any; ``""`` if none."""
    for message in reversed(messages):
        if message["role"] == "assistant":
            text = result_text(message["content"])
            if text:
                return text
    return ""


class Frontend(Protocol):
    """Shows a turn to the user. Every method is called from the thread running the turn or command."""

    def text(self, chunk: str) -> None:
        """Model text, streamed as it arrives."""

    def tool_call(self, call: dict) -> None:
        """A tool is about to run: ``{id, name, input, agent_id, parent_id}``.

        ``agent_id`` and ``parent_id`` say which agent calls it (``"main"`` and ``None`` for the user's turn, else
        a ``ctx.run_agent`` agent and the one that started it): extra keys of the same call, so a frontend that
        ignores them keeps working. :meth:`text` is the user's turn's only: nested agents do not stream text.

        When one reply calls several ``read_only`` tools in a row, each call is announced here, in order, before the
        calls run together; :meth:`tool_result` then follows in the same order as each result is ready.
        """

    def tool_result(self, call: dict, result: dict) -> None:
        """A tool finished: ``call`` as in :meth:`tool_call`; ``result`` is its ``tool_result`` block
        (``content``, ``is_error``) with the same ``agent_id`` and ``parent_id``."""

    def turn_end(self, error: str | None) -> None:
        """The turn finished; ``error`` is set if it failed."""

    def ask(self, question: str) -> bool:
        """Ask a yes/no question; return the answer."""

    def choose(self, title: str, options: list[tuple[str, str]]) -> str | None:
        """Pick one ``(value, label)`` option; return its value, or ``None`` if cancelled."""

    def choose_many(self, title: str, options: list[tuple[str, str, bool]]) -> list[str] | None:
        """Tick any ``(value, label, checked)`` options; return the ticked values in option order, or ``None`` if cancelled."""

    def ask_text(self, title: str) -> str | None:
        """Ask for one line of text; return it stripped, or ``None`` if empty or cancelled."""

    def reloaded(self) -> None:
        """Extensions were reloaded (widgets may have changed)."""

    def widget(self, name: str) -> object | None:
        """The widget the frontend created for extension widget ``name``; ``None`` if it shows none."""

    def notify(self, text: str, kind: str) -> None:
        """Show a short notice; ``kind`` is ``"info"``, ``"warning"`` or ``"error"``."""

    def queued(self) -> None:
        """A user message was queued (:meth:`App.take_queued`): send it as soon as no turn or command is running."""

    def mode_changed(self) -> None:
        """The current mode changed, from any thread: redraw whatever shows it."""

    def invalidate(self) -> None:
        """Something an extension shows changed (``ctx.invalidate()``), from any thread: redraw its panels, status
        segments and widgets soon. Cheap and coalesced; a frontend that draws nothing of the kind ignores it. API 2.3."""

    def theme_name(self) -> str | None:
        """The name of the colour theme the frontend shows (``ctx.theme``); ``None`` if it has none. API 2.3."""

    def progress(self, text: str | None) -> None:
        """Transient progress text of the tool or command running (:meth:`~minimal.core.context.Context.progress`):
        show it next to the working indicator; ``None`` clears it. May be called from any thread, often."""

    def permission(self, question: str, rule: str | None, saved_in: str | None) -> str:
        """Ask whether a tool call may run; return ``"once"``, ``"session"``, ``"always"`` or ``"deny"``.

        ``rule`` is the allow rule ``"session"`` and ``"always"`` would add (``None``: none can safely be offered, so
        answer only ``"once"`` or ``"deny"``); ``saved_in`` is the file ``"always"`` would write it to (``None``:
        ``"always"`` is not offered).
        """

    def draft(self) -> str:
        """The text in the prompt box, not yet sent (``ctx.ui.get_draft()``); ``""`` if the frontend has none. May be
        called from any thread. API 2.4."""

    def set_draft(self, text: str, append: bool) -> None:
        """Put ``text`` in the prompt box, replacing what is there, or after it with ``append``; the cursor goes to the
        end. A frontend without a prompt box ignores it. May be called from any thread. API 2.4."""

    def show(self, text: str, kind: str) -> None:
        """Add ``text`` to the conversation as a block of ``kind`` (``"output"`` or ``"error"``); a frontend that has
        suspended itself for a terminal command shows it once it is back. May be called from any thread. API 2.4."""

    def focus_widget(self, name: str, hold: bool) -> bool:
        """Give the keyboard focus to the widget shown for extension widget ``name`` (or its first focusable part);
        with ``hold`` the frontend does not move the focus back to the prompt box when a turn or command ends, until
        the widget goes away or the focus leaves it. Returns whether the widget took it. API 2.4."""

    def focus_composer(self) -> None:
        """Give the keyboard focus to the prompt box and drop any hold of :meth:`focus_widget`. API 2.4."""


class ExitRepl(Exception):
    """Raised by a command (``/exit``) to quit the interactive UI."""


class ConsoleFrontend:
    """Plain stdout/stderr and ``input()``: used by commands the TUI runs in the terminal, and by an :class:`App` with no frontend.

    Model and tool text is shown with control characters escaped (:func:`_visible`),
    except the answer itself when stdout is not a terminal, so piped output stays exact.
    """

    _progress: str | None = None

    def text(self, chunk: str) -> None:
        print(_visible(chunk) if sys.stdout.isatty() else chunk, end="", flush=True)

    def tool_call(self, call: dict) -> None:
        """Show a dimmed one-line summary of a tool call on stderr."""
        args = _visible(json.dumps(call["input"], ensure_ascii=False)[:120])
        print(f"\n\x1b[2m› {_visible(call['name'])} {args}\x1b[0m\n", end="", file=sys.stderr, flush=True)

    def tool_result(self, call: dict, result: dict) -> None:
        """Show the first line of a failed tool call on stderr."""
        if result["is_error"]:
            first = _visible(result["content"].strip().partition("\n")[0][:200])
            print(f"\x1b[31m✘ {_visible(call['name'])}: {first}\x1b[0m", file=sys.stderr, flush=True)

    def turn_end(self, error: str | None) -> None:
        if error is None:
            print()
        else:
            print(f"\nError: {_visible(error)}", file=sys.stderr)

    def ask(self, question: str) -> bool:
        """Accepts ``y``/``yes``; EOF means no, and so does having no terminal to ask in."""
        question = _visible(question)
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print(
                f"[confirm] denied, no terminal to ask (-y allows tool calls): {question}",
                file=sys.stderr,
            )
            return False
        try:
            answer = input(f"\n{question} [y/N] ")
        except EOFError:
            return False
        return re.fullmatch(r"y|yes", answer.strip(), re.IGNORECASE) is not None

    def choose(self, title: str, options: list[tuple[str, str]]) -> str | None:
        """Numbered list; empty, invalid input or EOF cancels."""
        print(f"\n{title}")
        for i, (_, label) in enumerate(options, 1):
            print(f"  {i:>2}. {label}")
        try:
            answer = input("Number (empty cancels): ").strip()
        except EOFError:
            return None
        if not answer.isdigit() or not 1 <= int(answer) <= len(options):
            return None
        return options[int(answer) - 1][0]

    def choose_many(self, title: str, options: list[tuple[str, str, bool]]) -> list[str] | None:
        """Numbered checklist: Enter keeps the shown ticks, ``0`` ticks none; invalid input or EOF cancels."""
        print(f"\n{title}")
        for i, (_, label, checked) in enumerate(options, 1):
            print(f"  {i:>2}. [{'x' if checked else ' '}] {label}")
        try:
            answer = input("Numbers to tick (Enter keeps the ticks shown, 0 for none): ").strip()
        except EOFError:
            return None
        if not answer:
            return [value for value, _, checked in options if checked]
        if answer == "0":
            return []
        tokens = re.split(r"[\s,]+", answer)
        if not all(t.isdigit() and 1 <= int(t) <= len(options) for t in tokens):
            return None
        picked = {int(t) - 1 for t in tokens}
        return [value for i, (value, _, _) in enumerate(options) if i in picked]

    def ask_text(self, title: str) -> str | None:
        """One ``input()`` line; EOF cancels."""
        try:
            answer = input(f"{title}: ").strip()
        except EOFError:
            return None
        return answer or None

    def reloaded(self) -> None:
        pass

    def widget(self, name: str) -> object | None:
        return None

    def notify(self, text: str, kind: str) -> None:
        """On stderr: ``info`` as is, ``warning``/``error`` coloured and prefixed with the kind."""
        text = _visible(text)
        if kind == "warning":
            text = f"\x1b[33mwarning: {text}\x1b[0m"
        elif kind == "error":
            text = f"\x1b[31merror: {text}\x1b[0m"
        print(text, file=sys.stderr, flush=True)

    def queued(self) -> None:
        """Nothing to do: a console has no loop that sends the next message."""

    def mode_changed(self) -> None:
        """Nothing to redraw in a console."""

    def invalidate(self) -> None:
        """Nothing to redraw in a console (it draws no panels or status line)."""

    def theme_name(self) -> str | None:
        """A console has no theme of its own."""
        return None

    def draft(self) -> str:
        """A console has no prompt box to read."""
        return ""

    def set_draft(self, text: str, append: bool) -> None:
        """A console has no prompt box to fill."""

    def show(self, text: str, kind: str) -> None:
        """On stdout, or on stderr for ``error``."""
        print(_visible(text), file=sys.stderr if kind == "error" else sys.stdout, flush=True)

    def focus_widget(self, name: str, hold: bool) -> bool:
        """A console shows no widgets."""
        return False

    def focus_composer(self) -> None:
        """A console has no focus to move."""

    def progress(self, text: str | None) -> None:
        """A dimmed line on stderr (only when it is a terminal) each time the text changes; ``None`` only forgets it."""
        if text is not None and text != self._progress and sys.stderr.isatty():
            print(f"\x1b[2m… {_visible(text)}\x1b[0m", file=sys.stderr, flush=True)
        self._progress = text

    def permission(self, question: str, rule: str | None, saved_in: str | None) -> str:
        """Same as :meth:`ask`: ``"once"`` for yes, ``"deny"`` otherwise; a console offers no wider answer."""
        return "once" if self.ask(question) else "deny"


class App:
    """Everything a running ``minimal`` process needs.

    Call :meth:`use_provider` before the first turn.

    Attributes:
        provider: Current model integration (``/provider`` changes it).
        provider_name: Registry name of ``provider``.
        model: Current model name (``/model`` changes it).
        registry: Tools, commands, providers, event handlers and widgets.
        session: Current session (``/new`` and ``/resume`` replace it).
        cwd: Working directory for tools and the system prompt.
        auto_approve: Run ``write``/``edit``/``bash`` without asking.
        trust_project: Apply the project's extensions and trust-gated settings without ``/trust`` (``--trust-project``).
        extra_extensions: ``--ext`` paths, loaded again on every reload.
        frontend: Where turns are shown and confirmations asked.
        register_commands: Registers the built-in slash commands into the registry
            (:attr:`~minimal.extensions.registry.Registry.builtin_commands`), now and on every reload.
        queued: User messages queued by extensions (``ctx.queue_user_message``), sent when the UI is idle.
        extensions: Extensions loaded by the last reload.
        extension_errors: Failures of the last reload, setting errors included.
        settings: The settings read by the last reload, with project trust applied.
        setting_errors: Problems found in the settings files by the last reload.
        untrusted: What the last reload did not apply because the user has not
            trusted the project (``/trust``); ``None`` if nothing.
        cancelled: Set to interrupt the turn in progress, and with it every agent running under it.
        agents: The session's agent records (``ctx.agents()``, per-agent cancel flags).
        usage: The tokens and requests this process has spent (``ctx.usage``).
        truncations: How many times a session's history was cut (:meth:`truncate_session`); a frontend that
            shows the history redraws it when this or the session's id changes.
        mode: The current mode's name, or ``None`` for no mode (:meth:`set_mode`).
    """

    def __init__(
        self,
        *,
        registry: Registry,
        session: Session,
        cwd: str,
        auto_approve: bool,
        trust_project: bool = False,
        extra_extensions: Iterable[str] = (),
        frontend: Frontend | None = None,
        register_commands: Callable[[Registry], None] | None = None,
    ) -> None:
        self.registry = registry
        self.session = session
        self.cwd = cwd
        self.auto_approve = auto_approve
        self.trust_project = trust_project
        self.extra_extensions = list(extra_extensions)
        self.frontend: Frontend = frontend or ConsoleFrontend()
        self.provider: Provider | None = None
        self.provider_name = ""
        self.model = ""
        self.extensions: list[Path] = []
        self.extension_errors: list[str] = []
        self.untrusted: ProjectSnapshot | None = None
        self.cancelled = CancelEvent()
        self.agents = Agents(self.cancelled)
        self.mode: str | None = None
        self._mode_lock = threading.Lock()
        self.settings = config.Settings()
        self._effort_override: tuple[str | None] | None = None
        self.setting_errors: list[str] = []
        self.queued: collections.deque[str] = collections.deque()
        self.context = Context(self)
        self.permissions = Permissions(self.settings)
        self.usage = UsageMeter()
        self.truncations = 0
        self._register_commands = register_commands
        self._git: GitPoller | None = None
        self._git_last: GitInfo | None = None
        self._git_lock = threading.Lock()
        self._frontend_commands: dict[str, tuple[CommandRun, str, bool, CommandComplete | None]] = {}
        self._register_builtins()  # usable before the first reload

    def _register_builtins(self) -> None:
        """Register the built-in commands and the terminal UI's commands."""
        if self._register_commands is not None:
            self._register_commands(self.registry)
        for name, (run, description, terminal, complete) in self._frontend_commands.items():
            self.registry.register_builtin_command(name, run, description, terminal, complete)

    def add_frontend_command(
        self,
        name: str,
        run: CommandRun,
        description: str = "",
        terminal: bool = False,
        complete: CommandComplete | None = None,
    ) -> None:
        """Add a built-in command of the terminal UI (``/theme``); kept across reloads."""
        self._frontend_commands[name] = (run, description, terminal, complete)
        self.registry.register_builtin_command(name, run, description, terminal, complete)

    def take_queued(self) -> str | None:
        """The oldest message queued with ``ctx.queue_user_message``, removed from the queue; ``None`` if none."""
        return self.queued.popleft() if self.queued else None

    def is_trusted(self, project: ProjectSnapshot) -> bool:
        """Whether ``project`` may apply: ``--trust-project``, or the user trusted exactly this snapshot."""
        return self.trust_project or config.load_trusted().get(project.key) == project.digest

    def reload_extensions(self) -> tuple[list[Path], list[str]]:
        """Rebuild the registry: built-ins (tools, providers, commands), then every extension: the bundled ones, then those on disk.

        The registry is cleared in place, so a turn in progress sees the new
        tools on its next model request. The current provider instance keeps
        working even if its spec is gone.

        The project's extensions (``<cwd>/.minimal/extensions``) and its trust-gated
        settings (:data:`~minimal.config.RISKY`) apply only if the
        user trusted exactly that snapshot (:meth:`is_trusted`). Otherwise they are
        skipped without asking and recorded in :attr:`untrusted`; that is not an
        error. The files are read once: that snapshot is what is hashed and run.

        The current mode stays if the reload registered it again; if not, there is no mode
        afterwards (``mode_change``, and a warning to the user).

        Returns:
            ``(loaded, errors)`` as from :func:`~minimal.extensions.loader.load_extensions`,
            followed by the settings files' errors.
        """
        layers, setting_errors = config.read_layers(self.cwd)
        risky = config.risky_subset(layers)
        read_error = None
        try:
            snapshot = read_project_snapshot(self.cwd, risky)
            trusted = snapshot is None or self.is_trusted(snapshot)
        except OSError as e:
            snapshot, trusted = None, False
            read_error = f"{project_extensions_dir(self.cwd)}: cannot read the project extensions ({e}), skipped them"
        project_trusted = self.trust_project or (snapshot is not None and trusted)
        self.settings = config.Settings(tuple(layers), trusted, project_trusted)
        self.permissions = Permissions(self.settings, self.permissions.session)
        self.setting_errors = setting_errors
        self.stop_git()
        self.registry.dispose()
        self.registry.reset()
        register_builtin_tools(self.registry)
        register_builtin_providers(self.registry)
        self._register_builtins()
        project = snapshot.sources if snapshot is not None and trusted else ()
        loaded, errors = load_extensions(self.registry, self.cwd, self.settings, self.extra_extensions, project=project)
        if read_error:
            errors.append(read_error)
        errors.extend(setting_errors)
        self.extensions, self.extension_errors = loaded, errors
        self.untrusted = snapshot if snapshot is not None and not trusted else None
        self.frontend.reloaded()
        self._validate_mode()
        return loaded, errors

    def untrusted_notice(self) -> str | None:
        """What the last reload skipped as untrusted, pointing to ``/trust``; ``None`` if nothing."""
        if self.untrusted is None:
            return None
        return (
            f"{self.untrusted.count} project extension(s) and/or settings in {self.untrusted.root} are not "
            "trusted, so they were not applied; run /trust to review them"
        )

    def create_provider(self, name: str, model: str | None = None) -> tuple[Provider, str]:
        """A new instance of provider ``name`` and the model to use with it (``model``, else its default), without switching to it.

        Raises:
            ValueError: if the provider is unknown, its optional SDK is not installed or its credentials are missing.
        """
        spec = self.registry.providers.get(name)
        if spec is None:
            raise ValueError(
                f"unknown provider: {name} (available: {', '.join(self.registry.providers)}; "
                "an extension can add one: /ext, or `minimal ext install <name>`)"
            )
        problem = spec.missing_sdk() or spec.missing_credentials()
        if problem:
            raise ValueError(problem)
        return spec.create(), model or spec.default_model

    def use_provider(self, name: str, model: str | None = None) -> None:
        """Switch to provider ``name`` and to ``model`` (default: the provider's default).

        The conversation is kept: history is stored in the provider-neutral format.

        Raises:
            ValueError: if the provider is unknown, its optional SDK is not installed or its credentials are missing.
        """
        self.provider, self.model = self.create_provider(name, model)
        self.provider_name = name

    def begin(self) -> None:
        """Fire ``session_start``: once per process, when the UI is up (a reload does not fire it again)."""
        self.registry.emit("session_start", {"ctx": self.context, "resumed": bool(self.messages)})

    def switch_session(self, session: Session) -> None:
        """Save the current session, continue in ``session`` and fire ``session_switch``.

        ``session`` is locked first (:meth:`Session.lock`) and the current one released.

        Raises:
            SessionLocked: if another minimal holds ``session``; the current session goes on.
        """
        old = self.session
        if session.file == old.file:
            session.take_lock(old)
        else:
            session.lock()
        try:
            old.save()
        except BaseException:
            if session.file == old.file:
                old.take_lock(session)
            else:
                session.unlock()
            raise
        old.unlock()
        self.session = session
        self.registry.emit("session_switch", {"ctx": self.context, "old_id": old.id, "new_id": session.id})

    def shutdown(self) -> None:
        """Fire ``shutdown`` (minimal is exiting), run the extensions' ``on_dispose`` functions and release the session."""
        try:
            self.registry.emit("shutdown", {"ctx": self.context})
            self.stop_git()
            self.registry.dispose()
        finally:
            self.session.unlock()

    def available_models(self) -> tuple[list[str], str | None]:
        """The models the current provider offers, queried from its API, and why the query failed (``None`` if it did not).

        Falls back to the provider's default model; the current model is always in the list.
        """
        lister = getattr(self.provider, "list_models", None)
        spec = self.registry.providers.get(self.provider_name)
        found: list[str] = []
        problem = None
        if lister is not None:
            try:
                found = list(lister())
            except Exception as e:
                problem = f"{type(e).__name__}: {e}"
        if not found and spec is not None:
            found = [spec.default_model]
        if self.model not in found:
            found.insert(0, self.model)
        return found, problem

    def commands(self) -> dict[str, Command]:
        """Every slash command by name: the built-ins, then extension commands (which replace a built-in of the same name)."""
        return {**self.registry.builtin_commands, **self.registry.commands}

    def find_command(self, name: str, *, builtin: bool = False) -> Command | None:
        """The command called ``name``: an extension's if registered (unless ``builtin``), else the built-in."""
        if builtin:
            return self.registry.builtin_commands.get(name)
        return self.registry.commands.get(name) or self.registry.builtin_commands.get(name)

    def run_command(self, line: str, *, builtin: bool = False) -> None:
        """Parse and run a ``/name args`` line; ``builtin`` runs the built-in even if an extension replaced it.

        Every command gets the :class:`~minimal.core.context.Context`.
        The interrupt flag is cleared first: it may be left over from an interrupted turn. Unknown
        commands and errors raised by a command are reported, not raised. What a ``terminal`` command returns, if it
        is a non-blank str, is shown with :meth:`Frontend.show`: its prints are lost when the interface comes back.

        Raises:
            ExitRepl: when the command asks to quit.
        """
        name, _, args = line.removeprefix("/").partition(" ")
        cmd = self.find_command(name, builtin=builtin)
        if cmd is None:
            print(f"Unknown command: /{name}")
            return
        self.cancelled.clear()
        try:
            result = cmd.run(args.strip(), self.context)
            if cmd.terminal and isinstance(result, str) and result.strip():
                self.frontend.show(result, "output")
        except ExitRepl:
            raise
        except Exception as e:
            print(f"Error in /{name}: {e}", file=sys.stderr)

    @property
    def messages(self) -> list[dict]:
        """Messages of the current session."""
        return self.session.messages

    @property
    def context_tokens(self) -> int | None:
        """Size of the context after the last reply that reported usage (its input plus its output), else ``None``."""
        for message in reversed(self.messages):
            usage = message.get("usage")
            if usage:
                return usage["input_tokens"] + usage["output_tokens"]
        return None

    @property
    def context_window(self) -> int | None:
        """The model's context size in tokens: the ``context_window`` setting, else what the provider's spec or minimal's
        table says for the current model; ``None`` if unknown."""
        configured = self.settings.context_window
        if configured is not None:
            return configured
        spec = self.registry.providers.get(self.provider_name)
        return windows.context_window(self.provider_name, self.model, spec.context_windows if spec else None)

    def theme_info(self) -> ThemeInfo:
        """The current colour theme with every colour resolved: the frontend's, else the ``theme`` setting, else the built-in one."""
        name = self.frontend.theme_name() or self.settings.theme or palette.DEFAULT
        spec = self.registry.themes.get(name) if name != palette.DEFAULT else None
        return palette.theme_info(spec or palette.BUILTIN)

    def theme_changed(self) -> None:
        """Fire ``theme_change`` for the theme the frontend shows now."""
        info = self.theme_info()
        self.registry.emit("theme_change", {"ctx": self.context, "name": info.name, "dark": info.dark})

    def invalidate(self) -> None:
        """Ask the frontend to redraw what extensions show (``ctx.invalidate()``)."""
        self.frontend.invalidate()

    def git_info(self) -> GitInfo | None:
        """The last git state the shared poller read (``ctx.git()``), starting the poller on the first call.


        ``None`` until the first poll ends and outside a repository. The poller runs every ``git.refresh`` seconds,
        asks the frontend to redraw (:meth:`invalidate`) when the state changes, and stops on :meth:`shutdown` and at
        every reload (the next call starts it again, with the settings of that reload).
        """
        with self._git_lock:
            if self._git is None:
                self._git = GitPoller(self.cwd, self.settings.git_refresh, self._git_changed)
                self._git.start()
        return self._git_last

    def _git_changed(self, info: GitInfo | None) -> None:
        self._git_last = info
        self.invalidate()

    def poke_git(self) -> None:
        """Poll git again now, if the poller runs (a turn or command probably changed files)."""
        with self._git_lock:
            if self._git is not None:
                self._git.poke()

    def stop_git(self) -> None:
        """Stop the git poller; :meth:`git_info` starts a new one."""
        with self._git_lock:
            poller, self._git = self._git, None
        if poller is not None:
            poller.stop()

    def confirm(self, call: dict) -> bool:
        """Ask the user, through the frontend, whether a tool call may run.

        Besides allowing it once, the user may allow what a rule (:func:`~minimal.core.permissions.suggest_rule`)
        covers for the rest of the session (:meth:`Permissions.allow_session`), or save that rule in the project's
        ``settings.local.json`` (:meth:`save_allow_rule`) and tell the transcript which one.
        """
        if self.auto_approve:
            return True
        name, inp = call["name"], call["input"]
        detail = inp.get("command") if name == "bash" else (inp.get("path") or json.dumps(inp))
        tool = self.registry.tools.get(name)
        rule = suggest_rule(tool, inp, self.cwd) if tool is not None else None
        file = config.project_settings_files(self.cwd)["local"] if rule is not None and self._rule_will_apply() else None
        answer = self.frontend.permission(f"Allow {name}: {detail}?", rule, str(file) if file is not None else None)
        if answer not in ("once", "session", "always"):
            return False
        if rule is None or answer == "once":
            return True
        self.permissions.allow_session(rule)
        shown = _visible(rule)
        if answer == "always" and file is not None:
            try:
                self.save_allow_rule(rule)
            except (OSError, ValueError) as e:
                print(f"Could not save the rule {shown} ({e}): it is allowed for this session only.", file=sys.stderr)
            else:
                print(f"Saved the rule {shown} in permissions.allow of {file}. Deny rules still win.")
        else:
            print(f"Allowing {shown} for this session (until minimal exits). Deny rules still win.")
        return True

    def _project_trusted_by_digest(self) -> bool | None:
        """Whether the project's extensions and trust-gated settings are the ones the user trusted (or there are none);
        ``None`` if they cannot be read."""
        try:
            snapshot = read_project_snapshot(self.cwd, config.risky_subset(config.read_layers(self.cwd)[0]))
        except OSError:
            return None
        return snapshot is None or config.load_trusted().get(snapshot.key) == snapshot.digest

    def _rule_will_apply(self) -> bool:
        """Whether an allow rule written to the project's ``settings.local.json`` applies from the next start:
        the project is trusted (or asks for nothing), so :meth:`save_allow_rule` can keep it trusted."""
        return self._project_trusted_by_digest() is True

    def save_allow_rule(self, rule: str) -> Path:
        """Add ``rule`` to ``permissions.allow`` of the project's ``settings.local.json`` and return that file.

        The project's trust-gated settings count as trusted only for the digest the user accepted, so a project that
        was trusted (or had nothing to trust) stays trusted with the new rule: the user just chose it.

        Raises:
            ValueError: if the project is not trusted (the rule would not apply), or the file cannot be edited
                (:func:`~minimal.config.add_allow_rule`).
            OSError: if the file cannot be written.
        """
        if not self._rule_will_apply():
            raise ValueError("the project is not trusted: run /trust first, or the rule would not apply")
        path = config.add_allow_rule(self.cwd, rule)
        snapshot = read_project_snapshot(self.cwd, config.risky_subset(config.read_layers(self.cwd)[0]))
        if snapshot is not None:
            config.save_trusted(snapshot.key, snapshot.digest)
        return path

    def _require_idle(self, action: str) -> None:
        """Raise ``ValueError`` if a turn or agent is running: ``action`` would change the history under it."""
        if any(a.status == "running" for a in self.agents.snapshot()):
            raise ValueError(f"cannot {action} while a turn is running")

    def fork_session(self, at_index: int | None = None) -> str:
        """Continue in a new session holding the first ``at_index`` messages of this one (all by default).

        What :meth:`~minimal.core.context.Context.fork` does: the old session stays saved, the new one names it as
        its ``parent`` and ``session_switch`` fires. Returns the new session's id.

        Raises:
            ValueError: if a turn is running, ``at_index`` is out of range or the cut would split a tool call from its result.
        """
        self._require_idle("fork the session")
        old = self.session
        end = len(old.messages) if at_index is None else at_index
        check_cut(old.messages, end)
        session = open_session(new_id(), self.cwd)
        session.parent = old.id
        session.messages.extend(copy.deepcopy(old.messages[:end]))
        self.switch_session(session)
        session.save()
        return session.id

    def truncate_session(self, at_index: int) -> int:
        """Drop the messages from ``at_index`` on from this session and its file; return how many.

        Raises:
            ValueError: if a turn is running, ``at_index`` is out of range or the cut would split a tool call from its result.
        """
        self._require_idle("rewind the session")
        dropped = self.session.truncate(at_index)
        if dropped:
            self.truncations += 1
        return dropped

    def permit(self, tool: Tool, input: dict) -> bool:
        """Whether the permission rules let ``tool`` run with ``input`` without asking.

        Raises:
            PermissionDenied: if a deny rule matches (even with ``-y``).
        """
        return self.permissions.check(tool, input, self.cwd)

    def base_system_prompt(self) -> str:
        """The system prompt without the current mode's note (:func:`build_system_prompt`)."""
        return build_system_prompt(self.registry, self.cwd, self.settings, self.context)

    def system_prompt(self) -> str:
        """The system prompt of the next turn: :meth:`base_system_prompt`, then the current mode's note."""
        return _join(self.base_system_prompt(), self.mode_note())

    def request_options(self, provider_name: str | None = None, model: str | None = None) -> dict:
        """Provider request options of the next request: ``max_tokens`` from the settings
        (:meth:`~minimal.config.Settings.max_tokens_for`) for the current provider and model, or for the given
        ones, and :attr:`reasoning_effort` when set; empty for the provider's defaults."""
        options: dict[str, Any] = {}
        n = self.settings.max_tokens_for(provider_name or self.provider_name, model or self.model)
        if n is not None:
            options["max_tokens"] = n
        if self.reasoning_effort is not None:
            options["reasoning_effort"] = self.reasoning_effort
        return options

    @property
    def reasoning_effort(self) -> str | None:
        """How much the model should think (one of :data:`~minimal.config.REASONING_EFFORTS`): what
        :meth:`set_reasoning_effort` set, else the ``reasoning_effort`` setting; ``None`` for the provider's default."""
        return self.settings.reasoning_effort if self._effort_override is None else self._effort_override[0]

    def set_reasoning_effort(self, value: str | None) -> None:
        """Use ``value`` from the next request on, over the setting (``None``: the provider's default).

        Raises:
            ValueError: if ``value`` is not ``None`` or one of :data:`~minimal.config.REASONING_EFFORTS`.
        """
        if value is not None and value not in config.REASONING_EFFORTS:
            raise ValueError(f"reasoning effort must be one of {', '.join(config.REASONING_EFFORTS)} (or None), not {value!r}")
        self._effort_override = (value,)

    # --- modes ---------------------------------------------------------------

    def mode_note(self) -> str:
        """The current mode's ``system_note``; ``""`` if there is none or no mode."""
        spec = self.registry.modes.get(self.mode) if self.mode is not None else None
        return spec.system_note if spec is not None else ""

    def mode_block_reason(self, name: str) -> str | None:
        """Why the current mode does not let the model use tool ``name`` (the error it gets); ``None`` if it may.

        It is the :data:`~minimal.core.agent.ToolGate` of every agent loop, asked on each request and each call.
        A mode that is no longer registered (a reload in progress) blocks everything rather than nothing.
        """
        mode = self.mode
        if mode is None:
            return None
        spec = self.registry.modes.get(mode)
        if spec is None:
            return f"{name} is blocked: the {mode} mode is not registered (extensions are reloading); try again"
        why = spec.block_reason(name, self.registry.tools.get(name))
        if why is None:
            return None
        return (
            f"{name} is blocked: {why}. Do not retry it; only the user can change the mode "
            f"(/mode <name>, available: {', '.join(self.registry.modes)}; /mode off for none)."
        )

    def set_mode(self, name: str | None) -> None:
        """Make ``name`` the current mode (``None``: no mode) and fire ``mode_change`` if it changed.

        Raises:
            ValueError: if no mode is called ``name``.
        """
        if name is not None and name not in self.registry.modes:
            available = ", ".join(self.registry.modes) or "none registered"
            raise ValueError(f"no mode {name!r} (modes: {available})")
        self._swap_mode(lambda old: name)

    def cycle_mode(self) -> str | None:
        """Go to the next mode in registration order: ``None``, the first mode, ..., the last, ``None``...;
        return the new one (``None`` and no change if no mode is registered)."""

        def advance(old: str | None) -> str | None:
            order = [None, *self.registry.modes]
            return order[(order.index(old) + 1) % len(order)] if old in order else None

        return self._swap_mode(advance)

    def _swap_mode(self, choose: Callable[[str | None], str | None]) -> str | None:
        """Set the mode to ``choose(current)`` atomically; fire ``mode_change`` after the lock is released if it changed."""
        with self._mode_lock:
            old = self.mode
            new = self.mode = choose(old)
        if new != old:
            self.registry.emit("mode_change", {"ctx": self.context, "old": old, "new": new})
            self.frontend.mode_changed()
        return new

    def _validate_mode(self) -> None:
        """After a reload: drop a mode that is no longer registered, telling the user."""
        mode = self.mode
        if mode is not None and mode not in self.registry.modes:
            self._swap_mode(lambda old: None)
            self.frontend.notify(f"Mode {mode!r} is no longer registered: no mode now.", "warning")

    def fallback_provider(self, name: str, model: str | None) -> tuple[Provider, str]:
        """Switch to provider ``name`` (:meth:`use_provider`) after a failed request; return the new provider and model.

        Raises:
            ValueError: if the provider is unknown or its credentials are missing.
        """
        self.use_provider(name, model)
        assert self.provider is not None  # use_provider just set it
        return self.provider, self.model

    def run_turn(self, text: str) -> None:
        """Send ``text`` to the model, stream the answer to the frontend and save the session.

        ``turn_start`` handlers may rewrite ``text`` first. The turn is the agent ``"main"``: ``agent_start``
        fires before its first request and ``agent_end`` after it ended, however it ended. If the turn fails or is
        interrupted, the frontend gets the error. If the model had already replied,
        the turn stays in the history, closed with an assistant note saying why it
        ended (every ``tool_use`` has its result), so the model still knows what it
        did. If it had not, the turn is discarded and the same prompt can be sent again.
        """
        self.cancelled.clear()
        for result in self.registry.emit("turn_start", {"ctx": self.context, "text": text}):
            if isinstance(result, dict) and isinstance(result.get("text"), str) and result["text"]:
                text = result["text"]
        before = len(self.messages)
        self.messages.append({"role": "user", "content": text})
        error = None
        status, answer = "interrupted", ""
        assert self.provider is not None  # startup (cli) picks a provider before any turn
        run = self.agents.begin_main(text, self.model, self.provider_name)
        try:
            self._emit_agent_start(self.context, run)
            reply = run_agent(
                provider=self.provider,
                model=self.model,
                system=self.base_system_prompt(),
                dynamic_system=self.mode_note,
                messages=self.messages,
                registry=self.registry,
                ctx=self.context,
                on_text=self.frontend.text,
                on_tool=self._on_tool(run),
                on_tool_result=self.frontend.tool_result,
                on_reply=self._on_reply(run),
                confirm=self.confirm,
                cancelled=self.cancelled,
                permit=self.permit,
                gate=self.mode_block_reason,
                max_turns=self.settings.max_turns,
                max_parallel=self.settings.max_parallel,
                options=self.request_options(),
                switch_provider=self.fallback_provider,
            )
            status, answer = "done", result_text(reply.content) or ""
        except KeyboardInterrupt:
            self._close_failed_turn(before, "interrupted by the user")
            raise
        except Exception as e:
            error = str(e)
            status = _failure_status(e)
            self._close_failed_turn(before, error)
        finally:
            try:
                self.session.save()
            finally:
                self._emit_agent_end(self.context, run, status, error if status != "interrupted" else None, answer)
        self.frontend.turn_end(error)

    def run_agent(
        self,
        parent_id: str,
        prompt: str,
        *,
        system_note: str = "",
        exclude_tools: Iterable[str] = (),
        name: str | None = None,
        system: str | None = None,
        tools: Iterable[str] | None = None,
        model: str | None = None,
        provider: str | None = None,
        max_turns: int | None = None,
    ) -> tuple[AgentResult, BaseException | None]:
        """Run ``prompt`` as a nested agent under agent ``parent_id``: what ``ctx.run_agent`` and ``ctx.run_agent_result`` do.

        Its tools are the registered ones minus ``exclude_tools`` (or only ``tools``), minus what the current mode
        blocks (asked live, so a mode can never be bypassed by ``tools``). With ``provider`` or ``model`` it talks to
        that provider/model for this agent only: the app's own do not change.

        Returns:
            ``(result, error)``: the :class:`~minimal.core.agents.AgentResult`, and the exception that ended the
            loop (``None`` if it finished).

        Raises:
            ValueError: for an invalid argument, or an unknown provider or one without credentials.
        """
        exclude = frozenset(exclude_tools)
        allow = None
        if tools is not None:
            if isinstance(tools, str):
                raise ValueError("run_agent: tools must be a collection of tool names, not a string")
            if exclude:
                raise ValueError("run_agent: tools and exclude_tools cannot be used together")
            allow = frozenset(tools)
            unknown = sorted(allow - self.registry.tools.keys())
            if unknown:
                raise ValueError(
                    f"run_agent: unknown tool(s) in tools: {', '.join(unknown)} "
                    f"(registered: {', '.join(self.registry.tools)})"
                )
        if max_turns is not None and (isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1):
            raise ValueError("run_agent: max_turns must be an integer >= 1")
        assert self.provider is not None  # startup (cli) picks a provider before any extension runs
        if provider is None and model is None:
            llm, llm_name, llm_model, switch = self.provider, self.provider_name, self.model, self.fallback_provider
        elif provider is None:
            llm, llm_name, llm_model, switch = self.provider, self.provider_name, model or "", self.create_provider
        else:
            llm, llm_model = self.create_provider(provider, model)
            llm_name, switch = provider, self.create_provider
        system_text = _join(self.base_system_prompt() if system is None else system, system_note)

        def gate(tool: str) -> str | None:
            if allow is not None and tool not in allow:
                return f"{tool} is not available to this agent (its tools: {', '.join(sorted(allow)) or 'none'})"
            return self.mode_block_reason(tool)

        run = self.agents.begin(
            name=name or "agent", parent_id=parent_id, prompt=prompt, model=llm_model, provider=llm_name
        )
        ctx = Context(self, run)
        messages: list[dict] = [{"role": "user", "content": prompt}]
        status, error, reply = "interrupted", None, None
        failure: BaseException | None = None
        try:
            self._emit_agent_start(ctx, run)
            reply = run_agent(
                provider=llm,
                model=llm_model,
                system=system_text,
                dynamic_system=self.mode_note,
                messages=messages,
                registry=self.registry,
                ctx=ctx,
                on_tool=self._on_tool(run),
                on_tool_result=self.frontend.tool_result,
                on_reply=self._on_reply(run),
                confirm=self.confirm,
                cancelled=run.cancel,
                permit=self.permit,
                exclude_tools=exclude,
                gate=gate,
                nested=True,
                max_turns=self.settings.max_turns if max_turns is None else max_turns,
                max_parallel=self.settings.max_parallel,
                options=self.request_options(llm_name, llm_model),
                switch_provider=switch,
                agent_id=run.id,
                parent_id=run.parent_id,
            )
            status = "done"
        except TurnInterrupted as e:
            failure = e
        except Exception as e:
            failure = e
            status, error = _failure_status(e), str(e) or type(e).__name__
        finally:
            text = result_text(reply.content) if reply is not None else _last_text(messages)
            usage = self._emit_agent_end(ctx, run, status, error, text)
        result = AgentResult(run.id, text, usage, status, error, messages)
        return result, failure

    def _on_tool(self, run: AgentRun) -> Callable[[dict], Any]:
        """The loop's ``on_tool`` for ``run``: count the call, then show it."""

        def on_tool(call: dict) -> None:
            self.agents.tool_called(run)
            self.frontend.tool_call(call)

        return on_tool

    def _on_reply(self, run: AgentRun) -> Callable[[Any], None]:
        """The loop's ``on_reply`` for ``run``: add the reply's usage to the agent's and to the process totals."""

        def on_reply(reply: Any) -> None:
            self.agents.replied(run, reply.usage)
            self.usage.add(reply.usage)

        return on_reply

    def _emit_agent_start(self, ctx: Context, run: AgentRun) -> None:
        self.registry.emit(
            "agent_start",
            {"ctx": ctx, "agent_id": run.id, "parent_id": run.parent_id, "name": run.name, "prompt": run.prompt},
        )

    def _emit_agent_end(self, ctx: Context, run: AgentRun, status: str, error: str | None, text: str) -> Usage:
        """Record how ``run`` ended, fire ``agent_end`` and return the agent's usage."""
        usage = self.agents.finish(run, status, error)
        self.registry.emit(
            "agent_end",
            {
                "ctx": ctx,
                "agent_id": run.id,
                "parent_id": run.parent_id,
                "name": run.name,
                "status": status,
                "text": text,
                "usage": usage,
                "error": error,
            },
        )
        return usage

    def _close_failed_turn(self, before: int, reason: str) -> None:
        """Leave the history valid after a turn that began at ``messages[before]`` failed (see :meth:`run_turn`)."""
        if not any(m["role"] == "assistant" for m in self.messages[before:]):
            del self.messages[before:]
        elif self.messages[-1]["role"] != "assistant":
            first_line = next(iter(reason.strip().splitlines()), "unknown error")
            note = f"[turn interrupted: {first_line[:300]}]"
            self.messages.append({"role": "assistant", "content": [{"type": "text", "text": note}]})
