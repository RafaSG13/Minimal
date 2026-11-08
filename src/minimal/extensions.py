"""Discovering and loading extensions.

An extension is a ``.py`` file that defines ``register(pi)``, where ``pi`` is the
:class:`~minimal.registry.Registry`::

    def register(pi):
        pi.register_tool(name="hello", run=lambda input, ctx: "hi", description="...")
        pi.register_command("stats", lambda args, ctx: print(len(ctx.messages)))
        pi.register_provider("myllm", create=MyProvider, default_model="my-model")
        pi.on("tool_call", lambda e: {"block": True, "reason": "no"})
        pi.append_system_prompt("Extra instructions.")
        pi.register_widget("panel", lambda ctx: MyWidget(), area="sidebar")

Search order (each folder in sorted order, files starting with ``_`` skipped):

1. ``~/.minimal/extensions/``
2. ``<cwd>/.minimal/extensions/``
3. every ``--ext`` path (a file or a folder)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Iterable

from minimal.registry import Registry

GLOBAL_EXTENSIONS_DIR = Path.home() / ".minimal" / "extensions"
"""Extensions loaded in every project."""


def project_extensions_dir(cwd: str) -> Path:
    """Extensions loaded only when running in ``cwd``."""
    return Path(cwd) / ".minimal" / "extensions"


def load_extensions(
    registry: Registry, cwd: str, extra: Iterable[str | Path] = ()
) -> tuple[list[Path], list[str]]:
    """Load every extension found and let it register into ``registry``.

    Args:
        registry: Passed to each extension's ``register(pi)``.
        cwd: Directory used for the project-local folder and to resolve ``extra``.
        extra: Additional files or folders (the ``--ext`` option).

    Returns:
        ``(loaded, errors)``: the files that loaded successfully, and one
        ``"<file>: <ErrorType>: <message>"`` line per file that failed (or
        ``"<path>: not found"`` for a missing ``extra`` entry). Failed files are
        skipped; anything they registered before failing stays registered.
    """
    files, errors = _find_extension_files(cwd, extra)
    loaded = []
    for file in files:
        try:
            _load_extension(file, registry)
            loaded.append(file)
        except Exception as e:
            errors.append(f"{file}: {type(e).__name__}: {e}")
    return loaded, errors


def _find_extension_files(cwd: str, extra: Iterable[str | Path]) -> tuple[list[Path], list[str]]:
    """Return candidate extension files in load order, and errors for missing ``extra`` paths."""
    files = _py_files(GLOBAL_EXTENSIONS_DIR)
    files += _py_files(project_extensions_dir(cwd))
    errors = []
    for entry in extra:
        path = Path(cwd, entry).resolve()
        if not path.exists():
            errors.append(f"{path}: not found")
        elif path.is_dir():
            files += _py_files(path)
        else:
            files.append(path)
    return files, errors


def _py_files(directory: Path) -> list[Path]:
    """Sorted ``*.py`` files in ``directory`` not starting with ``_``; empty if it doesn't exist."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.py") if not p.name.startswith("_"))


def _load_extension(file: Path, registry: Registry) -> None:
    """Run ``file`` as a module and call its ``register(registry)``.

    The source is compiled directly instead of through the import system, whose
    ``__pycache__`` check (whole-second mtime and size) could run stale code
    when a file is edited twice within a second and reloaded.
    """
    module_name = f"minimal_ext_{file.stem}"
    spec = importlib.util.spec_from_file_location(module_name, file)
    if spec is None or spec.loader is None:
        raise ValueError("cannot import file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    exec(compile(file.read_text(encoding="utf-8"), str(file), "exec"), module.__dict__)

    register = getattr(module, "register", None)
    if not callable(register):
        raise ValueError("missing register(pi) function")
    register(registry)
