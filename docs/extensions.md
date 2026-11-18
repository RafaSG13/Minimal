# Writing extensions

An extension is Python code defining `register(minimal)`. It can add tools for the model, slash commands, providers, event hooks, system-prompt notes and sections, modes, widgets, status-line segments, colour themes and a header logo, and run agents of its own. This guide covers minimal API 2.2 ([API versions](#api-versions)).

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

[`extensions/example.py`](../extensions/example.py) is a complete extension: a tool, a command, a guardrail with a setting, a prompt note and a sidebar widget.

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

- `@minimal.tool(name=None, *, description="", input_schema=None, confirm=False, subject_key=None)`: the name defaults to the function's, the description to the first line of its docstring. Returns the function unchanged.
- `register_tool(name, run, description="", input_schema=None, confirm=False, subject_key=None)`: the same without a decorator.
- `run(input, ctx)` returns the text the model sees. An exception becomes an `is_error` result, so the model can correct itself.
- `confirm` is `True` to ask the user before every call, or `confirm(input, ctx) -> bool` to decide per call. `-y` skips the question.
- `subject_key` names the `input` field that [permission rules](settings.md#permissions) `name(pattern)` match. With `subject_key="url"`, the rule `fetch(https://example.com/*)` in `permissions.allow` runs matching calls without asking, and in `permissions.deny` blocks them. Without it, only the bare rule `fetch` applies to the tool. Built-in tools use `command` (`bash`) and `path` (`read`, `write`, `edit`, `grep`, `glob`).

### Commands

```python
@minimal.command(description="show how many messages the session has")
def stats(args, ctx):
    print(f"{len(ctx.messages)} messages")

minimal.register_command("wizard", run_wizard, description="...", terminal=True)
```

- `@minimal.command(name=None, *, description="", terminal=False)` / `register_command(name, run, description="", terminal=False)` add `/name`, run as `run(args, ctx)` with the text after the name. What it prints appears in the transcript. `terminal=True` runs it with the UI suspended, in the real terminal.

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

- `tool_result` sees the tool's whole output: `bash` output is not clipped until after the handlers run (up to 1 MB captured), so a handler can compress it.
- `turn_end` fires when a loop ends with a reply without tool calls. `nested` is `True` for a loop run inside a tool (`ctx.run_agent`, such as the subagent), whose `messages` are its own history, not `ctx.messages`. Skip nested loops for session-level work: `if event["nested"]: return`.
- `session_start` fires once per process, when the terminal UI is up; `resumed` is true if the session already had messages (`-c`, `-s`). A reload does not fire it again.
- `session_switch` fires when `/new`, `/resume` or `ctx.new_session` replace the session, after the old one is saved.
- `turn_start` fires when the user sends a message, before it is added to the history.
- `before_request` fires before every model request of a loop (`nested` as in `turn_end`). What a handler returns is sent instead for that request only: the history is not changed. `options` are the request options for the provider (`max_tokens`, `temperature`; empty for its defaults), and a returned `options` dict is merged into them. Combined by key, the last handler wins; a value of the wrong type is ignored with a warning. Do not mutate what you receive: return copies.
- `after_response` fires after each reply is added to the history; `usage` is the reply's `Usage`, or `None`.
- `shutdown` fires when minimal exits.
- `agent_start` and `agent_end` bracket every agent loop: the user's turn (`name` `"main"`, `prompt` the user's message) and each `ctx.run_agent` ([Agents](#agents)). `agent_end` fires however the loop ended, after its `turn_end` (if it had one) and before the caller gets the result; `status` is `done`, `interrupted`, `error` or `max_turns`, `text` the last reply's text, `usage` the `Usage` summed over the loop's replies and `error` the message of a failure, else `None`.
- `mode_change` fires when the current mode changes (`ctx.set_mode`, `/mode`, `Shift+Tab`, a reload that lost the mode); `old` and `new` are mode names or `None` for no mode ([Modes](#modes)).
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

## `ctx`: the `Context`

Tools, commands, event payloads, widgets and status segments receive a `Context` as `ctx`. Its properties read minimal live, so a `ctx` kept by a widget always sees the current session, model and tools, and it is safe to use from the thread that runs a turn.

| Member                           | What it is                                                                                 |
| -------------------------------- | ------------------------------------------------------------------------------------------ |
| `cwd`                            | Working directory of tools and of the system prompt                                        |
| `messages`                       | The current session's messages in the canonical format (live)                              |
| `session_id`                     | The current session's id                                                                   |
| `provider_name`, `model`         | Current provider and model                                                                 |
| `context_tokens`                 | Context size after the last reply that reported usage, or `None`                           |
| `tools`                          | Every registered tool by name (read-only, live)                                            |
| `agent_id`, `parent_id`          | The agent this `ctx` belongs to: `"main"` and `None` for the user's turn, commands and widgets; for the `ctx` a tool or handler of a `run_agent` agent gets, that agent's id and its parent's ([Agents](#agents)) |
| `project_trusted`                | `bool`: the user trusted the current project ([Project trust](#project-trust))              |
| `cancelled`                      | `bool`: the user interrupted the turn or command in progress (or `cancel_agent` stopped the agent this `ctx` belongs to); poll it in long loops |
| `check_cancelled()`              | Raise `TurnInterrupted` if `cancelled`                                                      |
| `ui`                             | Questions to the user (below)                                                              |
| `system_prompt()`                | The system prompt the next turn would send                                                 |
| `complete(messages, *, system=None, tools=True, options=None)` | One model request with the current provider and model; returns a `Reply`. No tools are run; `tools=True` offers them anyway to keep the prompt cache warm. `options` (`max_tokens`, `temperature`) go over the ones a turn uses. Interruptible |
| `run_agent(prompt, *, system_note="", exclude_tools=(), name=None, system=None, tools=None, model=None, provider=None, max_turns=None)` | Runs `prompt` as its own agent loop with an empty history and the same tools (minus `exclude_tools`), confirmations and permissions; returns the text of its last reply and raises what went wrong. `system_note` is appended to the system prompt; its `turn_end` has `nested` set. The other keywords: [Agents](#agents) |
| `run_agent_result(prompt, **same)` | The same, but returns an `AgentResult` (`agent_id`, `text`, `usage`, `status`, `error`, `messages`) and reports a failure in it instead of raising ([Agents](#agents)) |
| `agents()`                       | A snapshot list of `AgentInfo` of the agents of this session, running and finished ([Agents](#agents)) |
| `cancel_agent(agent_id)`         | Interrupts that agent and the agents it started, not the one that started it; `False` if it is not running |
| `mode`, `modes()`, `mode_info(name=None)`, `set_mode(name)`, `cycle_mode()` | The current mode's name (`None`: no mode), every mode and its description, the `ModeSpec` of a mode (default the current one; `None` if none), switching (`None` leaves the mode; `ValueError` if unknown), and going to the next one ([Modes](#modes)) |
| `prompt_sections()`              | The names of the system prompt's sections in order ([Prompt sections](#prompt-sections))    |
| `new_session(messages)`          | Saves the current session and continues in a new one holding `messages` (its header names the old one as `parent`, which `/sessions` shows); returns the old id. `ValueError` if the new session cannot be locked |
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

`ctx.ui` asks through whatever is showing minimal (the terminal UI or the plain terminal):

| Method                         | Returns                                                                        |
| ------------------------------ | ------------------------------------------------------------------------------ |
| `ask(question)`                | `bool`: a yes/no answer                                                        |
| `choose(title, options)`       | The value of one `(value, label)` option, or `None` if cancelled               |
| `choose_many(title, options)`  | The ticked values of `(value, label, checked)` options in order, or `None`     |
| `ask_text(title)`              | One line of text, stripped, or `None` if empty or cancelled                    |

```python
@minimal.command(description="summarise this session into a new one")
def fold(args, ctx):
    reply = ctx.complete(ctx.messages + [{"role": "user", "content": "Summarise the conversation."}])
    if ctx.ui.ask("Start a new session from the summary?"):
        old = ctx.new_session([{"role": "user", "content": result_text(reply.content)}])
        print(f"Continued from {old}")
```

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
| `max_turns`    | Most model requests it may make (default 50)                                                                      |

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

`register_panel(name, render, area="sidebar", live=None, interval=0)` adds styled text to the terminal UI without Textual code, in `area` (`WIDGET_AREAS`). `render(ctx)` returns `None` to hide the panel, a string (one line per `\n`) or a list of lines; a line is a string, a `(text, style)` tuple or a list of those pieces. A style is space-separated names from `STYLES`: colours `foreground`, `accent`, `secondary`, `success`, `warning`, `error`, `muted`, `dim`, `diff_added`, `diff_removed` and the modifiers `bold`, `italic`, `underline`, `reverse` (`"bold muted"`); `""` is the default; any other name is an error. `render` runs on the UI thread after mount, after every turn, command and reload, after each `live` event and every `interval` seconds (`> 0`), so keep it cheap and never block: start a thread for slow work and return what it last found. `live(ctx, event)` receives `{"kind": "start", "turn": bool}`, `{"kind": "tool_call", "call": dict}`, `{"kind": "tool_result", "call": dict, "result": dict}` and `{"kind": "end"}` (after every turn or command and after every mount). A panel that raises is hidden and reported once.

## Widgets

```python
from textual.widgets import Static

class SessionPanel(Static):
    DEFAULT_CSS = "SessionPanel { padding: 0 1; }"

    def refresh_state(self, ctx):
        self.update(f"{len(ctx.messages)} messages · {ctx.model}")

minimal.register_widget("session", lambda ctx: SessionPanel(), area="sidebar")
```

`register_widget(name, create, area)` places the widget built by `create(ctx)` in `area`, one of `WIDGET_AREAS`: `"top"`, `"bottom"` or `"sidebar"`. A widget is any [Textual](https://textual.textualize.io/) widget, styled with its own `DEFAULT_CSS`. Besides Textual's standard variables (`$primary`, `$surface`, …), the CSS can use the theme's own: `$muted`, `$dim`, `$border-muted`, `$user-message-bg`, `$tool-success-bg`, … (see `src/minimal/ui/tui/theme.py`). If it has a `refresh_state(ctx)` method, the UI calls it after mount, after every turn and command, and after every reload. If it has a `refresh_live(ctx, event)` method, the UI calls it while a turn runs: when a turn or command starts (`{"kind": "start", "turn": bool}`), after each tool call is shown (`{"kind": "tool_call", "call": …}`) and after each result (`{"kind": "tool_result", "call": …, "result": …}`; the result is not in `ctx.messages` until every call of that reply has finished). The `top` and `bottom` areas size to their content: give the widget a height and don't `dock` it.

To only show text, use a [panel](#panels): it needs no Textual code.

## Status segments

`register_status(name, render, side)` adds a segment to the status line, `side` being one of `STATUS_SIDES`: `left` after the git branch or `right` before the context size. `render(ctx)` returns `None` for no segment, a string, a `(text, style)` tuple or a list of strings and `(text, style)` pieces (style names: [Panels](#panels)); the terminal UI also accepts a `textual.content.Content`. It runs on every redraw, so keep it cheap. A segment that raises, or returns an unknown style, is skipped and reported once.

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

`/reload`, the model's `reload_extensions` tool and `ctx.reload()` rebuild every registration from disk in place (modes and prompt sections included; the current mode stays if it is registered again): new tools are offered on the model's next request, even in the middle of a turn, and widgets are remounted. Before that, every `on_dispose` function of the extensions loaded so far runs, so a thread or timer started by the old copy does not outlive it. Load errors (syntax errors, a missing or unsupported `__api__`, setting errors) are listed in the summary, and returned to the model when it reloaded.

## Publishing and `minimal ext check`

A marketplace is a git repository with one top-level folder per category: `extensions/`, `themes/`, `tools/` and `widgets/`, with extension files and package folders. Other files and folders are not items. `/ext`, `ext_search` and `ext_install` install into `~/.minimal/extensions/<category>/` (`extensions` items directly into `~/.minimal/extensions/`), or the project's; a name is unique per category. Publishing (`/ext publish`, `ext_publish`) files an item under `<category>/`. Listings need a docstring, `__version__` and `__api__`.

**Versions are commits.** minimal keeps a clone of the marketplace and, for each item, looks at the commits that touched its path, newest first. It offers and installs the newest one whose `__api__` this minimal can run: HEAD's if it can, otherwise an older one (extracted with `git archive`, and labelled "latest for this minimal (newer needs API 2)"). So publishing a version for a new API major does not strand users of the old one, as long as the old version stays in the history; an item with no version for this minimal is shown but not installed.

**The lockfile.** Each extensions folder (`~/.minimal/extensions/` and `./.minimal/extensions/`) has a `.lock.json` written by the marketplace on install and update and cleaned on uninstall. It is keyed by extension id (`name`, or `category/name`) and holds `marketplace`, `commit`, `version`, `api` and `sha256` (over each installed file's path and bytes) of the copy as installed. The loader skips dot-files, so it is never loaded. Updates compare commits, not bytes: an item is outdated when its locked commit is not the one on offer. A copy whose files no longer match `sha256` has local changes; `/ext` leaves its update unticked ("local changes will be overwritten") and `ext_install` refuses it unless called with `force: true`. A copy without a lock entry (installed by hand or by an older minimal) is compared byte by byte with the offered version and has no local-change protection.

```
/ext publish <path> [name]     push a publish/<name>-<timestamp> branch and open a pull request (with `gh`)
```

The model's `ext_publish` tool does the same, with an optional `category` and `marketplace` (one of the configured URLs). Before publishing or opening a pull request by hand, validate:

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

In a marketplace checkout `ext check` looks at every item of every category folder. `WARN` means a module the extension imports is not installed here (also when the extension raises `ImportError(..., name="pkg")` for a missing `pkg` itself); `FAIL` is a missing docstring, version or `__api__`, an `__api__` this minimal does not provide, a malformed `__dependencies__`, a load error, a settings error, or both `x.py` and `x/` in one category. The exit status is 1 if any line is `FAIL`.

Only the package name of a `__dependencies__` entry is checked when deciding what is missing, not its version range.

## What `minimal.api` exports

Import from `minimal.api` only; these names stay put when internal modules move.

| Name                                                        | What it is                                                        |
| ----------------------------------------------------------- | ----------------------------------------------------------------- |
| `__version__`, `API_VERSION`                                | minimal's version, and the version of this API ([API versions](#api-versions)) |
| `Extension`, `Context`, `UI`, `Tool`                        | The objects described above                                       |
| `AgentResult`, `AgentInfo`, `MAIN_AGENT`, `AGENT_STATUSES`  | What `run_agent_result` returns, what `ctx.agents()` lists, the id of the user's turn (`"main"`) and the statuses |
| `ModeSpec`, `PROMPT_SECTIONS`, `PromptText`                 | What `ctx.mode_info()` returns (`name`, `description`, `tools`, `exclude_tools`, `system_note`, `read_only`), the built-in prompt section names, the type of a section's text |
| `Provider`, `ProviderSpec`, `Reply`, `Usage`, `result_text` | Providers and replies; `result_text(content)` is a reply's text   |
| `openai_compatible`                                         | `openai_compatible(name, base_url, default_model, *, key_env=None, description="")` returns a `ProviderSpec` for an OpenAI-compatible server |
| `AnthropicProvider`                                         | The Anthropic Messages provider, to subclass: set `name`, and `identity` (a first system block) for a provider that reaches Claude another way (the marketplace's `claude_subscription`), and build it with your own `client` |
| `TurnInterrupted`                                           | Raised when the user interrupts                                   |
| `USER_DENIED`                                               | The text of a tool result the user declined                       |
| `ToolCallEvent`, `ToolResultEvent`, `TurnEndEvent`, `SessionStartEvent`, `SessionSwitchEvent`, `TurnStartEvent`, `BeforeRequestEvent`, `AfterResponseEvent`, `ShutdownEvent`, `ProviderErrorEvent`, `AgentStartEvent`, `AgentEndEvent`, `ModeChangeEvent` | Event payload types |
| `EVENTS`, `WIDGET_AREAS`, `STATUS_SIDES`, `THEME_COLORS`    | Valid event names, widget areas, status sides and theme colours   |
| `LogoSpec`, `logo_content`, `theme`, `glyphs`               | Logo description, `logo_content(spec)` (a `LogoSpec` as Textual content) and the UI glyphs |
| `glob_regex`                                                | The glob matcher of `glob` and of path permission rules           |

## API versions

`minimal.api` and the `Extension`/`Context` objects it exports are versioned apart from minimal itself: `API_VERSION` is `MAJOR.MINOR` (this minimal: `2.2`). A minor version only adds names or members; a major version changes or removes something. A minimal release that leaves the API alone does not change it, so extensions do not need touching for it.

API 1.1 added the events `session_start`, `session_switch`, `turn_start`, `before_request`, `after_response`, `shutdown` and `provider_error`, `minimal.on_dispose`, `minimal.data_dir`, `ctx.notify`, `ctx.use_provider`, `ctx.queue_user_message`, and request options: `Provider.stream(..., options=None)` (`max_tokens`, `temperature`; passed only when set, so older providers keep working) and `ctx.complete(..., options=None)`. An extension using any of them declares `__api__ = "1.1"`.

API 1.3 (1.2 was never released) added `register_panel` and `register_renderer` (and `PanelSpec`, `RendererSpec`, `STYLES`, `styled_line`, `styled_lines` in `minimal.api`), the `end` live event, styled-text status segments and `minimal.api.glyphs`; importing `minimal.api` no longer imports Textual. An extension using any of them declares `__api__ = "1.3"`.

API 2.0 removed the theme names of `minimal.api.theme` (`DEFAULT`, `MINIMAL`, `build`, `available`): the colour themes are internal to the terminal UI (`minimal.ui.tui.theme`). They are in `REMOVED`. Everything else is unchanged, but a major version refuses every extension written for the old one, so extensions declare `__api__ = "2"` (nothing else in them needs to change unless they used those names).

API 2.1 added `minimal.add_provider(spec)` and `minimal.api.openai_compatible(...)` (several OpenAI-compatible servers at once), `minimal.api.AnthropicProvider`, and the `ProviderSpec` fields `sdk` and `install` (the optional package a provider imports and what to install when it is missing) and `login`, `logout` and `account` (a provider that signs in with a browser or an account instead of a key: `minimal --login [provider]`, `/login`, `/logout` and setup call them; `login()` blocks and prints its instructions, raises `ValueError` with a message for the user if it fails, and `account()` returns who is signed in or `None`). An extension using any of them declares `__api__ = "2.1"`.

API 2.2 added modes (`minimal.register_mode`, `ctx.mode`, `ctx.modes()`, `ctx.mode_info()`, `ctx.set_mode()`, `ctx.cycle_mode()`, the `mode_change` event, `/mode`), agent identity (`agent_id` and `parent_id` in the payloads of the loop events, the `agent_start` and `agent_end` events, `ctx.agent_id`, `ctx.parent_id`, `ctx.agents()`, `ctx.cancel_agent()`), the extended `ctx.run_agent` keywords and `ctx.run_agent_result` with `AgentResult`, named prompt sections (`minimal.register_prompt_section`, `minimal.remove_prompt_section`, `ctx.prompt_sections()`) and `project_trusted` on `Extension` and `Context`; `minimal.api` gained `AgentResult`, `AgentInfo`, `MAIN_AGENT`, `AGENT_STATUSES`, `ModeSpec`, `PROMPT_SECTIONS`, `PromptText` and the three event payload types. Nothing was removed or changed: the new payload keys are additions, the new `run_agent` keywords are optional, and the `ctx` an agent's tools get is the app's with its own identity and cancel flag. An extension using any of them declares `__api__ = "2.2"`.

Every extension declares the API it is written for with `__api__` (see [Metadata](#metadata)). The loader refuses one that declares nothing or another major version, before running any of its code, and says why; the marketplace installs the newest version of it written for this API, and lists it without installing if there is none. Importing a name `minimal.api` no longer has fails with the version that removed it and what to use instead (`minimal.api.REMOVED`), not a bare `cannot import name`.

**Changing the API.** Adding something bumps the minor version, and extensions that use it declare that minor (`__api__ = "1.1"`). Changing or removing something bumps the major version and is recorded in `REMOVED`; extensions written for the old major stop loading with a clear message until a version for the new one is installed. While minimal has no users outside its author there is no deprecation period: the old name goes in the same release. Once it does, a name will keep working, with a warning, for one minor version before the major that removes it.
