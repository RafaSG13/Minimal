# Changelog

## Unreleased

### Changed

- A turn that fails or is interrupted after the model replied is no longer thrown away. It stays in the history: the
  results of the tool calls that finished are kept, each call not finished is answered with an error, and an assistant
  note `[turn interrupted: <reason>]` closes it, so the model still knows what it already did (files it wrote, for
  example) when you continue. A turn that fails before any reply is still discarded, so the same prompt can be sent
  again. The agent loop, also behind `ctx.run_agent`, keeps every `tool_use` answered when it ends with an exception.
- Installing or updating an extension into the global folder, from `/ext` or with the model's `ext_install` tool, now
  prints the static scan of `/trust` (what each file can do) and asks before writing anything. Global extensions run in
  every project without `/trust`, so this was the only place the code was never shown. Project installs are unchanged.
- Session files start with a `{"type": "session", "cwd": ...}` header line. `/sessions` and `/resume` take `--all` to
  include every project's sessions (`/resume <id>` works for any id, and `/sessions` now shows a preview of each one);
  `ctx.sessions(all_projects=False)` and `list_sessions(cwd)`/`last_session(cwd)` filter by project. Sessions saved
  before this change have no header, so they only appear with `--all`.

### Fixed

- A reply cut off by the output token limit (`stop_reason == "max_tokens"`) in the middle of a tool call no longer
  ends the turn with a `tool_use` that has no `tool_result`, which made every later request fail. The agent loop
  does not run the possibly incomplete calls; it answers each with an error asking for smaller steps and continues.
- `minimal -c` resumed the latest session of any project. Sessions now start with a header line recording their
  project directory, and `-c`, `/sessions` and `/resume` default to the current directory's sessions.
- A `bash` deny rule such as `bash(rm -rf*)` only matched when the whole command started with the pattern, so
  `cd /tmp && rm -rf x` got past it. It now also matches every command inside a compound one (between `;`, `&&`,
  `||`, `&`, `|`, newlines, `$( )`, backticks, `( )` and `{ }`). It still does not see inside `sh -c`, `eval`,
  `xargs`, `find -exec` or wrappers such as `sudo`; use a leading `*` for those. Allow rules are unchanged.
- OpenAI-compatible and Gemini replies cut off in the middle of a tool call crashed the turn with a `JSONDecodeError`,
  and a cut-off reply that had tool calls was reported as `tool_use`, so the tool-call fix above never applied to
  them. Now a reply cut off by the limit (`finish_reason == "length"`, Gemini `incomplete`), or whose arguments are not
  valid JSON, is reported as `max_tokens` even when it has tool calls; the agent loop answers each call with an error.
- A crash in the middle of saving left a cut-off last line in the session file, and `minimal -c` or `/resume` failed
  to open it. Opening now ignores that last line and says so, and the next save removes it so the file stays valid.
  A bad line anywhere else is still an error.

### Removed

- The migration of minimal 0.1's `config.json`, `theme.json`, `marketplaces.json` and `prompt.md` into
  `settings.json` (`config.migrate_legacy`, `config.LEGACY_FILES`), and the "Migrating from 0.1" sections of the docs.
  Settings from 0.1 are no longer read or moved; set them in `~/.minimal/settings.json`.

## 0.2.0

### Breaking

- **Stable extension API.** `register(minimal)` now receives an `Extension` facade instead of the internal
  `Registry`, and every `ctx` (tools, commands, events, widgets, status segments) is a `Context` instead of the
  internal `App`. `minimal.api` is the only import surface; `Registry`, `run_agent`, `open_session`, `new_id`,
  `ID_RE`, `SESSIONS_DIR`, `build_system_prompt`, `tool_specs`, `raise_if_cancelled`, `reload_summary` and
  `BUILTIN_COMMANDS` were removed from it. See [docs/extensions.md](docs/extensions.md) for the API.
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
