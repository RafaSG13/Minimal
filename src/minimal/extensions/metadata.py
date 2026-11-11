"""An extension's metadata, read from its source without running it.

- description: first line of the module docstring;
- ``__version__ = "1.2"``: the extension's own version (the marketplace requires one);
- ``__requires__ = ">=0.2,<0.4"``: the minimal versions it works with (:func:`check_requires`).
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
    requires: str
    """Top-level ``__requires__`` string; ``""`` if none."""
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
                if isinstance(target, ast.Name) and target.id in ("__version__", "__requires__"):
                    strings[target.id] = node.value.value
    docstring = ast.get_docstring(tree) or ""
    return Metadata(
        description=docstring.strip().splitlines()[0] if docstring.strip() else "",
        version=strings.get("__version__", ""),
        requires=strings.get("__requires__", ""),
        has_register=any(isinstance(n, ast.FunctionDef) and n.name == "register" for n in tree.body),
    )


_CLAUSE = re.compile(r"(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)")

_COMPARE = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def _parts(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version))


def satisfies(spec: str, version: str = minimal.__version__) -> bool:
    """Whether minimal ``version`` satisfies ``spec``: ``,``-separated clauses like ``>=0.2``; empty always does.

    Versions compare number by number, the shorter padded with zeros (``0.2 == 0.2.0``).

    Raises:
        ValueError: if ``spec`` is malformed.
    """
    if not spec.strip():
        return True
    clauses = []
    for clause in spec.split(","):
        match = _CLAUSE.fullmatch(clause.strip())
        if match is None:
            raise ValueError(f'invalid __requires__ {spec!r}: use clauses like ">=0.2" or ">=0.2,<0.4"')
        clauses.append((match.group(1), _parts(match.group(2))))
    have = _parts(version)
    for op, want in clauses:
        width = max(len(have), len(want))
        if not _COMPARE[op](have + (0,) * (width - len(have)), want + (0,) * (width - len(want))):
            return False
    return True


def check_requires(spec: str, version: str = minimal.__version__) -> None:
    """Like :func:`satisfies`, raising instead of returning ``False``.

    Raises:
        ValueError: if ``spec`` is malformed or ``version`` does not satisfy it.
    """
    if not satisfies(spec, version):
        raise ValueError(f"requires minimal {spec}, this is {version}")
