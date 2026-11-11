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


class Permissions:
    """The allow and deny rules of :class:`~minimal.config.Settings` (a project's allow rules only once trusted)."""

    def __init__(self, settings: config.Settings) -> None:
        self.allow = settings.allow
        self.deny = settings.deny

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
        return any(matches(rule, tool, input, cwd) for rule, _ in self.allow)
