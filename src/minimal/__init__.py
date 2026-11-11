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

__version__ = "0.2.0"
