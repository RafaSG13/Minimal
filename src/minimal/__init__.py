"""minimal: a small, extensible coding-agent harness.

Modules, from the bottom up:

- :mod:`minimal.providers`  model integrations behind one interface (``base``, ``anthropic``, ``gemini``, ``openai``)
- :mod:`minimal.registry`   ``Registry``, ``Tool``, ``Command``: where tools, commands, providers, hooks and widgets are registered
- :mod:`minimal.tools`      built-in tools ``read``, ``write``, ``edit``, ``bash``, ``reload_extensions``
- :mod:`minimal.extensions` finds extension files and calls their ``register(minimal)``
- :mod:`minimal.marketplace` git-based extension marketplace: search, install, publish
- :mod:`minimal.session`    JSONL persistence of conversations
- :mod:`minimal.agent`      the model → tools → model loop (``run_agent``)
- :mod:`minimal.app`        ``App``: runtime state, system prompt, one user turn, extension reload
- :mod:`minimal.commands`   slash commands (/help, /reload, /ext, …)
- :mod:`minimal.config`     provider/model and API keys saved by setup (``~/.minimal``)
- :mod:`minimal.setup`      onboarding: pick the provider, sign in or enter its key, pick the model
- :mod:`minimal.theme`      colour theme and glyphs of the terminal UI
- :mod:`minimal.tui`        the Textual terminal UI, with extension widgets
- :mod:`minimal.cli`        option parsing, the ``minimal`` entry point
"""
