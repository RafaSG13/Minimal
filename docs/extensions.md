# Writing extensions

An extension is Python code defining `register(minimal)`. It can add tools for the model, slash commands, providers, event hooks, system-prompt notes and sections, modes, widgets, status-line segments, colour themes and a header logo, and run agents of its own. This guide covers minimal API 2.3 ([API versions](#api-versions)).

```python
"""Greets the user."""

__version__ = "1.0"
__api__ = "2"


def register(minimal):
    settings = minimal.declare_settings({"greeting": "hello"})

    @minimal.tool(input_schema={"type": "object", "properties": {}})
    def greet(input, ctx):
        """Greet the user."""
        return settings["greeting"]
```

[`extensions/example.py`](../extensions/example.py) is a complete extension: a tool, a command, a guardrail with a setting, a prompt note, a sidebar panel and a key.

## Layout and where to save

Extensions load from these places, the first one before the others:

| Place                       | Scope     | Notes                                                              |
| --------------------------- | --------- | ------------------------------------------------------------------ |
| inside minimal (`bundled/`) | `bundled` | First-party extensions that ship with minimal; `extensions.disabled` turns them off; a reload does not list them as loaded; no `/trust` needed |
| `~/.minimal/extensions/`    | `global`  | Always loaded                                                      |
| `./.minimal/extensions/`    | `project` | Loaded only once the project is trusted (see [Trust](#trust))      |
| `minimal -e <path>`         | `cli`     | A file or folder; repeatable; never skipped by `extensions.disabled` |

The one bundled extension is `marketplace_tools`: the model's `ext_search`, `ext_install` and `ext_publish` tools and the sentence in the system prompt that mentions them. List `marketplace_tools` in `extensions.disabled` to turn them off, or register a tool of the same name in your own extension to replace one.

The model's system prompt tells it which folders those are, so what it writes loads next time. Within a folder, its own entries load first, then those in its `themes/`, `tools/` and `widgets/` subfolders (where the marketplace installs those categories), alphabetically within a folder. Names starting with `_` or `.` are skipped.

An extension is either:

- **a file**, `name.py`; or
- **a package folder**, `name/` with an `__init__.py` that defines `register`. The other modules in the folder are helpers, imported with relative imports (`from .helpers import X`). Every `.py` in the folder is re-read on reload, so edits to helpers take effect.

If both `name.py` and `name/` exist, only `name.py` loads and the reload reports an error.

Directly in an extensions folder, `themes/`, `tools/` and `widgets/` are always category folders, never packages. Helpers are imported only from the files read at load time: a module added to the folder afterwards is not found until the next reload (and, in a project, the next `/trust`). The marketplace refuses a package with a symlink to a folder or to a file outside it.

**Ids.** An extension's id is its name (`compact`), or `category/name` inside `themes/`, `tools/` or `widgets/` (`tools/subagent`). The id is the key of its settings (`extensions.settings.<id>`) and what `extensions.disabled` lists (see [settings](settings.md)).

## Metadata

Metadata is read from the source without running it (for a package, from `__init__.py`):

| Item           | Meaning                                                                                                    |
| -------------- | ---------------------------------------------------------------------------------------------------------- |
| docstring      | Its first line is the description shown by the marketplace (required to publish)                           |
| `__version__`  | `"1.0"`: a string, required by the marketplace; shown as `minimal.version`                                 |
| `__api__`      | `"2"`: the [API version](#api-versions) it is written for, required. `"2"` runs on any 2.x; `"2.1"` needs 2.1 or a later 2.x |
| `__dependencies__` | `["termaid>=0.3"]`: optional list of third-party packages it needs (name, extras, version comparisons; no URLs or options). `/ext` and `ext_install` offer to install the missing ones into minimal's Python after installing the extension |
| `__tags__`     | `["git", "ui"]`: optional list of lowercase keywords (`[a-z0-9_-]+`) that `/ext search` and `ext_search` match besides the name and description; shown after the description |
| `__depends__`  | `["modes"]`: optional list of extension ids ([ids](#layout-and-where-to-save)) that must be loaded before this one. It is loaded after them; if one is missing, failed or skipped, this one is skipped with `needs 'modes': install it with /ext`. API 2.3 |
| `__conflicts__` | `["tools/subagent"]`: optional list of extension ids it cannot be loaded with. Whichever of the two loads first stays; the other is skipped with `conflicts with 'tools/subagent', which is already loaded`. API 2.3 |

`__depends__` and `__conflicts__` are ids, not package names (those are `__dependencies__`), and are read without running the extension: `minimal ext check` fails a malformed list, an extension that lists itself, and an id in both. Loading goes in the usual order (bundled, global, project, `--ext`; alphabetical in a folder), except that an extension comes after the ones it depends on; a dependency cycle skips its members. `/ext list` shows each extension's `needs` and `conflicts with`.

An extension without `__api__` is not loaded (`declares no __api__; add __api__ = "2"`), and neither is one written for another major version (`written for minimal API 1, this minimal has API 2.0`) or a newer minor (`needs minimal API 2.3, this minimal has API 2.0: upgrade minimal`).

## `minimal`: the `Extension`

`register(minimal)` receives an `Extension`, one per extension.

| Member                 | What it is                                                                                         |
| ---------------------- | -------------------------------------------------------------------------------------------------- |
| `id`, `path`, `scope`, `version` | Read-only: the id, the `.py` file or package folder, `"global"`/`"project"`/`"cli"`, `__version__` (`""` if unset) |
| `tools`                | Every registered tool by name (read-only, live): `minimal.tools["bash"].run(input, ctx)` reuses one |
| `settings`             | What `declare_settings` returned; empty before it is called                                         |
| `project_trusted`      | `bool`, read-only: the user trusted the current project ([Project trust](#project-trust)). Already right while `register` runs |

### Tools

```python
@minimal.tool(input_schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
              confirm=True, subject_key="url")
def fetch(input, ctx):
    """Download a URL and return its text."""
    ...

minimal.register_tool("hello", lambda input, ctx: "text", description="...", input_schema={...})
```

- `@minimal.tool(name=None, *, description="", input_schema=None, confirm=False, subject_key=None, read_only=False)`: the name defaults to the function's, the description to the first line of its docstring. Returns the function unchanged.
- `register_tool(name, run, description="", input_schema=None, confirm=False, subject_key=None, read_only=False)`: the same without a decorator.
- `run(input, ctx)` returns the text the model sees. An exception becomes an `is_error` result, so the model can correct itself. It may also return a non-empty list of `{"type": "text", "text": ...}` and `image_block(data, media_type)` blocks (API 2.3): the model then sees the picture too (the built-in `read` does this for PNG, JPEG, GIF and WebP files up to 3.75 MB). `tool_result` handlers and panels see the text, with `[image: image/png, 1234 bytes]` in place of each picture, and rewriting the output replaces the pictures too.
- `read_only=True` (API 2.3) says the tool changes nothing and `run` is thread-safe. When one model reply calls several tools, a run of consecutive calls to `read_only` tools runs concurrently, on worker threads, at most `tools.max_parallel` (default 4) at a time; every other call runs alone, after the ones before it finished. Calls are still announced, vetted (`tool_call` handlers, permissions, confirmation) and answered one by one in the order of the reply, on the thread that runs the turn, and the results always go back in that order: only `run` itself overlaps. Built-in `read`, `grep` and `glob` are `read_only`. Not to be confused with a [mode's](#modes) `read_only`, which blocks tools that ask for confirmation. Interrupting a batch stops waiting for it: calls not started never run, and every call still gets its result.
- `confirm` is `True` to ask the user before every call, or `confirm(input, ctx) -> bool` to decide per call. `-y` skips the question.
- `subject_key` names the `input` field that [permission rules](settings.md#permissions) `name(pattern)` match. With `subject_key="url"`, the rule `fetch(https://example.com/*)` in `permissions.allow` runs matching calls without asking, and in `permissions.deny` blocks them. Without it, only the bare rule `fetch` applies to the tool. Built-in tools use `command` (`bash`) and `path` (`read`, `write`, `edit`, `grep`, `glob`).

### Commands

```python
@minimal.command(description="show how many messages the session has")
def stats(args, ctx):
    print(f"{len(ctx.messages)} messages")

minimal.register_command("wizard", run_wizard, description="...", terminal=True)

@minimal.command(description="greet someone", complete=lambda args, ctx: [n for n in ("alice", "alan", "bob") if n.startswith(args)])
def greet(args, ctx):
    print(f"hi {args}")
```

- `@minimal.command(name=None, *, description="", terminal=False, complete=None)` / `register_command(name, run, description="", terminal=False, complete=None)` add `/name`, run as `run(args, ctx)` with the text after the name. What it prints appears in the transcript. `terminal=True` runs it with the UI suspended, in the real terminal.
- `complete(args, ctx)` (API 2.3) returns candidate completions for the argument text typed after `/name `: `args` is everything after the single space that follows the name, and each returned string **replaces the whole argument text** (`/greet a` → `["alice", "alan"]`; for several words, return the earlier words again). Filter by `args` yourself and keep it fast: it runs on the UI thread after each keystroke. The terminal UI lists the candidates in the composer's popup, `Tab` puts them in the text one after the other, and `Enter` sends the text as it is. A function that raises or does not return a list of strings is reported once (until the next reload) and offers nothing. The built-in `/theme`, `/mode`, `/resume`, `/provider`, `/login`, `/logout` and `/ext` complete the same way.

### Events

```python
@minimal.on("tool_call")
def guard(event):
    if event["name"] == "bash" and "rm -rf /" in event["input"].get("command", ""):
        return {"block": True, "reason": "not here"}

minimal.on("turn_end", lambda event: None)
```

`on(event, handler=None)` subscribes `handler(event)`; without a handler it is a decorator. An unknown event name raises `ValueError`. The payloads are typed in `minimal.api` (`ToolCallEvent`, `ToolResultEvent`, `TurnEndEvent`, `SessionStartEvent`, `SessionSwitchEvent`, `TurnStartEvent`, `BeforeRequestEvent`, `AfterResponseEvent`, `ShutdownEvent`, `ProviderErrorEvent`, `AgentStartEvent`, `AgentEndEvent`, `ModeChangeEvent`). The events a loop fires (`tool_call` … `provider_error`) all carry `agent_id` and `parent_id` ([Agents](#agents)):

| Event            | Payload                                          | Return                                                                 |
| ---------------- | ------------------------------------------------ | ---------------------------------------------------------------------- |
| `tool_call`      | `name`, `input`, `ctx`, `agent_id`, `parent_id`  | `{"block": True, "reason": str}` vetoes the call; a handler that raises blocks it too |
| `tool_result`    | `name`, `input`, `output`, `is_error`, `ctx`, `agent_id`, `parent_id` | `{"output": str}` rewrites what the model sees                         |
| `turn_end`       | `messages`, `ctx`, `nested`, `agent_id`, `parent_id` | ignored                                                                |
| `session_start`  | `ctx`, `resumed`                                 | ignored                                                                |
| `session_switch` | `ctx`, `old_id`, `new_id`                        | ignored                                                                |
| `turn_start`     | `ctx`, `text`                                    | `{"text": str}` rewrites the user's message (the last non-empty one wins) |
| `before_request` | `ctx`, `messages`, `system`, `tools`, `options`, `nested`, `agent_id`, `parent_id` | `{"messages": list, "system": str, "tools": list}`, any subset: sent instead, for this request only; `{"options": dict}` adds request options |
| `after_response` | `ctx`, `reply`, `usage`, `nested`, `agent_id`, `parent_id` | ignored                                                                |
| `shutdown`       | `ctx`                                            | ignored                                                                |
| `provider_error` | `ctx`, `error`, `attempt`, `provider`, `model`, `nested`, `agent_id`, `parent_id` | `{"retry_after": seconds}` retries after a wait; `{"provider": name, "model": name}` retries with another provider (the first such answer wins) |
| `agent_start`    | `ctx`, `agent_id`, `parent_id`, `name`, `prompt`  | ignored                                                                |
| `agent_end`      | `ctx`, `agent_id`, `parent_id`, `name`, `status`, `text`, `usage`, `error` | ignored                                           |
| `mode_change`    | `ctx`, `old`, `new`                              | ignored                                                                |
| `theme_change`   | `ctx`, `name`, `dark`                            | ignored; the terminal UI's colour theme changed (`/theme`, `Ctrl+P`, reload): API 2.3 |

- `tool_result` sees the tool's whole output: `bash` output is not clipped until after the handlers run (up to 1 MB captured), so a handler can compress it.
- `turn_end` fires when a loop ends with a reply without tool calls. `nested` is `True` for a loop run inside a tool (`ctx.run_agent`, such as the subagent), whose `messages` are its own history, not `ctx.messages`. Skip nested loops for session-level work: `if event["nested"]: return`.
- `session_start` fires once per process, when the terminal UI is up; `resumed` is true if the session already had messages (`-c`, `-s`). A reload does not fire it again.
- `session_switch` fires when `/new`, `/resume`, `/fork`, `ctx.new_session` or `ctx.fork` replace the session, after the old one is saved.
- `turn_start` fires when the user sends a message, before it is added to the history.
- `before_request` fires before every model request of a loop (`nested` as in `turn_end`). What a handler returns is sent instead for that request only: the history is not changed. `options` are the request options for the provider (`max_tokens`, `temperature`, `reasoning_effort`; empty for its defaults), and a returned `options` dict is merged into them. Combined by key, the last handler wins; a value of the wrong type is ignored with a warning. Do not mutate what you receive: return copies.
- `after_response` fires after each reply is added to the history; `usage` is the reply's `Usage`, or `None`.
- `shutdown` fires when minimal exits.
- `agent_start` and `agent_end` bracket every agent loop: the user's turn (`name` `"main"`, `prompt` the user's message) and each `ctx.run_agent` ([Agents](#agents)). `agent_end` fires however the loop ended, after its `turn_end` (if it had one) and before the caller gets the result; `status` is `done`, `interrupted`, `error` or `max_turns`, `text` the last reply's text, `usage` the `Usage` summed over the loop's replies and `error` the message of a failure, else `None`.
- `mode_change` fires when the current mode changes (`ctx.set_mode`, `/mode`, `Shift+Tab`, a reload that lost the mode); `old` and `new` are mode names or `None` for no mode ([Modes](#modes)). It runs on the thread that changed the mode: for `Shift+Tab` and `/mode` a worker thread (like a command), so a handler may use `ctx.ui`; the status line is redrawn after the handlers ran, whichever thread changed the mode.
- `provider_error` fires when a model request fails (rate limit, overload, network). `attempt` counts this request's failures from 1; nothing retries by itself, so a handler decides how often (`return None` gives up and the turn fails as before). `{"provider": ...}` switches provider for the rest of the session (`model` optional: its default). Esc interrupts the wait. Text streamed before the failure stays on screen.

```python
@minimal.on("provider_error")
def backoff(event):
    if "429" in str(event["error"]) and event["attempt"] <= 3:
        return {"retry_after": 2 ** event["attempt"]}
```

### Other registrations

| Method                                                              | What it adds                                                  |
| ------------------------------------------------------------------- | ------------------------------------------------------------- |
| `append_system_prompt(text)`                                        | A paragraph at the end of the system prompt                   |
| `register_prompt_section(name, text, *, replace=None)`, `remove_prompt_section(name)` | Add, replace or remove a named section of the system prompt ([Prompt sections](#prompt-sections)) |
| `register_mode(name, *, description, tools=None, exclude_tools=(), system_note="", read_only=False)` | A mode: a restriction on the model's tools ([Modes](#modes)) |
| `register_provider(name, create, default_model, env_keys=(), description="")` | A model integration: `create()` returns a `Provider` (see the README's "Adding a provider") |
| `add_provider(spec)`                                                | A ready-made `ProviderSpec`; with `openai_compatible(name, base_url, default_model, *, key_env=None, description="")` it adds one more OpenAI-compatible server (Ollama, OpenRouter, vLLM…) with its own URL and key, next to `openai` and each other. `key_env=None` means no key (a local server). Needs the `openai` package (`mnml-agent[openai]`) |
| `register_widget(name, create, area="sidebar")`                     | A terminal UI widget ([Widgets](#widgets))                    |
| `register_status(name, render, side="right")`                       | A status-line segment ([Status segments](#status-segments))   |
| `register_panel(name, render, area="sidebar", live=None, interval=0)` | Styled text in the terminal UI ([Panels](#panels))          |
| `register_renderer(kind, render)`                                   | Draws a code fence, an alert or a tool call yourself ([Renderers](#renderers)) |
| `register_theme(name, *, dark, background, foreground, accent, **colors)` | A colour theme ([Themes](#themes))                      |
| `register_logo(art, colors, align="center")`                        | Pixel art in the welcome header ([Header logo](#header-logo)) |
| `on_dispose(fn)`                                                    | `fn()` runs before a reload and when minimal exits (last registered first): stop the threads, timers and subprocesses the extension started. Returns `fn`, so it also works as a decorator |
| `data_dir(scope="project")`                                         | Not a registration: a private folder (mode 0700) for the extension's own files, outside the repository, created if missing. `"project"` is `~/.minimal/data/projects/<hash of the working directory>/<id>`, `"global"` is `~/.minimal/data/global/<id>` |

### Settings

```python
def register(minimal):
    settings = minimal.declare_settings({"min_chars": 2000, "ratio": 0.5, "enabled": True, "patterns": []})
    ...
    if len(output) > settings["min_chars"]: ...
```

`declare_settings(defaults)` declares the extension's settings and returns the effective values as a read-only mapping (also `minimal.settings`). Users set them in any [settings file](settings.md) under `extensions.settings.<id>`:

```json
{"extensions": {"settings": {"lenses": {"min_chars": 4000}}}}
```

- Layers merge per key: global, then project, then local (project and local only once the project is trusted).
- A value must have its default's type. `bool` is strict (`1` is not `true`); a `float` default accepts an `int`.
- An unknown key or a value of the wrong type is ignored, keeping the default, and reported by `/reload` (and by `reload_extensions`), e.g. `lenses: unknown setting 'min' (declared: min_chars, head, tail)` or `lenses: setting 'head' must be int`.
- Call it once; a second call raises `ValueError`.
- `declare_settings(defaults, schema=None)` (API 2.3) also takes `schema`: for any key of `defaults`, `{"description": str, "enum": [...], "min": n, "max": n}` (every field optional; `min`/`max` for numbers, `enum` a non-empty list of values of the default's type). A value outside it is a load error naming `extensions.settings.<id>.<key>` and the default stays. A schema that names an unknown key, has an unknown field or is not satisfied by its own default raises `ValueError` (so `minimal ext check` reports it).
- The user does not have to edit JSON: `/ext settings` lists every loaded extension's settings with their value, default and description, `/ext settings <id>` details one, `/ext settings <id> <key> <value>` validates and writes it to the global settings file and reloads, and `/ext settings <id> <key> reset` removes it. Put a `description` on every key, it is what the user reads there.

## `ctx`: the `Context`

Tools, commands, event payloads, widgets and status segments receive a `Context` as `ctx`. Its properties read minimal live, so a `ctx` kept by a widget always sees the current session, model and tools, and it is safe to use from the thread that runs a turn.

| Member                           | What it is                                                                                 |
| -------------------------------- | ------------------------------------------------------------------------------------------ |
| `cwd`                            | Working directory of tools and of the system prompt                                        |
| `messages`                       | The current session's messages in the canonical format (live)                              |
| `session_id`                     | The current session's id                                                                   |
| `provider_name`, `model`         | Current provider and model                                                                 |
| `max_turns`                      | Most model requests one user turn may make (the `agent.max_turns` setting, default 50) and the default of `run_agent`'s `max_turns` (API 2.3) |
| `reasoning_effort`, `set_reasoning_effort(value)` | How much the model is asked to think: `"low"`, `"medium"`, `"high"` or `None` (the provider's default). It starts as the `reasoning_effort` setting; `set_reasoning_effort` changes it for the session (`ValueError` for another value; `None` clears it) (API 2.3) |
| `context_tokens`                 | Context size after the last reply that reported usage, or `None`                           |
| `tools`                          | Every registered tool by name (read-only, live)                                            |
| `agent_id`, `parent_id`          | The agent this `ctx` belongs to: `"main"` and `None` for the user's turn, commands and widgets; for the `ctx` a tool or handler of a `run_agent` agent gets, that agent's id and its parent's ([Agents](#agents)) |
| `project_trusted`                | `bool`: the user trusted the current project ([Project trust](#project-trust))              |
| `cancelled`                      | `bool`: the user interrupted the turn or command in progress (or `cancel_agent` stopped the agent this `ctx` belongs to); poll it in long loops |
| `check_cancelled()`              | Raise `TurnInterrupted` if `cancelled`                                                      |
| `ui`                             | Questions to the user (below)                                                              |
| `system_prompt()`                | The system prompt the next turn would send                                                 |
| `complete(messages, *, system=None, tools=True, options=None, model=None)` | One model request with the current provider and model; returns a `Reply`. No tools are run; `tools=True` offers them anyway to keep the prompt cache warm. `options` (`max_tokens`, `temperature`, `reasoning_effort`) go over the ones a turn uses; `model` (API 2.3) uses another model of the current provider. A failed request goes to the `provider_error` handlers, which may retry it, and it counts in `usage`. Interruptible |
| `complete_text(prompt, *, system=None, model=None, max_tokens=1024)` | API 2.3. Asks one question and returns the answer as text: one request with `prompt` as the only message, no tools and no system prompt unless `system` is given (pass `ctx.system_prompt()` for the session's); not added to the history. For titles, summaries and classifications. `ValueError` for an empty prompt or a bad `max_tokens`. Interruptible, retried and counted like `complete` |
| `run_agent(prompt, *, system_note="", exclude_tools=(), name=None, system=None, tools=None, model=None, provider=None, max_turns=None)` | Runs `prompt` as its own agent loop with an empty history and the same tools (minus `exclude_tools`), confirmations and permissions; returns the text of its last reply and raises what went wrong. `system_note` is appended to the system prompt; its `turn_end` has `nested` set. The other keywords: [Agents](#agents) |
| `run_agent_result(prompt, **same)` | The same, but returns an `AgentResult` (`agent_id`, `text`, `usage`, `status`, `error`, `messages`) and reports a failure in it instead of raising ([Agents](#agents)) |
| `agents()`                       | A snapshot list of `AgentInfo` of the agents of this session, running and finished ([Agents](#agents)) |
| `cancel_agent(agent_id)`         | Interrupts that agent and the agents it started, not the one that started it; `False` if it is not running |
| `mode`, `modes()`, `mode_info(name=None)`, `set_mode(name)`, `cycle_mode()` | The current mode's name (`None`: no mode), every mode and its description, the `ModeSpec` of a mode (default the current one; `None` if none), switching (`None` leaves the mode; `ValueError` if unknown), and going to the next one ([Modes](#modes)) |
| `prompt_sections()`              | The names of the system prompt's sections in order ([Prompt sections](#prompt-sections))    |
| `new_session(messages)`          | Saves the current session and continues in a new one holding `messages` (its header names the old one as `parent`, which `/sessions` shows); returns the old id. `ValueError` if the new session cannot be locked |
| `fork(at_index=None)`            | API 2.3. Continues in a new session that starts as a copy of `messages[:at_index]` (all of them by default); the current session stays saved as it is, the new header names it as `parent`, `session_switch` fires and the terminal UI redraws the transcript. Returns the new id. `ValueError` while a turn is running, for an index outside `0..len(messages)`, or when the cut would leave an assistant `tool_use` without the `tool_result` message after it (cut before the call or after its results) |
| `truncate(at_index)`             | API 2.3. Drops `messages[at_index:]` from the current session, in place, and rewrites the session file atomically; returns how many messages it dropped. Same `ValueError`s as `fork`; it does not fire `session_switch` (same session, so `fork` first to keep the dropped part). Together they make rewind and undo extensions |
| `usage`                          | API 2.3. `UsageTotals` of everything this minimal process has spent: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` and `requests`, counting the user's turns, the agents they start and `complete`/`complete_text`. A snapshot. Per process: a new or resumed session does not reset it and `minimal -c` starts again from zero. `/usage` prints it |
| `progress(text)`                 | API 2.3. A one-line progress note (first line, at most 120 characters) shown next to the working indicator while a tool or command runs; `None` (or blank text) clears it, and the terminal UI clears it when the turn or command ends. Call it as often as you like, from any thread. The plain terminal prints each change on stderr when it is a terminal |
| `invalidate()`                   | API 2.3. Asks the UI to redraw panels, status segments and widgets (`render` and `refresh_state` run again). Cheap, coalesced and safe from any thread: call it from a poller or subscription when its data changed, instead of a panel `interval`. A frontend without panels does nothing |
| `theme`                          | API 2.3. The terminal UI's current colour theme as a `ThemeInfo`: `name`, `dark` and `colors`, every colour of `THEME_COLORS` plus `background`, `foreground` and `accent` as `#rrggbb` (read-only). Read it again after `theme_change` |
| `context_window`                 | API 2.3. The current model's context size in tokens, or `None` if unknown: the `context_window` setting, else the provider's own table, else minimal's table of the built-in providers' model families (longest name prefix). `context_tokens / context_window` is a gauge |
| `git()`                          | API 2.3. The working directory's repository as a cached `GitInfo` (`branch`, `dirty`, `ahead`, `behind`, `staged`, `unstaged`, `untracked`, `files`: up to 50 `(status, path)` pairs), or `None` before the first poll ends and outside a repository. One background poller shared with the status line refreshes it every `git.refresh` seconds (default 3) and calls `invalidate()` when it changes; the call itself never runs git |
| `keybindings()`                  | API 2.3. Every bound key as a `KeyInfo` (`key`, `action`, `description`, `source`: `"built-in"` or the extension id), minimal's own actions as `ui.keys` remapped them first: what a help screen lists |
| `transcript`                     | API 2.4. A `Transcript`: `scroll_to(message_index)`, `mark(message_index, label="")`, `unmark(message_index=None)`, `marks()` and `find(text, *, regex=False)` over the messages as the terminal UI draws them ([below](#screens-and-the-transcript)) |
| `sessions(*, all_projects=False)` | Saved session ids, oldest first: those started in this `cwd`, or every project's with `all_projects=True` |
| `read_session(session_id)`       | The messages of a saved session; `ValueError` if there is none                             |
| `reload()`                       | Reloads every extension from disk; returns the summary                                     |
| `commands()`                     | Every slash command name (without `/`) and its description: built-in and extension commands, an extension's replacing a built-in of the same name |
| `run_command(line, *, builtin=False)` | Runs `"/name args"` as if the user typed it; its output is printed and errors are reported, not raised. `builtin=True` runs the built-in even when an extension replaced it, so a replacement can hand over to the original |
| `models()`, `set_model(name)`    | The current provider's models (queried from its API, falling back to its default) and switching to one |
| `widget(name)`                   | The widget the terminal UI shows for `register_widget(name, ...)`, or `None` (the UI is not up, or it failed to mount) |
| `notify(text, kind="info")`      | A short notice: a toast in the terminal UI, a line on stderr otherwise. `kind` is `"info"`, `"warning"` or `"error"` |
| `use_provider(name, model=None)` | Switches provider (and model, default its default) from the next request on, keeping the conversation; `ValueError` if unknown or without credentials |
| `queue_user_message(text)`       | Sends `text` as the user's next message once the turn or command in progress ends (at once if nothing is running); `/…` runs a command. It does not steer the turn in progress |

`ctx.ui` asks through whatever is showing minimal (the terminal UI or the plain terminal). Every method that waits takes the keyword `timeout` (API 2.4): a number of seconds > 0 after which the dialog closes and the method returns what a cancel returns, so a tool or timer never hangs on a user who walked away. `None` (the default) waits for ever and anything else is a `ValueError`. The plain terminal honours it only when stdin is a terminal.

| Method                         | Returns                                                                        |
| ------------------------------ | ------------------------------------------------------------------------------ |
| `ask(question, *, timeout=None)` | `bool`: a yes/no answer (`False` on timeout)                                 |
| `choose(title, options, *, timeout=None)` | The value of one `(value, label)` option, or `None` if cancelled or timed out |
| `choose_many(title, options, *, timeout=None)` | The ticked values of `(value, label, checked)` options in order, or `None` |
| `ask_text(title, *, timeout=None)` | One line of text, stripped, or `None` if empty, cancelled or timed out      |
| `pick(title, items, *, preview=None, on_highlight=None, filter=True, timeout=None)` | API 2.4. The value of the item picked from a list filtered as the user types, or `None` ([details](#screens-and-the-transcript)) |
| `overlay(content, title="", *, timeout=None)` | API 2.4. Read-only scrollable text until the user presses Esc; returns `None` |
| `screen(factory, *, timeout=None)` | API 2.4. Your own modal Textual screen; returns what it is dismissed with |

```python
@minimal.command(description="summarise this session into a new one")
def fold(args, ctx):
    reply = ctx.complete(ctx.messages + [{"role": "user", "content": "Summarise the conversation."}])
    if ctx.ui.ask("Start a new session from the summary?"):
        old = ctx.new_session([{"role": "user", "content": result_text(reply.content)}])
        print(f"Continued from {old}")
```

## Screens and the transcript

API 2.4. These need the terminal UI (the plain terminal prints or falls back as noted).

**`ctx.ui.pick(title, items, *, preview=None, on_highlight=None, filter=True, timeout=None)`** is the building block for "choose among many": a themed, keyboard-driven list the user filters by typing (type, ↑/↓ or PageUp/PageDown, Enter picks, Esc cancels).

- `items` are `(value, label)` or `(value, label, detail)` tuples of strings; `value` is what comes back and must be unique (`ValueError` otherwise). `label` is listed and `detail` follows it dimmed; the filter matches both, fuzzily and without regard to case (`fb` finds `foo_bar`; several words must all match; better matches come first, an empty filter keeps your order). At most 300 rows are listed at once; typing narrows a longer list. With no items nothing is shown and it returns `None`.
- `on_highlight(value)` runs **on the UI thread** every time the highlighted row changes, the row first highlighted when the list opens included (once per change: typing that leaves the same row on top does not repeat it). Use it to preview, for instance to apply a colour theme while the cursor is on it and restore the old one when the pick returns `None`. An exception is reported once and does not close the list.
- `preview(value)` returns plain text for a side pane that follows the highlight (UI thread: keep it fast; an exception shows in the pane).
- `filter=False` shows no filter box (only the list moves).
- Returns the value, or `None` if the user pressed Esc, the `timeout` passed, minimal quit, or `items` was empty. The plain terminal shows `choose`'s numbered list instead (no filter, preview or highlight callback).

**`ctx.ui.overlay(content, title="", *, timeout=None)`** shows read-only text in a box over the transcript: ↑/↓, PageUp/PageDown, Home/End, `j`/`k` scroll; Esc or `q` closes it. `content` is a string, or styled lines as `register_panel` returns them (`[[("added ", "$diff-added"), ("name", "bold $accent")], "plain line"]`: `$name` styles follow the theme; `ValueError` for an unknown style), or a Textual `Content`. Use it for help, key lists and diffs. The plain terminal prints the text.

**`ctx.ui.screen(factory, *, timeout=None)`** shows a modal Textual screen of your own and returns the value it is dismissed with:

```python
from textual.screen import ModalScreen
from textual.widgets import Button

class Confirm(ModalScreen):
    def __init__(self, dismiss):
        super().__init__()
        self.done = dismiss

    def compose(self):
        yield Button("Apply", id="ok")

    def on_button_pressed(self, event):
        self.done("applied")              # or self.dismiss("applied")

result = ctx.ui.screen(lambda dismiss: Confirm(dismiss))   # "applied", or None
```

`factory(dismiss)` is called on the UI thread and returns a `ModalScreen`. `dismiss(value=None)` closes it from the screen's handlers or from any thread, as `screen.dismiss(value)` does; it does nothing once the screen is closed or while another screen covers it. The result is `None` if the screen closes without a value, minimal quits, or the `timeout` passes (it is closed then, once it is the top screen). Call it from a tool, command or handler that runs in a worker thread, never from the UI thread itself (widget callbacks and `terminal=True` commands cannot wait for a screen they must draw): there it raises `RuntimeError`, as it does in the plain terminal. What `factory` raises reaches the caller. Style it with the theme's CSS variables (`$surface`, `$panel`, `$accent`, `$modal-overlay`, …).

**`ctx.transcript`** (`minimal.api.Transcript`) works on the messages as the terminal UI draws them, named by their index in `ctx.messages` (safe from any thread):

| Member | What it does |
| --- | --- |
| `find(text, *, regex=False)` | The indexes of the messages containing `text` (case-insensitive; with `regex=True` a regular expression for `re.search`, case-sensitive unless it says `(?i)`). It searches the text of messages, a tool call's name and input as JSON and a tool result's text, not thinking. Works in the plain terminal too. `ValueError` for empty text or a bad pattern |
| `scroll_to(message_index)` | Scrolls the transcript so that the block showing that message is at the top, and stops following the bottom until the user scrolls down. `True` if it has a block; `False` for an index out of range, a message older than the 200 the UI redraws, or one that shows nothing (and in the plain terminal). A message of tool calls or tool results maps to its first tool call's block |
| `mark(message_index, label="")` | Marks the message: its block gets the CSS class `-marked` (stable; the default style is a thick `$accent` left border, restyle it with `register_css`) and `label` as its tooltip; marking again replaces the label. `ValueError` for an index outside the messages. A mark on a message the UI does not draw is kept but shows nothing |
| `unmark(message_index=None)` | Removes that message's mark (no error if it has none), or every mark with `None` |
| `marks()` | `[(index, label), ...]` by index |

Marks last while the session is in view: `/new`, `/resume` and `fork` clear them, and `truncate` (`/rewind`) drops the ones it cut. They are not saved. A tool call and its result are one block, so marking both keeps it marked until both are unmarked.

## Modes

```python
minimal.register_mode(
    "plan",
    description="read and think, change nothing",
    tools=["read", "grep", "glob", "ask_user"],
    system_note="You are in plan mode: investigate and propose a plan; do not modify anything.",
)
minimal.register_mode("review", description="no writes", read_only=True)
```

A mode restricts which tools the model may use and may add a note to the system prompt. `register_mode(name, *, description, tools=None, exclude_tools=(), system_note="", read_only=False)`:

- `name`: lowercase letters, digits, `_` and `-` (it is typed as `/mode <name>`); `off` is reserved. Registering a name again replaces the mode and keeps its place in the order.
- `tools`: an allow-list of tool names (`None`: every tool). `exclude_tools`: names the mode blocks. `read_only`: also block every tool registered with `confirm=True` (the built-in `write`, `edit`, `bash`, `reload_extensions`: tools that always ask), unless `tools` lists it; a tool with a `confirm(input, ctx)` function does not count (`read`, `grep` and `glob` use one to ask only outside the workspace). Names need not be registered tools.
- `system_note`: appended to the system prompt while the mode is current.

There is **no mode** by default (`ctx.mode` is `None`) and every tool is available. `ctx.set_mode(name)` makes a mode current (`None` leaves it; `ValueError` for an unknown name), `ctx.cycle_mode()` goes to the next one and returns it, in this order: no mode, each mode in registration order, no mode again (nothing happens when no mode is registered), and `ctx.modes()` is `{name: description}`. `mode_change` (`old`, `new`) fires on every change. The user switches with `/mode` (lists the modes; `/mode <name>` switches, `/mode off` leaves) and, in the terminal UI, with `Shift+Tab` when a mode is registered; the status line shows `mode <name>` only while one is set. The terminal UI's `Shift+Tab` is the one key it binds for this: it keeps moving the focus when no mode is registered, in a dialog and while a turn runs (`Tab` still moves the focus between the prompt and the transcript; both keys do that otherwise, so one of them had to give).

The restriction is enforced where tools run, not only in the prompt:

- a blocked tool is not offered to the model (`before_request` handlers see the list without it);
- a call to one is answered with an `is_error` result that says which mode blocks it and that only the user can change the mode (`/mode <name>`), before any `tool_call` handler or permission rule sees the call;
- it is checked live on every request and every call, so a tool that switches the mode in the middle of a turn (an "approve the plan" tool calling `ctx.set_mode("build")`) takes effect on the next request, note included.

Agents started with `ctx.run_agent` live under the current mode. `tools=` can only narrow it: a tool the mode blocks stays blocked even when `tools` lists it. A reload keeps the current mode if an extension registers it again; otherwise there is no mode afterwards (`mode_change`, and a warning to the user). Modes are registry entries like tools: a reload clears and registers them again.

## Agents

Every loop that talks to the model is an **agent**. The user's turn is `"main"` (`parent_id` `None`); each `ctx.run_agent` a tool or handler starts is another, with a short unique `agent_id` and the id of the agent whose `ctx` started it as `parent_id`.

- **Events.** `tool_call`, `tool_result`, `turn_end`, `before_request`, `after_response` and `provider_error` carry `agent_id` and `parent_id` (`nested` stays); `agent_start` (`agent_id`, `parent_id`, `name`, `prompt`) and `agent_end` (`agent_id`, `parent_id`, `name`, `status`, `text`, `usage`, `error`) bracket each loop. For a tool that runs an agent: `tool_call`, `agent_start`, the agent's own events, `agent_end`, `tool_result`. The user's turn fires `agent_start` after `turn_start` and `agent_end` after `turn_end`, also when it fails or is interrupted.
- **`ctx`.** The `ctx` a nested agent's tools, hooks and `before_request`-style handlers get is the app's `Context` with that agent's `agent_id`, `parent_id` and `cancelled` flag. Anything such a tool starts with it (even from a thread of its own) is attributed to that agent. Everything else (`messages`, `model`, `tools`) is the app's, so `ctx.messages` is the session's, not the agent's: the agent's own transcript is `AgentResult.messages`, and `turn_end`'s `messages`.
- **`ctx.agents()`** (and the terminal UI's `Host.agents()`) returns a snapshot list of `AgentInfo`, oldest first: `id`, `parent_id`, `name`, `status` (`running`, `done`, `interrupted`, `error`, `max_turns`), `model`, `provider`, `prompt`, `started` and `finished` (Unix seconds; `finished` is `None` while running), `tool_calls`, `usage` (a `Usage` summed over its replies) and `error`. `"main"` is listed once its first turn started, with the status of its latest turn and the usage and tool calls of the whole session; finished agents stay for the session.
- **Frontends.** The `call` that `Frontend.tool_call(call)` and `tool_result(call, result)` receive (and the `call` and `result` of the widgets' `refresh_live` events and renderers) have the extra keys `agent_id` and `parent_id`, so a frontend that ignores them keeps working; the history's blocks do not. `Frontend.text` is the user's turn's: nested agents do not stream text, an extension reads theirs from `agent_end` (or `after_response`).

```python
result = ctx.run_agent_result(
    "List the TODOs under src/",
    name="todo-scan",
    system="You scan code for TODO comments and report them as a list.",
    tools=["grep", "read"],
    max_turns=8,
)
if result.status == "done":
    return result.text
return f"agent {result.agent_id} {result.status}: {result.error or 'stopped'}"
```

`run_agent(prompt, *, system_note="", exclude_tools=(), name=None, system=None, tools=None, model=None, provider=None, max_turns=None)` returns the text of the last reply and raises what went wrong (the error that ended the loop, `TurnInterrupted` if it was interrupted, `RuntimeError` if it ran out of turns) as it always did. `run_agent_result` takes the same keywords and returns an `AgentResult`, a frozen dataclass: `agent_id`, `text` (the last reply's; of the last one it got to if it did not finish), `usage` (`Usage`), `status` (`done`, `interrupted`, `error` after a failed model request, `max_turns`), `error` (the message of an `error` or `max_turns`, else `None`) and `messages` (the agent's own transcript, starting with its prompt; every `tool_use` has its `tool_result` even when it was interrupted). Both raise `ValueError` before anything starts for an invalid argument.

| Keyword        | Meaning                                                                                                          |
| -------------- | ---------------------------------------------------------------------------------------------------------------- |
| `name`         | What `agent_start` and `ctx.agents()` call it (default `"agent"`)                                                |
| `system`       | Its whole system prompt instead of `ctx.system_prompt()`; `system_note` and the current mode's note are still appended |
| `tools`        | The only tools it may use. Not together with `exclude_tools`; a name that is not a registered tool is a `ValueError` |
| `exclude_tools`| Tools it may not use (as before)                                                                                 |
| `model`        | A model name for this agent; with `provider` omitted, the current provider's                                      |
| `provider`     | The name of a provider for this agent only (`ValueError` if unknown or without credentials); its default model unless `model` is given. The app's provider and model do not change, and a `provider_error` handler that switches provider switches only this agent |
| `max_turns`    | Most model requests it may make (default `ctx.max_turns`, 50 unless `agent.max_turns` says otherwise)                                                                      |

An agent uses the current mode ([Modes](#modes)): the mode's restriction and note apply, and `tools` can only narrow them.

**Cancelling.** Each agent has its own cancel flag; the user's interrupt (Esc, `Host.interrupt`) sets all of them. `ctx.cancel_agent(agent_id)` sets only that agent's and its children's, returns `False` if there is no such agent running, and leaves the agent that started it alone: `run_agent_result` returns `status="interrupted"`, `run_agent` raises `TurnInterrupted` (the calling tool then returns an error result, or handles it), and the parent's turn carries on. If the user's interrupt was the cause, the parent stops too, as before. A tool of a nested agent polls `ctx.cancelled` or calls `ctx.check_cancelled()` as usual: the flag is its own agent's.

## Prompt sections

The system prompt is made of named sections joined by blank lines, in this order (`minimal.api.PROMPT_SECTIONS`; an empty section is left out):

| Section        | Text                                                                                              |
| -------------- | ------------------------------------------------------------------------------------------------- |
| `base`         | The base prompt: `core/prompt.md`, or the `prompt.base` file of the [settings](settings.md)      |
| `environment`  | `Working directory: …` and today's date                                                           |
| `extending`    | How to extend minimal (where extensions go, the API)                                              |
| `instructions` | Each `prompt.files` file that exists (default `AGENTS.md`), as `Project instructions (<name>): …` |
| `append`       | Each `prompt.append` setting                                                                      |

Then come the sections extensions add, in the order they were registered (`append_system_prompt(text)` adds one named `append:<n>`). The built-in ones are the same bytes they always were.

```python
minimal.register_prompt_section("house_rules", "Answer in Spanish.")                       # after the others
minimal.register_prompt_section("terse_base", "You are a terse coding agent.", replace="base")
minimal.register_prompt_section("git", lambda ctx: "Branch policy: …" if ctx.cwd.endswith("repo") else None)
minimal.remove_prompt_section("extending")                                                  # save the tokens
```

- `register_prompt_section(name, text, *, replace=None)`: `text` is a string, or `text(ctx)` returning one (`None` or `""` leaves the section out), called every time the prompt is built. Without `replace` the section goes after the others; if `name` already exists its text is replaced in place. With `replace="base"` the new section takes the place of that one, which must exist: a built-in section or one registered earlier (extensions load in a fixed order: global before project before `--ext`, alphabetically inside a folder, so register yours after the one it replaces). `ValueError` if `replace` names no section or `name` is already another section.
- `remove_prompt_section(name)`: takes a built-in section or one registered earlier out; `ValueError` if there is none. The name can be used again afterwards.
- `ctx.prompt_sections()` lists the current names in order. A replaced or removed built-in section is never computed (a `prompt.base` file that is missing is not an error if `base` is replaced). A text function that raises is reported on stderr and its section skipped. A reload clears every edit and extensions register theirs again.
- **Trust.** A project's extensions load only after `/trust` (see [Trust](#trust)), so an untrusted repository cannot replace or remove a section: nothing extra is checked. A section that reads the repository's own files (a project's `AGENTS.md`-like instructions) should check `ctx.project_trusted` first.

## Panels

```python
def render(ctx):
    if not ctx.messages:
        return None  # hidden
    return [[("session ", "muted"), (ctx.model, "bold accent")], f"{len(ctx.messages)} messages"]

minimal.register_panel("session", render, area="sidebar")
```

`register_panel(name, render, area="sidebar", live=None, interval=0, *, order=0)` adds styled text to the terminal UI without Textual code, in `area` (`WIDGET_AREAS`; see [Placement](#placement-and-the-users-layout) for the areas and `order`). `render(ctx)` returns `None` to hide the panel, a string (one line per `\n`) or a list of lines; a line is a string, a `(text, style)` tuple or a list of those pieces. A style is space-separated names from `STYLES`: colours `foreground`, `accent`, `secondary`, `success`, `warning`, `error`, `muted`, `dim`, `diff_added`, `diff_removed` and the modifiers `bold`, `italic`, `underline`, `reverse` (`"bold muted"`); `""` is the default; any other name is an error. `render` runs on the UI thread after mount, after every turn, command and reload, after each `live` event and every `interval` seconds (`> 0`), so keep it cheap and never block: start a thread for slow work and return what it last found. `live(ctx, event)` receives `{"kind": "start", "turn": bool}`, `{"kind": "tool_call", "call": dict}`, `{"kind": "tool_result", "call": dict, "result": dict}` and `{"kind": "end"}` (after every turn or command and after every mount). A panel that raises is hidden and reported once.

API 2.3 adds to the style names: `$name` is a theme colour (`$code`, `$model`, `$git_dirty`, …) or a CSS variable of the UI (`$border-muted`, `$accent`; `CSS_VARIABLES`), and `#rrggbb` is a literal colour; both combine with the modifiers (`"bold $code"`, `"#ff8800 underline"`) and a `$name` follows `/theme`. And `register_panel(..., title="…", collapsible=True)` draws a heading line (muted, bold) above the lines; with `collapsible` a click on it, or Enter when it is focused, collapses the panel to the heading. The user can change the heading and the state with `ui.widgets.<name>.title` and `.collapsed` or `/ui title|collapse|expand <name>`.

## Widgets

```python
from textual.widgets import Static

class SessionPanel(Static):
    DEFAULT_CSS = "SessionPanel { padding: 0 1; }"

    def refresh_state(self, ctx):
        self.update(f"{len(ctx.messages)} messages · {ctx.model}")

minimal.register_widget("session", lambda ctx: SessionPanel(), area="sidebar")
```

`register_widget(name, create, area, *, order=0)` places the widget built by `create(ctx)` in `area`, one of `WIDGET_AREAS`: `"top"`, `"bottom"`, `"sidebar"`, `"left"` or `"footer"` ([Placement](#placement-and-the-users-layout)). A widget is any [Textual](https://textual.textualize.io/) widget, styled with its own `DEFAULT_CSS`. Besides Textual's standard variables (`$primary`, `$surface`, …), the CSS can use the theme's own: `$muted`, `$dim`, `$border-muted`, `$user-message-bg`, `$tool-success-bg`, … (see `src/minimal/ui/tui/theme.py`). If it has a `refresh_state(ctx)` method, the UI calls it after mount, after every turn and command, and after every reload. If it has a `refresh_live(ctx, event)` method, the UI calls it while a turn runs: when a turn or command starts (`{"kind": "start", "turn": bool}`), after each tool call is shown (`{"kind": "tool_call", "call": …}`) and after each result (`{"kind": "tool_result", "call": …, "result": …}`; the result is not in `ctx.messages` until every call of that reply has finished). The `top` and `bottom` areas size to their content: give the widget a height and don't `dock` it.

To only show text, use a [panel](#panels): it needs no Textual code.

`register_widget(..., title="…", collapsible=True)` (API 2.3) puts a heading line above the widget; with `collapsible`, a click on the heading (or Enter when it is focused) hides the widget and leaves the heading. The user's `ui.widgets.<name>.title` replaces the heading (`""` removes it) and `ui.widgets.<name>.collapsed` starts it collapsed; `/ui title <name> [text]`, `/ui collapse <name>` and `/ui expand <name>` set them.

## Status segments

`register_status(name, render, side, *, order=0)` adds a segment to the status line, `side` being one of `STATUS_SIDES`: `left` after the git branch or `right` before the context size (`order` sorts the segments of a side: [Placement](#placement-and-the-users-layout)). `render(ctx)` returns `None` for no segment, a string, a `(text, style)` tuple or a list of strings and `(text, style)` pieces (style names: [Panels](#panels)); the terminal UI also accepts a `textual.content.Content`. It runs on every redraw, so keep it cheap. A segment that raises, or returns an unknown style, is skipped and reported once.

## Styling the terminal UI

```python
minimal.register_css("tight", """
#sidebar { padding: 0; }
.tool { margin-top: 0; }
""")
```

`register_css(name, css)` (API 2.3) adds a Textual stylesheet to the terminal UI; registering the same `name` again replaces it, and it is gone when the extension no longer loads (every reload rebuilds them). It comes after minimal's own styles and before the user's `ui.css`, so the user has the last word. A stylesheet that does not parse is reported once as a UI error (`ext:name: stylesheet was not applied: line N: …`) and ignored. Prefer it to injecting CSS through Textual's private API, and prefer it to a widget's `DEFAULT_CSS` for changing minimal's own parts.

These selectors are stable; anything else (other ids, Textual's inner widgets) can change in any release:

| Selector | What it is |
| --- | --- |
| `#transcript` | The scrolling conversation; `Header` (its `.logo` and `.wordmark` children) is its first child |
| `#top`, `#bottom`, `#left`, `#sidebar`, `#footer` | The widget areas ([Placement](#placement-and-the-users-layout)) |
| `#composer`, `#prompt`, `#prompt-glyph` | The prompt box, its text area and the glyph before it |
| `#statusline`, `#working` | The status line and the working indicator |
| `.user`, `Markdown.assistant`, `.tool` (`.-pending`, `.-success`, `.-error`), `.output`, `.error` | The transcript's blocks: your message, the model's reply, a tool call by state, command output, an error |
| `.-marked` | API 2.4. A transcript block an extension marked with `ctx.transcript.mark` ([Screens and the transcript](#screens-and-the-transcript)); the default style is a thick `$accent` left border |

The theme's colours are CSS variables (`$accent`, `$muted`, `$dim`, `$border-muted`, `$user-message-bg`, `$tool-success-bg`, …; `CSS_VARIABLES` lists them), so a stylesheet follows `/theme`. Use `ctx.theme` when Python needs a colour, and listen for `theme_change` to recompute it.

## Placement and the user's layout

The terminal UI has five areas for widgets and panels (`WIDGET_AREAS`):

| Area      | Where                                                                                                   |
| --------- | ------------------------------------------------------------------------------------------------------- |
| `top`     | above the transcript                                                                                    |
| `bottom`  | between the transcript and the prompt                                                                   |
| `sidebar` | a column on the right (`ui.sidebar` puts it on the left or hides it); shown only when it has something |
| `left`    | a column on the left, built like the sidebar                                                            |
| `footer`  | under the status line                                                                                   |

`top`, `bottom` and `footer` size to their content: give a widget a height and do not `dock` it. The widgets and panels of an area stack by `order` (an integer, default `0`, lower first), then in registration order; status segments sort the same way within their side. minimal's own segments have orders from -40 to 0 on the left and 100 to 110 on the right (`model`, `path`, `git`, `mode`; `context`, `extensions`, `session`), so a segment you leave at `0` goes after them on the left and before them on the right, and `order=-50` puts yours first.

Your placement is a default. The user can move, reorder and hide any widget, panel or status segment, minimal's own included, without editing code: `/ui move session footer`, `/ui hide git`, or the `ui.widgets` and `ui.status` [settings](settings.md#ui-settings) (keys are the names you registered). A name you register as a status segment that is also one of minimal's own (`git`, `model`) replaces it. Do not assume your widget is mounted: `ctx.widget(name)` is `None` for one the user hid.

## Keybindings

```python
@minimal.keybinding("ctrl+o", name="stats", description="show session stats")
def show_stats(ctx):
    print(f"{len(ctx.messages)} messages")

minimal.register_keybinding("f6", lambda ctx: ctx.run_command("/mode plan"), "plan mode", show=True)
```

`register_keybinding(key, run, description="", *, show=False, name="")` (and the `@minimal.keybinding(key, *, description, show, name)` decorator, which takes the description from the function's docstring) binds a global key to `run(ctx)`. It runs on a worker thread, like a slash command: it may print (the output goes to the transcript), ask the user questions with `ctx.ui` and call anything on `ctx`; the prompt is locked meanwhile. An error it raises is shown once as `[extension] key <key> (<id>): <message>`. A key is ignored while a turn, a command or a dialog is running. The bindings are in force after every load and are dropped and made again on every `/reload`.

- **Key names** are Textual's: modifiers `ctrl`, `alt`, `shift`, `meta`, `super` joined with `+` to a letter, a digit, `f1`-`f24` or a named key (`ctrl+o`, `alt+x`, `f5`, `ctrl+shift+left`). Without `ctrl`, `alt`, `meta` or `super` only function keys and `insert` are accepted (a bare letter would steal typing). The terminal decides which combinations reach the app: `ctrl+<letter>`, function keys and `alt+<letter>` usually do.
- **Reserved keys** (`RESERVED_KEYS`) are refused with a `ValueError` (a load error for the extension): `escape`, `ctrl+c`, `ctrl+p`, `ctrl+q`, `enter`, `tab`, `shift+tab`, `ctrl+b`, the arrows, `home`/`end`, `pageup`/`pagedown`, `backspace`/`delete` and the prompt's editing keys (`ctrl+a`, `ctrl+e`, `ctrl+k`, `ctrl+u`, `ctrl+w`, `ctrl+z`, …).
- **Duplicates.** Two extensions binding the same key: the later one (load order) wins and the reload reports `<id>: key ctrl+o was bound by <other>'s binding '<label>'; replaced`. Two bindings of one extension cannot share a `name` (or description).
- **`show=True`** lists the key (with its description) in Textual's `Footer` widget, for an extension widget that uses one: a `Footer` subclass with `DEFAULT_CSS = "Keys { dock: none; }"` in the `footer` area (a docked widget leaves the area zero rows tall). minimal draws no key hints of its own: `/ui` lists every key.
- **Re-keying.** The user gives a binding another key under `ui.keys` by `<extension id>:<name>` (`name`, else the description, else the key): `{"ui": {"keys": {"example:stats": "alt+s"}}}`; `""` unbinds it. `ui.keys` also rebinds minimal's own actions (`interrupt`, `toggle_sidebar`, `cycle_mode`; `KEY_ACTIONS`). Give every binding a stable `name` so a description edit does not break users' settings.

## Renderers

```python
def render_fence(ctx, block):
    if block["closed"]:
        return [[("┌ ", "muted"), (block["language"], "bold")], *block["source"].splitlines()]
    return None  # still streaming: draw the default

minimal.register_renderer("fence:mermaid", render_fence)
```

`register_renderer(kind, render)` draws one kind of transcript block yourself; registering a kind again replaces it. `render(ctx, block)` returns `None` to keep the default drawing, a string, or lines as for panels. A renderer that raises is reported once and the default is drawn. Kinds and their `block`:

| Kind                | `block`                                                                                                                                       |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `fence:<language>`  | `{"kind", "language", "source", "closed", "width"}`: a Markdown code fence of that language (lowercase, first word of the info string). While it streams, `source` holds its complete lines and `closed` is false; a reply that ends with the fence open draws it once more as closed |
| `blockquote:alert`  | `{"kind", "alert", "text", "width"}`: a GitHub alert (`> [!NOTE]`); `alert` is `note`, `tip`, `important`, `warning` or `caution`, `text` the first paragraph after the marker. The quote's other paragraphs are drawn normally below the lines |
| `tool:<name>`       | `{"kind", "call", "result"}`: the body of a call to that tool. Called with `result` `None` when the call is shown and again with the `tool_result` block when it finishes |

Only the terminal UI draws renderers; `width` is the columns available.

## Themes

```python
minimal.register_theme("ocean", dark=True, background="#0b1622", foreground="#d0e4f5", accent="#4fc3f7")
```

A theme needs `background`, `foreground` and `accent` (`#rrggbb`); the other colours (`THEME_COLORS`: `surface`, `muted`, `error`, `user_bg`, `diff_added`, …) are optional and derived when left out. The built-in theme is `minimal` (black and white) and cannot be replaced; `amber`, `dark` and `light` are in the marketplace's `themes/` (install them with `/ext` → Themes). Registered themes appear in `/theme` and `Ctrl+P` → Theme. If a reload removes the active theme, the UI goes back to `minimal`. `minimal.api.theme` (also `minimal.api.glyphs`) holds the glyphs (`theme.SUCCESS`, `theme.BRANCH`, …) and imports no UI toolkit; its old names for the built-in theme (`DEFAULT`, `MINIMAL`, `build`, `available`) were removed in API 2.0 (the colour themes are internal to the terminal UI, `minimal.ui.tui.theme`).

## Header logo

```python
minimal.register_logo(["#.#", "###"], {"#": "#4fc3f7"}, align="center")
```

Without a logo, the welcome header is a left-aligned wordmark, `─●─  m i n i m a l ▌  ─●─`, with the tagline `bare by default. yours by config.` and the version under it. A logo replaces the wordmark with pixel art; `align` (`"left"`, `"center"` by default, or `"right"`) places it and the header text under it.

`art` is a list of equal-length strings, one character per pixel (`" "` or `"."` transparent); `colors` maps every other character to `#rrggbb` or a theme variable such as `$accent` or `$foreground`, so the logo can follow `/theme`. Two pixel rows make one terminal row, drawn with half blocks (`▀▄█`) in truecolor. The last logo loaded wins. The marketplace's `widgets/logo.py` is a gradient pixel-art M with the `minimal` wordmark; its colours are constants at the top of the file. `LogoSpec` and `logo_content` (in `minimal.api`) render a logo outside the header.

To make a logo from any image (pixel art or a picture), convert it with `scripts/logo.py` from the minimal repository (a standalone script, not part of the installed package; `uv run` fetches Pillow for it):

```bash
uv run scripts/logo.py logo.png -w 24 -o ~/.minimal/extensions/widgets/logo.py   # then /reload
```

`-w` is the width in pixels (one terminal column each; images only shrink), `-c` the most colours to keep (default 16; pixel art with no more keeps its exact colours), `--nearest` resizes without smoothing. Without `-o` the extension is printed.

## Trust

A project's `./.minimal/extensions/` is code from the repository, so minimal does not run it until you trust it. Untrusted, it is skipped without asking: the transcript says so and the status line shows `⚠ N untrusted`. `/trust` prints a static scan of each file (what it can do: run commands, use the network, read environment variables or credential files, write files, decode hidden data, hook tool calls…; a list of capabilities, not of intent), together with the project's trust-gated settings (`permissions.allow`, `extensions.disabled`, `extensions.settings`), and asks once. The answer is remembered for exactly the bytes that were scanned (`~/.minimal/trusted.json`); adding or editing a file, or a trust-gated setting, makes the project untrusted again, including files written by the model or installed by `/ext`. `--trust-project` applies them without `/trust`; `-y` does not.

### Project trust

`minimal.project_trusted` (on the `Extension`, already right while `register` runs) and `ctx.project_trusted` are `True` when the user trusted this project: `/trust` accepted its present extensions and trust-gated settings (and nothing changed since), or minimal runs with `--trust-project`. Read it before acting on what the repository controls (prompt text, servers, scripts): an extension in `~/.minimal/extensions/` runs in every project, trusted or not. `/trust` reloads, so a `minimal.project_trusted` read at registration is current. A project with nothing for `/trust` to review (no `.minimal/extensions`, no trust-gated settings) is **not** trusted unless `--trust-project` is set.

## Reloading

`/reload`, the model's `reload_extensions` tool and `ctx.reload()` rebuild every registration from disk in place (modes and prompt sections included; the current mode stays if it is registered again): new tools are offered on the model's next request, even in the middle of a turn, and widgets are remounted; the `ui.*` settings (layout, keys, density, stylesheet) are applied again. Before that, every `on_dispose` function of the extensions loaded so far runs, so a thread or timer started by the old copy does not outlive it. Load errors (syntax errors, a missing or unsupported `__api__`, setting errors) are listed in the summary, and returned to the model when it reloaded.

## Publishing and `minimal ext check`

A marketplace is a git repository with one top-level folder per category: `extensions/`, `themes/`, `tools/` and `widgets/`, with extension files and package folders. Other files and folders are not items. `/ext`, `ext_search` and `ext_install` install into `~/.minimal/extensions/<category>/` (`extensions` items directly into `~/.minimal/extensions/`), or the project's; a name is unique per category. Publishing (`/ext publish`, `ext_publish`) files an item under `<category>/`. Listings need a docstring, `__version__` and `__api__`.

**Versions are commits.** minimal keeps a clone of the marketplace and, for each item, looks at the commits that touched its path, newest first. It offers and installs the newest one whose `__api__` this minimal can run: HEAD's if it can, otherwise an older one (extracted with `git archive`, and labelled "latest for this minimal (newer needs API 2)"). So publishing a version for a new API major does not strand users of the old one, as long as the old version stays in the history; an item with no version for this minimal is shown but not installed.

**The lockfile.** Each extensions folder (`~/.minimal/extensions/` and `./.minimal/extensions/`) has a `.lock.json` written by the marketplace on install and update and cleaned on uninstall. It is keyed by extension id (`name`, or `category/name`) and holds `marketplace`, `commit`, `version`, `api` and `sha256` (over each installed file's path and bytes) of the copy as installed. The loader skips dot-files, so it is never loaded. Updates compare commits, not bytes: an item is outdated when its locked commit is not the one on offer. A copy whose files no longer match `sha256` has local changes; `/ext` leaves its update unticked ("local changes will be overwritten") and `ext_install` refuses it unless called with `force: true`. A copy without a lock entry (installed by hand or by an older minimal) is compared byte by byte with the offered version and has no local-change protection.

```
/ext search <query> [category] search every marketplace by name, description or tag
/ext publish <path> [name]     push a publish/<name>-<timestamp> branch and open a pull request (with `gh`)
```

The model's `ext_publish` tool does the same, with an optional `category` and `marketplace` (a configured name or URL; give a marketplace a name with `{"name": "team", "url": "…"}` in the global `marketplaces` setting or in the `/ext` marketplace picker, see [settings](settings.md)). Before publishing or opening a pull request by hand, validate:

```bash
minimal ext check my_ext.py my_pkg/      # files or package folders
minimal ext check .                      # a whole marketplace checkout
minimal ext check --install-deps .       # install every item's missing __dependencies__ first (CI)
```

It loads each extension without a terminal or a provider and prints one line per item:

```
ok   tools/fetch 1.0
WARN extensions/mermaid: needs termaid
FAIL widgets/broken: syntax error: '(' was never closed (broken.py, line 2)
```

In a marketplace checkout `ext check` looks at every item of every category folder. `WARN` means a module the extension imports is not installed here (also when the extension raises `ImportError(..., name="pkg")` for a missing `pkg` itself); `FAIL` is a missing docstring, version or `__api__`, an `__api__` this minimal does not provide, a malformed `__dependencies__`, `__depends__` or `__conflicts__`, a load error, a settings error, or both `x.py` and `x/` in one category. The exit status is 1 if any line is `FAIL`.

Only the package name of a `__dependencies__` entry is checked when deciding what is missing, not its version range.

## What `minimal.api` exports

Import from `minimal.api` only; these names stay put when internal modules move.

| Name                                                        | What it is                                                        |
| ----------------------------------------------------------- | ----------------------------------------------------------------- |
| `__version__`, `API_VERSION`                                | minimal's version, and the version of this API ([API versions](#api-versions)) |
| `Extension`, `Context`, `UI`, `Tool`                        | The objects described above                                       |
| `AgentResult`, `AgentInfo`, `MAIN_AGENT`, `AGENT_STATUSES`  | What `run_agent_result` returns, what `ctx.agents()` lists, the id of the user's turn (`"main"`) and the statuses |
| `ModeSpec`, `PROMPT_SECTIONS`, `PromptText`                 | What `ctx.mode_info()` returns (`name`, `description`, `tools`, `exclude_tools`, `system_note`, `read_only`), the built-in prompt section names, the type of a section's text |
| `Provider`, `ProviderSpec`, `Reply`, `Usage`, `result_text` | Providers and replies; `result_text(content)` is a reply's or a tool result's text (a picture counts as `[image: media_type, N bytes]`) |
| `image_block`, `IMAGE_TYPES`                                | `image_block(data, media_type)` builds an `image` block from bytes (or base64 text) of one of `IMAGE_TYPES`, for a tool result or a message (API 2.3) |
| `REASONING_EFFORTS`                                         | `("low", "medium", "high")`: the values of `reasoning_effort` and `ctx.set_reasoning_effort` (API 2.3) |
| `openai_compatible`                                         | `openai_compatible(name, base_url, default_model, *, key_env=None, description="")` returns a `ProviderSpec` for an OpenAI-compatible server |
| `AnthropicProvider`                                         | The Anthropic Messages provider, to subclass: set `name`, and `identity` (a first system block) for a provider that reaches Claude another way (the marketplace's `claude_subscription`), and build it with your own `client` |
| `TurnInterrupted`                                           | Raised when the user interrupts                                   |
| `USER_DENIED`                                               | The text of a tool result the user declined                       |
| `ToolCallEvent`, `ToolResultEvent`, `TurnEndEvent`, `SessionStartEvent`, `SessionSwitchEvent`, `TurnStartEvent`, `BeforeRequestEvent`, `AfterResponseEvent`, `ShutdownEvent`, `ProviderErrorEvent`, `AgentStartEvent`, `AgentEndEvent`, `ModeChangeEvent` | Event payload types |
| `EVENTS`, `WIDGET_AREAS`, `STATUS_SIDES`, `THEME_COLORS`    | Valid event names, widget areas (`top`, `bottom`, `sidebar`, `left`, `footer`), status sides and theme colours |
| `KeybindingSpec`, `RESERVED_KEYS`, `KEY_ACTIONS`            | A registered keybinding (`key`, `run`, `description`, `show`, `name`, `ext`, `id`), the keys no extension may bind, and minimal's own key actions with their default keys |
| `LogoSpec`, `logo_content`, `theme`, `glyphs`               | Logo description, `logo_content(spec)` (a `LogoSpec` as Textual content) and the UI glyphs |
| `UsageTotals`                                               | What `ctx.usage` returns: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `requests` |
| `glob_regex`                                                | The glob matcher of `glob` and of path permission rules           |
| `ThemeInfo`, `ThemeChangeEvent`, `CSS_VARIABLES`, `CssSpec`  | What `ctx.theme` returns (`name`, `dark`, `colors`: `#rrggbb` by name), the `theme_change` payload, the CSS variables `$name` may stand for in styled text, and a registered stylesheet (API 2.3) |
| `GitInfo`, `KeyInfo`, `SettingsSpec`                        | What `ctx.git()`, `ctx.keybindings()` and a `declare_settings` call (`id`, `defaults`, `schema`, `values`) give (API 2.3) |

## API versions

`minimal.api` and the `Extension`/`Context` objects it exports are versioned apart from minimal itself: `API_VERSION` is `MAJOR.MINOR` (this minimal: `2.3`). A minor version only adds names or members; a major version changes or removes something. A minimal release that leaves the API alone does not change it, so extensions do not need touching for it.

API 1.1 added the events `session_start`, `session_switch`, `turn_start`, `before_request`, `after_response`, `shutdown` and `provider_error`, `minimal.on_dispose`, `minimal.data_dir`, `ctx.notify`, `ctx.use_provider`, `ctx.queue_user_message`, and request options: `Provider.stream(..., options=None)` (`max_tokens`, `temperature`; passed only when set, so older providers keep working) and `ctx.complete(..., options=None)`. An extension using any of them declares `__api__ = "1.1"`.

API 1.3 (1.2 was never released) added `register_panel` and `register_renderer` (and `PanelSpec`, `RendererSpec`, `STYLES`, `styled_line`, `styled_lines` in `minimal.api`), the `end` live event, styled-text status segments and `minimal.api.glyphs`; importing `minimal.api` no longer imports Textual. An extension using any of them declares `__api__ = "1.3"`.

API 2.0 removed the theme names of `minimal.api.theme` (`DEFAULT`, `MINIMAL`, `build`, `available`): the colour themes are internal to the terminal UI (`minimal.ui.tui.theme`). They are in `REMOVED`. Everything else is unchanged, but a major version refuses every extension written for the old one, so extensions declare `__api__ = "2"` (nothing else in them needs to change unless they used those names).

API 2.1 added `minimal.add_provider(spec)` and `minimal.api.openai_compatible(...)` (several OpenAI-compatible servers at once), `minimal.api.AnthropicProvider`, and the `ProviderSpec` fields `sdk` and `install` (the optional package a provider imports and what to install when it is missing) and `login`, `logout` and `account` (a provider that signs in with a browser or an account instead of a key: `minimal --login [provider]`, `/login`, `/logout` and setup call them; `login()` blocks and prints its instructions, raises `ValueError` with a message for the user if it fails, and `account()` returns who is signed in or `None`). An extension using any of them declares `__api__ = "2.1"`.

API 2.2 added modes (`minimal.register_mode`, `ctx.mode`, `ctx.modes()`, `ctx.mode_info()`, `ctx.set_mode()`, `ctx.cycle_mode()`, the `mode_change` event, `/mode`), agent identity (`agent_id` and `parent_id` in the payloads of the loop events, the `agent_start` and `agent_end` events, `ctx.agent_id`, `ctx.parent_id`, `ctx.agents()`, `ctx.cancel_agent()`), the extended `ctx.run_agent` keywords and `ctx.run_agent_result` with `AgentResult`, named prompt sections (`minimal.register_prompt_section`, `minimal.remove_prompt_section`, `ctx.prompt_sections()`) and `project_trusted` on `Extension` and `Context`; `minimal.api` gained `AgentResult`, `AgentInfo`, `MAIN_AGENT`, `AGENT_STATUSES`, `ModeSpec`, `PROMPT_SECTIONS`, `PromptText` and the three event payload types. Nothing was removed or changed: the new payload keys are additions, the new `run_agent` keywords are optional, and the `ctx` an agent's tools get is the app's with its own identity and cancel flag. An extension using any of them declares `__api__ = "2.2"`.

API 2.3 added (read-only tools, turn limit, pictures and reasoning) `read_only` on `Tool`, `register_tool` and `@minimal.tool` (concurrent calls to read-only tools), tools that return a list of `text` and `image` blocks, `ctx.max_turns`, `ctx.reasoning_effort` and `ctx.set_reasoning_effort()`, the `reasoning_effort` request option of `Provider.stream`, `image` blocks (in user messages and tool results) and `thinking` blocks (in assistant replies) in the canonical history, and `minimal.api.image_block`, `IMAGE_TYPES` and `REASONING_EFFORTS`; `result_text` shows a picture as `[image: media_type, N bytes]`. An extension using any of them declares `__api__ = "2.3"`.

API 2.3 added `ctx.fork(at_index=None)` and `ctx.truncate(at_index)` (with `/fork` and `/rewind`), `ctx.complete_text(prompt, ...)` and the `model=` keyword of `ctx.complete`, `ctx.usage` (`UsageTotals`, also in `HostStatus`) with `/usage`, `ctx.progress(text)`, and the permission dialog's "this session" and "always" answers (the `Frontend.permission` and `Frontend.progress` methods a frontend implements). An extension using any of them declares `__api__ = "2.3"`.

API 2.3 added `complete` to `@minimal.command` and `register_command` (and the `complete` field of the `Command` the registry keeps): `complete(args, ctx) -> list[str]` returns the completions of the text typed after `/name ` ([Commands](#commands)). An extension using it declares `__api__ = "2.3"`.

API 2.3 added, for customisable interfaces: `minimal.register_keybinding` and the `@minimal.keybinding` decorator (`KeybindingSpec`, `RESERVED_KEYS`, `KEY_ACTIONS` in `minimal.api`), the `order` keyword of `register_widget`, `register_panel` and `register_status`, the areas `left` and `footer` in `WIDGET_AREAS`, and the metadata `__depends__` and `__conflicts__`. An extension using any of them declares `__api__ = "2.3"`.

API 2.3 added, for extensions that look and behave the way the user wants: `ctx.invalidate()` (redraw from any thread), `ctx.theme` (`ThemeInfo`) and the `theme_change` event (`ThemeChangeEvent`), `$name` and `#rrggbb` pieces in styled text, the `title` and `collapsible` keywords of `register_panel` and `register_widget`, `minimal.register_css` (`CssSpec`, `CSS_VARIABLES`) with the stable selectors of [Styling the terminal UI](#styling-the-terminal-ui), the `schema` argument of `declare_settings` (`SettingsSpec`) with `/ext settings`, `ctx.context_window`, `ctx.git()` (`GitInfo`) and `ctx.keybindings()` (`KeyInfo`). An extension using any of them declares `__api__ = "2.3"`.

API 2.4 added, for extensions that show their own interface: `ctx.ui.screen(factory)` (a modal Textual screen), `ctx.ui.pick(...)` (a fuzzy-filtered list with a live highlight callback and a preview pane) and `ctx.ui.overlay(...)` (read-only scrollable text), the `timeout` keyword of every `ctx.ui` question, `ctx.transcript` (`Transcript`: jump to, mark and search messages; the stable `-marked` class) and the `ui.preview_lines` setting. An extension using any of them declares `__api__ = "2.4"`.

Every extension declares the API it is written for with `__api__` (see [Metadata](#metadata)). The loader refuses one that declares nothing or another major version, before running any of its code, and says why; the marketplace installs the newest version of it written for this API, and lists it without installing if there is none. Importing a name `minimal.api` no longer has fails with the version that removed it and what to use instead (`minimal.api.REMOVED`), not a bare `cannot import name`.

**Changing the API.** Adding something bumps the minor version, and extensions that use it declare that minor (`__api__ = "1.1"`). Changing or removing something bumps the major version and is recorded in `REMOVED`; extensions written for the old major stop loading with a clear message until a version for the new one is installed. While minimal has no users outside its author there is no deprecation period: the old name goes in the same release. Once it does, a name will keep working, with a warning, for one minor version before the major that removes it.
