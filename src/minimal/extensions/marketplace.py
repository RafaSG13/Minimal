"""The extension marketplace: a git repository of shareable extensions.

Layout of the repository: one folder per category (:data:`~minimal.extensions.loader.CATEGORIES`):
``extensions/`` (general), ``themes/`` (colour themes), ``tools/`` (tools for the
model) and ``widgets/`` (terminal UI components), each holding ``<name>.py`` files.
Installing mirrors the category: ``themes/ocean.py`` goes to
``~/.minimal/extensions/themes/ocean.py``. There is no index; each file carries
its own metadata, so concurrent publishes never conflict:

* description: first line of the module docstring (required);
* version: optional top-level ``__version__ = "x"`` string, default ``""``.

Marketplaces are ``$MINIMAL_MARKETPLACE`` (if set), :data:`DEFAULT_URL` and
the ones saved from the ``/ext`` picker (:func:`marketplaces`); a local path
works as well as a git URL. The model's tools use :func:`marketplace_url`.
Searching and installing use a cached clone per marketplace under
:data:`CACHE_ROOT`; the picker lists one through :func:`catalog`. Publishing
pushes a new ``publish/<name>-<timestamp>`` branch and, when the ``gh`` CLI is
installed, opens a pull request.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from minimal import config
from minimal.extensions import loader
from minimal.extensions.registry import Registry

DEFAULT_URL = "https://github.com/RafaSG13/minimal-marketplace.git"
CACHE_ROOT = Path.home() / ".minimal" / "marketplace"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_]*")
"""Valid extension names (always ``fullmatch``): no path separators, no leading ``_``."""
GIT_TIMEOUT = 120
"""Seconds a ``git`` or ``gh`` command may take before it is given up (a dead network must not hang ``/ext``)."""


@dataclass
class Listing:
    """One extension in the marketplace (or about to be published)."""

    name: str
    description: str
    version: str
    path: Path
    category: str
    """Its marketplace folder: one of :data:`~minimal.extensions.loader.CATEGORIES`."""


@dataclass
class Entry:
    """A marketplace extension and its local copies."""

    listing: Listing
    installed: list[str]
    """Scopes holding ``<name>.py`` in the listing's category, ``"global"`` before ``"project"``."""
    outdated: list[str]
    """Scopes whose copy differs from the marketplace file."""
    installed_version: str
    """Version of the first installed copy; ``""`` if none, unset or unreadable."""


def marketplace_url() -> str:
    """The marketplace repository: ``$MINIMAL_MARKETPLACE``, else :data:`DEFAULT_URL`."""
    return os.environ.get("MINIMAL_MARKETPLACE") or DEFAULT_URL


def marketplaces() -> list[str]:
    """Marketplaces to offer: :func:`marketplace_url`, :data:`DEFAULT_URL`, then the saved ones."""
    return list(dict.fromkeys([marketplace_url(), DEFAULT_URL, *config.load_marketplaces()]))


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
    config.save_marketplaces([*config.load_marketplaces(), url])
    return url


def remove_marketplace(url: str) -> None:
    """Forget a saved marketplace.

    Raises:
        ValueError: if ``url`` was not saved (the default and ``$MINIMAL_MARKETPLACE`` cannot be removed).
    """
    saved = config.load_marketplaces()
    if url not in saved:
        raise ValueError(f"{url} is not a saved marketplace")
    config.save_marketplaces([u for u in saved if u != url])


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


def describe(path: Path, name: str | None = None) -> Listing:
    """Read an extension's metadata without running it.

    Its category is the name of its folder when that is a category
    (``themes/ocean.py`` is a theme), else ``extensions``.

    Args:
        path: The extension file.
        name: Name to publish it under (default: the file name without ``.py``).

    Raises:
        ValueError: on a syntax error, an invalid name, a missing ``register``
            function or a missing module docstring.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        raise ValueError(f"{path}: syntax error: {e}") from None
    name = path.stem if name is None else name
    _check_name(name)
    if not any(isinstance(node, ast.FunctionDef) and node.name == "register" for node in tree.body):
        raise ValueError(f"{path}: missing a top-level register(minimal) function")
    docstring = ast.get_docstring(tree)
    if not docstring:
        raise ValueError(f"{path}: add a module docstring; its first line is the marketplace description")
    version = ""
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            version = node.value.value
    category = path.parent.name if path.parent.name in loader.CATEGORIES else "extensions"
    return Listing(name, docstring.strip().splitlines()[0], version, path, category)


def format_listing(listing: Listing) -> str:
    """One line: ``name version — description`` (the version only when set)."""
    head = f"{listing.name} {listing.version}" if listing.version else listing.name
    return f"{head} — {listing.description}"


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
    name; only ``category``'s if given. Files with invalid metadata are skipped.

    Raises:
        ValueError: if ``category`` is not one of :data:`~minimal.extensions.loader.CATEGORIES`.
    """
    if category:
        loader.category_dir(Path(), category)  # validates it: the folder name must not be a path
    root = sync(url)
    needle = query.lower()
    listings = []
    for folder in [category] if category else loader.CATEGORIES:
        for path in sorted((root / folder).glob("*.py")):
            if path.name.startswith(("_", ".")):
                continue
            try:
                listing = describe(path)
            except ValueError:
                continue
            if needle in listing.name.lower() or needle in listing.description.lower():
                listings.append(listing)
    return listings


def catalog(url: str, cwd: str) -> list[Entry]:
    """Every extension in marketplace ``url`` with its installed copies (global and ``cwd``'s project)."""
    entries = []
    for listing in search("", url):
        installed, outdated = [], []
        for scope in ("global", "project"):
            path = _target_dir(scope, cwd, listing.category) / f"{listing.name}.py"
            if path.is_file():
                installed.append(scope)
                if path.read_bytes() != listing.path.read_bytes():
                    outdated.append(scope)
        version = ""
        if installed:
            try:
                version = describe(_target_dir(installed[0], cwd, listing.category) / f"{listing.name}.py").version
            except ValueError:
                pass
        entries.append(Entry(listing, installed, outdated, version))
    return entries


def _target_dir(scope: str, cwd: str, category: str = "extensions") -> Path:
    """Folder for ``category`` files in ``scope``: ``"global"`` or ``"project"``."""
    if scope == "global":
        return loader.category_dir(loader.GLOBAL_EXTENSIONS_DIR, category)
    if scope == "project":
        return loader.category_dir(loader.project_extensions_dir(cwd), category)
    raise ValueError("scope must be global or project")


def install(
    name: str, scope: str = "global", cwd: str = ".", url: str | None = None, category: str | None = None
) -> tuple[Path, bool]:
    """Copy extension ``name`` from the marketplace into the ``scope`` folder of its category.

    It is not loaded; reload the extensions to activate it.

    Args:
        category: Where to look; needed only when several categories have ``name``.

    Returns:
        ``(dest, updated)``: the installed file, and whether it replaced an existing one.

    Raises:
        ValueError: if the name or category is invalid, or the name is not in the marketplace or in several categories.
    """
    if category:
        loader.category_dir(Path(), category)  # validates it: the folder name must not be a path
    _check_name(name)
    root = sync(url)
    folders = [category] if category else loader.CATEGORIES
    found = [root / folder / f"{name}.py" for folder in folders if (root / folder / f"{name}.py").is_file()]
    if not found:
        raise ValueError(f"{name} is not in the marketplace (open /ext to browse it)")
    if len(found) > 1:
        raise ValueError(f"{name} is in several categories ({', '.join(p.parent.name for p in found)}); pick one")
    return install_from(found[0], scope, cwd)


def install_from(src: Path, scope: str, cwd: str) -> tuple[Path, bool]:
    """Copy marketplace file ``src`` (from a synced clone) into the ``scope`` folder of its category.

    Returns:
        ``(dest, updated)``: the installed file, and whether it replaced an existing one.
    """
    dest = _target_dir(scope, cwd, src.parent.name) / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    updated = dest.exists()
    shutil.copyfile(src, dest)
    return dest, updated


def uninstall(name: str, scope: str = "global", cwd: str = ".", category: str = "extensions") -> Path:
    """Delete an installed extension and return its path.

    Raises:
        ValueError: if the name is invalid or the extension is not installed in ``scope``'s ``category`` folder.
    """
    _check_name(name)
    dest = _target_dir(scope, cwd, category) / f"{name}.py"
    if not dest.exists():
        raise ValueError(f"{name} is not installed in {dest.parent}")
    dest.unlink()
    return dest


def installed(cwd: str) -> list[tuple[str, str, Path]]:
    """Installed extensions as ``(scope, category, path)``: global ones first, then project ones."""
    return [
        (scope, category, path)
        for scope in ("global", "project")
        for category in loader.CATEGORIES
        for path in loader._py_files(_target_dir(scope, cwd, category))
    ]


def publish(path: str | Path, name: str | None = None, url: str | None = None, category: str | None = None) -> str:
    """Propose an extension to the marketplace on a new branch, opening a pull request if ``gh`` is installed.

    Never force-pushes: the branch name carries a timestamp, so it is always new.

    Args:
        category: Marketplace folder (default: the file's own folder if it is a
            category, such as ``~/.minimal/extensions/themes``, else ``extensions``).

    Returns:
        What happened, for the user.

    Raises:
        ValueError: if the file is missing, invalid, or already published unchanged, or the category is unknown.
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
        dest = Path(tmp, category, f"{listing.name}.py")
        if dest.exists() and dest.read_bytes() == path.read_bytes():
            raise ValueError(f"{listing.name} is already published with identical content")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        branch = f"publish/{listing.name}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
        title = f"Publish {listing.name} {listing.version}".strip()
        _git("checkout", "-b", branch, cwd=Path(tmp))
        _git("add", f"{category}/{listing.name}.py", cwd=Path(tmp))
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


# --- tools for the model -----------------------------------------------------


def _ext_search(input: dict, ctx: Any) -> str:
    listings = search(input.get("query", ""), category=input.get("category"))
    return "\n".join(f"{x.category}/{format_listing(x)}" for x in listings) or "No extensions found."


def _ext_install(input: dict, ctx: Any) -> str:
    name = input["name"]
    dest, updated = install(name, input.get("scope", "global"), ctx.cwd, category=input.get("category"))
    return f"{'Updated' if updated else 'Installed'} {name} at {dest}. Call reload_extensions to activate it."


def _ext_publish(input: dict, ctx: Any) -> str:
    return publish(Path(ctx.cwd, input["path"]), input.get("name"), category=input.get("category"))


_CATEGORY_SCHEMA = {
    "type": "string",
    "enum": list(loader.CATEGORIES),
    "description": "extensions (general), themes (colour themes), tools (tools for you) or widgets (UI components)",
}


def register_marketplace_tools(registry: Registry) -> None:
    """Add ``ext_search``, ``ext_install`` and ``ext_publish`` to ``registry``."""
    registry.register_tool(
        name="ext_search",
        run=_ext_search,
        description=(
            "Search the minimal extension marketplace. Returns one line per extension: category/name, version and "
            "description. Check it before writing a new extension; one may already exist."
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
            "Install an extension from the marketplace into the global (default) or project extensions folder, "
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
            },
            "required": ["name"],
        },
        confirm=True,
    )
    registry.register_tool(
        name="ext_publish",
        run=_ext_publish,
        description=(
            "Publish an extension file to the marketplace: pushes a new branch and opens a pull request. "
            "The file must define register(minimal) and start with a docstring; its first line is the description. "
            "It goes in the category of its folder (for example ~/.minimal/extensions/themes) unless one is given."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The extension file"},
                "name": {"type": "string", "description": "Name to publish under (default: the file name)"},
                "category": _CATEGORY_SCHEMA,
            },
            "required": ["path"],
        },
        confirm=True,
    )
