# Changelog

## Unreleased

### Added

- `edit` takes `replace_all` (change every occurrence) and `edits` (several replacements to one file, applied in
  order, all or none), and returns the changed lines numbered like `read`. The terminal UI shows a diff for each.
- `/title [text]` names the session. The title, and the session a new one was started from with `ctx.new_session`
  (`parent`, e.g. by `compact`), are kept in the session file's header and shown by `/sessions` and `/resume`.
- Extension API 1.2: frontends. `--ui <name>` (or the `ui` setting) picks the interface: `tui`,
  the terminal UI (the `minimal-textual` package, the default once installed), or `console`, a plain line-by-line REPL (Ctrl-C interrupts a turn, Ctrl-D quits).
  `minimal.register_frontend(name, run, description)` adds another; `run(host)` gets a `Host`, the API a frontend drives
  minimal with (`submit`, `run_command`, `interrupt`, `complete`, `status`, the widgets, status segments, themes and logo
  extensions registered, …), and shows minimal through a `Frontend` it attaches (`ConsoleFrontend` is a ready-made
  one). `Host`, `HostStatus`, `Frontend`, `ConsoleFrontend`, `FrontendSpec`, `ExitRepl` and `glyphs` are in `minimal.api`.
- Extension API 1.3: `minimal.register_panel(name, render, area, live, interval)` adds styled text lines that any
  frontend can draw (styles are space-separated names from `minimal.api.STYLES`, e.g. `"bold muted"`); `live(ctx, event)`
  gets the turn and tool events plus a new `end` event, and `interval` redraws it on a timer.
  `minimal.register_renderer(kind, render)` draws Markdown fences (`fence:mermaid`), GitHub alerts (`blockquote:alert`)
  and tool-call bodies (`tool:edit`) in the terminal UI. `register_frontend(..., toolkit=)` names a frontend's UI
  toolkit. `PanelSpec`, `RendererSpec`, `STYLES`, `styled_line` and `styled_lines` are in `minimal.api`.
- `minimal serve`: minimal without an interface, driven over JSON lines on stdin and stdout by a frontend written in
  any language: messages in, text, tool calls, questions, panels, status segments and renderer output out, with the
  extensions' Python panels and renderers running inside. It takes the session options of `minimal` (`-c`, `-s`,
  `--provider`, `-m`, `-e`, `-y`, `--trust-project`); `/setup` and `/login` run on the controlling terminal while the
  frontend steps aside. Protocol (version 2) and every message: [docs/serve.md](docs/serve.md).
- Frontends of installed packages: a package declares a `minimal.frontends` entry point, `register(registry)`, that calls
  `registry.register_frontend(...)`; minimal calls it on every reload. A failing entry point is reported with the
  extension errors. `tui` is the default frontend when one registers that name, else `console`.

### Changed

- **Breaking:** the Textual UI is no longer part of minimal. It is the `minimal-textual` package (import package
  `minimal_textual`, sources in the marketplace repository under `frontends/textual/`): install it next to minimal
  (`uv tool install mnml-agent --with "minimal-textual @ git+https://github.com/RafaSG13/minimal-marketplace#subdirectory=frontends/textual"`)
  and it registers the `tui` frontend. There is no `tui` extra
  and minimal no longer depends on Textual; without the package the default frontend is `console`, and `--ui tui` says
  that `minimal-textual` is missing. `src/minimal/ui/tui/` is gone.
- **Breaking, extension API 2.0:** `minimal.api.logo_content` and the theme names of `minimal.api.theme`
  (`DEFAULT`, `MINIMAL`, `build`, `available`) were removed; they are `minimal_textual.logo_content` and
  `minimal_textual.theme.*` now, and importing the old names says so (`minimal.api.REMOVED`). Removing names is a major
  version, so extensions declare `__api__ = "2"`; those written for API 1 stop loading until a version for 2 is installed.
- **Breaking:** the marketplace has a new layout: items live in `shared/<category>/` (work under any frontend) and
  `frontends/<frontend>/<category>/` (need that frontend, e.g. `frontends/textual/widgets/`); the old flat
  `<category>/` folders at the root are no longer read. Installed files and lock ids are unchanged. `/ext` and
  `ext_search` show which frontend an item is for, and `minimal ext check <checkout>` checks the whole layout (items
  for the Textual frontend get static checks only when Textual is not installed).
- Extensions that need Textual declare `__frontend__ = "textual"`. Under a frontend with another toolkit (`console`) they
  are skipped, not failed (`Skipped:` in the reload summary and at startup); `/ext` marks them `textual only`; and
  `minimal ext check` fails an item that imports `textual` or `rich` without it. Such extensions load after the others.
- A status segment's `render(ctx)` may return styled text (a string, a `(text, style)` tuple or a list of them); the
  terminal UI still accepts a `textual.content.Content`. `extensions/example.py` uses a panel instead of a widget.
- `edit` and `write` refuse a file that changed on disk since the model last read, wrote or edited it in this session
  (the user, a formatter or a `bash` command modified it), and ask the model to read it again.
- `read` shows bytes that are not UTF-8 as U+FFFD, with a note, instead of failing; a binary file (a NUL byte in its
  first 8 KiB) is reported, not printed. `edit` on a file that is not UTF-8 fails with a clear message.
- `import minimal.api` imports no UI toolkit: `minimal.api.theme` is the glyphs module (`minimal.glyphs`).
- `/theme` belongs to the terminal UI, which registers it; the `Frontend` protocol lost `themes`, `set_theme` and
  `choose_theme`. The terminal UI drives minimal through the `Host` instead of the `App`'s internals.
- CI runs `mypy` and measures test coverage (`coverage report` fails below 83%); `dev` gains `mypy` and `coverage`.

### Fixed

- Two minimal processes could continue the same session (`-c` in two terminals, `/resume` of a session open
  elsewhere) and interleave their messages in one file. A session is now locked (`<id>.lock`, `flock`) by the minimal
  that has it open: starting or resuming it elsewhere fails and names the process that holds it.

## 0.4.0

### Added

- `__dependencies__ = ["termaid>=0.3"]` declares the third-party packages an extension needs. After installing it,
  `/ext` and the model's `ext_install` ask whether to install the missing ones into minimal's Python (`uv pip install`,
  else `pip`); the extension stays installed either way. `minimal ext check --install-deps` installs them before
  checking, which is what CI runs (no more `requirements-ci.txt`).
- Extension API 1.1 (`minimal.API_VERSION = "1.1"`). New events: `session_start` (once, when the UI is up),
  `session_switch` (`/new`, `/resume`, `ctx.new_session`), `turn_start` (may rewrite the user's message),
  `before_request` (may replace the messages, system prompt or tools of one model request without touching the
  history), `after_response` (each reply and its usage) and `shutdown`. Their payload types are in `minimal.api`.
- `minimal.on_dispose(fn)`: `fn()` runs before a reload and when minimal exits (last registered first), so an
  extension can stop the threads, timers and subprocesses it started instead of leaking them on every `/reload`.
- `minimal.data_dir(scope="project")`: a private folder for an extension's own files outside the repository,
  `~/.minimal/data/projects/<hash of the working directory>/<id>` or, with `"global"`, `~/.minimal/data/global/<id>`.
- Request options for providers: `Provider.stream(..., options=None)` takes `max_tokens` and `temperature` (the
  built-in providers honour both; OpenAI gets `max_completion_tokens`, other `OPENAI_BASE_URL` servers `max_tokens`).
  minimal passes `options` only when one is set, so providers written before keep working. `before_request` handlers
  can add options and `ctx.complete` takes `options`.
- `ctx.notify(text, kind="info")` shows a notice (a toast in the terminal UI), `ctx.use_provider(name, model=None)`
  switches provider, and `ctx.queue_user_message(text)` sends `text` as the user's next message once the current turn
  or command ends.
- `max_tokens` setting: the output-token limit of each request, as an integer or per `"provider"`/`"provider/model"`
  (`{"max_tokens": {"anthropic": 16000}}`); unset keeps each provider's default (8192 for Anthropic, Claude and
  Gemini). See docs/settings.md.
- `provider_error` event: when a model request fails, a handler can return `{"retry_after": seconds}` to send it
  again after a wait or `{"provider": name, "model": name}` to send it to another provider; `attempt` lets it give up.
  Without a handler a failed request ends the turn, as before.

### Changed

- `minimal ext check` reports `WARN … needs <pkg>` (not `FAIL`) when an extension raises
  `ImportError(..., name="<pkg>")` for a package that is not installed.
- Built-in slash commands live in the registry (`Registry.builtin_commands`, `register_builtin_command`) and receive
  the same `ctx` as extension commands. `App(commands=BUILTIN_COMMANDS)` became
  `App(register_commands=register_builtin_commands)`, which registers them again on every reload.

## 0.3.0

### Breaking

- The extension API is versioned on its own: `minimal.API_VERSION` (`"1.0"`, also in `minimal.api`) and a required
  `__api__ = "1"` in every extension, which replaces `__requires__`. An extension without `__api__`, or written for
  another major version, is not loaded and the reload says why; the marketplace lists it but does not install it, and
  `minimal ext check` fails it. Importing a name `minimal.api` removed (such as `BUILTIN_COMMANDS`) names the version
  that removed it and what to use instead. See "API versions" in docs/extensions.md.
- `minimal.ui.commands.run_command`/`find_command` became `App.run_command`/`App.find_command`, and `ExitRepl` moved
  to `minimal.core.app`; an `App` runs the built-in commands it is given (`App(commands=BUILTIN_COMMANDS)`).
- A `Frontend` must implement `widget(name)`: the widget it shows for an extension's `register_widget(name, ...)`, or
  `None` (what `ctx.widget` returns).

### Added

- `ctx.commands()`, `ctx.run_command(line, builtin=False)`, `ctx.models()`, `ctx.set_model(name)` and
  `ctx.widget(name)`, so extensions such as a slash-command menu or a model picker no longer reach into internals.
- CI checks the default marketplace against every change to minimal (`minimal ext check` of
  `RafaSG13/minimal-marketplace`), and a release tag does not publish if any marketplace item fails to load with it.

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
- The marketplace installs versions, not just whatever is at HEAD. For each item it takes the newest commit in the
  marketplace's git history whose `__api__` this minimal can run (HEAD's if it can, else the latest older one, shown
  as "latest for this minimal (newer needs API 2)"), in `/ext`, `ext_search` and `ext_install`. Each extensions folder
  (global and project) now has a `.lock.json` recording, for each marketplace extension installed in it, the
  marketplace, commit, `__version__`, `__api__` and the SHA-256 of the installed files. An update is offered when the
  commit differs, no longer when the bytes do, so a local edit is not mistaken for an update; an edited copy is marked
  "local changes", starts unticked in "Update these?" (ticking it overwrites it) and `ext_install` refuses to replace
  it unless called with the new `force: true`. A copy without a lock entry is still compared byte by byte.

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
- `extensions/compact.py`, `extensions/tools/subagent.py` and `extensions/tools/web_search.py` left the repository:
  they are published in the default marketplace (`/ext`). Copies loaded with `--ext extensions/...` from a checkout of
  this repo no longer exist.

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
