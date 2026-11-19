"""Discovering and loading extensions.

An extension is a ``.py`` file, or a folder with an ``__init__.py`` (a package
that may hold helper modules), that defines ``register(minimal)``, where ``minimal``
is an :class:`~minimal.extensions.facade.Extension`::

    def register(minimal):
        minimal.register_tool(name="hello", run=lambda input, ctx: "hi", description="...")
        minimal.register_command("stats", lambda args, ctx: print(len(ctx.messages)))
        minimal.on("tool_call", lambda e: {"block": True, "reason": "no"})

Search order (each folder in sorted order, names starting with ``_`` or ``.`` skipped):

1. ``~/.minimal/extensions/``
2. ``<cwd>/.minimal/extensions/`` (from a snapshot the user trusted)
3. every ``--ext`` path (a file, a package folder, or a folder of extensions)

Within each folder, its own extensions load first, then those of its ``themes/``,
``tools/`` and ``widgets/`` subfolders: the marketplace's categories
(:data:`CATEGORIES`), which only group them; any extension may register anything.
An extension's id (:func:`ext_id`) is its name, prefixed by its category folder
outside ``extensions`` (``tools/subagent``); ``extensions.disabled`` in the
settings lists ids not to load from the global and project folders. ``__depends__`` and
``__conflicts__`` (lists of ids, see :mod:`minimal.extensions.metadata`) reorder and skip extensions
(:func:`load_sources`).

Every file is read once, before anything runs: the bytes that are hashed for
``/trust`` are the bytes executed, imported through :class:`_SnapshotImporter`.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from minimal import config
from minimal.extensions.facade import Extension
from minimal.extensions.metadata import Metadata, SourceSyntaxError, check_api, read_metadata
from minimal.extensions.registry import Registry

GLOBAL_EXTENSIONS_DIR = Path.home() / ".minimal" / "extensions"
"""The global extensions, loaded in every project (:func:`global_extensions_dir`)."""

CATEGORIES = ("extensions", "themes", "tools", "widgets")
"""Marketplace categories: ``extensions`` live in an extensions folder itself, the others in a subfolder."""

BUNDLED_DIR = Path(__file__).resolve().parent.parent / "bundled"
"""First-party extensions shipped inside minimal: loaded before every other one (:func:`load_bundled`)."""


def global_extensions_dir() -> Path:
    """Extensions loaded in every project."""
    return GLOBAL_EXTENSIONS_DIR


def project_extensions_dir(cwd: str) -> Path:
    """Extensions loaded only when running in ``cwd``."""
    return Path(cwd) / ".minimal" / "extensions"


def ext_id(path: Path) -> str:
    """The id of the extension at ``path`` (a ``.py`` file or a package folder): ``name`` or ``category/name``."""
    name = path.stem if path.suffix == ".py" else path.name
    return f"{path.parent.name}/{name}" if path.parent.name in CATEGORIES[1:] else name


@dataclass(frozen=True)
class ExtensionSource:
    """An extension as read once: its path and the exact bytes that were reviewed and will run.

    Attributes:
        path: The ``.py`` file or the package folder.
        files: ``(path, bytes)`` of the file itself, or of every ``*.py`` under the
            folder (sorted by relative path, ``__pycache__`` left out).
    """

    path: Path
    files: tuple[tuple[Path, bytes], ...]

    @property
    def is_package(self) -> bool:
        return not (len(self.files) == 1 and self.files[0][0] == self.path)

    @property
    def entry_path(self) -> Path:
        """The file holding ``register``: the file itself or ``<folder>/__init__.py``."""
        return self.path / "__init__.py" if self.is_package else self.path

    @property
    def entry(self) -> bytes:
        """The source of :attr:`entry_path`."""
        wanted = self.entry_path
        return next((data for path, data in self.files if path == wanted), b"")


def read_extensions(paths: Iterable[Path]) -> list[ExtensionSource]:
    """Read every file of the extensions at ``paths`` once, in order. Raises ``OSError`` if any cannot be read."""
    sources = []
    for path in paths:
        if path.is_dir():
            files = sorted(
                (p for p in path.rglob("*.py") if "__pycache__" not in p.relative_to(path).parts),
                key=lambda p: p.relative_to(path).as_posix(),
            )
            sources.append(ExtensionSource(path, tuple((p, p.read_bytes()) for p in files)))
        else:
            sources.append(ExtensionSource(path, ((path, path.read_bytes()),)))
    return sources


def files_digest(sources: Iterable[ExtensionSource]) -> str:
    """SHA-256 over the paths and contents of every file of ``sources`` in order: changes when a file is added, removed, renamed or edited."""
    digest = hashlib.sha256()
    for source in sources:
        for path, data in source.files:
            digest.update(path.as_posix().encode() + b"\0" + data + b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class ProjectSnapshot:
    """What a project asks to be trusted for, read once: its extensions and its trust-gated settings."""

    directory: Path
    """:func:`project_extensions_dir` of the project, unresolved (for display)."""
    key: str
    """``str(directory.resolve())``: the key in :data:`~minimal.config.TRUST_FILE`."""
    sources: tuple[ExtensionSource, ...]
    risky: dict
    """:func:`~minimal.config.risky_subset` of the project's settings layers."""
    digest: str
    """:func:`files_digest` of ``sources``, combined with ``risky`` when there is any."""

    @property
    def root(self) -> Path:
        """The project's ``.minimal`` folder (for display)."""
        return self.directory.parent

    @property
    def count(self) -> int:
        """Extensions plus one for the trust-gated settings, if any: what ``/trust`` would apply."""
        return len(self.sources) + (1 if self.risky else 0)


def read_project_snapshot(cwd: str, risky: dict) -> ProjectSnapshot | None:
    """Snapshot of ``cwd``'s project extensions and trust-gated settings ``risky``; ``None`` if there are neither.

    Raises:
        OSError: if an extension file cannot be read.
    """
    directory = project_extensions_dir(cwd)
    sources = tuple(read_extensions(extension_entries(directory)))  # the only read: what is hashed, reviewed and run
    if not sources and not risky:
        return None
    digest = files_digest(sources)
    if risky:
        payload = digest.encode() + b"\0settings\0" + json.dumps(risky, sort_keys=True).encode()
        digest = hashlib.sha256(payload).hexdigest()
    return ProjectSnapshot(directory, str(directory.resolve()), sources, risky, digest)


def category_dir(directory: Path, category: str) -> Path:
    """Where extensions of ``category`` live inside extensions folder ``directory``.

    Raises:
        ValueError: if ``category`` is not one of :data:`CATEGORIES`.
    """
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}; use one of {', '.join(CATEGORIES)}")
    return directory if category == "extensions" else directory / category


def category_entries(directory: Path, category: str) -> tuple[list[Path], list[Path]]:
    """``(entries, shadowed)`` of ``category``'s folder ``directory``.

    ``entries``: sorted ``*.py`` files and package folders (with ``__init__.py``) whose
    names do not start with ``_`` or ``.``; ``shadowed``: package folders left out
    because a ``.py`` file of the same name exists. In the ``extensions`` category a
    folder named like another category (``tools/``) is that category's folder, never a package.
    """
    if not directory.is_dir():
        return [], []
    reserved = CATEGORIES[1:] if category == "extensions" else ()
    files = {p.stem: p for p in directory.glob("*.py") if not p.name.startswith(("_", ".")) and p.is_file()}
    folders = {
        p.name: p
        for p in directory.iterdir()
        if p.is_dir()
        and not p.name.startswith(("_", "."))
        and p.name not in reserved
        and (p / "__init__.py").is_file()
    }
    shadowed = [folders.pop(name) for name in sorted(set(files) & set(folders))]
    return sorted([*files.values(), *folders.values()], key=lambda p: p.name), shadowed


def extension_entries(directory: Path) -> list[Path]:
    """The extensions in folder ``directory``, in load order: its own, then each category subfolder's."""
    return [p for category in CATEGORIES for p in category_entries(category_dir(directory, category), category)[0]]


def entry_conflicts(directory: Path) -> list[str]:
    """One error per ``x.py`` and ``x/`` pair in ``directory``'s category folders (only ``x.py`` loads)."""
    return [
        f"{folder}: both {folder.name}.py and {folder.name}/ exist; remove one"
        for category in CATEGORIES
        for folder in category_entries(category_dir(directory, category), category)[1]
    ]


def module_name(path: Path) -> str:
    """Module name of the extension at ``path``: ``minimal_ext_[<category>_]<name>_<hash of the resolved path>``.

    Each path gets its own namespace, so a global and a project (or ``--ext``) copy
    of ``tools/x`` never replace each other's snapshotted modules.
    """
    category = path.parent.name if path.parent.name in CATEGORIES[1:] else ""
    name = path.stem if path.suffix == ".py" else path.name
    where = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
    return f"minimal_ext_{category}_{name}_{where}" if category else f"minimal_ext_{name}_{where}"


class _SnapshotImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Imports extension modules from the bytes read into an :class:`ExtensionSource`, never from disk.

    The source is compiled directly instead of through the file system, whose
    ``__pycache__`` check (whole-second mtime and size) could run stale code when
    a file is edited twice within a second and reloaded, and which would run what
    is on disk now rather than what was reviewed. A module beneath a package
    extension that is not in its snapshot (a file added since) is not found, so a
    lazy import cannot reach bytes that were never reviewed.
    """

    def __init__(self) -> None:
        self.modules: dict[str, tuple[Path, bytes | None, bool]] = {}
        """Module name -> ``(path, source, is_package)``; ``source`` is ``None`` for a folder without ``__init__.py``."""
        self.packages: set[str] = set()
        """Names of the package extensions mapped: everything beneath them comes from :attr:`modules` only."""

    def map(self, name: str, source: ExtensionSource) -> None:
        """Serve module ``name`` (and, for a package, its submodules) from ``source``'s bytes."""
        for key in [k for k in self.modules if k == name or k.startswith(name + ".")]:
            del self.modules[key]
        self.packages.discard(name)
        if not source.is_package:
            self.modules[name] = (source.path, source.entry, False)
            return
        self.packages.add(name)
        for path, data in source.files:
            parts = path.relative_to(source.path).with_suffix("").parts
            is_package = parts[-1] == "__init__"
            dotted = ".".join(parts[:-1] if is_package else parts)
            self.modules[f"{name}.{dotted}" if dotted else name] = (path, data, is_package)
        for path, _ in source.files:
            folders = path.relative_to(source.path).parts[:-1]
            for depth in range(1, len(folders) + 1):
                namespace = f"{name}." + ".".join(folders[:depth])
                self.modules.setdefault(namespace, (source.path.joinpath(*folders[:depth]), None, True))

    def find_spec(self, fullname, path=None, target=None):
        entry = self.modules.get(fullname)
        if entry is None:
            if any(fullname.startswith(package + ".") for package in self.packages):
                raise ModuleNotFoundError(f"No module named {fullname!r} in the extension as read", name=fullname)
            return None
        origin = None if entry[1] is None else str(entry[0])
        return importlib.util.spec_from_loader(fullname, self, origin=origin, is_package=entry[2])

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        path, data, is_package = self.modules[module.__name__]
        if data is None:
            module.__path__ = [str(path)]
            return
        module.__file__ = str(path)
        if is_package:
            module.__path__ = [str(path.parent)]
        exec(compile(data.decode("utf-8"), str(path), "exec"), module.__dict__)


_IMPORTER = _SnapshotImporter()


def _import(source: ExtensionSource) -> object:
    """Import ``source`` afresh from its bytes and return the module."""
    if _IMPORTER not in sys.meta_path:
        sys.meta_path.insert(0, _IMPORTER)
    name = module_name(source.path)
    _IMPORTER.map(name, source)
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]
    return importlib.import_module(name)


def load_source(
    source: ExtensionSource,
    registry: Registry,
    settings: config.Settings,
    scope: str,
    errors: list[str],
    cwd: str,
    meta: Metadata | None = None,
) -> None:
    """Check ``source``'s ``__api__``, import it and call its ``register`` with an :class:`Extension`.

    Settings it rejects (:meth:`Extension.declare_settings`) are appended to ``errors``. ``cwd`` is the
    working directory, which picks the extension's project data folder (:meth:`Extension.data_dir`).
    ``meta`` is its metadata if the caller already read it (:func:`read_metadata`); ``None`` reads it here.

    Raises:
        ValueError: if it declares no ``__api__`` or one this minimal does not provide, a malformed
            ``__dependencies__``, or has no ``register`` function.
        Exception: whatever importing or ``register`` raised.
    """
    if meta is None:
        try:
            meta = read_metadata(source.entry, str(source.entry_path))
        except SourceSyntaxError:  # importing reports it as a SyntaxError with its line
            meta = Metadata("", "", "", False)
        else:
            check_api(meta.api)
    else:
        check_api(meta.api)
    module = _import(source)
    register = getattr(module, "register", None)
    if not callable(register):
        raise ValueError("missing register(minimal) function")
    register(
        Extension(
            id=ext_id(source.path),
            path=source.path,
            scope=scope,
            cwd=cwd,
            metadata=meta,
            registry=registry,
            settings=settings,
            errors=errors,
        )
    )


def _load_order(ids: list[str], depends: list[tuple[str, ...]]) -> tuple[list[int], dict[int, str]]:
    """``(order, cycles)``: the indexes of ``ids`` in load order, and a description of the cycle each member of one is in.

    The order is the given one, except that an extension comes after the ones it depends on that are also in
    ``ids``.
    """
    by_id: dict[str, list[int]] = {}
    for i, name in enumerate(ids):
        by_id.setdefault(name, []).append(i)
    state: dict[int, int] = {}
    order: list[int] = []
    cycles: dict[int, str] = {}

    def visit(i: int, stack: list[int]) -> None:
        if state.get(i) == 2:
            return
        if state.get(i) == 1:
            loop = stack[stack.index(i) :]
            text = " -> ".join(ids[k] for k in [*loop, i])
            cycles.update({k: text for k in loop})
            return
        state[i] = 1
        for dep in depends[i]:
            for j in by_id.get(dep, ()):
                if j != i:
                    visit(j, [*stack, i])
        state[i] = 2
        order.append(i)

    for i in range(len(ids)):
        visit(i, [])
    return order, cycles


def _skip_reason(
    name: str, meta: Metadata, loaded_meta: dict[str, Metadata], failed: set[str], cycle: str | None
) -> str | None:
    """Why the extension ``name`` cannot load now (a dependency missing or failed, a conflict, a cycle); ``None`` if it can."""
    if cycle:
        return f"DependencyError: dependency cycle {cycle}"
    for dep in meta.depends:
        if dep not in loaded_meta:
            if dep in failed:
                return f"DependencyError: needs {dep!r}, which did not load"
            return f"DependencyError: needs {dep!r}: install it with /ext (or enable it if it is in extensions.disabled)"
    for other in meta.conflicts:
        if other in loaded_meta:
            return f"ConflictError: conflicts with {other!r}, which is already loaded"
    for other, other_meta in loaded_meta.items():
        if name in other_meta.conflicts:
            return f"ConflictError: conflicts with {other!r}, which is already loaded and lists it in __conflicts__"
    return None


def load_sources(
    registry: Registry,
    sources: Iterable[tuple[ExtensionSource, str]],
    settings: config.Settings,
    cwd: str,
    loaded_meta: dict[str, Metadata] | None = None,
) -> tuple[list[Path], list[str]]:
    """Load each ``(source, scope)`` into ``registry``, in order, each one after the extensions it ``__depends__`` on.

    One whose dependency is missing or failed, whose ``__conflicts__`` names one already loaded (or that one
    names it) or that is in a dependency cycle is skipped with an error: the first loaded wins.

    Args:
        loaded_meta: Metadata of the extensions loaded so far by id; updated with these, so a later call
            (the bundled extensions, then the others) sees what loaded before.

    Returns:
        ``(loaded, errors)``: the paths that loaded, in load order, and one ``"<path>: <ErrorType>: <message>"``
        line per extension that failed or was skipped plus one per setting it rejected. A failed
        extension is skipped; anything it registered before failing stays registered.
    """
    loaded_meta = {} if loaded_meta is None else loaded_meta
    items = list(sources)
    ids = [ext_id(source.path) for source, _ in items]
    loaded: list[Path] = []
    errors: list[str] = []
    failed: set[str] = set()
    metas: list[Metadata | None] = []
    for (source, _), name in zip(items, ids):
        try:
            metas.append(read_metadata(source.entry, str(source.entry_path)))
        except SourceSyntaxError:  # importing reports it as a SyntaxError with its line
            metas.append(None)
        except ValueError as e:
            metas.append(None)
            errors.append(f"{source.path}: {type(e).__name__}: {e}")
            failed.add(name)
    order, cycles = _load_order(ids, [m.depends if m else () for m in metas])
    for i in order:
        source, scope = items[i]
        meta = metas[i]
        if meta is None and ids[i] in failed:
            continue  # its metadata was malformed: reported above
        if meta is not None and (why := _skip_reason(ids[i], meta, loaded_meta, failed, cycles.get(i))):
            errors.append(f"{source.path}: {why}")
            failed.add(ids[i])
            continue
        try:
            load_source(source, registry, settings, scope, errors, cwd, meta)
            loaded.append(source.path)
            loaded_meta[ids[i]] = meta or Metadata("", "", "", False)
        except Exception as e:
            errors.append(f"{source.path}: {type(e).__name__}: {e}")
            failed.add(ids[i])
    return loaded, errors


def load_bundled(
    registry: Registry, settings: config.Settings, cwd: str, loaded_meta: dict[str, Metadata] | None = None
) -> list[str]:
    """Load minimal's own extensions (:data:`BUNDLED_DIR`) into ``registry``, except the ``disabled`` ones.

    They are first-party code that ships with minimal, so they need no ``/trust``, are not part of the
    ``loaded`` list a reload reports, and are turned off like any extension, by id in ``extensions.disabled``.
    ``loaded_meta`` is as for :func:`load_sources`.

    Returns:
        One ``"<path>: <ErrorType>: <message>"`` line per one that failed, plus one per setting it rejected.
    """
    errors: list[str] = []
    sources: list[tuple[ExtensionSource, str]] = []
    for path in category_entries(BUNDLED_DIR, "extensions")[0]:
        if ext_id(path) in settings.disabled:
            continue
        try:
            sources += [(s, "bundled") for s in read_extensions([path])]
        except OSError as e:
            errors.append(f"{path}: {type(e).__name__}: {e}")
    return errors + load_sources(registry, sources, settings, cwd, loaded_meta)[1]


def load_extensions(
    registry: Registry,
    cwd: str,
    settings: config.Settings,
    extra: Iterable[str | Path] = (),
    project: Iterable[ExtensionSource] = (),
) -> tuple[list[Path], list[str]]:
    """Load minimal's bundled extensions (:func:`load_bundled`), then every extension found, into ``registry``.

    Args:
        registry: What each extension's ``register(minimal)`` registers into.
        cwd: Directory used to resolve ``extra``.
        settings: Its ``disabled`` ids are skipped in the global folder and ``project``.
        extra: Additional files or folders (the ``--ext`` option), never skipped.
        project: Project extensions the caller already reviewed, loaded from these
            bytes and never re-read from disk; the project folder is not scanned
            for extensions here. Empty means none (untrusted or absent).

    Returns:
        ``(loaded, errors)`` as from :func:`load_sources`, plus ``"<path>: not found"``
        for a missing ``extra`` entry and one error per ``x.py``/``x/`` clash.
    """
    loaded_meta: dict[str, Metadata] = {}
    errors: list[str] = load_bundled(registry, settings, cwd, loaded_meta)
    sources: list[tuple[ExtensionSource, str]] = []
    disabled = settings.disabled
    global_dir = global_extensions_dir()
    for path in extension_entries(global_dir):
        if ext_id(path) in disabled:
            continue
        try:
            sources += [(s, "global") for s in read_extensions([path])]
        except OSError as e:
            errors.append(f"{path}: {type(e).__name__}: {e}")
    errors += entry_conflicts(global_dir)
    project = list(project)
    sources += [(s, "project") for s in project if ext_id(s.path) not in disabled]
    if project:
        errors += entry_conflicts(project_extensions_dir(cwd))
    for entry in extra:
        path = Path(cwd, entry).resolve()
        if not path.exists():
            errors.append(f"{path}: not found")
            continue
        if path.is_dir() and not (path / "__init__.py").is_file():
            paths = extension_entries(path)
            errors += entry_conflicts(path)
        else:
            paths = [path]
        for p in paths:
            try:
                sources += [(s, "cli") for s in read_extensions([p])]
            except OSError as e:
                errors.append(f"{p}: {type(e).__name__}: {e}")
    loaded, load_errors = load_sources(registry, sources, settings, cwd, loaded_meta)
    return loaded, errors + load_errors


def reload_summary(loaded: list[Path], errors: list[str], notice: str | None) -> str:
    """Describe the result of :meth:`~minimal.core.app.App.reload_extensions` for the user or the model.

    ``notice`` is :meth:`~minimal.core.app.App.untrusted_notice`, added as a ``Not loaded:`` line when set.
    """
    summary = f"Loaded {len(loaded)} extension(s)"
    if loaded:
        summary += ": " + ", ".join(p.name for p in loaded)
    summary += "".join(f"\nFailed: {e}" for e in errors)
    if notice:
        summary += f"\nNot loaded: {notice}"
    return summary
