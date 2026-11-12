"""The extension marketplace: a git repository of shareable extensions.

Layout of the repository: one folder per category (:data:`~minimal.extensions.loader.CATEGORIES`):
``extensions/`` (general), ``themes/`` (colour themes), ``tools/`` (tools for the
model) and ``widgets/`` (terminal UI components), each holding ``<name>.py`` files
or ``<name>/`` package folders (with an ``__init__.py``). Installing mirrors the
category: ``themes/ocean.py`` goes to ``~/.minimal/extensions/themes/ocean.py``.
There is no index; each extension carries its own metadata
(:mod:`~minimal.extensions.metadata`), so concurrent publishes never conflict:

* description: first line of the module docstring (required);
* ``__version__ = "x"``: its version (required);
* ``__api__ = "1"``: the minimal API it is written for (required).

Versions come from the git history of the cached clone, not from its HEAD alone: the
candidates of an item are the commits that touched its path, newest first, and the
one offered and installed is the newest whose ``__api__`` this minimal can run
(:class:`Release`, :func:`releases`). HEAD's is used when it runs here; otherwise an
older commit's is extracted with ``git archive`` and installed from there. An item with
no such version anywhere is listed as incompatible and not installed.

Each extensions folder (global and project) has a ``.lock.json`` recording where every
marketplace extension in it came from: the marketplace, the commit, ``__version__``,
``__api__`` and the SHA-256 of the installed copy (:func:`_content_digest`). The loader
skips dot-files, so it is never loaded. A copy with a lock entry is outdated when its
commit is not the offered one, and modified when its files no longer match the lock
(:func:`catalog`); the picker pre-ticks only unmodified updates and :func:`install`
refuses to overwrite a modified copy unless ``force``. A copy without an entry (installed
by hand or by an older minimal) is compared by bytes.

Marketplaces are ``$MINIMAL_MARKETPLACE`` (if set), :data:`DEFAULT_URL` and
the ``marketplaces`` of ``~/.minimal/settings.json`` (:func:`marketplaces`); a
local path works as well as a git URL. Searching and installing use a cached
clone per marketplace under :data:`CACHE_ROOT`; the picker lists one through
:func:`catalog`. Publishing pushes a new ``publish/<name>-<timestamp>`` branch
and, when the ``gh`` CLI is installed, opens a pull request. :func:`check`
validates extensions, or a whole marketplace, without git (``minimal ext check``).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import minimal
from minimal import config
from minimal.extensions import audit, loader
from minimal.extensions.metadata import api_problem, read_metadata
from minimal.extensions.registry import Registry

DEFAULT_URL = "https://github.com/RafaSG13/minimal-marketplace.git"
CACHE_ROOT = Path.home() / ".minimal" / "marketplace"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_]*")
"""Valid extension names (always ``fullmatch``): no path separators, no leading ``_``."""
GIT_TIMEOUT = 120
"""Seconds a ``git`` or ``gh`` command may take before it is given up (a dead network must not hang ``/ext``)."""
_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


@dataclass
class Listing:
    """One extension in the marketplace (or about to be published)."""

    name: str
    description: str
    version: str
    path: Path
    """The ``.py`` file or package folder."""
    category: str
    """Its marketplace folder: one of :data:`~minimal.extensions.loader.CATEGORIES`."""
    api: str = ""
    """Its ``__api__``."""
    problem: str | None = None
    """Why this minimal cannot run it (:func:`~minimal.extensions.metadata.api_problem`); ``None`` if it can."""

    @property
    def compatible(self) -> bool:
        return self.problem is None


@dataclass
class Release:
    """The version of a marketplace extension that this minimal offers and installs (:func:`releases`)."""

    listing: Listing
    """That version's metadata. ``listing.path`` is where the item lives in the clone at HEAD; an older
    version's files exist only at :attr:`commit` (:func:`install_release` extracts them)."""
    commit: str
    """The commit of that version: the newest one touching the item whose ``__api__`` this minimal can run
    (HEAD's version if it can; the item's last commit if no version can)."""
    root: Path
    """The cached clone."""
    url: str
    """The marketplace it came from."""
    newer: Listing | None = None
    """HEAD's version when it is newer than the one offered because it needs another API; else ``None``."""

    @property
    def relpath(self) -> str:
        """The item's path inside the repository: ``<category>/<name>.py`` or ``<category>/<name>``."""
        return self.listing.path.relative_to(self.root).as_posix()


@dataclass
class Entry:
    """A marketplace extension and its local copies."""

    release: Release
    installed: list[str]
    """Scopes holding ``<name>.py`` or ``<name>/`` in the listing's category, ``"global"`` before ``"project"``."""
    outdated: list[str]
    """Scopes whose copy is not the offered version: another commit than the lock's, or other bytes if it has no lock entry."""
    modified: list[str]
    """Scopes whose copy has local changes: its files no longer match the lock entry's ``sha256``."""
    installed_version: str
    """Version of the first installed copy; ``""`` if none, unset or unreadable."""

    @property
    def listing(self) -> Listing:
        return self.release.listing


def marketplace_url() -> str:
    """The marketplace repository: ``$MINIMAL_MARKETPLACE``, else :data:`DEFAULT_URL`."""
    return os.environ.get("MINIMAL_MARKETPLACE") or DEFAULT_URL


def _saved_marketplaces() -> list[str]:
    """The ``marketplaces`` of ``~/.minimal/settings.json``, in the order they were added."""
    return list(config.load_global().get("marketplaces", []))


def marketplaces() -> list[str]:
    """Marketplaces to offer: :func:`marketplace_url`, :data:`DEFAULT_URL`, then the saved ones."""
    return list(dict.fromkeys([marketplace_url(), DEFAULT_URL, *_saved_marketplaces()]))


def add_marketplace(url: str) -> str:
    """Save a marketplace after checking that it can be cloned; return it as saved.

    Existing local paths are saved as absolute paths.

    Raises:
        ValueError: if ``url`` is empty or already listed.
        RuntimeError: if git cannot clone it.
    """
    url = url.strip()
    if not url:
        raise ValueError("enter a git URL or a local path")
    if Path(url).expanduser().exists():
        url = str(Path(url).expanduser().resolve())
    if url in marketplaces():
        raise ValueError(f"{url} is already in the list")
    sync(url)
    config.update_global({"marketplaces": [*_saved_marketplaces(), url]})
    return url


def remove_marketplace(url: str) -> None:
    """Forget a saved marketplace.

    Raises:
        ValueError: if ``url`` was not saved (the default and ``$MINIMAL_MARKETPLACE`` cannot be removed).
    """
    saved = _saved_marketplaces()
    if url not in saved:
        raise ValueError(f"{url} is not a saved marketplace")
    config.update_global({"marketplaces": [u for u in saved if u != url]})


def _run_git(args: tuple[str, ...], cwd: Path | None) -> bytes:
    """Run ``git`` and return its stdout. Credential prompts fail instead of hanging.

    Raises:
        RuntimeError: if git is missing, exits with an error or takes longer than :data:`GIT_TIMEOUT`.
    """
    if shutil.which("git") is None:
        raise RuntimeError("git is not installed; the marketplace needs it")
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {args[0]} timed out after {GIT_TIMEOUT}s") from None
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip()).decode(errors="replace")
        raise RuntimeError(f"git {args[0]} failed: {detail}")
    return result.stdout


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run ``git`` and return its stdout as text (:func:`_run_git`)."""
    return _run_git(args, cwd).decode(errors="replace")


def _check_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise ValueError(f"invalid extension name {name!r}: use lowercase letters, digits and _")


def _entry_file(path: Path) -> Path:
    """The file holding ``register``: ``path`` itself or ``<path>/__init__.py`` for a package folder."""
    return path / "__init__.py" if path.is_dir() else path


def _category_of(path: Path) -> str:
    return path.parent.name if path.parent.name in loader.CATEGORIES else "extensions"


def describe(path: Path, name: str | None = None) -> Listing:
    """Read an extension's metadata without running it.

    Its category is the name of its folder when that is a category
    (``themes/ocean.py`` is a theme), else ``extensions``.

    Args:
        path: The extension file or package folder.
        name: Name to publish it under (default: the file name without ``.py``, or the folder name).

    Raises:
        ValueError: on a syntax error, an invalid name, a missing ``register``
            function, module docstring, ``__version__`` or ``__api__``, an invalid ``__api__``,
            a package named like a category folder, or a package symlink to a folder or outside it.
    """
    entry = _entry_file(path)
    try:
        source = entry.read_bytes()
    except OSError as e:
        raise ValueError(f"{path}: cannot be read ({e})") from None
    meta = read_metadata(source, str(entry))
    name = (path.name if path.is_dir() else path.stem) if name is None else name
    _check_name(name)
    if not meta.has_register:
        raise ValueError(f"{path}: missing a top-level register(minimal) function")
    if not meta.description:
        raise ValueError(f"{path}: add a module docstring; its first line is the marketplace description")
    if not meta.version:
        raise ValueError(f'{path}: set __version__ = "1.0"; the marketplace needs a version')
    if not meta.api:
        raise ValueError(f'{path}: set __api__ = "{minimal.API_VERSION.split(".")[0]}"; the minimal API it is written for')
    problem = api_problem(meta.api)
    if problem is not None and problem.startswith("invalid"):
        raise ValueError(f"{path}: {problem}")
    category = _category_of(path)
    _check_package_name(path, name, category)
    if path.is_dir():
        _copied_files(path)  # validates its symlinks
    return Listing(name, meta.description, meta.version, path, category, meta.api, problem)


def format_listing(listing: Listing) -> str:
    """One line: ``name version — description``."""
    head = f"{listing.name} {listing.version}" if listing.version else listing.name
    return f"{head} — {listing.description}"


def _incompatible(listing: Listing) -> str:
    return f"{listing.name}: {listing.problem}"


def sync(url: str | None = None) -> Path:
    """Update (or create) the cached clone of the marketplace and return its folder.

    If the update fails but an older copy exists, the older copy is used.

    Raises:
        RuntimeError: if git fails and there is no cached copy.
    """
    url = url or marketplace_url()
    target = CACHE_ROOT / hashlib.sha1(url.encode()).hexdigest()[:12]
    if (target / ".git").is_dir():
        try:
            _git("pull", "--ff-only", "--quiet", cwd=target)
            return target
        except RuntimeError:
            pass
    fresh = target.with_name(target.name + ".new")
    shutil.rmtree(fresh, ignore_errors=True)
    fresh.parent.mkdir(parents=True, exist_ok=True)
    try:
        _git("clone", "--quiet", "--", url, str(fresh))  # "--": a URL starting with "-" is not an option
    except RuntimeError:
        if (target / ".git").is_dir():
            print(f"[marketplace] could not update {url}; using the cached copy", file=sys.stderr)
            return target
        raise
    shutil.rmtree(target, ignore_errors=True)
    fresh.rename(target)
    return target


def releases(query: str = "", url: str | None = None, category: str | None = None) -> list[Release]:
    """The offered version (:class:`Release`) of each extension whose name or description contains ``query`` (case-insensitive); all if empty.

    Listed by category in :data:`~minimal.extensions.loader.CATEGORIES` order, then by
    name; only ``category``'s if given. Extensions whose HEAD has invalid metadata are skipped.
    The description is the offered version's.

    Raises:
        ValueError: if ``category`` is not one of :data:`~minimal.extensions.loader.CATEGORIES`.
        RuntimeError: if git fails.
    """
    if category:
        loader.category_dir(Path(), category)  # validates it: the folder name must not be a path
    url = url or marketplace_url()
    root = sync(url)
    needle = query.lower()
    found = []
    for folder in [category] if category else loader.CATEGORIES:
        for path in loader.category_entries(root / folder, folder)[0]:
            try:
                release = _resolve(root, url, path)
            except ValueError:
                continue
            listing = release.listing
            if needle in listing.name.lower() or needle in listing.description.lower():
                found.append(release)
    return found


def search(query: str = "", url: str | None = None, category: str | None = None) -> list[Listing]:
    """The offered version of each extension matching ``query``: :func:`releases`' listings."""
    return [release.listing for release in releases(query, url, category)]


@contextlib.contextmanager
def _materialised(root: Path, commit: str, relpath: str) -> Iterator[Path]:
    """Extract ``relpath`` as of ``commit`` of clone ``root`` into a temporary folder; yield the folder (``<it>/<relpath>`` is the item).

    Raises:
        RuntimeError: if git fails, for instance because ``relpath`` did not exist at ``commit``.
        ValueError: if the archive cannot be extracted safely (``tarfile``'s ``data`` filter).
    """
    archive = _run_git(("archive", "--format=tar", commit, "--", relpath), root)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(tmp, filter="data")
        except tarfile.TarError as e:
            raise ValueError(f"{relpath} at {commit[:12]}: cannot be extracted ({e})") from None
        yield Path(tmp)


def _resolve(root: Path, url: str, path: Path) -> Release:
    """The version of the item at ``path`` of clone ``root`` to offer: the newest commit touching it whose ``__api__`` this minimal runs.

    HEAD's version if it runs here (one ``git log -1`` for its commit). Otherwise each older commit
    that touched the path is tried, newest first; if none runs here, HEAD's incompatible listing is returned.

    Raises:
        ValueError: if the item's HEAD is not a valid extension (:func:`describe`).
        RuntimeError: if git fails.
    """
    head = describe(path)
    relpath = path.relative_to(root).as_posix()
    if head.compatible:
        commit = _git("log", "-1", "--format=%H", "--", relpath, cwd=root).strip()
        return Release(head, commit, root, url)
    commits = _git("log", "--format=%H", "--", relpath, cwd=root).split()
    for commit in commits[1:]:  # the first one is HEAD's version
        try:
            with _materialised(root, commit, relpath) as tmp:
                older = describe(tmp / relpath)
        except (RuntimeError, ValueError):  # deleted or invalid at that commit: not a candidate
            continue
        if older.compatible:
            return Release(replace(older, path=path), commit, root, url, newer=head)
    return Release(head, commits[0] if commits else "", root, url)


def _copied_files(root: Path) -> list[Path]:
    """The files ``shutil.copytree(root, …, ignore=_COPY_IGNORE)`` copies, sorted by path inside ``root``.

    Raises:
        ValueError: if a symlink points to a folder, or to a file outside ``root``:
            copying would materialize what it points to (``~/.minimal/keys.json``, say).
    """
    inside = root.resolve()
    files, folders = [], [root]
    while folders:
        folder = folders.pop()
        names = os.listdir(folder)
        skipped = _COPY_IGNORE(str(folder), names)
        for name in names:
            if name in skipped:
                continue
            path = folder / name
            if path.is_symlink():
                target = path.resolve()
                if not target.is_file() or not target.is_relative_to(inside):
                    raise ValueError(f"{path}: links to {target}; a package may only link to files inside it")
            (folders if path.is_dir() else files).append(path)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def _content_digest(path: Path) -> str:
    """SHA-256 of an extension as installed or published: a file's bytes, or every copied file of a package by its path inside it.

    Equal for identical copies in different places, and for a file published under another name.

    Raises:
        ValueError: if a package has a symlink :func:`_copied_files` refuses.
    """
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    digest = hashlib.sha256()
    for file in _copied_files(path):
        digest.update(file.relative_to(path).as_posix().encode() + b"\0" + file.read_bytes() + b"\0")
    return digest.hexdigest()


def _is_extension(path: Path) -> bool:
    """Whether ``path`` is an extension: a ``.py`` file or a package folder (a category folder such as ``tools/`` is neither)."""
    return (path.suffix == ".py" and path.is_file()) or (path / "__init__.py").is_file()


def _check_package_name(path: Path, name: str, category: str) -> None:
    """Refuse a package that would sit where a category folder lives (``extensions/tools/``)."""
    if path.is_dir() and category == "extensions" and name in loader.CATEGORIES[1:]:
        raise ValueError(f"{path}: a package in extensions cannot be named {name}; that is the {name}/ category folder")


def _installed_path(scope: str, cwd: str, category: str, name: str) -> Path | None:
    """The installed copy of ``name`` in ``scope``'s ``category`` folder (the ``.py`` file wins); ``None`` if none."""
    folder = _target_dir(scope, cwd, category)
    return next((path for path in (folder / f"{name}.py", folder / name) if _is_extension(path)), None)


def _digest_or_none(path: Path) -> str | None:
    """:func:`_content_digest` of ``path``; ``None`` if it has a symlink :func:`_copied_files` refuses."""
    try:
        return _content_digest(path)
    except ValueError:
        return None


def _release_digest(release: Release) -> str:
    """:func:`_content_digest` of the offered version, extracting it if it is not HEAD's."""
    if release.newer is None:
        return _content_digest(release.listing.path)
    with _materialised(release.root, release.commit, release.relpath) as tmp:
        return _content_digest(tmp / release.relpath)


LOCK_NAME = ".lock.json"
"""Where each extensions folder records the marketplace extensions installed in it (a dot-file: never loaded)."""


def _lock_id(category: str, name: str) -> str:
    """Key of an extension in the lock: its id (:func:`~minimal.extensions.loader.ext_id`): ``name`` or ``category/name``."""
    return name if category == "extensions" else f"{category}/{name}"


def _lock_path(scope: str, cwd: str) -> Path:
    return _target_dir(scope, cwd) / LOCK_NAME


def _read_lock(scope: str, cwd: str) -> dict[str, dict]:
    """The lock of ``scope``; empty if it is missing or unreadable (the next install rewrites it)."""
    try:
        data = json.loads(_lock_path(scope, cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, dict)} if isinstance(data, dict) else {}


def _lock_entry(scope: str, cwd: str, category: str, name: str) -> dict | None:
    """The lock's entry for ``name``; ``None`` if there is none or it lacks a string ``commit`` or ``sha256``."""
    entry = _read_lock(scope, cwd).get(_lock_id(category, name))
    if entry is None or not isinstance(entry.get("commit"), str) or not isinstance(entry.get("sha256"), str):
        return None
    return entry


def _write_lock(scope: str, cwd: str, lock: dict[str, dict]) -> None:
    """Replace the lock of ``scope`` atomically (private, mode 0600, in the global folder); delete it if ``lock`` is empty."""
    path = _lock_path(scope, cwd)
    if not lock:
        _remove(path)
    elif scope == "global":
        config.write_private(path, lock)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(lock, indent=2))
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


def _is_modified(scope: str, cwd: str, category: str, name: str) -> bool:
    """Whether the installed copy of ``name`` has local changes: it has a lock entry its files no longer match."""
    path = _installed_path(scope, cwd, category, name)
    entry = _lock_entry(scope, cwd, category, name)
    return path is not None and entry is not None and _digest_or_none(path) != entry["sha256"]


def catalog(url: str, cwd: str) -> list[Entry]:
    """Every extension in marketplace ``url`` (as offered, :func:`releases`) with its installed copies (global and ``cwd``'s project).

    A copy is outdated when its lock entry from ``url`` names another commit than the offered one;
    without such an entry its files are compared with the offered version's.
    """
    entries = []
    for release in releases("", url):
        listing = release.listing
        installed, outdated, modified, version = [], [], [], ""
        wanted = None
        for scope in ("global", "project"):
            path = _installed_path(scope, cwd, listing.category, listing.name)
            if path is None:
                continue
            installed.append(scope)
            current = _digest_or_none(path)  # None: a symlink was added to the installed copy
            lock = _lock_entry(scope, cwd, listing.category, listing.name)
            if lock is not None and current != lock["sha256"]:
                modified.append(scope)
            if lock is not None and lock.get("marketplace") == release.url:
                stale = lock["commit"] != release.commit
            else:
                wanted = wanted or _release_digest(release)
                stale = current != wanted
            if stale:
                outdated.append(scope)
            if len(installed) == 1:
                try:
                    version = describe(path).version
                except ValueError:
                    pass
        entries.append(Entry(release, installed, outdated, modified, version))
    return entries


def _target_dir(scope: str, cwd: str, category: str = "extensions") -> Path:
    """Folder for ``category`` extensions in ``scope``: ``"global"`` or ``"project"``."""
    if scope == "global":
        return loader.category_dir(loader.GLOBAL_EXTENSIONS_DIR, category)
    if scope == "project":
        return loader.category_dir(loader.project_extensions_dir(cwd), category)
    raise ValueError("scope must be global or project")


def _find(root: Path, name: str, category: str | None) -> list[Path]:
    """``name``'s file or package folder in each category (or only ``category``) of marketplace clone ``root``."""
    found = []
    for folder in [category] if category else loader.CATEGORIES:
        path = next((p for p in (root / folder / f"{name}.py", root / folder / name) if _is_extension(p)), None)
        if path is not None:
            found.append(path)
    return found


def _find_release(name: str, url: str | None, category: str | None) -> Release:
    """The offered version (:func:`_resolve`) of ``name`` in the marketplace at ``url`` (default: :func:`marketplace_url`).

    Raises:
        ValueError: if the name or category is invalid, or the name is not in the marketplace or is in several categories.
    """
    if category:
        loader.category_dir(Path(), category)  # validates it: the folder name must not be a path
    _check_name(name)
    url = url or marketplace_url()
    root = sync(url)
    found = _find(root, name, category)
    if not found:
        raise ValueError(f"{name} is not in the marketplace (open /ext to browse it)")
    if len(found) > 1:
        raise ValueError(f"{name} is in several categories ({', '.join(p.parent.name for p in found)}); pick one")
    return _resolve(root, url, found[0])


def _check_installable(release: Release, scope: str, cwd: str, force: bool) -> None:
    """Raise ``ValueError`` if no version of ``release`` runs here, or installing would overwrite local changes without ``force``."""
    listing = release.listing
    if not listing.compatible:
        raise ValueError(_incompatible(listing))
    if not force and _is_modified(scope, cwd, listing.category, listing.name):
        raise ValueError(
            f"{listing.name} has local changes in {_installed_path(scope, cwd, listing.category, listing.name)}; "
            "installing would overwrite them (pass force to do it)"
        )


def confirm_global(releases: Iterable[Release], ask: Callable[[str], bool]) -> bool:
    """Print the static scan of marketplace extensions about to be installed into the global folder, then ask whether to go on.

    The scan covers the version that would be installed (an older commit's files when that is the one offered).
    Global extensions run in every project without ``/trust``, so this is the user's only look at the code first.
    ``ask`` is a yes/no question to the user; a refusal means nothing is installed.
    """
    with contextlib.ExitStack() as stack:
        sources = [stack.enter_context(release_source(release)) for release in releases]
        print(audit.format_install_report(loader.read_extensions(sources)))
    return ask("Allow these extensions to run with your permissions in every project? Review the scan above.")


def install(
    name: str,
    scope: str = "global",
    cwd: str = ".",
    url: str | None = None,
    category: str | None = None,
    force: bool = False,
) -> tuple[Path, bool]:
    """Copy the offered version of extension ``name`` (:class:`Release`) from the marketplace into the ``scope`` folder of its category.

    It is not loaded; reload the extensions to activate it.

    Args:
        category: Where to look; needed only when several categories have ``name``.
        force: Overwrite an installed copy that has local changes.

    Returns:
        ``(dest, updated)``: the installed file or folder, and whether it replaced an existing copy.

    Raises:
        ValueError: if the name or category is invalid, the name is not in the marketplace or in
            several categories, no version of the extension is written for an API this minimal provides,
            or the installed copy has local changes and ``force`` is false.
    """
    release = _find_release(name, url, category)
    _check_installable(release, scope, cwd, force)
    return install_release(release, scope, cwd)


@contextlib.contextmanager
def release_source(release: Release) -> Iterator[Path]:
    """The files of ``release``: its path in the clone if it is HEAD's, else a temporary extraction of its commit."""
    if release.newer is None:
        yield release.listing.path
        return
    with _materialised(release.root, release.commit, release.relpath) as tmp:
        yield tmp / release.relpath


def install_release(release: Release, scope: str, cwd: str) -> tuple[Path, bool]:
    """Install ``release`` (see :func:`install_from`), extracting it first if it is not HEAD's, and record it in the lock.

    Overwrites local changes: :func:`install` is what asks for ``force``.
    """
    with release_source(release) as src:
        return install_from(src, scope, cwd, (release.url, release.commit))


def install_from(src: Path, scope: str, cwd: str, origin: tuple[str, str] | None = None) -> tuple[Path, bool]:
    """Copy marketplace extension ``src`` (a file or package folder of a synced clone, or extracted from one) into ``scope``'s category folder.

    A copy in the other form (``x.py`` for a folder ``x/``, or the reverse) is removed. The lock of
    ``scope`` gets an entry for it when ``origin`` ``(marketplace URL, commit)`` is given; without
    it the copy no longer comes from the marketplace and its entry, if any, is dropped.

    Returns:
        ``(dest, updated)``: the installed file or folder, and whether it replaced an existing copy.

    Raises:
        ValueError: if the extension is written for an API this minimal does not provide, or its place
            is taken by a folder that is not an extension (such as the ``tools/`` category).
    """
    listing = describe(src)
    if not listing.compatible:
        raise ValueError(_incompatible(listing))
    folder = _target_dir(scope, cwd, src.parent.name)
    dest = folder / src.name
    other = folder / listing.name if src.is_file() else folder / f"{listing.name}.py"
    _check_replaceable(dest)
    updated = _is_extension(dest) or _is_extension(other)
    folder.mkdir(parents=True, exist_ok=True)
    if _is_extension(other):
        _remove(other)
    if src.is_dir():
        _remove(dest)
        shutil.copytree(src, dest, ignore=_COPY_IGNORE)
    else:
        shutil.copyfile(src, dest)
    lock = _read_lock(scope, cwd)
    key = _lock_id(listing.category, listing.name)
    if origin is not None:
        url, commit = origin
        lock[key] = {
            "marketplace": url,
            "commit": commit,
            "version": listing.version,
            "api": listing.api,
            "sha256": _content_digest(dest),
        }
        _write_lock(scope, cwd, lock)
    elif lock.pop(key, None) is not None:
        _write_lock(scope, cwd, lock)
    return dest, updated


def _check_replaceable(dest: Path) -> None:
    """Refuse to overwrite ``dest`` if it is a folder but not an extension package: it may hold other extensions."""
    if dest.is_dir() and not _is_extension(dest):
        raise ValueError(f"{dest} is a folder that is not an extension package; not replacing it")


def _remove(path: Path) -> None:
    """Delete file or folder ``path`` if it exists."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def uninstall(name: str, scope: str = "global", cwd: str = ".", category: str = "extensions") -> Path:
    """Delete an installed extension (file or package folder), drop its lock entry and return its path.

    Raises:
        ValueError: if the name is invalid or the extension is not installed in ``scope``'s ``category`` folder.
    """
    _check_name(name)
    folder = _target_dir(scope, cwd, category)
    removed = None
    for path in (folder / f"{name}.py", folder / name):
        if _is_extension(path):
            _remove(path)
            removed = removed or path
    if removed is None:
        raise ValueError(f"{name} is not installed in {folder}")
    lock = _read_lock(scope, cwd)
    if lock.pop(_lock_id(category, name), None) is not None:
        _write_lock(scope, cwd, lock)
    return removed


def installed(cwd: str) -> list[tuple[str, str, Path]]:
    """Installed extensions (files and package folders) as ``(scope, category, path)``: global ones first, then project ones."""
    return [
        (scope, category, path)
        for scope in ("global", "project")
        for category in loader.CATEGORIES
        for path in loader.category_entries(_target_dir(scope, cwd, category), category)[0]
    ]


def publish(path: str | Path, name: str | None = None, url: str | None = None, category: str | None = None) -> str:
    """Propose an extension to the marketplace on a new branch, opening a pull request if ``gh`` is installed.

    Never force-pushes: the branch name carries a timestamp, so it is always new.

    Args:
        path: The extension file or package folder.
        category: Marketplace folder (default: the extension's own folder if it is a
            category, such as ``~/.minimal/extensions/themes``, else ``extensions``).

    Returns:
        What happened, for the user.

    Raises:
        ValueError: if the extension is missing, invalid, or already published unchanged, or the category is unknown.
        RuntimeError: if a git command fails (for example, no push access).
    """
    path = Path(path).resolve()
    if not path.exists():
        raise ValueError(f"{path} not found")
    listing = describe(path, name)
    category = category or listing.category
    loader.category_dir(Path(), category)  # validates it
    _check_package_name(path, listing.name, category)
    url = url or marketplace_url()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _git("clone", "--quiet", "--", url, tmp)  # "--": a URL starting with "-" is not an option
        folder = Path(tmp, category)
        dest = folder / (listing.name if path.is_dir() else f"{listing.name}.py")
        other = folder / (f"{listing.name}.py" if path.is_dir() else listing.name)
        if _is_extension(dest) and not _is_extension(other) and _content_digest(dest) == _content_digest(path):
            raise ValueError(f"{listing.name} is already published with identical content")
        _check_replaceable(dest)
        folder.mkdir(parents=True, exist_ok=True)
        replaced = [p for p in (dest, other) if _is_extension(p)]
        for p in replaced:
            _remove(p)
        if path.is_dir():
            shutil.copytree(path, dest, ignore=_COPY_IGNORE)
        else:
            shutil.copyfile(path, dest)
        branch = f"publish/{listing.name}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
        title = f"Publish {listing.name} {listing.version}".strip()
        _git("checkout", "-b", branch, cwd=Path(tmp))
        changed = {dest, *replaced}
        _git("add", "-A", "--", *(p.relative_to(tmp).as_posix() for p in changed), cwd=Path(tmp))
        _git("commit", "--quiet", "-m", title, cwd=Path(tmp))
        _git("push", "--quiet", "origin", branch, cwd=Path(tmp))
        if shutil.which("gh") is None:
            return f"Pushed branch {branch} to {url}; open a pull request to publish it."
        try:
            result = subprocess.run(
                ["gh", "pr", "create", "--head", branch, "--title", title, "--body", listing.description],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return f"Pushed branch {branch} to {url}; gh pr create timed out"
        if result.returncode == 0:
            return f"Opened pull request {result.stdout.strip()}"
        return f"Pushed branch {branch} to {url}; gh pr create failed: {result.stderr.strip()}"


# --- validation ---------------------------------------------------------------


def check(paths: list[Path], make_registry: Callable[[], Registry] = Registry) -> tuple[list[str], bool]:
    """Validate extensions the way the marketplace and the loader see them, for ``minimal ext check``.

    Each path is an extension file or package folder, or a marketplace root (a
    folder holding any category folder), whose every extension is checked. Each
    extension must describe itself (:func:`describe`), accept this minimal
    (``__api__``) and load into a fresh registry from ``make_registry``.

    Returns:
        ``(lines, ok)``: ``ok   category/name version``, ``WARN category/name: needs <module>``
        when it imports a module that is not installed here, or ``FAIL category/name: problem``;
        ``ok`` is false if any line is ``FAIL``.
    """
    lines: list[str] = []
    for path in paths:
        path = path.resolve()
        if not path.exists():
            lines.append(f"FAIL {path}: not found")
        elif path.is_dir() and not (path / "__init__.py").is_file() and any(
            (path / c).is_dir() for c in loader.CATEGORIES
        ):
            for category in loader.CATEGORIES:
                entries, shadowed = loader.category_entries(path / category, category)
                lines += [f"FAIL {category}/{p.name}: both {p.name}.py and {p.name}/ exist; remove one" for p in shadowed]
                lines += [_check_one(p, make_registry) for p in entries]
        else:
            lines.append(_check_one(path, make_registry))
    return lines, not any(line.startswith("FAIL") for line in lines)


def _check_one(path: Path, make_registry: Callable[[], Registry]) -> str:
    label = f"{_category_of(path)}/{path.name if path.is_dir() else path.stem}"
    try:
        listing = describe(path)
    except ValueError as e:
        return f"FAIL {label}: {str(e).removeprefix(f'{path}: ')}"
    if not listing.compatible:
        return f"FAIL {label}: {listing.problem}"
    errors: list[str] = []
    try:
        source = loader.read_extensions([path])[0]
        loader.load_source(source, make_registry(), config.Settings(), "cli", errors)
    except ModuleNotFoundError as e:
        if e.name and not e.name.startswith(("minimal", loader.module_name(path))):
            return f"WARN {label}: needs {e.name}"
        return f"FAIL {label}: {type(e).__name__}: {e}"
    except Exception as e:
        return f"FAIL {label}: {type(e).__name__}: {e}"
    if errors:
        return f"FAIL {label}: {'; '.join(errors)}"
    return f"ok   {label} {listing.version}"


# --- tools for the model -----------------------------------------------------


def _known_marketplace(url: str | None) -> str | None:
    if url is not None and url not in marketplaces():
        raise ValueError("unknown marketplace; add it with /ext first")
    return url


def _ext_search(input: dict, ctx: Any) -> str:
    category = input.get("category")
    if category:
        loader.category_dir(Path(), category)  # validates it before any marketplace is synced
    urls = marketplaces()
    lines = []
    for url in urls:
        try:
            found = releases(input.get("query", ""), url, category)
        except (RuntimeError, ValueError) as e:
            lines.append(f"({url}: {e})")
            continue
        prefix = f"{url} " if len(urls) > 1 else ""
        for release in found:
            x = release.listing
            note = "" if x.compatible else f" [{x.problem}]"
            if release.newer is not None:
                note = f" [latest for this minimal; newer needs API {release.newer.api}]"
            lines.append(f"{prefix}{x.category}/{format_listing(x)}{note}")
    return "\n".join(lines) or "No extensions found."


def _ext_install(input: dict, ctx: Any) -> str:
    name, category = input["name"], input.get("category")
    url = _known_marketplace(input.get("marketplace"))
    if url is None:
        _check_name(name)
        if category:
            loader.category_dir(Path(), category)
        having = []
        for candidate in marketplaces():
            try:
                if _find(sync(candidate), name, category):
                    having.append(candidate)
            except RuntimeError:
                continue
        if len(having) > 1:
            raise ValueError(f"{name} is in several marketplaces ({', '.join(having)}); pass marketplace")
        url = having[0] if having else None
    scope = input.get("scope", "global")
    release = _find_release(name, url, category)
    _check_installable(release, scope, ctx.cwd, bool(input.get("force")))
    if scope == "global" and not confirm_global([release], ctx.ui.ask):
        raise ValueError(f"{name} was not installed: the user did not approve it")
    dest, updated = install_release(release, scope, ctx.cwd)
    return f"{'Updated' if updated else 'Installed'} {name} at {dest}. Call reload_extensions to activate it."


def _ext_publish(input: dict, ctx: Any) -> str:
    url = _known_marketplace(input.get("marketplace")) or marketplace_url()
    return publish(Path(ctx.cwd, input["path"]), input.get("name"), url=url, category=input.get("category"))


_CATEGORY_SCHEMA = {
    "type": "string",
    "enum": list(loader.CATEGORIES),
    "description": "extensions (general), themes (colour themes), tools (tools for you) or widgets (UI components)",
}

_MARKETPLACE_SCHEMA = {"type": "string", "description": "Marketplace URL, as listed by ext_search"}


def register_marketplace_tools(registry: Registry) -> None:
    """Add ``ext_search``, ``ext_install`` and ``ext_publish`` to ``registry``."""
    registry.register_tool(
        name="ext_search",
        run=_ext_search,
        description=(
            "Search every configured minimal extension marketplace. Returns one line per extension: "
            "category/name, version and description, prefixed by the marketplace when there are several. "
            "Check it before writing a new extension; one may already exist."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Words to match in the name or description; empty lists everything",
                },
                "category": _CATEGORY_SCHEMA,
            },
        },
    )
    registry.register_tool(
        name="ext_install",
        run=_ext_install,
        description=(
            "Install an extension from a marketplace into the global (default) or project extensions folder, "
            "in the subfolder of its category: the newest version written for this minimal's API. It is not "
            "active until you call reload_extensions. An installed copy with local changes is not overwritten "
            "unless force is true."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Extension name, as listed by ext_search"},
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "global (every project, default) or project (this folder only)",
                },
                "category": {**_CATEGORY_SCHEMA, "description": "Needed only if the name is in several categories"},
                "marketplace": {**_MARKETPLACE_SCHEMA, "description": "Needed only if several marketplaces have the name"},
                "force": {
                    "type": "boolean",
                    "description": "Overwrite an installed copy that has local changes (default false)",
                },
            },
            "required": ["name"],
        },
        confirm=True,
    )
    registry.register_tool(
        name="ext_publish",
        run=_ext_publish,
        description=(
            "Publish an extension file or package folder to a marketplace: pushes a new branch and opens a pull "
            "request. It must define register(minimal), start with a docstring (its first line is the description) "
            "and set __version__. It goes in the category of its folder (for example ~/.minimal/extensions/themes) "
            "unless one is given."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The extension file or package folder"},
                "name": {"type": "string", "description": "Name to publish under (default: the file name)"},
                "category": _CATEGORY_SCHEMA,
                "marketplace": {**_MARKETPLACE_SCHEMA, "description": "Where to publish (default: $MINIMAL_MARKETPLACE or the default)"},
            },
            "required": ["path"],
        },
        confirm=True,
    )
