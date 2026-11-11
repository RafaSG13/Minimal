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
* ``__requires__ = ">=0.2"``: the minimal versions it works with (optional); an
  extension this minimal does not satisfy is listed but not installed.

Marketplaces are ``$MINIMAL_MARKETPLACE`` (if set), :data:`DEFAULT_URL` and
the ``marketplaces`` of ``~/.minimal/settings.json`` (:func:`marketplaces`); a
local path works as well as a git URL. Searching and installing use a cached
clone per marketplace under :data:`CACHE_ROOT`; the picker lists one through
:func:`catalog`. Publishing pushes a new ``publish/<name>-<timestamp>`` branch
and, when the ``gh`` CLI is installed, opens a pull request. :func:`check`
validates extensions, or a whole marketplace, without git (``minimal ext check``).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import minimal
from minimal import config
from minimal.extensions import loader
from minimal.extensions.metadata import read_metadata, satisfies
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
    requires: str = ""
    """Its ``__requires__``; ``""`` if any minimal will do."""
    compatible: bool = True
    """Whether this minimal satisfies ``requires``."""


@dataclass
class Entry:
    """A marketplace extension and its local copies."""

    listing: Listing
    installed: list[str]
    """Scopes holding ``<name>.py`` or ``<name>/`` in the listing's category, ``"global"`` before ``"project"``."""
    outdated: list[str]
    """Scopes whose copy differs from the marketplace's."""
    installed_version: str
    """Version of the first installed copy; ``""`` if none, unset or unreadable."""


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


def _git(*args: str, cwd: Path | None = None) -> str:
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
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {args[0]} timed out after {GIT_TIMEOUT}s") from None
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


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
            function, module docstring or ``__version__``, or an invalid ``__requires__``.
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
    try:
        compatible = satisfies(meta.requires)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from None
    return Listing(name, meta.description, meta.version, path, _category_of(path), meta.requires, compatible)


def format_listing(listing: Listing) -> str:
    """One line: ``name version — description``."""
    head = f"{listing.name} {listing.version}" if listing.version else listing.name
    return f"{head} — {listing.description}"


def _incompatible(listing: Listing) -> str:
    return f"{listing.name}: requires minimal {listing.requires}, this is {minimal.__version__}"


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


def search(query: str = "", url: str | None = None, category: str | None = None) -> list[Listing]:
    """Extensions whose name or description contains ``query`` (case-insensitive); all if empty.

    Listed by category in :data:`~minimal.extensions.loader.CATEGORIES` order, then by
    name; only ``category``'s if given. Extensions with invalid metadata are skipped.

    Raises:
        ValueError: if ``category`` is not one of :data:`~minimal.extensions.loader.CATEGORIES`.
    """
    if category:
        loader.category_dir(Path(), category)  # validates it: the folder name must not be a path
    root = sync(url)
    needle = query.lower()
    listings = []
    for folder in [category] if category else loader.CATEGORIES:
        for path in loader.category_entries(root / folder)[0]:
            try:
                listing = describe(path)
            except ValueError:
                continue
            if needle in listing.name.lower() or needle in listing.description.lower():
                listings.append(listing)
    return listings


def _content_digest(path: Path) -> str:
    """SHA-256 of an extension's files by their path inside it: equal for identical copies in different places."""
    digest = hashlib.sha256()
    for source in loader.read_extensions([path]):
        for file, data in source.files:
            name = file.relative_to(path).as_posix() if source.is_package else file.name
            digest.update(name.encode() + b"\0" + data + b"\0")
    return digest.hexdigest()


def _installed_path(scope: str, cwd: str, category: str, name: str) -> Path | None:
    """The installed copy of ``name`` in ``scope``'s ``category`` folder (the ``.py`` file wins); ``None`` if none."""
    folder = _target_dir(scope, cwd, category)
    for path in (folder / f"{name}.py", folder / name):
        if path.is_file() or (path / "__init__.py").is_file():
            return path
    return None


def catalog(url: str, cwd: str) -> list[Entry]:
    """Every extension in marketplace ``url`` with its installed copies (global and ``cwd``'s project)."""
    entries = []
    for listing in search("", url):
        installed, outdated, version = [], [], ""
        wanted = _content_digest(listing.path)
        for scope in ("global", "project"):
            path = _installed_path(scope, cwd, listing.category, listing.name)
            if path is None:
                continue
            installed.append(scope)
            if _content_digest(path) != wanted:
                outdated.append(scope)
            if len(installed) == 1:
                try:
                    version = describe(path).version
                except ValueError:
                    pass
        entries.append(Entry(listing, installed, outdated, version))
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
        for path in (root / folder / f"{name}.py", root / folder / name):
            if path.is_file() or (path / "__init__.py").is_file():
                found.append(path)
                break
    return found


def install(
    name: str, scope: str = "global", cwd: str = ".", url: str | None = None, category: str | None = None
) -> tuple[Path, bool]:
    """Copy extension ``name`` from the marketplace into the ``scope`` folder of its category.

    It is not loaded; reload the extensions to activate it.

    Args:
        category: Where to look; needed only when several categories have ``name``.

    Returns:
        ``(dest, updated)``: the installed file or folder, and whether it replaced an existing copy.

    Raises:
        ValueError: if the name or category is invalid, the name is not in the marketplace or in
            several categories, or the extension requires another minimal version.
    """
    if category:
        loader.category_dir(Path(), category)  # validates it: the folder name must not be a path
    _check_name(name)
    found = _find(sync(url), name, category)
    if not found:
        raise ValueError(f"{name} is not in the marketplace (open /ext to browse it)")
    if len(found) > 1:
        raise ValueError(f"{name} is in several categories ({', '.join(p.parent.name for p in found)}); pick one")
    return install_from(found[0], scope, cwd)


def install_from(src: Path, scope: str, cwd: str) -> tuple[Path, bool]:
    """Copy marketplace extension ``src`` (a file or package folder of a synced clone) into ``scope``'s category folder.

    A copy in the other form (``x.py`` for a folder ``x/``, or the reverse) is removed.

    Returns:
        ``(dest, updated)``: the installed file or folder, and whether it replaced an existing copy.

    Raises:
        ValueError: if the extension requires another minimal version.
    """
    listing = describe(src)
    if not listing.compatible:
        raise ValueError(_incompatible(listing))
    folder = _target_dir(scope, cwd, src.parent.name)
    dest = folder / src.name
    other = folder / listing.name if src.is_file() else folder / f"{listing.name}.py"
    updated = dest.exists() or other.exists()
    folder.mkdir(parents=True, exist_ok=True)
    _remove(other)
    if src.is_dir():
        _remove(dest)
        shutil.copytree(src, dest, ignore=_COPY_IGNORE)
    else:
        shutil.copyfile(src, dest)
    return dest, updated


def _remove(path: Path) -> None:
    """Delete file or folder ``path`` if it exists."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def uninstall(name: str, scope: str = "global", cwd: str = ".", category: str = "extensions") -> Path:
    """Delete an installed extension (file or package folder) and return its path.

    Raises:
        ValueError: if the name is invalid or the extension is not installed in ``scope``'s ``category`` folder.
    """
    _check_name(name)
    folder = _target_dir(scope, cwd, category)
    removed = None
    for path in (folder / f"{name}.py", folder / name):
        if path.exists():
            _remove(path)
            removed = removed or path
    if removed is None:
        raise ValueError(f"{name} is not installed in {folder}")
    return removed


def installed(cwd: str) -> list[tuple[str, str, Path]]:
    """Installed extensions (files and package folders) as ``(scope, category, path)``: global ones first, then project ones."""
    return [
        (scope, category, path)
        for scope in ("global", "project")
        for category in loader.CATEGORIES
        for path in loader.category_entries(_target_dir(scope, cwd, category))[0]
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
    url = url or marketplace_url()
    with tempfile.TemporaryDirectory() as tmp:
        _git("clone", "--quiet", "--", url, tmp)  # "--": a URL starting with "-" is not an option
        folder = Path(tmp, category)
        dest = folder / (listing.name if path.is_dir() else f"{listing.name}.py")
        other = folder / (f"{listing.name}.py" if path.is_dir() else listing.name)
        if dest.exists() and not other.exists() and _content_digest(dest) == _content_digest(path):
            raise ValueError(f"{listing.name} is already published with identical content")
        folder.mkdir(parents=True, exist_ok=True)
        replaced = [p for p in (dest, other) if p.exists()]
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
    (``__requires__``) and load into a fresh registry from ``make_registry``.

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
                entries, shadowed = loader.category_entries(path / category)
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
        return f"FAIL {label}: requires minimal {listing.requires}, this is {minimal.__version__}"
    errors: list[str] = []
    try:
        source = loader.read_extensions([path])[0]
        loader.load_source(source, make_registry(), config.Settings(), "cli", errors)
    except ImportError as e:
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
            listings = search(input.get("query", ""), url, category)
        except (RuntimeError, ValueError) as e:
            lines.append(f"({url}: {e})")
            continue
        prefix = f"{url} " if len(urls) > 1 else ""
        for x in listings:
            note = "" if x.compatible else f" [requires minimal {x.requires}]"
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
    dest, updated = install(name, input.get("scope", "global"), ctx.cwd, url=url, category=category)
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
            "in the subfolder of its category. It is not active until you call reload_extensions."
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
