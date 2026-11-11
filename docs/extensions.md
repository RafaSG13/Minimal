# Writing extensions

An extension is Python code defining `register(minimal)`. It can add tools for the model, slash commands, providers, event hooks, system-prompt notes, widgets, status-line segments, colour themes and a header logo. This guide covers the API of minimal 0.2. Coming from 0.1? See [Migrating from 0.1](#migrating-from-01).

```python
"""Greets the user."""

__version__ = "1.0"
__requires__ = ">=0.2"


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
| `__requires__` | Versions of minimal it works with: comma-separated clauses of `>=`, `<=`, `==`, `!=`, `>`, `<` and a version, e.g. `">=0.2"` or `">=0.2,<0.4"` |

An unmet `__requires__` stops the extension from loading with `requires minimal >=9, this is 0.2.0`; an invalid one is an error too. Set `__requires__ = ">=0.2"` on anything that uses the API below.

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

`on(event, handler=None)` subscribes `handler(event)`; without a handler it is a decorator. An unknown event name raises `ValueError`. The payloads are typed as `ToolCallEvent`, `ToolResultEvent` and `TurnEndEvent` in `minimal.api`:

| Event         | Payload                                      | Return                                                                 |
| ------------- | -------------------------------------------- | ---------------------------------------------------------------------- |
| `tool_call`   | `name`, `input`, `ctx`                       | `{"block": True, "reason": str}` vetoes the call; a handler that raises blocks it too |
| `tool_result` | `name`, `input`, `output`, `is_error`, `ctx` | `{"output": str}` rewrites what the model sees                         |
| `turn_end`    | `messages`, `ctx`, `nested`                  | ignored                                                                |

- `tool_result` sees the tool's whole output: `bash` output is not clipped until after the handlers run (up to 1 MB captured), so a handler can compress it.
- `turn_end` fires when a loop ends with a reply without tool calls. `nested` is `True` for a loop run inside a tool (`ctx.run_agent`, such as the subagent), whose `messages` are its own history, not `ctx.messages`. Skip nested loops for session-level work: `if event["nested"]: return`.

### Other registrations

| Method                                                              | What it adds                                                  |
| ------------------------------------------------------------------- | ------------------------------------------------------------- |
| `append_system_prompt(text)`                                        | A paragraph at the end of the system prompt                   |
| `register_provider(name, create, default_model, env_keys=(), description="")` | A model integration: `create()` returns a `Provider` (see the README's "Adding a provider") |
| `register_widget(name, create, area="sidebar")`                     | A terminal UI widget ([Widgets](#widgets))                    |
| `register_status(name, render, side="right")`                       | A status-line segment ([Status segments](#status-segments))   |
| `register_theme(name, *, dark, background, foreground, accent, **colors)` | A colour theme ([Themes](#themes))                      |
| `register_logo(art, colors, align="center")`                        | Pixel art in the welcome header ([Header logo](#header-logo)) |

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
| `complete(messages, *, system=None, tools=True)` | One model request with the current provider and model; returns a `Reply`. No tools are run; `tools=True` offers them anyway to keep the prompt cache warm. Interruptible |
| `run_agent(prompt, *, system_note="", exclude_tools=())` | Runs `prompt` as its own agent loop with an empty history and the same tools (minus `exclude_tools`), confirmations and permissions; returns the text of its last reply. `system_note` is appended to the system prompt; its `turn_end` has `nested` set |
| `new_session(messages)`          | Saves the current session and continues in a new one holding `messages`; returns the old id |
| `sessions()`                     | Saved session ids, oldest first                                                            |
| `read_session(session_id)`       | The messages of a saved session; `ValueError` if there is none                             |
| `reload()`                       | Reloads every extension from disk; returns the summary                                     |

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

A theme needs `background`, `foreground` and `accent` (`#rrggbb`); the other colours (`THEME_COLORS`: `surface`, `muted`, `error`, `user_bg`, `diff_added`, …) are optional and derived when left out. The built-in theme is `minimal` (black and white) and cannot be replaced; `amber`, `dark` and `light` are in the marketplace's `themes/` (install them with `/ext` → Themes). Registered themes appear in `/theme` and `Ctrl+P` → Theme. If a reload removes the active theme, the UI goes back to `minimal`. The `theme` module in `minimal.api` holds the UI glyphs and the built-in theme.

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

## Trust

A project's `./.minimal/extensions/` is code from the repository, so minimal does not run it until you trust it. Untrusted, it is skipped without asking: the transcript says so and the status line shows `⚠ N untrusted`. `/trust` prints a static scan of each file (what it can do: run commands, use the network, read environment variables or credential files, write files, decode hidden data, hook tool calls…; a list of capabilities, not of intent), together with the project's trust-gated settings (`permissions.allow`, `extensions.disabled`, `extensions.settings`), and asks once. The answer is remembered for exactly the bytes that were scanned (`~/.minimal/trusted.json`); adding or editing a file, or a trust-gated setting, makes the project untrusted again, including files written by the model or installed by `/ext`. `--trust-project` applies them without `/trust`; `-y` does not.

## Reloading

`/reload`, the model's `reload_extensions` tool and `ctx.reload()` rebuild every registration from disk in place: new tools are offered on the model's next request, even in the middle of a turn, and widgets are remounted. Load errors (syntax errors, unmet `__requires__`, setting errors) are listed in the summary, and returned to the model when it reloaded.

## Publishing and `minimal ext check`

A marketplace is a git repository with `extensions/`, `themes/`, `tools/` and `widgets/` folders holding extension files and package folders. Listings need a docstring and `__version__`; one whose `__requires__` this minimal does not meet is shown but not installed.

```
/ext publish <path> [name]     push a publish/<name>-<timestamp> branch and open a pull request (with `gh`)
```

The model's `ext_publish` tool does the same, with an optional `category` and `marketplace` (one of the configured URLs). Before publishing or opening a pull request by hand, validate:

```bash
minimal ext check my_ext.py my_pkg/      # files or package folders
minimal ext check .                      # a whole marketplace checkout
```

It loads each extension without a terminal or a provider and prints one line per item:

```
ok   tools/fetch 1.0
WARN extensions/mermaid: needs termaid
FAIL widgets/broken: syntax error: '(' was never closed (broken.py, line 2)
```

`WARN` means a module the extension imports is not installed here; `FAIL` is a missing docstring or version, an unmet or invalid `__requires__`, a load error, a settings error, or both `x.py` and `x/` in one category. The exit status is 1 if any line is `FAIL`.

## What `minimal.api` exports

Import from `minimal.api` only; these names stay put when internal modules move.

| Name                                                        | What it is                                                        |
| ----------------------------------------------------------- | ----------------------------------------------------------------- |
| `__version__`                                               | minimal's version                                                 |
| `Extension`, `Context`, `UI`, `Tool`                        | The objects described above                                       |
| `Provider`, `ProviderSpec`, `Reply`, `Usage`, `result_text` | Providers and replies; `result_text(content)` is a reply's text   |
| `TurnInterrupted`                                           | Raised when the user interrupts                                   |
| `USER_DENIED`                                               | The text of a tool result the user declined                       |
| `ToolCallEvent`, `ToolResultEvent`, `TurnEndEvent`          | Event payload types                                               |
| `EVENTS`, `WIDGET_AREAS`, `STATUS_SIDES`, `THEME_COLORS`    | Valid event names, widget areas, status sides and theme colours   |
| `LogoSpec`, `logo_content`, `theme`                         | Logo rendering, UI glyphs and the built-in theme                  |
| `glob_regex`                                                | The glob matcher of `glob` and of path permission rules           |

## Migrating from 0.1

0.2 is a breaking change: `register(minimal)` no longer receives the internal registry, and `ctx` is no longer the app. Add `__requires__ = ">=0.2"` and update:

| 0.1                                                                 | 0.2                                                              |
| ------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `ctx.frontend.ask/choose/choose_many/ask_text(…)`                   | `ctx.ui.ask/choose/choose_many/ask_text(…)`                      |
| `ctx.session = open_session(new_id())` and copying messages         | `old_id = ctx.new_session(messages)`                             |
| `ctx.session.id`                                                    | `ctx.session_id`                                                 |
| `run_agent(...)` from `minimal.api`                                 | `ctx.run_agent(prompt, system_note=..., exclude_tools=...)`      |
| `ctx.provider.stream(...)` with `build_system_prompt` and `tool_specs` | `ctx.complete(messages, system=None, tools=True)`             |
| `raise_if_cancelled(ctx)`                                           | `ctx.check_cancelled()`                                          |
| `ctx.cancelled.is_set()`                                            | `ctx.cancelled` (a `bool`)                                       |
| `ctx.cancelled.clear()` in a command                                | not needed: every command starts uninterrupted                   |
| `ctx.registry.tools`                                                | `ctx.tools` or `minimal.tools`                                   |
| `ctx.reload_extensions()` and `reload_summary(...)`                 | `ctx.reload()`                                                   |
| `ID_RE`, `SESSIONS_DIR` and reading session files                   | `ctx.sessions()`, `ctx.read_session(id)`                         |
| a child `Registry()` for a subagent                                 | `ctx.run_agent(..., exclude_tools={"subagent"})`                 |
| monkeypatching `minimal.tools.shell.clip` to see full bash output   | not needed: `tool_result` handlers see unclipped output          |
| `Path.cwd()` in handlers                                            | `event["ctx"].cwd`                                               |
| `"Error: the user denied the execution"`                            | `"Error: " + USER_DENIED`                                        |
| constants edited at the top of the file                             | `minimal.declare_settings({...})`                                |

`BUILTIN_COMMANDS`, `ID_RE`, `SESSIONS_DIR`, `Registry`, `build_system_prompt`, `new_id`, `open_session`, `raise_if_cancelled`, `reload_summary`, `run_agent` and `tool_specs` are no longer in `minimal.api`. The `register_*` methods and `on` keep their 0.1 signatures (`register_tool` adds `subject_key`), and `on` rejects unknown events.
