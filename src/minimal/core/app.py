"""Runtime state of a ``minimal`` process, and how one user turn is executed.

:class:`App` is internal: extensions see it through :attr:`App.context`, the
:class:`~minimal.core.context.Context` passed as ``ctx`` to their tools, slash
commands, events and widgets. Built-in slash commands get the App itself.

What the user sees goes through a :class:`Frontend`: the terminal UI's, or
:class:`ConsoleFrontend` (plain stdout, the default of an :class:`App` built without one).
"""

from __future__ import annotations

import json
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol

import minimal.core.context
import minimal.extensions.facade
import minimal.extensions.loader
from minimal import config
from minimal.core.agent import run_agent
from minimal.core.context import Context
from minimal.core.permissions import Permissions
from minimal.core.session import Session
from minimal.extensions.loader import (
    ProjectSnapshot,
    load_extensions,
    project_extensions_dir,
    read_project_snapshot,
)
from minimal.extensions.marketplace import register_marketplace_tools
from minimal.extensions.registry import Registry, Tool
from minimal.providers import register_builtin_providers
from minimal.providers.base import Provider
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
    'describes it), set __version__ = "1.0" and, as it uses this API, __requires__ = ">=0.2". '
    "Save new extensions to {global_dir}/<name>.py, or {project_dir}/<name>.py when the user wants it only for this "
    "project; themes, tools and UI widgets go in the themes/, tools/ or widgets/ subfolder, as the marketplace groups "
    "them. "
    "@minimal.tool(description=..., input_schema=..., confirm=...) on run(input, ctx) adds a tool (name and "
    "description default to the function's name and docstring); @minimal.command() on run(args, ctx) adds a slash "
    'command; @minimal.on("tool_call"|"tool_result"|"turn_end") on handler(event) hooks events (event["ctx"] is the '
    'Context; turn_end has event["nested"] for a subagent\'s loop); the same exist as minimal.register_tool, '
    "register_command and on(event, handler). minimal.register_provider(...) adds a model; "
    "minimal.append_system_prompt(text) adds instructions; settings = minimal.declare_settings({{...defaults}}) "
    "declares settings the user sets under extensions.settings.<id> in ~/.minimal/settings.json (id is the name, or "
    "category/name in a subfolder). minimal.register_widget(name, create, area) adds a visual "
    'component to the terminal UI: create(ctx) returns a textual Widget for area "top", "bottom" or "sidebar", '
    "styled with its DEFAULT_CSS, and an optional refresh_state(ctx) method is called after every turn; "
    "an optional refresh_live(ctx, event) method is called live during a turn (event kind 'start', 'tool_call' or "
    "'tool_result'); minimal.register_status(name, render, side='right') adds a status-line segment: render(ctx) "
    "returns text or None; "
    "minimal.register_theme(name, dark=..., background=..., foreground=..., accent=..., **more #rrggbb colours) "
    "adds a colour theme the user picks with /theme. "
    "minimal.register_logo(art, colors, align='center') replaces the default wordmark in the welcome header with "
    "pixel art, aligned 'left', 'center' or 'right': art is a list of "
    "equal-length strings, one character per pixel (' ' or '.' transparent), two pixel rows per terminal row; colors "
    "maps every other character to #rrggbb or a theme variable such as $accent. "
    "In ctx: ctx.ui.ask/choose/choose_many/ask_text question the user; ctx.complete(messages) makes one model "
    "request and ctx.run_agent(prompt) runs a nested agent loop; ctx.new_session(messages) switches to a new session; "
    "ctx.cancelled and ctx.check_cancelled() notice an interrupt. "
    "After writing or editing an extension, call reload_extensions and fix any reported errors. "
    "Share and reuse extensions with ext_search, ext_install and ext_publish."
)


def build_system_prompt(registry: Registry, cwd: str, settings: config.Settings) -> str:
    """Assemble the system prompt.

    Parts, separated by blank lines: the base prompt (:func:`base_prompt`), the
    working directory and today's date, how to extend minimal, each existing
    ``prompt.files`` file (default ``AGENTS.md``) resolved against ``cwd``, each
    ``prompt.append`` text, and the paragraphs added by extensions.

    Files listed by a project or local settings file must be inside ``cwd``:
    a project cannot pull ``~/.ssh/id_rsa`` into the prompt.
    """
    parts = [
        base_prompt(settings),
        f"Working directory: {cwd}\nDate: {datetime.now(timezone.utc).date().isoformat()}",
        SELF_EXTEND_PROMPT.format(
            facade_path=Path(minimal.extensions.facade.__file__),
            context_path=Path(minimal.core.context.__file__),
            global_dir=minimal.extensions.loader.GLOBAL_EXTENSIONS_DIR,
            project_dir=project_extensions_dir(cwd),
        ),
    ]
    files, scope = settings.prompt_files
    root = Path(cwd).resolve()
    for name in files:
        path = (root / Path(name).expanduser()).resolve()
        if scope in ("project", "local") and not path.is_relative_to(root):
            continue
        if path.is_file():
            parts.append(f"Project instructions ({name}):\n{path.read_text(encoding='utf-8')}")
    parts.extend(settings.prompt_append)
    parts.extend(registry.prompt_parts)
    return "\n\n".join(p for p in parts if p)



_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _visible(text: str) -> str:
    """``text`` with control characters (all but tab and newline) written as ``\\xNN``.

    Text from the model or from a tool must not reach the terminal raw: escape
    sequences can move the cursor and rewrite what is on screen, including the
    text of a confirmation prompt.
    """
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


class Frontend(Protocol):
    """Shows a turn to the user. Every method is called from the thread running the turn or command."""

    def text(self, chunk: str) -> None:
        """Model text, streamed as it arrives."""

    def tool_call(self, call: dict) -> None:
        """A tool is about to run: ``{name, input}``."""

    def tool_result(self, call: dict, result: dict) -> None:
        """A tool finished: ``result`` is its ``tool_result`` block (``content``, ``is_error``)."""

    def turn_end(self, error: str | None) -> None:
        """The turn finished; ``error`` is set if it failed."""

    def ask(self, question: str) -> bool:
        """Ask a yes/no question; return the answer."""

    def choose(self, title: str, options: list[tuple[str, str]]) -> str | None:
        """Pick one ``(value, label)`` option; return its value, or ``None`` if cancelled."""

    def choose_theme(self, title: str, options: list[tuple[str, str]]) -> str | None:
        """Like :meth:`choose`, for colour themes; the frontend may decorate labels."""

    def choose_many(self, title: str, options: list[tuple[str, str, bool]]) -> list[str] | None:
        """Tick any ``(value, label, checked)`` options; return the ticked values in option order, or ``None`` if cancelled."""

    def ask_text(self, title: str) -> str | None:
        """Ask for one line of text; return it stripped, or ``None`` if empty or cancelled."""

    def reloaded(self) -> None:
        """Extensions were reloaded (widgets may have changed)."""

    def themes(self) -> tuple[list[str], str | None]:
        """The colour themes on offer and the current one; ``([], None)`` if the frontend has none."""

    def set_theme(self, name: str) -> None:
        """Switch to colour theme ``name``, one of :meth:`themes`."""


class ConsoleFrontend:
    """Plain stdout/stderr and ``input()``: used by commands the TUI runs in the terminal, and by an :class:`App` with no frontend.

    Model and tool text is shown with control characters escaped (:func:`_visible`),
    except the answer itself when stdout is not a terminal, so piped output stays exact.
    """

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

    def choose_theme(self, title: str, options: list[tuple[str, str]]) -> str | None:
        """Same as :meth:`choose`: a console cannot show the colours."""
        return self.choose(title, options)

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

    def themes(self) -> tuple[list[str], str | None]:
        return [], None

    def set_theme(self, name: str) -> None:
        raise ValueError("colour themes only apply to the terminal UI")


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
        extensions: Extensions loaded by the last reload.
        extension_errors: Failures of the last reload, setting errors included.
        settings: The settings read by the last reload, with project trust applied.
        setting_errors: Problems found in the settings files by the last reload.
        untrusted: What the last reload did not apply because the user has not
            trusted the project (``/trust``); ``None`` if nothing.
        cancelled: Set to interrupt the turn in progress.
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
        self.cancelled = threading.Event()
        self.settings = config.Settings()
        self.setting_errors: list[str] = []
        self.context = Context(self)
        self.permissions = Permissions(self.settings)

    def is_trusted(self, project: ProjectSnapshot) -> bool:
        """Whether ``project`` may apply: ``--trust-project``, or the user trusted exactly this snapshot."""
        return self.trust_project or config.load_trusted().get(project.key) == project.digest

    def reload_extensions(self) -> tuple[list[Path], list[str]]:
        """Rebuild the registry: built-ins, marketplace tools, then every extension from disk.

        The registry is cleared in place, so a turn in progress sees the new
        tools on its next model request. The current provider instance keeps
        working even if its spec is gone.

        The project's extensions (``<cwd>/.minimal/extensions``) and its trust-gated
        settings (:data:`~minimal.config.RISKY`) apply only if the
        user trusted exactly that snapshot (:meth:`is_trusted`). Otherwise they are
        skipped without asking and recorded in :attr:`untrusted`; that is not an
        error. The files are read once: that snapshot is what is hashed and run.

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
        self.settings = config.Settings(tuple(layers), trusted)
        self.permissions = Permissions(self.settings)
        self.setting_errors = setting_errors
        self.registry.reset()
        register_builtin_tools(self.registry)
        register_builtin_providers(self.registry)
        register_marketplace_tools(self.registry)
        project = snapshot.sources if snapshot is not None and trusted else ()
        loaded, errors = load_extensions(self.registry, self.cwd, self.settings, self.extra_extensions, project=project)
        if read_error:
            errors.append(read_error)
        errors.extend(setting_errors)
        self.extensions, self.extension_errors = loaded, errors
        self.untrusted = snapshot if snapshot is not None and not trusted else None
        self.frontend.reloaded()
        return loaded, errors

    def untrusted_notice(self) -> str | None:
        """What the last reload skipped as untrusted, pointing to ``/trust``; ``None`` if nothing."""
        if self.untrusted is None:
            return None
        return (
            f"{self.untrusted.count} project extension(s) and/or settings in {self.untrusted.directory.parent} are not "
            "trusted, so they were not applied; run /trust to review them"
        )

    def use_provider(self, name: str, model: str | None = None) -> None:
        """Switch to provider ``name`` and to ``model`` (default: the provider's default).

        The conversation is kept: history is stored in the provider-neutral format.

        Raises:
            ValueError: if the provider is unknown or its credentials are missing.
        """
        spec = self.registry.providers.get(name)
        if spec is None:
            raise ValueError(f"unknown provider: {name} (available: {', '.join(self.registry.providers)})")
        problem = spec.missing_credentials()
        if problem:
            raise ValueError(problem)
        self.provider = spec.create()
        self.provider_name = name
        self.model = model or spec.default_model

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

    def confirm(self, call: dict) -> bool:
        """Ask the user, through the frontend, whether a tool call may run."""
        if self.auto_approve:
            return True
        name, inp = call["name"], call["input"]
        detail = inp.get("command") if name == "bash" else (inp.get("path") or json.dumps(inp))
        return self.frontend.ask(f"Allow {name}: {detail}?")

    def permit(self, tool: Tool, input: dict) -> bool:
        """Whether the permission rules let ``tool`` run with ``input`` without asking.

        Raises:
            PermissionDenied: if a deny rule matches (even with ``-y``).
        """
        return self.permissions.check(tool, input, self.cwd)

    def system_prompt(self) -> str:
        """The system prompt of the next turn (:func:`build_system_prompt`)."""
        return build_system_prompt(self.registry, self.cwd, self.settings)

    def run_turn(self, text: str) -> None:
        """Send ``text`` to the model, stream the answer to the frontend and save the session.

        If the turn fails or is interrupted, its messages are discarded so the
        history never ends with a ``tool_use`` that has no result, and the
        frontend gets the error.
        """
        self.cancelled.clear()
        before = len(self.messages)
        self.messages.append({"role": "user", "content": text})
        error = None
        try:
            run_agent(
                provider=self.provider,
                model=self.model,
                system=self.system_prompt(),
                messages=self.messages,
                registry=self.registry,
                ctx=self.context,
                on_text=self.frontend.text,
                on_tool=self.frontend.tool_call,
                on_tool_result=self.frontend.tool_result,
                confirm=self.confirm,
                cancelled=self.cancelled,
                permit=self.permit,
            )
        except KeyboardInterrupt:
            del self.messages[before:]
            raise
        except Exception as e:
            del self.messages[before:]
            error = str(e)
        finally:
            self.session.save()
        self.frontend.turn_end(error)
