# Settings

minimal reads its settings from up to three JSON files, lowest first:

| Scope     | File                           | Purpose                                                                 |
| --------- | ------------------------------ | ----------------------------------------------------------------------- |
| `global`  | `~/.minimal/settings.json`     | Your defaults. `minimal --setup`, `/setup`, `/theme` and `/ext` save here |
| `project` | `.minimal/settings.json`       | This project's settings, committed and shared with it                   |
| `local`   | `.minimal/settings.local.json` | Your personal settings for this project (keep it out of git)           |

Then come the environment (`MINIMAL_PROVIDER`, `MINIMAL_MODEL`) and the command line (`--provider`, `-m`), which beat every file. Missing files are skipped. `/settings` lists the files and every effective value with the scope it comes from. Prompt, permission and extension settings are re-read on `/reload`; the provider, model and theme are read at startup.

Secrets and state stay out of settings: API keys in `~/.minimal/keys.json`, the Claude login in `~/.minimal/claude-auth.json`, trust decisions in `~/.minimal/trusted.json`, sessions in `~/.minimal/sessions/`.

## Keys

Nested objects group the keys: `prompt.append` is `{"prompt": {"append": …}}`. A top-level `$schema` is ignored.

| Key                   | Type                         | Layers read  | Merge                                                                |
| --------------------- | ---------------------------- | ------------ | -------------------------------------------------------------------- |
| `provider`            | string                       | all          | highest layer wins                                                   |
| `model`               | string                       | all          | highest layer wins                                                   |
| `theme`               | string                       | all          | highest layer wins                                                   |
| `ui`                  | string: a frontend name (`tui`, `console` or one an extension registers) | all | highest layer wins; `--ui` beats it; default `tui` |
| `max_tokens`          | positive integer, or object: `"provider"` or `"provider/model"` → positive integer | all | highest layer wins; default: each provider's own (8192 for Anthropic, Claude and Gemini, none sent to OpenAI-compatible servers) |
| `marketplaces`        | list of strings (git URLs or paths) | global only | —                                                         |
| `prompt.base`         | string: path of the base system prompt (`~` allowed) | global only | — (default: the packaged prompt)          |
| `prompt.append`       | string or list of strings    | all          | concatenated global → project → local, added to the system prompt     |
| `prompt.files`        | list of strings              | all          | highest layer wins; default `["AGENTS.md"]`                          |
| `permissions.allow`   | list of rules                | all          | concatenated; project/local only once trusted                        |
| `permissions.deny`    | list of rules                | all          | concatenated                                                         |
| `extensions.disabled` | list of extension ids        | all          | union; project/local only once trusted                               |
| `extensions.settings` | object: extension id → object | all         | per id, each layer's object updates the lower ones; project/local only once trusted |

- **`prompt.files`** are read relative to the working directory and added to the system prompt as `Project instructions (<name>)`. Missing files are skipped. Files set by the project or local layer must stay inside the project; others are ignored.
- **`max_tokens`** is the output-token limit of each model request. `{"max_tokens": 16000}` applies to every model; `{"max_tokens": {"anthropic": 16000, "openai/gpt-5.5": 32000}}` picks by `provider/model` first, then by provider, and leaves the rest at their default.
- **`extensions.disabled`** lists ids (`compact`, `tools/subagent`, see [extensions](extensions.md#layout-and-where-to-save)) not to load from `~/.minimal/extensions` or `.minimal/extensions`. Extensions passed with `--ext` always load.
- **`extensions.settings`** holds the values an extension declares with `declare_settings` ([extensions](extensions.md#settings)).

A file that is not valid JSON is left out entirely. An unknown key, a value of the wrong type, an invalid permission rule, or a global-only key in a project file is dropped. Each problem is reported by `/reload` and `/settings` with the file's path, and the rest still applies.

## Trust

A project's files come from its repository. Most keys apply as soon as you open the project, but the three that grant something or switch protections off apply only after `/trust` (or `--trust-project`):

- `permissions.allow`: a project could otherwise approve its own commands.
- `extensions.disabled`: a project could otherwise turn off a global guardrail extension.
- `extensions.settings`: values that change what extensions do.

Untrusted, they are skipped: the status line counts them with the project's extensions (`⚠ N untrusted`) and `/settings` marks them `(not trusted: /trust)`. `/trust` shows them alongside the extension scan, and the decision covers exactly what was shown: editing any of these keys makes the project untrusted again. `permissions.deny` and every other key always apply. `marketplaces` and `prompt.base` are never read from a project.

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

## `/settings`

```
/settings
```

prints each file with its scope (or `(not found)`), then one line per effective value, `key  value  (scope)`; lists get one line per item and `extensions.settings` one line per extension id. Trust-gated values that do not apply yet end with `(not trusted: /trust)`. The last reload's settings errors come last.

## Examples

`~/.minimal/settings.json`:

```json
{
  "provider": "claude",
  "model": "claude-sonnet-5-5",
  "theme": "dark",
  "marketplaces": ["https://github.com/me/my-marketplace.git"],
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
