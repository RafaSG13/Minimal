"""One-time move of the old flat install layout into per-frontend folders.

Before frontends had their own folders, ``~/.minimal/extensions/`` held its extensions directly (``x.py``,
``x/``) plus ``themes/``, ``tools/`` and ``widgets/`` subfolders and one ``.lock.json``. Now each frontend has
``~/.minimal/extensions/<folder>/`` (:func:`~minimal.extensions.loader.frontend_folder`) laid out the same way, and
only the running frontend's folder loads.

:func:`migrate_global` moves what is left of the old layout: an extension without ``__frontend__`` is copied into the
folder of every frontend available here, one with ``__frontend__ = X`` only into ``X/``. A legacy copy is removed
only once every destination holds identical bytes, and its lock entry is carried into each destination's
``.lock.json``. Nothing is overwritten and nothing is deleted otherwise: an item that clashes with a different one
already in a destination stays where it was and is reported. The project's folder is never moved
(:func:`project_notice` only says what is not loaded any more).
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

from minimal.extensions import loader, marketplace
from minimal.extensions.metadata import read_metadata

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def legacy_items(base: Path) -> list[tuple[Path, str]]:
    """The ``(path, category)`` of every extension directly in ``base`` and its ``themes/``, ``tools/``, ``widgets/``: the old layout."""
    return [
        (path, category)
        for category in loader.CATEGORIES
        for path in loader.category_entries(loader.category_dir(base, category), category)[0]
    ]


def _declared_frontend(path: Path) -> str:
    """The ``__frontend__`` of the extension at ``path``; ``""`` if it sets none, a name that is not a plain one, or cannot be read."""
    entry = path / "__init__.py" if path.is_dir() else path
    try:
        frontend = read_metadata(entry.read_bytes(), str(entry)).frontend
    except (OSError, ValueError):
        return ""
    return frontend if marketplace.NAME_RE.fullmatch(frontend) else ""


def _tree(path: Path) -> dict[str, bytes]:
    """Relative path -> bytes of every file under ``path`` (a link as its target), or ``{"": bytes}`` for a file; caches left out."""
    if not path.is_dir() or path.is_symlink():
        return {"": os.readlink(path).encode() if path.is_symlink() else path.read_bytes()}
    found: dict[str, bytes] = {}
    for root, dirs, files in os.walk(path):
        links = [d for d in dirs if (Path(root) / d).is_symlink()]
        dirs[:] = [d for d in dirs if d != "__pycache__" and d not in links]
        for name in [*files, *links]:
            if name.endswith(".pyc"):
                continue
            file = Path(root) / name
            found[file.relative_to(path).as_posix()] = (
                os.readlink(file).encode() if file.is_symlink() else file.read_bytes()
            )
    return found


def _discard(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def _copy(src: Path, dest: Path) -> bool:
    """Make ``dest`` a copy of ``src``, never replacing anything; whether ``dest`` now holds ``src``'s bytes.

    The copy is made beside ``dest`` under a name the loader skips and renamed into place, so an interrupted
    run leaves either no ``dest`` or a complete one.
    """
    if os.path.lexists(dest):
        return _tree(src) == _tree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.with_name(f".migrating-{dest.name}")
    _discard(staging)
    if src.is_dir():
        shutil.copytree(src, staging, symlinks=True, ignore=_IGNORE)
    else:
        shutil.copy2(src, staging, follow_symlinks=False)
    os.replace(staging, dest)
    return True


def migrate_global(available: Callable[[], Iterable[str]]) -> str | None:
    """Move the old flat layout of ``~/.minimal/extensions`` into per-frontend folders; the notice to show, or ``None`` if there was nothing to move.

    ``available`` returns the folders of the frontends available here (``console``, ``textual`` if installed);
    it is only called when there is something to move. Failures while moving one item leave it in place
    and are part of the notice.
    """
    base = loader.GLOBAL_EXTENSIONS_DIR
    items = legacy_items(base)
    if not items:
        return None
    folders = list(dict.fromkeys(available()))
    legacy_lock_path = base / marketplace.LOCK_NAME
    legacy_lock = marketplace.read_lock(legacy_lock_path)
    locks: dict[str, dict[str, dict]] = {}
    moved: list[tuple[Path, str, str, list[str]]] = []
    notes: list[str] = []
    for path, category in items:
        ident = loader.ext_id(path)
        key = marketplace.lock_id(category, path.stem if path.suffix == ".py" else path.name)
        declared = _declared_frontend(path)
        wanted = [declared] if declared else folders
        try:
            clashes = [
                folder
                for folder in wanted
                if not _copy(path, loader.category_dir(base / folder, category) / path.name)
            ]
        except OSError as e:
            notes.append(f"  {ident}: left in place, could not copy it ({e})")
            continue
        if clashes:
            notes.append(f"  {ident}: left in place, a different copy is already in {', '.join(f'{c}/' for c in clashes)}")
            continue
        if key in legacy_lock:
            for folder in wanted:
                lock = locks.setdefault(folder, marketplace.read_lock(base / folder / marketplace.LOCK_NAME))
                lock.setdefault(key, legacy_lock[key])
        moved.append((path, ident, key, wanted))

    try:
        for folder, lock in locks.items():
            marketplace.write_lock(base / folder / marketplace.LOCK_NAME, lock, private=True)
    except OSError as e:
        return _notice(base, [], [*notes, f"  lock entries could not be carried over, so nothing was removed from {base} ({e})"])
    done: list[tuple[str, list[str]]] = []
    carried = False
    for path, ident, key, wanted in moved:
        try:
            _discard(path)
        except OSError as e:
            notes.append(f"  {ident}: copied to {', '.join(f'{w}/' for w in wanted)} but the old copy could not be removed ({e})")
            continue
        done.append((ident, wanted))
        if legacy_lock.pop(key, None) is not None:
            carried = True
    if carried:
        try:
            marketplace.write_lock(legacy_lock_path, legacy_lock, private=True)
        except OSError as e:
            notes.append(f"  {legacy_lock_path} could not be updated ({e})")
    for category in loader.CATEGORIES[1:]:
        try:
            os.rmdir(base / category)  # only if it is empty now
        except OSError:
            pass
    return _notice(base, done, notes)


def _notice(base: Path, done: list[tuple[str, list[str]]], notes: list[str]) -> str:
    lines = [f"minimal now keeps extensions per frontend, in {base}/<frontend>/. Moved the ones in {base}:"]
    lines += [f"  {ident} -> {', '.join(f'{w}/' for w in wanted)}" for ident, wanted in done]
    return "\n".join(lines + notes)


def project_notice(cwd: str) -> str | None:
    """What to tell the user about extensions still in the old flat layout of ``<cwd>/.minimal/extensions``; ``None`` if there are none.

    They are not loaded any more and are not moved: a project's folder belongs to its repository.
    """
    base = Path(cwd) / ".minimal" / "extensions"
    names = [loader.ext_id(path) for path, _ in legacy_items(base)]
    if not names:
        return None
    return (
        f"{base} holds extensions directly ({', '.join(names)}); minimal now loads only {base}/<frontend>/ "
        "(console or textual, the running frontend's), so these are not loaded. Move them there to use them again."
    )
