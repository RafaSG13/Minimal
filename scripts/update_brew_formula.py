"""Point the Homebrew formula of ``minimal`` at a released version of ``mnml-agent`` on PyPI.

Usage: ``python scripts/update_brew_formula.py VERSION PATH/TO/minimal.rb``

Reads the sdist URL and SHA-256 of ``mnml-agent==VERSION`` from the PyPI JSON API and rewrites the ``url`` and
``sha256`` lines of the formula in place. It waits for PyPI to list the release (an upload takes a moment to show),
prints ``unchanged`` and leaves the file alone when it already points there, and exits 1 on anything else that
does not look right. Standard library only: ``release.yml`` runs it on a bare runner.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PYPI_JSON = "https://pypi.org/pypi/mnml-agent/{version}/json"
"""PyPI's description of one release."""

WAIT_SECONDS = 300
"""How long to wait for PyPI to list a release that was just uploaded."""


def sdist(version: str) -> tuple[str, str]:
    """Return the ``(url, sha256)`` of the source distribution of ``mnml-agent==version``.

    Raises:
        ValueError: if PyPI does not list the release within :data:`WAIT_SECONDS`, or it has no sdist.
    """
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            with urllib.request.urlopen(PYPI_JSON.format(version=version), timeout=30) as response:
                files = json.load(response)["urls"]
            break
        except urllib.error.HTTPError as e:
            if e.code != 404 or time.monotonic() > deadline:
                raise ValueError(f"PyPI has no mnml-agent {version}: HTTP {e.code}") from e
            time.sleep(10)
    for file in files:
        if file["packagetype"] == "sdist":
            return file["url"], file["digests"]["sha256"]
    raise ValueError(f"mnml-agent {version} on PyPI has no source distribution")


def rewrite(formula: str, url: str, sha256: str) -> str:
    """Return ``formula`` with its ``url`` and ``sha256`` lines replaced.

    Raises:
        ValueError: if either line is missing or appears more than once.
    """
    for key, value in (("url", url), ("sha256", sha256)):
        formula, count = re.subn(rf'^(\s*{key} )"[^"]*"$', rf'\1"{value}"', formula, flags=re.MULTILINE)
        if count != 1:
            raise ValueError(f"expected one `{key}` line in the formula, found {count}")
    return formula


def main(argv: list[str]) -> int:
    """Run the command line; return the exit status."""
    if len(argv) != 2 or not re.fullmatch(r"\d+(\.\d+)*", argv[0]):
        print(__doc__, file=sys.stderr)
        return 1
    version, path = argv[0], Path(argv[1])
    try:
        url, sha256 = sdist(version)
        old = path.read_text()
        new = rewrite(old, url, sha256)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if new == old:
        print("unchanged")
        return 0
    path.write_text(new)
    print(f"{path}: now {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
