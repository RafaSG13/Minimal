# Changelog

## 0.10.0

### Added

- `register_keybinding(..., terminal=True)` and `@minimal.keybinding(key, terminal=True)`: the key suspends the terminal
  UI and runs on the UI thread in the real terminal, like a `terminal` command, for `fzf`, `$EDITOR`, `lazygit` and
  other programs that need the keyboard (`KeybindingSpec.terminal`). A key is still ignored while a turn or command
  runs; an error it raises is shown after the resume.
- `ctx.ui.get_draft()` and `ctx.ui.set_draft(text, *, append=False)` read and fill the prompt box (undoable, cursor at
  the end, no completion popup, safe from any thread and from a `terminal` command or key); `Frontend.draft` and
  `Frontend.set_draft`, a no-op in the plain terminal.
- `ctx.show(text, kind="output")` adds a transcript block (`output` or `error`) that stays, from any thread; while the
  UI is suspended for a `terminal` command or key it is drawn once the UI is back. A `terminal` command or key may also
  return a str, which is shown the same way (`Frontend.show`).
- `Extension.builtin_commands()` gives the names of minimal's own commands at `register` time, and
  `ctx.git_refresh()` asks the shared git poller to look again now.
- `ctx.focus_widget(name, *, hold=False)` and `ctx.focus_composer()` move the keyboard focus (`Frontend.focus_widget`,
  `Frontend.focus_composer`). A widget focused with `hold=True` keeps the focus when a turn or command ends, until it
  is removed, the focus leaves it, `escape` reaches the app or `focus_composer()` is called.
- `ui.widgets.<name>.height` (1 to 100 rows) sizes any extension widget or panel in any area; `/ui` shows it.

### Added

- `minimal.ctx` (API 2.4): the app's `Context` once `register` has returned, so timer and watcher threads of an extension
  can act without waiting for a widget or command, also after `/reload`; `None` while `register` runs and under
  `minimal ext check`. `ctx.run_command` no longer clears the user's interrupt flag while a turn or another command
  runs (it still does when idle); `ctx.queue_user_message("/cmd")` runs a command after the turn.
- `ctx.settings` (API 2.4): `get(key, default)` reads the merged effective value of any dotted settings key,
  `layers(key)` says which files set it, and `update(changes, *, scope="global"|"project"|"local", apply=True)` /
  `unset(key, *, scope)` validate and write keys atomically and reload once. Project and local scopes refuse the
  global-only keys, and the trust-gated keys while the project is untrusted. `/ui` and `/ext settings` now use the same
  writer (`config.update_settings`), which also validates the values they write.
- `ctx.set_theme(name, *, save=False)` switches the terminal UI's colour theme like `/theme <name>` (firing
  `theme_change`), returns `False` for an unknown theme or in the plain terminal, and can save it as the `theme`
  setting. Frontends implement `set_theme(name) -> bool`.
- `minimal.api.normalize_key` (and `ContextSettings`, `SETTING_MERGES`) are exported for extensions that validate
  user-chosen keys.
- `declare_settings` schema entries take `"merge": "deep"` for `dict` settings: the global, project and local layers
  are merged key by key instead of the highest replacing the others.
- `minimal.report_problem(text)` records a non-fatal problem of an extension: the reload summary and the terminal UI show
  it as a warning, `/ext list` prints it and `minimal ext check` prints a `WARN` line that does not fail the exit status.

### Added

- API 2.4 screens: `ctx.ui.screen(factory)` shows your own modal Textual screen (`factory(dismiss)` runs on the UI
  thread) and returns what it is dismissed with. `ctx.ui.pick(title, items, ...)` is a themed, keyboard-driven list the
  user filters by typing: `(value, label)` or `(value, label, detail)` items, fuzzy matching, `on_highlight(value)` on
  every change of the highlighted row for live previews, `preview(value)` for a side pane. `ctx.ui.overlay(content)`
  shows scrollable read-only text (plain or styled lines), closed with Esc. The plain terminal raises `RuntimeError`
  for `screen`, prints for `overlay` and shows `choose`'s numbered list for `pick`.
- `timeout=` (seconds, default `None`) on every `ctx.ui` question: `ask`, `choose`, `choose_many`, `ask_text`, `pick`,
  `overlay` and `screen`. On expiry the dialog closes and the call returns what a cancel returns (`False` for `ask`,
  else `None`), so a worker never waits for ever. The plain terminal honours it when stdin is a terminal. Quitting
  wakes every worker waiting for an answer.
- `ctx.transcript` (`minimal.api.Transcript`): `scroll_to(message_index)`, `mark(message_index, label="")`,
  `unmark(message_index=None)`, `marks()` and `find(text, *, regex=False)`, by index into `ctx.messages`. A marked
  block gets the stable CSS class `-marked` (default: a thick accent left border) and the label as its tooltip; marks
  follow the session in view. New `Frontend` members back it and the screens (`transcript_scroll_to`,
  `transcript_mark`, `transcript_unmark`, `transcript_marks`, `screen`, `pick`, `overlay`); `ConsoleFrontend` has
  harmless defaults.
- `ui.preview_lines` setting (default 8): how many lines of a tool's output show before the fold, applied on `/reload`.

### Added

- `ctx.run_agent` / `run_agent_result` take `cwd=` (the agent's `ctx.cwd`, built-in tools, `bash`, "outside the
  workspace" checks, system prompt and permission rules follow that folder; parallel agents may use different ones),
  `reasoning_effort=` (for that agent only) and `mentions=` (expand `@file` in its prompt). `ctx.complete` and
  `complete_text` take `reasoning_effort=`, and `complete_text` also `messages=` and `options=`.
- Usage by model: `UsageTotals.by_model` (keyed `provider/model`), `model`, `provider` and `source` on `after_response`,
  which `ctx.complete` now fires too (`source` `"complete"`).
- `ctx.session_info(id=None)` and `ctx.sessions_info(limit=None, all_projects=False)` (`SessionInfo`): title, parent,
  cwd, preview, mtime, message count, creation time and lock state without loading a transcript.
- `ctx.progress(text, key=None)` keeps one slot per tool call: parallel tools and agents no longer overwrite each other;
  the terminal UI shows the latest and `(+n more)`.
- `notify` event (`NotifyEvent`: `text`, `kind`, `source`), fired by `ctx.notify` and minimal's own notices.
- `Tool.read_only` (and `register_tool`/`@minimal.tool`) may be a function `(input, ctx) -> bool` decided per call.

### Fixed

- Text printed while the terminal UI has no transcript (as it starts or quits, e.g. by a worker thread) is written to
  the real terminal instead of crashing the app with `NoMatches`.

## 0.9.0

### Added

- Tools marked `read_only` run concurrently: when one model reply calls several of them in a row, their calls overlap
  (up to `tools.max_parallel`, default 4), while confirmations, `tool_call`/`tool_result` handlers and the UI still
  see the calls in order and the results go back in order; any other call runs alone as a barrier. `read`, `grep` and
  `glob` are `read_only`. Extensions get `read_only=` on `Tool`, `Extension.register_tool` and `@minimal.tool`.
- `agent.max_turns` setting (default 50): the most model requests one turn may make; `ctx.max_turns` reads it and
  `ctx.run_agent` uses it as the default of `max_turns`.
- `image` and `thinking` blocks in the history. `read` returns a PNG, JPEG, GIF or WebP file (up to 3.75 MB) as a
  picture the model sees, and tools may return lists of `text`/`image` blocks (`minimal.api.image_block`); Anthropic,
  OpenAI-compatible servers (`image_url` data URLs) and Gemini take pictures in messages and tool results. The model's
  reasoning is kept as `thinking` blocks where a provider shows it (Anthropic with its signatures, which are sent back
  with the request; Gemini thought summaries; `reasoning_content` of OpenAI-compatible servers) and is dropped from
  requests to providers that cannot use it. Pictures show as `[image: media_type, N bytes]` in the UI, logs and
  `result_text`, never as base64.
- `reasoning_effort` setting (`low`, `medium` or `high`) and `ctx.reasoning_effort` / `ctx.set_reasoning_effort()`:
  Anthropic extended-thinking budget, OpenAI `reasoning_effort` for reasoning models, Gemini `thinking_level`;
  ignored by anything that cannot use it. `HostStatus.reasoning_effort` carries it to a status line.
- `ctx.fork(at_index=None)` continues in a new session copied from the first `at_index` messages of the current one (the
  old one stays saved and is named as `parent`), and `ctx.truncate(at_index)` drops the messages from `at_index` on and
  rewrites the session file atomically. Both refuse to split a `tool_use` from its `tool_result`, refuse while a turn
  runs, and make the terminal UI redraw the transcript. The built-in `/fork [n]` and `/rewind [n]` use them (`n`: back
  to before your n-th last message; `/rewind` defaults to 1).
- `ctx.complete_text(prompt, *, system=None, model=None, max_tokens=1024)` asks the model one question outside the
  conversation and returns the text, and `ctx.complete` takes `model=` and now goes through the `provider_error`
  handlers like a turn's request does.
- `ctx.usage` (`UsageTotals`: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `requests`)
  counts every model request of the process, agents and `ctx.complete` included; it is also in `HostStatus.usage` and
  printed by the new `/usage`. It is per process: `minimal -c` starts counting again.
- `ctx.progress(text)` shows a one-line progress note next to the working indicator while a tool or command runs
  (`Frontend.progress` for other frontends; the plain terminal prints each change on stderr).
- The permission dialog of the terminal UI offers Allow once, Allow this session, Always allow and Deny, and shows the
  narrowest rule the two wider answers add (`bash(git status*)`, `write(src/app.py)`; a `bash` command that chains,
  pipes or redirects gets none). "Always" appends it to `permissions.allow` in the project's
  `.minimal/settings.local.json`, keeps the project trusted and tells the transcript where it went; `deny` rules still
  win. `Frontend` gained `permission(question, rule, saved_in)` and `progress(text)`, and `config.add_allow_rule`
  edits the file.
- Multi-line prompt: the composer grows with the text up to `ui.composer_max_lines` rows (default 8). `Enter` sends;
  `Shift+Enter`, `Ctrl+J` or `Alt+Enter` insert a newline; pasted text keeps its lines. The prompt is now a
  `TextArea`, so a marketplace item that patches the old `Input` (`widgets/command_menu`) needs updating.
- Prompt history: `Up` on the first line and `Down` on the last walk the messages and commands sent in this project,
  keeping the draft. It is saved in `~/.minimal/data/projects/<key>/history.json` (mode 0600), capped by
  `ui.history_size` (default 500), consecutive duplicates collapsed.
- The prompt stays usable while a turn runs: a message sent meanwhile shows as `queued: …` above it and goes out when
  the turn ends (`Host.queue_message()`, `Host.queued_messages()`); a `/command` typed meanwhile is refused with a notice.
- `@file` mentions: `@` and a few letters complete a project file (`.gitignore` respected), and on sending each
  `@path` naming a text file inside the working directory is attached to the message as a `<file path="…">` block
  (cut at `ui.mention_max_chars`, default 20000). Binary files and paths outside the directory are never read.
- `ui.notify` (`off`, `bell`, `osc9`) and `ui.notify_after` (seconds, default 10): ring the bell or send an OSC 9
  desktop notification when a long turn ends while the terminal is in the background.
- `/copy [n]` copies the last (or n-th from the end) assistant message to the clipboard (OSC 52, and `pbcopy`,
  `wl-copy`, `xclip` or `xsel` when installed), and `/export [path] [--force]` writes the session as Markdown.
- Every slash command, built-in and extension, is in the `Ctrl+P` command palette; one that needs an argument is typed
  into the prompt instead of run.
- Command argument completion: `register_command(..., complete=fn)` and `@minimal.command(complete=fn)` take
  `complete(args, ctx) -> list[str]`, offered in the composer's popup (`Tab` cycles); `Host.complete_args()` and
  `Command.complete` expose it. `/theme`, `/mode`, `/resume`, `/provider`, `/login`, `/logout` and `/ext` complete their
  arguments.
- Typing `/` in the prompt lists the slash commands with their descriptions, narrowed as you type; `Up`/`Down` move,
  `Enter` runs the highlighted one and `Tab` completes it into the prompt to add arguments. The marketplace's
  `command_menu` item, which patched the old single-line prompt, is retired by this.
- `register_keybinding(key, run, description="", *, show=False, name="")` and the `@minimal.keybinding(key, ...)`
  decorator bind a global key to `run(ctx)`, run on a worker thread like a slash command; errors are shown once. Keys the
  UI needs (`RESERVED_KEYS`) and bare letters are refused, and a duplicate key between extensions is reported on
  reload. `Host.keybindings` lists them. `extensions/example.py` shows a key and an ordered panel.
- `order` keyword for `register_widget`, `register_panel` and `register_status`, and the new widget areas `left`
  (a second sidebar) and `footer` (under the status line) in `WIDGET_AREAS`; `Registry.placement_index` breaks ties.
- `ui.*` settings, applied on `/reload`: `ui.sidebar` (`right`, `left`, `hidden`), `ui.sidebar_width`, `ui.widgets` and
  `ui.status` (move, reorder or hide any widget, panel or status segment, minimal's own included, by name), `ui.keys`
  (rebind `interrupt`, `toggle_sidebar`, `cycle_mode` and extension keybindings), `ui.prompt_glyph`, `ui.placeholder`,
  `ui.density` (`compact` removes the blank lines between transcript blocks) and `ui.css` (a Textual stylesheet from
  `~/.minimal`, global only; one that fails to parse is reported and ignored).
- `/ui` shows the effective layout (areas, status segments, keys, problems) and `/ui sidebar|density|hide|show|move|reset`
  save changes to the global settings and reload. `config.update_global` accepts dotted keys such as `ui.sidebar`.
- Extension metadata `__depends__` and `__conflicts__` (lists of extension ids): a dependency loads first and a
  missing or failed one skips the extension (`needs 'x': install it with /ext`); of two conflicting extensions the
  first loaded stays. `minimal ext check` validates them and `/ext list` shows them.
- `ctx.invalidate()` redraws panels, status segments and widgets from any thread (cheap, coalesced), so a poller can
  replace a panel `interval`. `ctx.theme` (`ThemeInfo`: name, `dark`, every colour as `#rrggbb`) and the `theme_change`
  event give extensions the current theme, and styled text accepts `$name` (a theme colour such as `$code`, or a CSS
  variable such as `$border-muted`) and `#rrggbb` next to the old style names, so a panel follows `/theme`.
- `register_panel` and `register_widget` take `title=` and `collapsible=`: a heading line above the panel or widget, and
  a click or Enter on it collapses it. `ui.widgets.<name>.title` and `.collapsed` and `/ui title|collapse|expand` let the
  user change them.
- `minimal.register_css(name, css)` adds a Textual stylesheet from an extension (replaced by name, dropped on reload, a
  parse error reported and ignored). The selectors it may rely on are documented and pinned by a test.
- `declare_settings(defaults, schema)` takes a `description`, `enum`, `min` and `max` per key: a value outside it is a load
  error naming `extensions.settings.<id>.<key>` and the default stays. `/ext settings` lists every extension's settings
  with value, default and description, and `/ext settings <id> <key> <value|reset>` edits the global settings file and
  reloads.
- `ctx.context_window` (the `context_window` setting, else a table of the built-in providers' models), `ctx.git()` (branch,
  ahead/behind, staged/unstaged/untracked counts and changed files, from one shared poller every `git.refresh` seconds that
  the status line uses too) and `ctx.keybindings()` (every bound key with its source and description) replace what UI
  extensions computed themselves.

### Changed

- `HostStatus` has `usage` and `truncations`; the terminal UI redraws its transcript when the latter changes.
- `WIDGET_AREAS` gained `left` and `footer`; the marketplace's `git_panel` offers them in its `area` setting.
- The status line's own segments (`model`, `path`, `git`, `mode`, `context`, `extensions`, `session`) are named
  segments that `ui.status` can hide, move or reorder; an extension segment with the same name replaces one.

## 0.8.0

### Added

- Named marketplaces: an entry of the global `marketplaces` setting may be `{"name": "team", "url": "…"}` as well as a
  URL. `ext_search` and `/ext` show the name instead of the URL, `ext_install` and `ext_publish` accept it as
  `marketplace`, and adding a marketplace in `/ext` asks for an optional name. The default marketplace is called
  `minimal-marketplace`. Names are unique and use letters, digits, `-` and `_`.
- `__tags__ = ["git", "ui"]` extension metadata (optional, lowercase). `ext_search` matches a query against the name,
  the description and the tags, and listings show the tags.
- `/ext search <query> [category]` searches every marketplace from the command line.

## 0.7.0

### Added

- `minimal --version` (`-V`) prints the installed version and exits, with no terminal needed.
- Extension API 2.2 (additions only: every extension for API 2.0 or 2.1 keeps working). Modes:
  `minimal.register_mode(name, description=..., tools=None, exclude_tools=(), system_note="", read_only=False)` adds a
  mode, a restriction on the tools the model may use. While one is current (`/mode <name>`, `/mode off`, `Shift+Tab` in
  the terminal UI, `ctx.set_mode(name)`, `ctx.cycle_mode()`; `ctx.mode`, `ctx.modes()` and `ctx.mode_info()` read them) the other tools
  are not offered to the model, and a call to one is refused with an error that names the mode and says only the user
  can change it. The check is in the tool path, so it holds for tools a mode's agents start too: `ctx.run_agent(...,
  tools=[...])` can only narrow a mode. The mode's `system_note` joins the system prompt, the status line shows the mode
  (only when one is set) and `mode_change` fires on every switch. A reload keeps the mode if an extension registers it
  again and otherwise falls back to no mode with a warning.
- Agent identity. Every loop that talks to the model has an `agent_id` (`"main"` for your turn, a short id for each
  `ctx.run_agent`) and a `parent_id`. `tool_call`, `tool_result`, `turn_end`, `before_request`, `after_response` and
  `provider_error` carry both (`nested` stays), and so do the calls the terminal UI's widgets and a `Frontend` are
  shown. New events `agent_start` and `agent_end` (status, text, token usage) bracket each loop; `ctx.agents()` (and
  `Host.agents()`) list the agents of the session, running and finished, with status, model, tool-call count and usage.
  `ctx.agent_id` and `ctx.parent_id` say which agent a tool's `ctx` belongs to.
- `ctx.run_agent` takes `name`, `system` (the agent's whole system prompt), `tools` (an allow-list; unknown names raise
  `ValueError`), `model`, `provider` (another provider for that agent only; the app's own does not change) and
  `max_turns`. `ctx.run_agent_result(...)` takes the same and returns an `AgentResult` (`agent_id`, `text`, `usage`,
  `status` of `done`, `interrupted`, `error` or `max_turns`, `error`, `messages`) instead of raising. Each agent has its
  own cancel flag: `ctx.cancel_agent(agent_id)` stops that agent and the ones it started, and its parent carries on with
  the result; Esc still stops everything.
- The system prompt is made of named sections (`base`, `environment`, `extending`, `instructions`, `append`, then what
  extensions add). `minimal.register_prompt_section(name, text_or_function, replace=None)` adds one or replaces another
  (`replace="base"`), `minimal.remove_prompt_section(name)` drops one, and `ctx.prompt_sections()` lists them. The
  default prompt is byte for byte what it was.
- `ctx.project_trusted` and `minimal.project_trusted`: whether you trusted the project with `/trust` (or started with
  `--trust-project`), for extensions that would act on what a repository controls.

### Changed

- The `/trust` scan reports `register_prompt_section` and `remove_prompt_section` as it does `append_system_prompt`
  (it "adds to, replaces or removes parts of the system prompt").

## 0.6.0

### Breaking

- The `claude` provider (a Claude Pro/Max subscription through a browser login) is no longer built in: it is the
  marketplace item `claude_subscription`. Install it with `minimal ext install claude_subscription` (or `/ext`), then
  `minimal --login claude`. A login saved in `~/.minimal/claude-auth.json` moves to the item's data folder the first
  time it is used; `provider: "claude"` in settings keeps working once the item is installed. `minimal.providers.claude`
  and `minimal.providers.claude_auth` are gone.
- `gemini` and `openai` are extras: `uv tool install 'mnml-agent[gemini]'`, `'mnml-agent[openai]'` or
  `'mnml-agent[all]'`. `anthropic` stays a dependency. Picking a provider whose package is missing says what to install.
- `minimal --login` and `--logout` take an optional provider name and act on any provider that has a login; with none
  installed they say so. `/login` and `/logout` do the same for the current provider.
- `python -m minimal.extensions.logo` is `scripts/logo.py` of the repository (`uv run scripts/logo.py IMAGE`): the
  converter is not part of the installed package.

### Added

- Extension API 2.1 (additions only: every extension for API 2 keeps working). `minimal.add_provider(spec)` registers a
  ready-made `ProviderSpec`, and `minimal.api.openai_compatible(name, base_url, default_model, key_env=None)` builds
  one for an OpenAI-compatible server with its own URL and key (or none, for a local one): Ollama, OpenRouter and the
  `openai` provider can all be registered at once. `ProviderSpec` gains `sdk` and `install` (the optional package it
  imports and what installs it), and `login`, `logout` and `account` for providers that sign in instead of using a key.
  `minimal.api.AnthropicProvider` is exported to subclass.
- `minimal ext install [--project] [<category>/]<name>` installs one marketplace item from the command line (a global
  install shows the static scan and asks first, as `/ext` does), so a provider that is not built in can be added before
  the first start.

### Changed

- Setup asks through the `Frontend` (numbered lists on a console) and reads the key hidden with `getpass`;
  `questionary` is no longer a dependency. A provider whose package is not installed is shown as needing it.
- The model's `ext_search`, `ext_install` and `ext_publish` tools are registered by a bundled extension,
  `marketplace_tools`, loaded like any other before the rest (scope `bundled`, not listed as loaded by a reload). Put
  `marketplace_tools` in `extensions.disabled` to remove the tools and the sentence about them in the system prompt;
  `/ext` is unaffected.
- An unknown provider's error says an extension can add one.
- A key saved in setup still wins over the same variable in the shell; docs/settings.md and the setup message now say
  so.
- CI installs the extras (`uv sync --locked --all-extras`).

## 0.5.0

### Breaking

- Extension API 2.0: the theme names of `minimal.api.theme` (`DEFAULT`, `MINIMAL`, `build`, `available`) were removed:
  the colour themes are internal to the terminal UI (`minimal.ui.tui.theme`), and importing the old names says so
  (`minimal.api.REMOVED`). Removing names is a major version, so extensions declare `__api__ = "2"`; those written for
  API 1 stop loading until a version for 2 is installed.

### Added

- `edit` takes `replace_all` (change every occurrence) and `edits` (several replacements to one file, applied in
  order, all or none), and returns the changed lines numbered like `read`. The terminal UI shows a diff for each.
- `/title [text]` names the session. The title, and the session a new one was started from with `ctx.new_session`
  (`parent`, e.g. by `compact`), are kept in the session file's header and shown by `/sessions` and `/resume`.
- Extension API 1.3: `minimal.register_panel(name, render, area, live, interval)` adds styled text lines to the
  terminal UI without Textual code (styles are space-separated names from `minimal.api.STYLES`, e.g. `"bold muted"`);
  `live(ctx, event)` gets the turn and tool events plus a new `end` event, and `interval` redraws it on a timer.
  `minimal.register_renderer(kind, render)` draws Markdown fences (`fence:mermaid`), GitHub alerts (`blockquote:alert`)
  and tool-call bodies (`tool:edit`) in the terminal UI. `PanelSpec`, `RendererSpec`, `STYLES`, `styled_line`,
  `styled_lines` and `glyphs` are in `minimal.api`.

### Changed

- The Textual UI stays part of minimal (`textual` is a dependency) and is its only interface: `minimal` always opens it.
  `cli` imports Textual only when the UI opens, so `minimal --help` and `--login` do not load it. Extensions live flat
  in `~/.minimal/extensions/` and `./.minimal/extensions/`, and the marketplace is flat (`<category>/`), as in 0.4.
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
