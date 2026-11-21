# Settings

minimal reads its settings from up to three JSON files, lowest first:

| Scope     | File                           | Purpose                                                                 |
| --------- | ------------------------------ | ----------------------------------------------------------------------- |
| `global`  | `~/.minimal/settings.json`     | Your defaults. `minimal --setup`, `/setup`, `/theme`, `/ui` and `/ext` save here |
| `project` | `.minimal/settings.json`       | This project's settings, committed and shared with it                   |
| `local`   | `.minimal/settings.local.json` | Your personal settings for this project (keep it out of git)           |

Then come the environment (`MINIMAL_PROVIDER`, `MINIMAL_MODEL`) and the command line (`--provider`, `-m`), which beat every file. Missing files are skipped. `/settings` lists the files and every effective value with the scope it comes from. Prompt, permission, extension, `ui.*`, `reasoning_effort`, `agent.max_turns` and `tools.max_parallel` settings are re-read on `/reload`; the provider, model and theme are read at startup.

Secrets and state stay out of settings: API keys in `~/.minimal/keys.json` (a key saved there beats the same variable in your shell, which is why setup warns that it is used instead of the environment), the logins of providers that extensions add in their own data folder (`~/.minimal/data/global/<id>/`), trust decisions in `~/.minimal/trusted.json`, sessions in `~/.minimal/sessions/`.

## Keys

Nested objects group the keys: `prompt.append` is `{"prompt": {"append": …}}`. A top-level `$schema` is ignored.

| Key                   | Type                         | Layers read  | Merge                                                                |
| --------------------- | ---------------------------- | ------------ | -------------------------------------------------------------------- |
| `provider`            | string                       | all          | highest layer wins                                                   |
| `model`               | string                       | all          | highest layer wins                                                   |
| `theme`               | string                       | all          | highest layer wins                                                   |
| `max_tokens`          | positive integer, or object: `"provider"` or `"provider/model"` → positive integer | all | highest layer wins; default: each provider's own (8192 for Anthropic, Claude and Gemini, none sent to OpenAI-compatible servers) |
| `reasoning_effort`    | `"low"`, `"medium"` or `"high"` | all       | highest layer wins; default: unset (each provider's own default) |
| `agent.max_turns`     | positive integer             | all          | highest layer wins; default `50`                                      |
| `tools.max_parallel`  | positive integer             | all          | highest layer wins; default `4`                                       |
| `marketplaces`        | list of strings (git URLs or paths) or `{"name": "…", "url": "…"}` objects | global only | — (a name is shown instead of the URL and can be passed as `marketplace` to `ext_install`/`ext_publish`) |
| `prompt.base`         | string: path of the base system prompt (`~` allowed) | global only | — (default: the packaged prompt)          |
| `prompt.append`       | string or list of strings    | all          | concatenated global → project → local, added to the system prompt     |
| `prompt.files`        | list of strings              | all          | highest layer wins; default `["AGENTS.md"]`                          |
| `permissions.allow`   | list of rules                | all          | concatenated; project/local only once trusted                        |
| `permissions.deny`    | list of rules                | all          | concatenated                                                         |
| `extensions.disabled` | list of extension ids        | all          | union; project/local only once trusted                               |
| `extensions.settings` | object: extension id → object | all         | per id, each layer's object updates the lower ones; project/local only once trusted |
| `ui.composer_max_lines` | positive integer           | all          | highest layer wins; default `8`                                      |
| `ui.history_size`     | integer ≥ 0                  | all          | highest layer wins; default `500`                                    |
| `ui.mention_max_chars` | positive integer            | all          | highest layer wins; default `20000`                                  |
| `ui.notify`           | `"off"`, `"bell"` or `"osc9"` | all         | highest layer wins; default `"off"`                                  |
| `ui.notify_after`     | number ≥ 0 (seconds)         | all          | highest layer wins; default `10`                                     |
| `ui.sidebar`          | `right`, `left` or `hidden`  | all          | highest layer wins; default `right`                                  |
| `ui.sidebar_width`    | integer, 20 to 80            | all          | highest layer wins; default 36                                       |
| `context_window`      | positive integer             | all          | highest layer wins; the model's context size in tokens (`ctx.context_window`), over what minimal knows; default: unset |
| `git.refresh`         | number > 0 (seconds)         | all          | highest layer wins; seconds between the shared git polls behind `ctx.git()` and the status line; default `3` |
| `ui.widgets`          | object: widget or panel name → `{"area", "order", "enabled", "title", "collapsed"}` | all | per name, field by field, higher layers update lower ones |
| `ui.status`           | object: segment name → `{"side", "order", "enabled"}` | all | per name, field by field, higher layers update lower ones   |
| `ui.keys`             | object: action or keybinding id → key | all | per id, higher layers update lower ones                              |
| `ui.prompt_glyph`     | string, 1 to 3 characters    | all          | highest layer wins; default the theme's `❯`                          |
| `ui.placeholder`      | string                       | all          | highest layer wins; default `Message, or / for commands`             |
| `ui.css`              | string: path of a Textual CSS file, relative to `~/.minimal` (`~` allowed) | global only | —                                  |
| `ui.density`          | `normal` or `compact`        | all          | highest layer wins; default `normal`                                 |
| `ui.preview_lines`    | positive integer             | all          | highest layer wins; default `8`                                      |

- **`prompt.files`** are read relative to the working directory and added to the system prompt as `Project instructions (<name>)`. Missing files are skipped. Files set by the project or local layer must stay inside the project; others are ignored.
- **`max_tokens`** is the output-token limit of each model request. `{"max_tokens": 16000}` applies to every model; `{"max_tokens": {"anthropic": 16000, "openai/gpt-5.5": 32000}}` picks by `provider/model` first, then by provider, and leaves the rest at their default.
- **`reasoning_effort`** asks the model to think less or more. Anthropic turns extended thinking on with a token budget of 1024, 4096 or 16000 (the budget is added to `max_tokens`, or at most half of a `max_tokens` you set; thinking stays off if half of it is under 1024, and a request with thinking takes no `temperature`). OpenAI-compatible servers get it as `reasoning_effort` when the model is a reasoning one (`o1`, `o3`…, `gpt-5…`, also as `vendor/gpt-5`) and not otherwise. Gemini gets it as `thinking_level` (with thought summaries on). A provider or model that cannot use it ignores it. Extensions read it as `ctx.reasoning_effort` and change it for the session with `ctx.set_reasoning_effort(...)`.
- **`agent.max_turns`** is the most model requests one user turn may make; a turn still calling tools after that many ends with an error. It is also the default of `ctx.run_agent`'s `max_turns` (`ctx.max_turns` reads it).
- **`tools.max_parallel`** is how many tool calls run at once when one reply calls several tools marked `read_only` in a row (`read`, `grep`, `glob`, and extension tools registered with `read_only=True`). Every other call runs alone, in order; `1` runs everything one by one.
- **`extensions.disabled`** lists ids (`compact`, `tools/subagent`, see [extensions](extensions.md#layout-and-where-to-save)) not to load from `~/.minimal/extensions` or `.minimal/extensions`, and the id of a bundled extension: `marketplace_tools` removes the model's `ext_search`, `ext_install` and `ext_publish`. Extensions passed with `--ext` always load.
- **`extensions.settings`** holds the values an extension declares with `declare_settings` ([extensions](extensions.md#settings)).
- **`ui.composer_max_lines`**, **`ui.history_size`**, **`ui.mention_max_chars`**, **`ui.notify`** and **`ui.notify_after`** shape the terminal UI's prompt (see [the composer](../README.md#the-composer)); `/reload` applies a change:
  - `ui.composer_max_lines` (default `8`): rows the prompt grows to for a multi-line message before it scrolls.
  - `ui.history_size` (default `500`): entries kept in the project's prompt history, `~/.minimal/data/projects/<key>/history.json` (mode 0600; `<key>` is the hash an extension's [`data_dir`](extensions.md#layout-and-where-to-save) uses). `0` keeps none. Consecutive duplicates are saved once.
  - `ui.mention_max_chars` (default `20000`): characters of each `@file` mention appended to the message; the rest is cut with a note.
  - `ui.notify` (`"off"` by default, `"bell"` or `"osc9"`) and `ui.notify_after` (seconds, default `10`): when a turn that took longer than `notify_after` ends and the terminal is in the background, ring the bell or send the OSC 9 desktop notification (iTerm2, WezTerm, Kitty, Ghostty and others). A terminal that never reports focus changes counts as in the background, so it is notified every time.
  - `ui.preview_lines` (default `8`): how many lines of a tool call's output show under it before the fold (`… N more lines · click to expand`; for `bash` the last lines, with `… N earlier lines`). An output of at most that many lines shows whole; a click expands the rest (up to 500 lines). `/reload` applies it, to the blocks already in the transcript too.

A file that is not valid JSON is left out entirely. An unknown key, a value of the wrong type, an invalid permission rule, or a global-only key in a project file is dropped. Each problem is reported by `/reload` and `/settings` with the file's path, and the rest still applies.

## UI settings

The terminal UI is shaped without code by the `ui` group. Every key applies on `/reload`, no restart; `/ui` shows the effective result and changes most of them for you ([`/ui`](#ui)).

```json
{
  "ui": {
    "sidebar": "left",
    "sidebar_width": 44,
    "density": "compact",
    "prompt_glyph": "λ",
    "placeholder": "ask me anything",
    "widgets": {"session": {"area": "footer", "order": -5}, "token_sparkline": {"enabled": false}},
    "status": {"git": {"side": "right"}, "model": {"order": 50}, "clock": {"enabled": false}},
    "keys": {"toggle_sidebar": "ctrl+t", "example:stats": "alt+s", "cycle_mode": "f2"},
    "css": "my.tcss"
  }
}
```

- **`ui.sidebar`** puts the `sidebar` area on the `right` (default) or the `left` of the transcript, or hides it: widgets and panels placed in it are not even created. The `left` area (a second column, always on the left) and `ui.sidebar_width` (both columns) are independent of it. `Ctrl+B` (`toggle_sidebar`) collapses and restores both columns.
- **`ui.widgets`** overrides where an extension put a widget or a panel, by the name it registered (`/ui` lists them): `area` is one of `top`, `bottom`, `sidebar`, `left`, `footer`; `order` is an integer (lower first, default `0`; ties go to registration order); `enabled: false` hides it, and it is not created. Any field left out keeps the extension's value. A name that is not installed is reported.
  A widget or panel may also have `"title"` (its heading line: text, `""` for none) and `"collapsed"` (`true` starts a collapsible one collapsed): `{"session": {"title": "SESSION", "collapsed": true}}`.
- **`ui.status`** does the same for status-line segments: `side` is `left` or `right`. minimal's own segments are named `model`, `path`, `git`, `mode` (left, orders -40, -30, -20, -10) and `context`, `extensions`, `session` (right, orders 100, 105, 110), so `{"ui": {"status": {"session": {"enabled": false}}}}` removes the session id. An extension segment with one of these names replaces the built-in one.
- **`ui.keys`** rebinds keys: `interrupt` (default `escape`), `toggle_sidebar` (`ctrl+b`) and `cycle_mode` (`shift+tab`) are minimal's actions; an extension's keybinding is `<extension id>:<name or description>` (`/ui` lists the ids). The value is a key in Textual's spelling (`ctrl+t`, `alt+s`, `f2`, `ctrl+shift+left`), or `""` to unbind. Keys the UI needs for typing and navigation are refused (`escape`, `ctrl+c`, `ctrl+p`, `ctrl+q`, `enter`, `tab`, arrows, the prompt's editing keys, …; `docs/extensions.md` lists them), a key bound twice is refused for the one asking, and a bare letter is refused. Problems are shown as `[ui] ...` errors on reload and by `/ui`; the binding keeps its default key.
- **`ui.prompt_glyph`** and **`ui.placeholder`** replace the prompt's `❯` and its hint text.
- **`ui.css`** is a [Textual CSS](https://textual.textualize.io/guide/CSS/) file loaded after the UI's own styles, so its rules win ties: `#statusline { background: #202040; }`, `.user { margin-top: 0; }`, `#transcript { padding: 0 3; }`. It is read from `~/.minimal` (an absolute path or `~` also works) and only from the global settings, because a stylesheet from a repository could hide what a tool does. The file is read again on every `/reload`; one that cannot be read or has a parse error is reported (`[ui] ui.css: … was not applied: line 3: …`) and ignored, the UI keeps working. Widget ids and classes you can style: `#top`, `#bottom`, `#footer`, `#left`, `#sidebar`, `#transcript`, `#working`, `#composer`, `#prompt`, `#prompt-glyph`, `#statusline`, `#status-left`, `#status-right`, `.user`, `.tool`, `.output`, `.error`, `Markdown.assistant`, and the theme variables (`$accent`, `$muted`, …; `src/minimal/ui/tui/theme.py`).
- **`ui.density`** `compact` removes the blank lines between transcript blocks (messages, tool calls, output, the welcome header), by a `-compact` class on the app that your `ui.css` can use too (`MinimalTui.-compact .user { … }`).

`ui.widgets`, `ui.status` and `ui.keys` merge across layers by name, so a project can move one widget and keep your other overrides; every other `ui` key is won by the highest layer.

## Trust

A project's files come from its repository. Most keys apply as soon as you open the project, but the three that grant something or switch protections off apply only after `/trust` (or `--trust-project`):

- `permissions.allow`: a project could otherwise approve its own commands.
- `extensions.disabled`: a project could otherwise turn off a global guardrail extension.
- `extensions.settings`: values that change what extensions do.

Untrusted, they are skipped: the status line counts them with the project's extensions (`⚠ N untrusted`) and `/settings` marks them `(not trusted: /trust)`. `/trust` shows them alongside the extension scan, and the decision covers exactly what was shown: editing any of these keys makes the project untrusted again. `permissions.deny` and every other key always apply. `marketplaces`, `prompt.base` and `ui.css` are never read from a project.

## Permissions

A rule is a tool name, `name`, or a tool name with a pattern, `name(pattern)`:

- **`deny`** rules are checked first. A matching call fails with `denied by permission rule '<rule>' (<scope> settings)`, even with `-y`, and even for tools that never ask.
- **`allow`** rules let a matching call run without asking for confirmation.
- Anything else asks as usual.

The pattern matches the tool's subject:

| Tool                                        | Subject   | Pattern                                                                                                       |
| ------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------- |
| `bash`                                      | `command` | shell-style wildcard over the whole command (`*`, `?`, `[…]`)                                                 |
| `read`, `write`, `edit`, `grep`, `glob`     | `path`    | glob (`**` crosses folders; `~` in the pattern expands, `~` in a tool's path does not, as for the tools). A relative pattern matches the path relative to the working directory, or the absolute path when outside it; an absolute or `~` pattern always matches the absolute path. `grep`/`glob` without a path mean `.` |
| extension tools with `subject_key`          | that input field | shell-style wildcard                                                                                   |

A `bash` allow rule never matches a command containing `;`, `&`, `|`, `` ` ``, `$(`, `>`, `<` or a newline, so `bash(git status*)` does not approve `git status && rm -rf x`.

A `bash` deny rule matches the whole command **or any command inside it**: the pieces between `;`, `&&`, `||`, `&`, `|`, newlines, `$( )`, backticks, `( )` and `{ }` (without leading `if`, `then`, `do`, `time`, …). `bash(rm -rf*)` therefore blocks `cd /tmp && rm -rf x`, `echo $(rm -rf x)` and `if true; then rm -rf x; fi`. Quotes are not interpreted, so a piece inside quotes can match too. A deny rule is a guardrail, not a sandbox: it does not look inside `sh -c "…"`, `eval`, `xargs`, `find -exec`, scripts, or wrappers such as `sudo` or `env`. Start the pattern with `*` (`bash(*rm -rf*)`) to catch those too.

| Rule                           | Effect                                                     |
| ------------------------------ | ---------------------------------------------------------- |
| `"allow": ["bash(git status*)", "bash(uv run pytest*)"]` | run these commands without asking  |
| `"allow": ["write(src/**)", "edit(src/**)"]` | write and edit under `src/` without asking   |
| `"allow": ["reload_extensions"]` | reload without asking                                    |
| `"deny": ["read(.env)", "read(**/*.pem)"]` | never read these files                         |
| `"deny": ["bash(*rm -rf*)"]`   | never run these commands                                   |
| `"allow": ["fetch(https://docs.python.org/*)"]` | an extension tool `fetch` with `subject_key="url"` |

### Allow this session, allow always

The terminal UI's permission dialog offers **Allow once**, **Allow this session**, **Always allow** and **Deny** (arrows and `enter`, or `y`, `s`, `a`, `n`/`esc`). The dialog shows the rule the two wider answers add, always the narrowest one that covers the call:

- `bash`: the command's first two words when the second is a plain subcommand (`git status --short` → `bash(git status*)`); otherwise the exact command. Commands that run other programs or change many files by their arguments (`sh`, `env`, `sudo`, `xargs`, `find`, interpreters such as `python`, `rm`, `curl`, …) and commands with options right after the first word always get the exact command. A command that chains, pipes, substitutes or redirects (see above) gets **no rule**: no allow rule can match it, so the dialog offers only once or deny.
- file tools: the exact path (relative to the working directory when inside it, so `write(src/app.py)`); a path containing `[` gets no rule.
- other tools: `name(subject)` with their exact subject, or the bare `name` for a tool without a `subject_key`.

*This session* keeps the rule in memory until minimal exits (a `/reload` and `/new` keep it). *Always* also appends it to `permissions.allow` in the project's `.minimal/settings.local.json` (not committed; created with mode 0600, everything else in the file is kept) and the transcript says which rule went where. It is offered when the project's trust-gated settings are trusted or it has none; the rule you just chose counts as trusted, so `/trust` is not asked again for it. In a project with unreviewed extensions or settings only *this session* is offered; run `/trust` first. If the file cannot be edited (invalid JSON, say) it is left alone and the rule holds for the session. `deny` rules still win over every allowed call, `-y` is unchanged, and the plain terminal keeps its yes/no question.

## `/settings`

```
/settings
```

prints each file with its scope (or `(not found)`), then one line per effective value, `key  value  (scope)`; lists get one line per item and `extensions.settings` one line per extension id. Trust-gated values that do not apply yet end with `(not trusted: /trust)`. The last reload's settings errors come last.

## `/ui`

```
/ui                                 show the layout and the keys
/ui sidebar right|left|hidden       move or hide the sidebar
/ui density normal|compact          spacing between transcript blocks
/ui hide <name>   /ui show <name>   hide or show a widget, panel or status segment
/ui move <name> <area> [order]      place it in an area (or status side: left, right), optionally at an order
/ui reset [name]                    forget the overrides of one name; without a name, of the whole layout
/ui title <name> [text]             set a widget's or panel's heading (no text: the extension's; '' hides it)
/ui collapse <name>   /ui expand <name>   collapse a collapsible widget or panel to its heading, or open it
```

`/ui` prints the sidebar and density, then every area with its widgets and panels in order, the two sides of the status line with their segments in order (each marked `[extension]` or `[built-in]`, and `user: area order` for what your settings changed), what is hidden, the keys with the actions they run, and the problems found in the `ui.*` settings. The other forms save to `~/.minimal/settings.json` (only the keys they change, atomically, mode 0600) and reload, so the change shows at once. An unknown name lists the valid ones. `reset` without a name clears `ui.sidebar`, `ui.density`, `ui.widgets` and `ui.status`, not `ui.keys`, `ui.css`, the glyph or the placeholder. If a project or local file sets the same `ui` key it beats the global one, and `/ui` says so.

## Examples

`~/.minimal/settings.json`:

```json
{
  "provider": "claude",
  "model": "claude-sonnet-5-5",
  "theme": "dark",
  "marketplaces": ["https://github.com/me/my-marketplace.git", {"name": "team", "url": "git@example.com:team/market.git"}],
  "prompt": {"append": "Prefer small, focused diffs."},
  "permissions": {
    "allow": ["bash(git status*)", "bash(git diff*)"],
    "deny": ["read(~/.ssh/**)"]
  },
  "extensions": {
    "disabled": ["widgets/token_sparkline"],
    "settings": {"compact": {"compact_at": 150000}}
  }
}
```

`.minimal/settings.json` (committed with the project):

```json
{
  "prompt": {"files": ["AGENTS.md", "docs/CONVENTIONS.md"], "append": ["Run the tests with `uv run pytest`."]},
  "permissions": {
    "allow": ["bash(uv run pytest*)"],
    "deny": ["read(.env)", "edit(migrations/**)"]
  },
  "extensions": {"settings": {"example": {"blocked": ["\\bgit\\s+push\\b"]}}}
}
```

`.minimal/settings.local.json` (personal, not committed):

```json
{
  "model": "claude-opus-5-5",
  "permissions": {"allow": ["write(src/**)", "edit(src/**)"]}
}
```
