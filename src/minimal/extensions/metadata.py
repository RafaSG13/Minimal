"""An extension's metadata, read from its source without running it.

- description: first line of the module docstring;
- ``__version__ = "1.2"``: the extension's own version (the marketplace requires one);
- ``__api__ = "1"``: the :data:`minimal.API_VERSION` it is written for (required; :func:`check_api`);
- ``__dependencies__ = ["termaid>=0.3"]``: third-party packages it needs (optional; :data:`DEPENDENCY_RE`).
  ``/ext`` offers to install the missing ones into minimal's Python.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

import minimal


@dataclass(frozen=True)
class Metadata:
    description: str
    """First line of the module docstring; ``""`` if none."""
    version: str
    """Top-level ``__version__`` string; ``""`` if none."""
    api: str
    """Top-level ``__api__`` string; ``""`` if none."""
    has_register: bool
    """Whether a top-level ``def register`` exists."""
    dependencies: tuple[str, ...] = ()
    """Top-level ``__dependencies__``: package requirements such as ``"termaid>=0.3"``."""


DEPENDENCY_RE = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(\[[A-Za-z0-9._,-]+\])?"
    r"(\s*(==|!=|<=|>=|~=|<|>)\s*[A-Za-z0-9.*+!_-]+(\s*,\s*(==|!=|<=|>=|~=|<|>)\s*[A-Za-z0-9.*+!_-]+)*)?"
)
"""A valid ``__dependencies__`` entry (always ``fullmatch``): a package name, optional extras and version
comparisons. No URLs, and nothing starting with ``-`` (it would be an option to ``pip install``)."""


def _dependencies(node: ast.expr, filename: str) -> tuple[str, ...]:
    if isinstance(node, (ast.List, ast.Tuple)) and all(
        isinstance(e, ast.Constant) and isinstance(e.value, str) and DEPENDENCY_RE.fullmatch(e.value)
        for e in node.elts
    ):
        return tuple(e.value for e in node.elts)  # type: ignore[attr-defined]
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        value = ast.unparse(node)
    raise ValueError(
        f'{filename}: __dependencies__ must be a list of package requirements such as "termaid>=0.3"; got {value!r}'
    )


class SourceSyntaxError(ValueError):
    """:func:`read_metadata` could not parse the source."""


def read_metadata(source: bytes, filename: str) -> Metadata:
    """Read ``source``'s metadata with :mod:`ast`; nothing is executed.

    Raises:
        SourceSyntaxError: if ``source`` does not parse.
        ValueError: if ``__dependencies__`` is not a list of package requirements.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, ValueError) as e:
        raise SourceSyntaxError(f"{filename}: syntax error: {e}") from None
    strings = {}
    dependencies: tuple[str, ...] = ()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "__dependencies__" in names:
            dependencies = _dependencies(node.value, filename)
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for name in names:
                if name in ("__version__", "__api__"):
                    strings[name] = node.value.value
    docstring = ast.get_docstring(tree) or ""
    return Metadata(
        description=docstring.strip().splitlines()[0] if docstring.strip() else "",
        version=strings.get("__version__", ""),
        api=strings.get("__api__", ""),
        has_register=any(isinstance(n, ast.FunctionDef) and n.name == "register" for n in tree.body),
        dependencies=dependencies,
    )


_API = re.compile(r"(\d+)(?:\.(\d+))?")


def api_problem(declared: str, have: str = minimal.API_VERSION) -> str | None:
    """Why an extension declaring ``__api__ = declared`` cannot run on API ``have``; ``None`` if it can.

    ``"1"`` runs on any 1.x; ``"1.2"`` on 1.2 or a later 1.x. A different major never does: it
    changed or removed something the extension may use.
    """
    if not declared.strip():
        return f'declares no __api__; add __api__ = "{have.split(".")[0]}" (the minimal API it is written for)'
    match = _API.fullmatch(declared.strip())
    if match is None:
        return f'invalid __api__ {declared!r}: use "MAJOR" or "MAJOR.MINOR", like "1"'
    major, minor = int(match.group(1)), int(match.group(2) or 0)
    have_major, have_minor = (int(p) for p in have.split("."))
    if major != have_major:
        return f"written for minimal API {declared}, this minimal has API {have}: install a version of it made for API {have_major}"
    if minor > have_minor:
        return f"needs minimal API {declared}, this minimal has API {have}: upgrade minimal"
    return None


def check_api(declared: str, have: str = minimal.API_VERSION) -> None:
    """Like :func:`api_problem`, raising instead of returning the reason.

    Raises:
        ValueError: if an extension declaring ``declared`` cannot run on API ``have``.
    """
    problem = api_problem(declared, have)
    if problem is not None:
        raise ValueError(problem)
