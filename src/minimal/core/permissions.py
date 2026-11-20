"""Permission rules from settings.json: which tool calls run without asking, and which never run.

A rule is ``name`` (every call of tool ``name``) or ``name(pattern)``, where the
pattern is matched against the tool's subject (:attr:`~minimal.extensions.registry.Tool.subject_key`):

- ``bash(git status*)``: the command, as a shell-style wildcard (``fnmatch``);
- ``read(.env)``, ``write(src/**)``: the path as a glob (``**`` crosses folders,
  ``~`` in the pattern expands). A relative pattern matches the path relative to the working
  directory when inside it, else absolute; an absolute (or ``~``) pattern always
  matches the absolute path;
- ``fetch(https://example.com/*)``: any other subject, as a shell-style wildcard.

``permissions.deny`` rules win over everything, ``-y`` included, and apply to
tools that never ask. A ``bash`` deny rule matches the whole command or any command inside it
(``cd /tmp && rm -rf x`` is caught by ``bash(rm -rf*)``); it does not see through ``sh -c "..."``,
``eval``, ``xargs``, ``find -exec`` or wrappers such as ``sudo``: start the pattern with ``*`` for those.
``permissions.allow`` rules run a call without asking; a ``bash`` allow rule matches only the whole
command, and never one that chains, pipes, substitutes or
redirects (``;``, ``&``, ``|``, backtick, ``$(``, ``>``, ``<`` or a newline).
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterable
from pathlib import Path

from minimal import config
from minimal.config import RULE_RE
from minimal.extensions.registry import Tool
from minimal.tools.search import glob_regex

_SHELL_OPERATORS = (";", "&", "|", "`", "$(", ">", "<", "\n")
"""What a ``bash`` command may not contain for an allow rule to match it: another command could hide behind it."""

_COMMAND_BREAKS = re.compile(r";|&|\||\n|`|\$\(|\(|\)|\{|\}")
"""Where a ``bash`` command may hand over to another one: ``;``, ``&&``, ``&``, ``||``, ``|``, a newline, a
substitution, a subshell or a group. Quotes are ignored on purpose: an extra piece can only add a ``deny`` match."""

_LEADING_KEYWORDS = re.compile(r"(?:(?:if|then|elif|else|do|while|until|time|!)\s+)+")
"""Shell words that may come before a command (``then rm -rf x``)."""


def _commands_in(command: str) -> list[str]:
    """The pieces of ``command`` between operators, each without leading shell keywords.

    ``cd /tmp && rm -rf x`` gives ``cd /tmp`` and ``rm -rf x``; ``echo $(rm x)`` gives ``echo``, ``rm x`` and an
    empty rest, which is dropped. It does not look inside ``sh -c "..."``, ``eval``, ``xargs`` or ``find -exec``.
    """
    pieces = (_LEADING_KEYWORDS.sub("", part.strip()) for part in _COMMAND_BREAKS.split(command))
    return [piece for piece in pieces if piece]


_DEFAULT_SUBJECTS = {"grep": ".", "glob": "."}
"""Subjects of tools whose ``path`` input is optional."""


class PermissionDenied(RuntimeError):
    """A ``permissions.deny`` rule matched the call."""


def parse_rule(text: str) -> tuple[str, str | None]:
    """Split ``name`` or ``name(pattern)`` into ``(name, pattern)``.

    Raises:
        ValueError: if ``text`` is neither.
    """
    match = RULE_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"invalid permission rule {text!r}: use name or name(pattern)")
    return match.group(1), match.group(2)


def matches(rule: str, tool: Tool, input: dict, cwd: str, *, any_command: bool = False) -> bool:
    """Whether ``rule`` covers a call of ``tool`` with ``input`` in ``cwd``.

    With ``any_command``, a ``bash`` pattern also matches each command inside a compound one
    (see :func:`_commands_in`); ``deny`` rules use it, ``allow`` rules never do.
    """
    name, pattern = parse_rule(rule)
    if name != tool.name:
        return False
    if pattern is None:
        return True
    key = tool.subject_key
    if key is None:
        return False
    value = input.get(key, _DEFAULT_SUBJECTS.get(tool.name) if key == "path" else None)
    if not isinstance(value, str):
        return False
    if tool.name == "bash":
        parts = (value, *_commands_in(value)) if any_command else (value,)
        return any(fnmatch.fnmatchcase(part, pattern) for part in parts)
    if key == "path":
        root = Path(cwd).resolve()
        resolved = Path(cwd, value).resolve()  # as the file tools do (resolve_path): no ~ expansion in the input
        pattern = os.path.expanduser(pattern)
        if os.path.isabs(pattern) or not resolved.is_relative_to(root):
            subject = resolved.as_posix()
        else:
            subject = resolved.relative_to(root).as_posix()
        return glob_regex(pattern).fullmatch(subject) is not None
    return fnmatch.fnmatchcase(str(value), pattern)


_EXACT_ONLY = frozenset(
    "sh bash zsh dash ksh fish csh tcsh env sudo doas su xargs eval exec nohup nice time timeout watch find awk gawk sed "
    "ssh command builtin source npx uvx pipx bunx rm mv cp dd chmod chown ln kill pkill killall curl wget tee truncate "
    "shred".split()
)
"""First words of ``bash`` commands that run other programs or change many files by their arguments: a rule suggested
for them matches only the exact command, never a prefix."""

_INTERPRETER = re.compile(r"(?:python|pypy|node|ruby|perl|php)[\d.]*")
_PLAIN_WORD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")
_SUBCOMMAND = re.compile(r"[a-z][a-z0-9_-]*")


def _escape(text: str) -> str:
    """``text`` as a pattern that matches only itself (``*``, ``?``, ``[`` and ``{`` are wildcards in the patterns)."""
    return re.sub(r"[*?\[{]", lambda m: f"[{m.group()}]", text)


def _bash_pattern(command: str) -> str | None:
    """The narrowest pattern worth saving for ``command``; ``None`` if no allow rule can ever match it.

    A command that chains, pipes, substitutes or redirects is never matched by an allow rule, so none is offered.
    ``git status --short`` gives ``git status*``; a command whose first word runs other programs
    (:data:`_EXACT_ONLY`) or whose second word is not a plain subcommand matches only itself.
    """
    command = command.strip()
    if not command or any(op in command for op in _SHELL_OPERATORS):
        return None
    words = command.split()
    first = words[0]
    if (
        len(words) > 1
        and _PLAIN_WORD.fullmatch(first)
        and first not in _EXACT_ONLY
        and not _INTERPRETER.fullmatch(first)
        and _SUBCOMMAND.fullmatch(words[1])
    ):
        return f"{first} {words[1]}*"
    return _escape(command)


def suggest_rule(tool: Tool, input: dict, cwd: str) -> str | None:
    """The allow rule that would cover this call and little else, for the "this session" and "always" answers.

    ``bash`` gets a command pattern (:func:`_bash_pattern`), a tool with a ``subject_key`` the exact path (relative to
    ``cwd`` inside it) or subject, and a tool without one its bare name. ``None`` if no safe rule exists: the call
    could never be matched by an allow rule, or the tool's name or subject cannot be written as one.
    """
    key = tool.subject_key
    if key is None:
        rule = tool.name
    else:
        value = input.get(key, _DEFAULT_SUBJECTS.get(tool.name) if key == "path" else None)
        if not isinstance(value, str) or not value:
            return None
        pattern: str | None
        if tool.name == "bash":
            pattern = _bash_pattern(value)
        elif key == "path":
            root = Path(cwd).resolve()
            resolved = Path(cwd, value).resolve()
            subject = resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else resolved.as_posix()
            pattern = None if "[" in subject else _escape(subject)  # a path glob has no way to write a literal [
        else:
            pattern = _escape(value)
        if pattern is None:
            return None
        rule = f"{tool.name}({pattern})"
    if RULE_RE.fullmatch(rule) is None or not matches(rule, tool, input, cwd):
        return None
    return rule


class Permissions:
    """The allow and deny rules of :class:`~minimal.config.Settings` (a project's allow rules only once trusted),
    and the allow rules the user granted "for this session" (:meth:`allow_session`)."""

    def __init__(self, settings: config.Settings, session: Iterable[str] = ()) -> None:
        self.allow = settings.allow
        self.deny = settings.deny
        self.session: list[str] = list(session)

    def allow_session(self, rule: str) -> None:
        """Allow calls that ``rule`` covers until minimal exits (a reload keeps it); deny rules still win.

        Raises:
            ValueError: if ``rule`` is neither ``name`` nor ``name(pattern)``.
        """
        parse_rule(rule)
        if rule not in self.session:
            self.session.append(rule)

    def check(self, tool: Tool, input: dict, cwd: str) -> bool:
        """Whether the call may run without asking.

        Raises:
            PermissionDenied: if a deny rule matches.
        """
        for rule, scope in self.deny:
            if matches(rule, tool, input, cwd, any_command=True):
                raise PermissionDenied(f"denied by permission rule '{rule}' ({scope} settings)")
        if tool.name == "bash" and any(op in str(input.get("command", "")) for op in _SHELL_OPERATORS):
            return False
        return any(matches(rule, tool, input, cwd) for rule in (*(r for r, _ in self.allow), *self.session))
