# Changelog

## 0.2.0

### Breaking

- **Stable extension API.** `register(minimal)` now receives an `Extension` facade instead of the internal
  `Registry`, and every `ctx` (tools, commands, events, widgets, status segments) is a `Context` instead of the
  internal `App`. `minimal.api` is the only import surface; `Registry`, `run_agent`, `open_session`, `new_id`,
  `ID_RE`, `SESSIONS_DIR`, `build_system_prompt`, `tool_specs`, `raise_if_cancelled`, `reload_summary` and
  `BUILTIN_COMMANDS` were removed from it. See [docs/extensions.md](docs/extensions.md) for the API and a
  migration table from 0.1.
- `ctx.cancelled` is a `bool`; `ctx.check_cancelled()` raises `TurnInterrupted`.
- Event payloads carry `ctx` (`tool_call`, `tool_result`) and `nested` (`turn_end`); `on()` rejects unknown events.
- `tool_result` handlers see a command's whole output; the agent loop clips what the model sees afterwards.
- The PyPI distribution is now **`mnml-agent`** (`uv tool install mnml-agent`); the command and the
  import package stay `minimal`.

### Added

- **Layered settings**: `~/.minimal/settings.json` < `.minimal/settings.json` < `.minimal/settings.local.json`,
  then `$MINIMAL_PROVIDER`/`$MINIMAL_MODEL`, then the command line. Provider, model and theme; the base prompt,
  appended prompt text and instruction files; permissions; extension enable/disable and per-extension settings;
  marketplaces. `/settings` shows the files and where each value comes from. See [docs/settings.md](docs/settings.md).
- **Migration**: on first start, `config.json`, `theme.json` and `marketplaces.json` are moved into
  `~/.minimal/settings.json`; `prompt.md` is kept and referenced as `prompt.base`.
- **Permissions**: `permissions.allow` rules (`bash(git status*)`, `write(src/**)`) run matching tool calls without
  asking; `permissions.deny` rules block them, even with `-y`. A project's allow rules, disabled extensions and
  extension settings apply only once the project is trusted (`/trust`), which now reviews them alongside its extensions.
- **Extension metadata and packages**: `__version__` and `__requires__ = ">=0.2"`; an extension may be a folder
  with an `__init__.py` and helper modules. `extensions.disabled` turns extensions off by id.
- `Extension` decorators (`@minimal.tool`, `@minimal.command`, `@minimal.on`), `minimal.declare_settings(...)`,
  and `Context` helpers: `ctx.ui`, `ctx.complete`, `ctx.run_agent`, `ctx.new_session`, `ctx.sessions`,
  `ctx.read_session`, `ctx.reload`.
- **`minimal ext check <path>...`** validates extension files, packages or a whole marketplace checkout; the
  marketplace runs it in CI. The model's `ext_search` searches every configured marketplace; `ext_install` and
  `ext_publish` take an optional `marketplace`.
- Releases are published to PyPI from `v*` tags.
