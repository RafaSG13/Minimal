"""Permission rules from settings.json: which tool calls run without asking, and which never run.

A rule is ``name`` (every call of tool ``name``) or ``name(pattern)``, where the
pattern is matched against the tool's subject (:attr:`~minimal.extensions.registry.Tool.subject_key`):

- ``bash(git status*)``: the command, as a shell-style wildcard (``fnmatch``);
- ``read(.env)``, ``write(src/**)``: the path, relative to the working directory
  when inside it, else absolute, as a glob (``**`` crosses folders, ``~`` expands);
- ``fetch(https://example.com/*)``: any other subject, as a shell-style wildcard.

``permissions.deny`` rules win over everything, ``-y`` included, and apply to
tools that never ask. ``permissions.allow`` rules run a call without asking; a
``bash`` allow rule never matches a command that chains, pipes, substitutes or
redirects (``;``, ``&``, ``|``, backtick, ``$(``, ``>``, ``<`` or a newline).
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from minimal import config
from minimal.config import RULE_RE
from minimal.extensions.registry import Tool
from minimal.tools.search import glob_regex

_SHELL_OPERATORS = (";", "&", "|", "`", "$(", ">", "<", "\n")
"""What a ``bash`` command may not contain for an allow rule to match it: another command could hide behind it."""

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


def matches(rule: str, tool: Tool, input: dict, cwd: str) -> bool:
    """Whether ``rule`` covers a call of ``tool`` with ``input`` in ``cwd``."""
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
        return fnmatch.fnmatchcase(value, pattern)
    if key == "path":
        root = Path(cwd).resolve()
        resolved = (root / os.path.expanduser(value)).resolve()
        subject = resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else resolved.as_posix()
        return glob_regex(os.path.expanduser(pattern)).fullmatch(subject) is not None
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
            if matches(rule, tool, input, cwd):
                raise PermissionDenied(f"denied by permission rule '{rule}' ({scope} settings)")
        if tool.name == "bash" and any(op in str(input.get("command", "")) for op in _SHELL_OPERATORS):
            return False
        return any(matches(rule, tool, input, cwd) for rule, _ in self.allow)
