"""minimal: a small, extensible coding-agent harness.

Packages, from the bottom up; a package imports only from the ones above it in this list:

- :mod:`minimal.config`      settings and keys saved in ``~/.minimal`` (used by every layer)
- :mod:`minimal.providers`   model integrations behind one interface
- :mod:`minimal.extensions`  the Registry, extension loading, the /trust scan, the marketplace
- :mod:`minimal.tools`       built-in tools: read, write, edit, bash, grep, glob, reload_extensions
- :mod:`minimal.core`        the engine: agent loop, App (one user turn), sessions, base prompt
- :mod:`minimal.ui`          slash commands, the setup wizard, the Textual UI
- :mod:`minimal.cli`         option parsing, the ``minimal`` entry point

Extensions import only from :mod:`minimal.api`.
"""

__version__ = "0.4.0"

API_VERSION = "1.3"
"""Version of :mod:`minimal.api`, separate from :data:`__version__`: ``MAJOR.MINOR``.

A minor release only adds to the API; a major one changes or removes something. An extension
declares the API it is written for with ``__api__ = "1"`` (any 1.x) or ``"1.2"`` (1.2 or later 1.x).
"""
