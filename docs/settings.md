# Settings

minimal reads its settings from up to three JSON files, lowest first:

| Scope     | File                           | Purpose                                                                 |
| --------- | ------------------------------ | ----------------------------------------------------------------------- |
| `global`  | `~/.minimal/settings.json`     | Your defaults. `minimal --setup`, `/setup`, `/theme` and `/ext` save here |
| `project` | `.minimal/settings.json`       | This project's settings, committed and shared with it                   |
| `local`   | `.minimal/settings.local.json` | Your personal settings for this project (keep it out of git)           |

Then come the environment (`MINIMAL_PROVIDER`, `MINIMAL_MODEL`) and the command line (`--provider`, `-m`), which beat every file. Missing files are skipped. `/settings` lists the files and every effective value with the scope it comes from. Prompt, permission, extension, `reasoning_effort`, `agent.max_turns` and `tools.max_parallel` settings are re-read on `/reload`; the provider, model and theme are read at startup.

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

- **`prompt.files`** are read relative to the working directory and added to the system prompt as `Project instructions (<name>)`. Missing files are skipped. Files set by the project or local layer must stay inside the project; others are ignored.
- **`max_tokens`** is the output-token limit of each model request. `{"max_tokens": 16000}` applies to every model; `{"max_tokens": {"anthropic": 16000, "openai/gpt-5.5": 32000}}` picks by `provider/model` first, then by provider, and leaves the rest at their default.
- **`reasoning_effort`** asks the model to think less or more. Anthropic turns extended thinking on with a token budget of 1024, 4096 or 16000 (the budget is added to `max_tokens`, or at most half of a `max_tokens` you set; thinking stays off if half of it is under 1024, and a request with thinking takes no `temperature`). OpenAI-compatible servers get it as `reasoning_effort` when the model is a reasoning one (`o1`, `o3`…, `gpt-5…`, also as `vendor/gpt-5`) and not otherwise. Gemini gets it as `thinking_level` (with thought summaries on). A provider or model that cannot use it ignores it. Extensions read it as `ctx.reasoning_effort` and change it for the session with `ctx.set_reasoning_effort(...)`.
- **`agent.max_turns`** is the most model requests one user turn may make; a turn still calling tools after that many ends with an error. It is also the default of `ctx.run_agent`'s `max_turns` (`ctx.max_turns` reads it).
- **`tools.max_parallel`** is how many tool calls run at once when one reply calls several tools marked `read_only` in a row (`read`, `grep`, `glob`, and extension tools registered with `read_only=True`). Every other call runs alone, in order; `1` runs everything one by one.
- **`extensions.disabled`** lists ids (`compact`, `tools/subagent`, see [extensions](extensions.md#layout-and-where-to-save)) not to load from `~/.minimal/extensions` or `.minimal/extensions`, and the id of a bundled extension: `marketplace_tools` removes the model's `ext_search`, `ext_install` and `ext_publish`. Extensions passed with `--ext` always load.
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
