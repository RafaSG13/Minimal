"""Discovering and loading extensions.

An extension is a ``.py`` file that defines ``register(minimal)``, where ``minimal`` is the
:class:`~minimal.registry.Registry`::

    def register(minimal):
        minimal.register_tool(name="hello", run=lambda input, ctx: "hi", description="...")
        minimal.register_command("stats", lambda args, ctx: print(len(ctx.messages)))
        minimal.register_provider("myllm", create=MyProvider, default_model="my-model")
        minimal.on("tool_call", lambda e: {"block": True, "reason": "no"})
        minimal.append_system_prompt("Extra instructions.")
        minimal.register_widget("panel", lambda ctx: MyWidget(), area="sidebar")
        minimal.register_status("cost", lambda ctx: "$0.12", side="right")
        minimal.register_theme("ocean", dark=True, background="#0b1622", foreground="#d0e4f5", accent="#4fc3f7")
        minimal.register_logo(["#.#", "###"], {"#": "#4fc3f7"})

Search order (each folder in sorted order, files starting with ``_`` skipped):

1. ``~/.minimal/extensions/``
2. ``<cwd>/.minimal/extensions/`` (from a snapshot the caller read and had approved)
3. every ``--ext`` path (a file or a folder)

Within each folder, its own files load first, then those of its ``themes/``,
``tools/`` and ``widgets/`` subfolders: the marketplace's categories
(:data:`CATEGORIES`), which only group files; any file may register anything.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from minimal.registry import Registry

GLOBAL_EXTENSIONS_DIR = Path.home() / ".minimal" / "extensions"
"""Extensions loaded in every project."""

CATEGORIES = ("extensions", "themes", "tools", "widgets")
"""Marketplace categories: ``extensions`` files live in an extensions folder itself, the others in a subfolder."""


def project_extensions_dir(cwd: str) -> Path:
    """Extensions loaded only when running in ``cwd``."""
    return Path(cwd) / ".minimal" / "extensions"


@dataclass(frozen=True)
class ExtensionSource:
    """An extension file as read once: its path and the exact bytes that were reviewed and will run."""

    path: Path
    data: bytes


def read_extensions(files: list[Path]) -> list[ExtensionSource]:
    """Read ``files`` once, in order. Raises ``OSError`` if any cannot be read."""
    return [ExtensionSource(file, file.read_bytes()) for file in files]


def category_dir(directory: Path, category: str) -> Path:
    """Where files of ``category`` live inside extensions folder ``directory``.

    Raises:
        ValueError: if ``category`` is not one of :data:`CATEGORIES`.
    """
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}; use one of {', '.join(CATEGORIES)}")
    return directory if category == "extensions" else directory / category


def load_extensions(
    registry: Registry, cwd: str, extra: Iterable[str | Path] = (), project: Iterable[ExtensionSource] = ()
) -> tuple[list[Path], list[str]]:
    """Load every extension found and let it register into ``registry``.

    Args:
        registry: Passed to each extension's ``register(minimal)``.
        cwd: Directory used to resolve ``extra``.
        extra: Additional files or folders (the ``--ext`` option).
        project: Project extensions the caller already reviewed, loaded from these
            bytes and never re-read from disk; the project folder is not scanned
            here. Empty means none (untrusted or absent).

    Returns:
        ``(loaded, errors)``: the files that loaded successfully, and one
        ``"<file>: <ErrorType>: <message>"`` line per file that failed (or
        ``"<path>: not found"`` for a missing ``extra`` entry). Failed files are
        skipped; anything they registered before failing stays registered.
    """
    project = list(project)
    pinned = {s.path: s.data for s in project}
    files, errors = _find_extension_files(cwd, extra, [s.path for s in project])
    loaded = []
    for file in files:
        try:
            data = pinned[file] if file in pinned else file.read_bytes()
            _load_extension(file, data, registry)
            loaded.append(file)
        except Exception as e:
            errors.append(f"{file}: {type(e).__name__}: {e}")
    return loaded, errors


def _find_extension_files(
    cwd: str, extra: Iterable[str | Path], project_files: list[Path]
) -> tuple[list[Path], list[str]]:
    """Return candidate extension files in load order, and errors for missing ``extra`` paths."""
    files = extension_files(GLOBAL_EXTENSIONS_DIR)
    files += project_files
    errors = []
    for entry in extra:
        path = Path(cwd, entry).resolve()
        if not path.exists():
            errors.append(f"{path}: not found")
        elif path.is_dir():
            files += extension_files(path)
        else:
            files.append(path)
    return files, errors


def files_digest(sources: list[ExtensionSource]) -> str:
    """SHA-256 over the paths and contents of ``sources`` in order: changes when a file is added, removed, renamed or edited."""
    digest = hashlib.sha256()
    for s in sources:
        digest.update(s.path.as_posix().encode() + b"\0" + s.data + b"\0")
    return digest.hexdigest()


def extension_files(directory: Path) -> list[Path]:
    """The extensions in folder ``directory``, in load order: its own files, then each category subfolder's."""
    return [p for category in CATEGORIES for p in _py_files(category_dir(directory, category))]


def _py_files(directory: Path) -> list[Path]:
    """Sorted ``*.py`` files in ``directory`` not starting with ``_`` or ``.``; empty if it doesn't exist."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.py") if not p.name.startswith(("_", ".")))


def _load_extension(file: Path, data: bytes, registry: Registry) -> None:
    """Run the source ``data`` of ``file`` as a module and call its ``register(registry)``.

    The source is compiled directly instead of through the import system, whose
    ``__pycache__`` check (whole-second mtime and size) could run stale code
    when a file is edited twice within a second and reloaded.
    """
    # Category subfolders may reuse a name (tools/x.py and widgets/x.py): keep their modules apart.
    category = file.parent.name if file.parent.name in CATEGORIES[1:] else ""
    module_name = f"minimal_ext_{category}_{file.stem}" if category else f"minimal_ext_{file.stem}"
    spec = importlib.util.spec_from_file_location(module_name, file)
    if spec is None or spec.loader is None:
        raise ValueError("cannot import file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    exec(compile(data.decode("utf-8"), str(file), "exec"), module.__dict__)

    register = getattr(module, "register", None)
    if not callable(register):
        raise ValueError("missing register(minimal) function")
    register(registry)
