"""Static scan of extension source: what a file can do (run commands, use the network, read keys, hook minimal), found without running it.

It lists capabilities, not intent; code built at runtime can hide from it.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from minimal.extensions.loader import ExtensionSource, ProjectSnapshot

CAPABILITIES: dict[str, str] = {
    "unparsable": "cannot be parsed, so it was not scanned (it will fail to load)",
    "commands": "runs commands",
    "network": "uses the network",
    "dynamic": "runs dynamically built code",
    "environment": "reads environment variables (where API keys live)",
    "secrets": "touches credential files",
    "files": "writes or deletes files",
    "decoding": "decodes hidden data",
    "tool_call": "sees every tool call and can block it",
    "tool_result": "sees every tool result and can rewrite it",
    "turn_end": "sees the whole conversation after each turn",
    "provider": "adds a model provider (receives the conversation)",
    "prompt": "adds instructions to the system prompt",
}
"""Capability key → description, in report order."""

RISKY = frozenset(list(CAPABILITIES)[:8])
"""Capabilities reported as warnings (``unparsable`` … ``decoding``); the rest are informational hooks."""

_ORDER = {c: i for i, c in enumerate(CAPABILITIES)}

_IMPORTS = {
    "commands": ("subprocess", "pty", "pexpect", "sh", "plumbum"),
    "network": (
        "socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "ftplib", "smtplib",
        "poplib", "imaplib", "telnetlib", "xmlrpc", "websocket", "websockets", "paramiko",
    ),
    "dynamic": ("importlib", "ctypes", "cffi", "marshal", "pickle", "runpy"),
    "secrets": ("minimal.config", "minimal.providers.claude_auth", "keyring"),
}  # fmt: skip

_CALLS = {
    "commands": {
        "os.system", "os.popen", "os.fork", "os.forkpty", "pty.spawn",
        "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    },
    "dynamic": {"eval", "exec", "compile", "__import__", "builtins.eval", "builtins.exec", "importlib.import_module"},
    "environment": {"os.getenv", "os.putenv", "os.unsetenv"},
    "files": {
        "os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.rename", "os.replace", "os.truncate",
        "shutil.rmtree", "shutil.move", "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree",
    },
}  # fmt: skip
_COMMAND_PREFIXES = ("subprocess.", "os.exec", "os.spawn", "os.posix_spawn")

_METHODS = {
    "files": {"write_text", "write_bytes", "unlink", "rmdir", "rmtree"},
    "decoding": {
        "b64decode", "urlsafe_b64decode", "b32decode", "b85decode", "a85decode", "unhexlify", "fromhex", "decompress",
    },
    "prompt": {"append_system_prompt"},
}  # fmt: skip
_EVENTS = ("tool_call", "tool_result", "turn_end")
_ENVIRON = ("os.environ", "os.environb")

_SECRET_PATH = re.compile(
    r"(?:^|[/\\~])(?:\.ssh|\.aws|\.gnupg|\.netrc|\.env|id_rsa|id_ed25519|keys\.json|claude-auth\.json|trusted\.json)(?![\w-])"
)


@dataclass(frozen=True)
class Finding:
    """One construct that gives a file a capability."""

    line: int
    capability: str
    """A :data:`CAPABILITIES` key."""
    detail: str
    """The construct, e.g. ``import subprocess``, ``os.environ``, ``.on("tool_call")``."""


def scan(source: bytes) -> list[Finding]:
    """The capabilities of Python ``source``, one finding per line and capability, in line then report order."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as e:
        return [Finding(getattr(e, "lineno", 0) or 0, "unparsable", str(e))]
    visitor = _Scanner(_aliases(tree))
    visitor.visit(tree)
    return sorted(visitor.findings.values(), key=lambda f: (f.line, _ORDER[f.capability]))


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Local name → the absolute module or object it was imported as."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    aliases[a.asname] = a.name
                else:
                    top = a.name.partition(".")[0]
                    aliases[top] = top
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    return aliases


def _module_matches(name: str, modules: tuple[str, ...]) -> bool:
    return any(name == m or name.startswith(m + ".") for m in modules)


def _str_arg(node: ast.Call) -> str | None:
    """The first positional argument if it is a string literal."""
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


class _Scanner(ast.NodeVisitor):
    def __init__(self, aliases: dict[str, str]) -> None:
        self.aliases = aliases
        self.findings: dict[tuple[int, str], Finding] = {}

    def add(self, node: ast.AST, capability: str, detail: str) -> None:
        self.findings.setdefault((node.lineno, capability), Finding(node.lineno, capability, detail))

    def dotted(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self.dotted(node.value)
            return None if base is None else f"{base}.{node.attr}"
        return None

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            for capability, modules in _IMPORTS.items():
                if _module_matches(a.name, modules):
                    self.add(node, capability, f"import {a.name}")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module:
            return
        for a in node.names:
            for capability, modules in _IMPORTS.items():
                if _module_matches(node.module, modules) or _module_matches(f"{node.module}.{a.name}", modules):
                    self.add(node, capability, f"from {node.module} import {a.name}")

    def visit_Call(self, node: ast.Call) -> None:
        name = self.dotted(node.func)
        if name is not None:
            for capability, names in _CALLS.items():
                if name in names:
                    self.add(node, capability, f"{name}()")
            if name.startswith(_COMMAND_PREFIXES):
                self.add(node, "commands", f"{name}()")
            if name in ("open", "io.open"):
                self._open(node)
        if isinstance(node.func, ast.Attribute):
            self._method(node, node.func.attr)
        self.generic_visit(node)

    def _open(self, node: ast.Call) -> None:
        mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
        if mode is None:
            return
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            if any(c in mode.value for c in "wax+"):
                self.add(node, "files", f'open(…, "{mode.value}")')
        else:
            self.add(node, "files", "open(…, <mode not literal>)")

    def _method(self, node: ast.Call, attr: str) -> None:
        for capability, names in _METHODS.items():
            if attr in names:
                self.add(node, capability, f".{attr}()")
        if attr == "on":
            event = _str_arg(node)
            if event in _EVENTS:
                self.add(node, event, f'.on("{event}")')
        elif attr == "register_provider":
            provider = _str_arg(node)
            self.add(node, "provider", ".register_provider()" if provider is None else f'.register_provider("{provider}")')

    def visit_Name(self, node: ast.Name) -> None:
        if (name := self.dotted(node)) in _ENVIRON:
            self.add(node, "environment", name)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (name := self.dotted(node)) in _ENVIRON:
            self.add(node, "environment", name)
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return  # docstrings and bare strings say nothing about what the code does
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _SECRET_PATH.search(node.value):
            text = repr(node.value)
            self.add(node, "secrets", text if len(text) <= 40 else text[:39] + "…")


_SCAN_NOTE = (
    "Extensions are Python code that runs with your permissions. Static scan: what each file can do, "
    "not what it means to do; code built at runtime can hide from it."
)


def format_install_report(sources: Iterable[ExtensionSource]) -> str:
    """Plain-text scan of every file of extensions about to be installed (read from the marketplace), for the user to review first."""
    lines = ["Extensions to install into your global folder, where they apply to every project.", _SCAN_NOTE, ""]
    for source in sources:
        for path, data in source.files:
            lines.extend(_file_report(path.relative_to(source.path.parent).as_posix(), data))
    return "\n".join(lines)


def format_report(project: ProjectSnapshot) -> str:
    """Plain-text scan of every file of ``project``, grouped by capability, then its trust-gated settings, for the user to review before trusting it."""
    lines = [f"Project extensions and settings in {project.directory.parent}: not trusted yet.", _SCAN_NOTE, ""]
    for source in project.sources:
        for path, data in source.files:
            lines.extend(_file_report(path.relative_to(project.directory).as_posix(), data))
    if project.risky:
        lines += ["", "Settings that need trust:"]
        for scope, keys in project.risky.items():
            for rule in keys.get("permissions.allow", []):
                lines.append(f"  ⚠ permissions.allow {rule} ({scope})")
            for ext_id in keys.get("extensions.disabled", []):
                lines.append(f"  ⚠ extensions.disabled {ext_id} ({scope})")
            for ext_id, values in keys.get("extensions.settings", {}).items():
                lines.append(f"  · extensions.settings {ext_id} {json.dumps(values, separators=(',', ':'))} ({scope})")
    return "\n".join(lines)


def _file_report(name: str, source: bytes) -> list[str]:
    lines = [f"  {name}"]
    by_capability: dict[str, list[Finding]] = {}
    for finding in scan(source):
        by_capability.setdefault(finding.capability, []).append(finding)
    if not by_capability:
        lines.append("    nothing found")
    for capability in CAPABILITIES:
        found = by_capability.get(capability)
        if not found:
            continue
        entries = [f"line {f.line} {f.detail}" for f in found[:3]]
        if len(found) > 3:
            entries.append(f"+{len(found) - 3} more")
        mark = "⚠" if capability in RISKY else "·"
        lines.append(f"    {mark} {CAPABILITIES[capability]}: {' · '.join(entries)}")
    return lines
