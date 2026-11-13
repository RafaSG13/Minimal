# Writing extensions

An extension is Python code defining `register(minimal)`. It can add tools for the model, slash commands, providers, event hooks, system-prompt notes, widgets, status-line segments, colour themes and a header logo. This guide covers minimal API 1.1 ([API versions](#api-versions)).

```python
"""Greets the user."""

__version__ = "1.0"
__api__ = "1"


def register(minimal):
    settings = minimal.declare_settings({"greeting": "hello"})

    @minimal.tool(input_schema={"type": "object", "properties": {}})
    def greet(input, ctx):
        """Greet the user."""
        return settings["greeting"]
```

[`extensions/example.py`](../extensions/example.py) is a complete extension: a tool, a command, a guardrail with a setting, a prompt note and a sidebar widget.

## Layout and where to save

Extensions load from three places:

| Place                    | Scope     | Notes                                                              |
| ------------------------ | --------- | ------------------------------------------------------------------ |
| `~/.minimal/extensions/` | `global`  | Always loaded                                                      |
| `./.minimal/extensions/` | `project` | Loaded only once the project is trusted (see [Trust](#trust))      |
| `minimal -e <path>`      | `cli`     | A file or folder; repeatable; never skipped by `extensions.disabled` |

Each folder's own entries load first, then those in its `themes/`, `tools/` and `widgets/` subfolders (where the marketplace installs those categories), alphabetically within a folder. Names starting with `_` or `.` are skipped.

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
| `__api__`      | `"1"`: the [API version](#api-versions) it is written for, required. `"1"` runs on any 1.x; `"1.2"` needs 1.2 or a later 1.x |
| `__dependencies__` | `["termaid>=0.3"]`: optional list of third-party packages it needs (name, extras, version comparisons; no URLs or options). `/ext` and `ext_install` offer to install the missing ones into minimal's Python after installing the extension |

An extension without `__api__` is not loaded (`declares no __api__; add __api__ = "1"`), and neither is one written for another major version (`written for minimal API 0, this minimal has API 1.1`) or a newer minor (`needs minimal API 1.3, this minimal has API 1.1: upgrade minimal`).

## `minimal`: the `Extension`

`register(minimal)` receives an `Extension`, one per extension.

| Member                 | What it is                                                                                         |
| ---------------------- | -------------------------------------------------------------------------------------------------- |
| `id`, `path`, `scope`, `version` | Read-only: the id, the `.py` file or package folder, `"global"`/`"project"`/`"cli"`, `__version__` (`""` if unset) |
| `tools`                | Every registered tool by name (read-only, live): `minimal.tools["bash"].run(input, ctx)` reuses one |
| `settings`             | What `declare_settings` returned; empty before it is called                                         |

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

`on(event, handler=None)` subscribes `handler(event)`; without a handler it is a decorator. An unknown event name raises `ValueError`. The payloads are typed in `minimal.api` (`ToolCallEvent`, `ToolResultEvent`, `TurnEndEvent`, `SessionStartEvent`, `SessionSwitchEvent`, `TurnStartEvent`, `BeforeRequestEvent`, `AfterResponseEvent`, `ShutdownEvent`, `ProviderErrorEvent`):

| Event            | Payload                                          | Return                                                                 |
| ---------------- | ------------------------------------------------ | ---------------------------------------------------------------------- |
| `tool_call`      | `name`, `input`, `ctx`                           | `{"block": True, "reason": str}` vetoes the call; a handler that raises blocks it too |
| `tool_result`    | `name`, `input`, `output`, `is_error`, `ctx`     | `{"output": str}` rewrites what the model sees                         |
| `turn_end`       | `messages`, `ctx`, `nested`                      | ignored                                                                |
| `session_start`  | `ctx`, `resumed`                                 | ignored                                                                |
| `session_switch` | `ctx`, `old_id`, `new_id`                        | ignored                                                                |
| `turn_start`     | `ctx`, `text`                                    | `{"text": str}` rewrites the user's message (the last non-empty one wins) |
| `before_request` | `ctx`, `messages`, `system`, `tools`, `options`, `nested` | `{"messages": list, "system": str, "tools": list}`, any subset: sent instead, for this request only; `{"options": dict}` adds request options |
| `after_response` | `ctx`, `reply`, `usage`, `nested`                | ignored                                                                |
| `shutdown`       | `ctx`                                            | ignored                                                                |
| `provider_error` | `ctx`, `error`, `attempt`, `provider`, `model`, `nested` | `{"retry_after": seconds}` retries after a wait; `{"provider": name, "model": name}` retries with another provider (the first such answer wins) |

- `tool_result` sees the tool's whole output: `bash` output is not clipped until after the handlers run (up to 1 MB captured), so a handler can compress it.
- `turn_end` fires when a loop ends with a reply without tool calls. `nested` is `True` for a loop run inside a tool (`ctx.run_agent`, such as the subagent), whose `messages` are its own history, not `ctx.messages`. Skip nested loops for session-level work: `if event["nested"]: return`.
- `session_start` fires once per process, when the terminal UI is up; `resumed` is true if the session already had messages (`-c`, `-s`). A reload does not fire it again.
- `session_switch` fires when `/new`, `/resume` or `ctx.new_session` replace the session, after the old one is saved.
- `turn_start` fires when the user sends a message, before it is added to the history.
- `before_request` fires before every model request of a loop (`nested` as in `turn_end`). What a handler returns is sent instead for that request only: the history is not changed. `options` are the request options for the provider (`max_tokens`, `temperature`; empty for its defaults), and a returned `options` dict is merged into them. Combined by key, the last handler wins; a value of the wrong type is ignored with a warning. Do not mutate what you receive: return copies.
- `after_response` fires after each reply is added to the history; `usage` is the reply's `Usage`, or `None`.
- `shutdown` fires when minimal exits.
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
| `register_provider(name, create, default_model, env_keys=(), description="")` | A model integration: `create()` returns a `Provider` (see the README's "Adding a provider") |
| `register_widget(name, create, area="sidebar")`                     | A terminal UI widget ([Widgets](#widgets))                    |
| `register_status(name, render, side="right")`                       | A status-line segment ([Status segments](#status-segments))   |
| `register_theme(name, *, dark, background, foreground, accent, **colors)` | A colour theme ([Themes](#themes))                      |
| `register_logo(art, colors, align="center")`                        | Pixel art in the welcome header ([Header logo](#header-logo)) |
| `register_frontend(name, run, description="")`                      | A user interface, picked with `--ui name` ([Frontends](#frontends)) |
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
| `cancelled`                      | `bool`: the user interrupted the turn or command in progress; poll it in long loops        |
| `check_cancelled()`              | Raise `TurnInterrupted` if the user interrupted                                            |
| `ui`                             | Questions to the user (below)                                                              |
| `system_prompt()`                | The system prompt the next turn would send                                                 |
| `complete(messages, *, system=None, tools=True, options=None)` | One model request with the current provider and model; returns a `Reply`. No tools are run; `tools=True` offers them anyway to keep the prompt cache warm. `options` (`max_tokens`, `temperature`) go over the ones a turn uses. Interruptible |
| `run_agent(prompt, *, system_note="", exclude_tools=())` | Runs `prompt` as its own agent loop with an empty history and the same tools (minus `exclude_tools`), confirmations and permissions; returns the text of its last reply. `system_note` is appended to the system prompt; its `turn_end` has `nested` set |
| `new_session(messages)`          | Saves the current session and continues in a new one holding `messages` (its header names the old one as `parent`, which `/sessions` shows); returns the old id. `ValueError` if the new session cannot be locked |
| `sessions(*, all_projects=False)` | Saved session ids, oldest first: those started in this `cwd`, or every project's with `all_projects=True` |
| `read_session(session_id)`       | The messages of a saved session; `ValueError` if there is none                             |
| `reload()`                       | Reloads every extension from disk; returns the summary                                     |
| `commands()`                     | Every slash command name (without `/`) and its description: built-in and extension commands, an extension's replacing a built-in of the same name |
| `run_command(line, *, builtin=False)` | Runs `"/name args"` as if the user typed it; its output is printed and errors are reported, not raised. `builtin=True` runs the built-in even when an extension replaced it, so a replacement can hand over to the original |
| `models()`, `set_model(name)`    | The current provider's models (queried from its API, falling back to its default) and switching to one |
| `widget(name)`                   | The widget the frontend shows for `register_widget(name, ...)`, or `None` (no terminal UI, or it failed to mount) |
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

## Widgets

```python
from textual.widgets import Static

class SessionPanel(Static):
    DEFAULT_CSS = "SessionPanel { padding: 0 1; }"

    def refresh_state(self, ctx):
        self.update(f"{len(ctx.messages)} messages · {ctx.model}")

minimal.register_widget("session", lambda ctx: SessionPanel(), area="sidebar")
```

`register_widget(name, create, area)` places the widget built by `create(ctx)` in `area`, one of `WIDGET_AREAS`: `"top"`, `"bottom"` or `"sidebar"`. A widget is any [Textual](https://textual.textualize.io/) widget, styled with its own `DEFAULT_CSS`. Besides Textual's standard variables (`$primary`, `$surface`, …), the CSS can use the theme's own: `$muted`, `$dim`, `$border-muted`, `$user-message-bg`, `$tool-success-bg`, … (see `ui/tui/theme.py`). If it has a `refresh_state(ctx)` method, the UI calls it after mount, after every turn and command, and after every reload. If it has a `refresh_live(ctx, event)` method, the UI calls it while a turn runs: when a turn or command starts (`{"kind": "start", "turn": bool}`), after each tool call is shown (`{"kind": "tool_call", "call": …}`) and after each result (`{"kind": "tool_result", "call": …, "result": …}`; the result is not in `ctx.messages` until every call of that reply has finished). The `top` and `bottom` areas size to their content: give the widget a height and don't `dock` it.

## Status segments

`register_status(name, render, side)` adds a segment to the status line, `side` being one of `STATUS_SIDES`: `left` after the git branch or `right` before the context size. `render(ctx)` returns text, a `textual.content.Content` or `None` for no segment; it runs on every redraw, so keep it cheap. A segment that raises is skipped and reported once.

## Themes

```python
minimal.register_theme("ocean", dark=True, background="#0b1622", foreground="#d0e4f5", accent="#4fc3f7")
```

A theme needs `background`, `foreground` and `accent` (`#rrggbb`); the other colours (`THEME_COLORS`: `surface`, `muted`, `error`, `user_bg`, `diff_added`, …) are optional and derived when left out. The built-in theme is `minimal` (black and white) and cannot be replaced; `amber`, `dark` and `light` are in the marketplace's `themes/` (install them with `/ext` → Themes). Registered themes appear in `/theme` and `Ctrl+P` → Theme; both belong to the terminal UI, so other frontends ignore themes. If a reload removes the active theme, the UI goes back to `minimal`. `minimal.api.theme` (also `minimal.api.glyphs`) holds the glyphs (`theme.SUCCESS`, `theme.BRANCH`, …) and imports no UI toolkit; its old names for the built-in theme (`DEFAULT`, `MINIMAL`, `build`, `available`) still work and load the terminal UI when used.

## Header logo

```python
minimal.register_logo(["#.#", "###"], {"#": "#4fc3f7"}, align="center")
```

Without a logo, the welcome header is a left-aligned wordmark, `─●─  m i n i m a l ▌  ─●─`, with the tagline `bare by default. yours by config.` and the version under it. A logo replaces the wordmark with pixel art; `align` (`"left"`, `"center"` by default, or `"right"`) places it and the header text under it.

`art` is a list of equal-length strings, one character per pixel (`" "` or `"."` transparent); `colors` maps every other character to `#rrggbb` or a theme variable such as `$accent` or `$foreground`, so the logo can follow `/theme`. Two pixel rows make one terminal row, drawn with half blocks (`▀▄█`) in truecolor. The last logo loaded wins. The marketplace's `widgets/logo.py` is a gradient pixel-art M with the `minimal` wordmark; its colours are constants at the top of the file. `LogoSpec` and `logo_content` (in `minimal.api`) render a logo outside the header.

To make a logo from any image (pixel art or a picture), convert it with Pillow, which minimal does not depend on:

```bash
uv run --with pillow python -m minimal.extensions.logo logo.png -w 24 -o ~/.minimal/extensions/widgets/logo.py   # then /reload
```

`-w` is the width in pixels (one terminal column each; images only shrink), `-c` the most colours to keep (default 16; pixel art with no more keeps its exact colours), `--nearest` resizes without smoothing. Without `-o` the extension is printed.

## Frontends

A frontend is the interface minimal runs in. `tui` (the terminal UI) is the default and `console` is a plain line-by-line REPL; `--ui <name>` or the `ui` setting picks one, and `register_frontend(name, run, description)` adds another (or replaces one by name). minimal loads the extensions, starts the provider, then calls `run(host)` and exits when it returns.

```python
from minimal.api import ConsoleFrontend, ExitRepl

__version__ = "1.0"
__api__ = "1.2"


def register(minimal):
    def run(host):
        host.attach(ConsoleFrontend())       # or your own Frontend
        host.begin()                         # fires session_start
        while (line := input("> ").strip()) != "quit":
            if line.startswith("/"):
                try:
                    host.run_command(line)
                except ExitRepl:
                    return
            else:
                host.submit(line)            # runs the turn; returns when it ends

    minimal.register_frontend("tiny", run, "a minimal REPL")
```

The `Host` is everything a frontend may use: `attach(frontend)`, `begin()`, `submit(text)` and `run_command(line)` (both block until the turn or command ends; call them from a worker thread if the frontend has an event loop), `interrupt()` and `interrupting`, `command(name)` (a `Command` with `terminal` set needs the real terminal), `commands()`, `complete(prefix)` (`"/re"` → `["/reload", "/resume"]`), `register_command(name, run, description, terminal=False)` for commands only this frontend has (the terminal UI adds `/theme` this way), `next_queued()` (messages extensions queued with `ctx.queue_user_message`: send them between turns), `status()` (a `HostStatus`: model, provider, cwd, session id, context tokens, extension and untrusted counts), `messages`, `settings`, `ctx`, `extension_errors`, `untrusted_notice()`, and what extensions add to the interface: `widgets`, `status_segments`, `themes` and `logo`.

A `Frontend` shows minimal: `text(chunk)`, `tool_call(call)`, `tool_result(call, result)`, `turn_end(error)`, `ask(question)`, `choose(title, options)`, `choose_many(title, options)`, `ask_text(title)`, `notify(text, kind)`, `reloaded()`, `queued()` (an extension queued a message while idle) and `widget(name)`. `ConsoleFrontend` implements all of them with `print` and `input`; subclass it and override what you draw differently.

## Trust

A project's `./.minimal/extensions/` is code from the repository, so minimal does not run it until you trust it. Untrusted, it is skipped without asking: the transcript says so and the status line shows `⚠ N untrusted`. `/trust` prints a static scan of each file (what it can do: run commands, use the network, read environment variables or credential files, write files, decode hidden data, hook tool calls…; a list of capabilities, not of intent), together with the project's trust-gated settings (`permissions.allow`, `extensions.disabled`, `extensions.settings`), and asks once. The answer is remembered for exactly the bytes that were scanned (`~/.minimal/trusted.json`); adding or editing a file, or a trust-gated setting, makes the project untrusted again, including files written by the model or installed by `/ext`. `--trust-project` applies them without `/trust`; `-y` does not.

## Reloading

`/reload`, the model's `reload_extensions` tool and `ctx.reload()` rebuild every registration from disk in place: new tools are offered on the model's next request, even in the middle of a turn, and widgets are remounted. Before that, every `on_dispose` function of the extensions loaded so far runs, so a thread or timer started by the old copy does not outlive it. Load errors (syntax errors, a missing or unsupported `__api__`, setting errors) are listed in the summary, and returned to the model when it reloaded.

## Publishing and `minimal ext check`

A marketplace is a git repository with `extensions/`, `themes/`, `tools/` and `widgets/` folders holding extension files and package folders. Listings need a docstring, `__version__` and `__api__`.

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

`WARN` means a module the extension imports is not installed here (also when the extension raises `ImportError(..., name="pkg")` for a missing `pkg` itself); `FAIL` is a missing docstring, version or `__api__`, an `__api__` this minimal does not provide, a malformed `__dependencies__`, a load error, a settings error, or both `x.py` and `x/` in one category. The exit status is 1 if any line is `FAIL`.

Only the package name of a `__dependencies__` entry is checked when deciding what is missing, not its version range.

## What `minimal.api` exports

Import from `minimal.api` only; these names stay put when internal modules move.

| Name                                                        | What it is                                                        |
| ----------------------------------------------------------- | ----------------------------------------------------------------- |
| `__version__`, `API_VERSION`                                | minimal's version, and the version of this API ([API versions](#api-versions)) |
| `Extension`, `Context`, `UI`, `Tool`                        | The objects described above                                       |
| `Host`, `HostStatus`, `Frontend`, `ConsoleFrontend`, `FrontendSpec`, `ExitRepl` | Frontends ([Frontends](#frontends)); `ExitRepl` is raised by `/exit` |
| `Provider`, `ProviderSpec`, `Reply`, `Usage`, `result_text` | Providers and replies; `result_text(content)` is a reply's text   |
| `TurnInterrupted`                                           | Raised when the user interrupts                                   |
| `USER_DENIED`                                               | The text of a tool result the user declined                       |
| `ToolCallEvent`, `ToolResultEvent`, `TurnEndEvent`, `SessionStartEvent`, `SessionSwitchEvent`, `TurnStartEvent`, `BeforeRequestEvent`, `AfterResponseEvent`, `ShutdownEvent`, `ProviderErrorEvent` | Event payload types |
| `EVENTS`, `WIDGET_AREAS`, `STATUS_SIDES`, `THEME_COLORS`    | Valid event names, widget areas, status sides and theme colours   |
| `LogoSpec`, `logo_content`, `theme`, `glyphs`               | Logo rendering (`logo_content` returns Textual content and loads the terminal UI on first use) and the UI glyphs |
| `glob_regex`                                                | The glob matcher of `glob` and of path permission rules           |

## API versions

`minimal.api` and the `Extension`/`Context` objects it exports are versioned apart from minimal itself: `API_VERSION` is `MAJOR.MINOR` (this minimal: `1.2`). A minor version only adds names or members; a major version changes or removes something. A minimal release that leaves the API alone does not change it, so extensions do not need touching for it.

API 1.1 added the events `session_start`, `session_switch`, `turn_start`, `before_request`, `after_response`, `shutdown` and `provider_error`, `minimal.on_dispose`, `minimal.data_dir`, `ctx.notify`, `ctx.use_provider`, `ctx.queue_user_message`, and request options: `Provider.stream(..., options=None)` (`max_tokens`, `temperature`; passed only when set, so older providers keep working) and `ctx.complete(..., options=None)`. An extension using any of them declares `__api__ = "1.1"`.

API 1.2 added frontends: `register_frontend`, `Host`, `HostStatus`, `Frontend`, `ConsoleFrontend`, `FrontendSpec` and `ExitRepl`, and `minimal.api.glyphs`. Importing `minimal.api` no longer imports Textual. An extension using any of them declares `__api__ = "1.2"`.

Every extension declares the API it is written for with `__api__` (see [Metadata](#metadata)). The loader refuses one that declares nothing or another major version, before running any of its code, and says why; the marketplace installs the newest version of it written for this API, and lists it without installing if there is none. Importing a name `minimal.api` no longer has fails with the version that removed it and what to use instead (`minimal.api.REMOVED`), not a bare `cannot import name`.

**Changing the API.** Adding something bumps the minor version, and extensions that use it declare that minor (`__api__ = "1.1"`). Changing or removing something bumps the major version and is recorded in `REMOVED`; extensions written for the old major stop loading with a clear message until a version for the new one is installed. While minimal has no users outside its author there is no deprecation period: the old name goes in the same release. Once it does, a name will keep working, with a warning, for one minor version before the major that removes it.
