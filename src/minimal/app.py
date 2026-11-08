"""Runtime state of a ``minimal`` process, and how one user turn is executed.

:class:`App` is also the ``ctx`` object passed to tools, slash commands and
widgets, so extensions can read ``ctx.cwd``, ``ctx.messages``,
``ctx.provider_name``, ``ctx.model`` and ``ctx.registry``.

What the user sees goes through a :class:`Frontend`: :class:`ConsoleFrontend`
(plain stdout, used by ``-p``) or the terminal UI's.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol

import minimal.extensions
import minimal.registry
from minimal.agent import run_agent
from minimal.extensions import load_extensions, project_extensions_dir
from minimal.marketplace import register_marketplace_tools
from minimal.providers import register_builtin_providers
from minimal.providers.base import Provider
from minimal.registry import Registry
from minimal.session import Session
from minimal.tools import register_builtin_tools

BASE_PROMPT = (
    "You are a coding agent working in the user's terminal. Use the tools to read, edit and run things; "
    "do not invent file contents. Be concise."
)

SELF_EXTEND_PROMPT = (
    "You can extend minimal, the harness you are running in. An extension is one Python file defining "
    "register(minimal); minimal is the Registry (API and docstrings: {registry_path}). Save new extensions to "
    "{global_dir}/<name>.py, or {project_dir}/<name>.py when the user wants it only for this project. "
    "minimal.register_tool(name, run, description, input_schema, confirm) adds a tool run(input, ctx); "
    "minimal.register_command(name, run, description, terminal=False) adds a slash command run(args, ctx); "
    'minimal.register_provider(...) adds a model; minimal.on("tool_call"|"tool_result"|"turn_end", handler) hooks events; '
    "minimal.append_system_prompt(text) adds instructions; minimal.register_widget(name, create, area) adds a visual "
    'component to the terminal UI: create(ctx) returns a textual Widget for area "top", "bottom" or "sidebar", '
    "styled with its DEFAULT_CSS, and an optional refresh_state(ctx) method is called after every turn. "
    "After writing or editing an extension, call reload_extensions and fix any reported errors. "
    "Share and reuse extensions with ext_search, ext_install and ext_publish."
)


def build_system_prompt(registry: Registry, cwd: str) -> str:
    """Assemble the system prompt.

    Parts, separated by blank lines: the base prompt, the working directory and
    today's date, how to extend minimal, ``AGENTS.md`` from ``cwd`` if present,
    and the paragraphs added by extensions.
    """
    parts = [
        BASE_PROMPT,
        f"Working directory: {cwd}\nDate: {datetime.now(timezone.utc).date().isoformat()}",
        SELF_EXTEND_PROMPT.format(
            registry_path=Path(minimal.registry.__file__),
            global_dir=minimal.extensions.GLOBAL_EXTENSIONS_DIR,
            project_dir=project_extensions_dir(cwd),
        ),
    ]
    agents_md = Path(cwd, "AGENTS.md")
    if agents_md.exists():
        parts.append(f"Project instructions (AGENTS.md):\n{agents_md.read_text(encoding='utf-8')}")
    parts.extend(registry.prompt_parts)
    return "\n\n".join(parts)


def reload_summary(loaded: list[Path], errors: list[str]) -> str:
    """Describe the result of :meth:`App.reload_extensions` for the user or the model."""
    summary = f"Loaded {len(loaded)} extension(s)"
    if loaded:
        summary += ": " + ", ".join(p.name for p in loaded)
    return summary + "".join(f"\nFailed: {e}" for e in errors)


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

    def reloaded(self) -> None:
        """Extensions were reloaded (widgets may have changed)."""


class ConsoleFrontend:
    """Plain stdout/stderr and ``input()``: used by ``-p``."""

    def text(self, chunk: str) -> None:
        print(chunk, end="", flush=True)

    def tool_call(self, call: dict) -> None:
        """Show a dimmed one-line summary of a tool call on stderr."""
        args = json.dumps(call["input"], ensure_ascii=False)[:120]
        print(f"\n\x1b[2m› {call['name']} {args}\x1b[0m\n", end="", file=sys.stderr, flush=True)

    def tool_result(self, call: dict, result: dict) -> None:
        """Show the first line of a failed tool call on stderr."""
        if result["is_error"]:
            first = result["content"].strip().partition("\n")[0][:200]
            print(f"\x1b[31m✘ {call['name']}: {first}\x1b[0m", file=sys.stderr, flush=True)

    def turn_end(self, error: str | None) -> None:
        if error is None:
            print()
        else:
            print(f"\nError: {error}", file=sys.stderr)

    def ask(self, question: str) -> bool:
        """Accepts ``y``/``yes``; EOF means no."""
        try:
            answer = input(f"\n{question} [y/N] ")
        except EOFError:
            return False
        return re.fullmatch(r"y|yes", answer.strip(), re.IGNORECASE) is not None

    def reloaded(self) -> None:
        pass


class App:
    """Everything a running ``minimal`` process needs.

    Call :meth:`use_provider` before the first turn.

    Attributes:
        provider: Current model integration (``/provider`` changes it).
        provider_name: Registry name of ``provider``.
        model: Current model name (``/model`` changes it).
        registry: Tools, commands, providers, event handlers and widgets.
        session: Current session (``/new`` replaces it).
        cwd: Working directory for tools and the system prompt.
        auto_approve: Run ``write``/``edit``/``bash`` without asking.
        extra_extensions: ``--ext`` paths, loaded again on every reload.
        frontend: Where turns are shown and confirmations asked.
        extensions: Extension files loaded by the last reload.
        extension_errors: Failures of the last reload.
    """

    def __init__(
        self,
        *,
        registry: Registry,
        session: Session,
        cwd: str,
        auto_approve: bool,
        extra_extensions: Iterable[str] = (),
        frontend: Frontend | None = None,
    ) -> None:
        self.registry = registry
        self.session = session
        self.cwd = cwd
        self.auto_approve = auto_approve
        self.extra_extensions = list(extra_extensions)
        self.frontend: Frontend = frontend or ConsoleFrontend()
        self.provider: Provider | None = None
        self.provider_name = ""
        self.model = ""
        self.extensions: list[Path] = []
        self.extension_errors: list[str] = []

    def reload_extensions(self) -> tuple[list[Path], list[str]]:
        """Rebuild the registry: built-ins, marketplace tools, then every extension from disk.

        The registry is cleared in place, so a turn in progress sees the new
        tools on its next model request. The current provider instance keeps
        working even if its spec is gone.

        Returns:
            ``(loaded, errors)`` as from :func:`~minimal.extensions.load_extensions`.
        """
        self.registry.reset()
        register_builtin_tools(self.registry)
        register_builtin_providers(self.registry)
        register_marketplace_tools(self.registry)
        loaded, errors = load_extensions(self.registry, self.cwd, self.extra_extensions)
        self.extensions, self.extension_errors = loaded, errors
        self.frontend.reloaded()
        return loaded, errors

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

    def confirm(self, call: dict) -> bool:
        """Ask the user, through the frontend, whether a tool call may run."""
        if self.auto_approve:
            return True
        name, inp = call["name"], call["input"]
        detail = inp.get("command") if name == "bash" else (inp.get("path") or json.dumps(inp))
        return self.frontend.ask(f"Allow {name}: {detail}?")

    def run_turn(self, text: str) -> None:
        """Send ``text`` to the model, stream the answer to the frontend and save the session.

        If the turn fails, its messages are discarded so the history never ends
        with a ``tool_use`` that has no result, and the frontend gets the error.
        """
        before = len(self.messages)
        self.messages.append({"role": "user", "content": text})
        error = None
        try:
            run_agent(
                provider=self.provider,
                model=self.model,
                system=build_system_prompt(self.registry, self.cwd),
                messages=self.messages,
                registry=self.registry,
                ctx=self,
                on_text=self.frontend.text,
                on_tool=self.frontend.tool_call,
                on_tool_result=self.frontend.tool_result,
            )
        except Exception as e:
            del self.messages[before:]
            error = str(e)
        finally:
            self.session.save()
        self.frontend.turn_end(error)
