"""An extension's metadata, read from its source without running it.

- description: first line of the module docstring;
- ``__version__ = "1.2"``: the extension's own version (the marketplace requires one);
- ``__api__ = "1"``: the :data:`minimal.API_VERSION` it is written for (required; :func:`check_api`).
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


def read_metadata(source: bytes, filename: str) -> Metadata:
    """Read ``source``'s metadata with :mod:`ast`; nothing is executed.

    Raises:
        ValueError: if ``source`` does not parse.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, ValueError) as e:
        raise ValueError(f"{filename}: syntax error: {e}") from None
    strings = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("__version__", "__api__"):
                    strings[target.id] = node.value.value
    docstring = ast.get_docstring(tree) or ""
    return Metadata(
        description=docstring.strip().splitlines()[0] if docstring.strip() else "",
        version=strings.get("__version__", ""),
        api=strings.get("__api__", ""),
        has_register=any(isinstance(n, ast.FunctionDef) and n.name == "register" for n in tree.body),
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
