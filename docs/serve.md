# `minimal serve`: minimal without an interface

`minimal serve` runs the agent, the tools, the permissions and the extensions, and nothing else: no screen, no prompt. A **frontend** (a program in any language) starts it as a child process and talks to it in **JSON lines** on the child's standard input and output. minimal says what to show; the frontend says what the user did. The Go terminal UI in [`minimal-marketplace/frontends/go`](https://github.com/RafaSG13/minimal-marketplace/tree/main/frontends/go) is one; the Textual UI (`minimal-textual`) runs inside minimal's process instead, through `register_frontend` (see [extensions.md](extensions.md#frontends)).

```
minimal serve [options]
```

| Option | |
| ------ | - |
| `-c`, `--continue` | resume the last session of this directory |
| `-s`, `--session <id>` | open (or create) a specific session |
| `--provider <name>` | model integration (default `$MINIMAL_PROVIDER`, else the settings' provider) |
| `-m`, `--model <name>` | model (default `$MINIMAL_MODEL`, else the settings' model, else the provider's default) |
| `-e`, `--ext <path>` | load an extension file or folder (repeatable) |
| `-y`, `--yes` | do not ask for confirmation for `write`, `edit` and `bash` |
| `--trust-project` | apply this project's extensions and trust-gated settings without `/trust` |
| `-h`, `--help` | the help |

The project is the directory `minimal serve` is started in. The settings and the environment are those of `minimal` ([settings.md](settings.md)). `--ui`, `--setup`, `--login` and `--logout` do not exist here.

**Startup.** A provider must already be chosen (`minimal --setup`, the settings, `$MINIMAL_PROVIDER` or `--provider`) with working credentials: there is no terminal to ask in, so otherwise `minimal serve` prints the reason on stderr and exits with status 1 before sending anything. A session id that is not a plain name, or a session open in another minimal, exits with status 2. Extensions that need a UI toolkit (`__frontend__ = "textual"`) are skipped: under `serve` the toolkit is none; they are listed in the `skipped` field of `hello`.

**Streams.** Standard input and output carry only protocol lines. minimal moves them to private descriptors and points descriptor 0 at the null device and descriptor 1 at stderr, so a stray `print`, a library or a child process cannot corrupt the stream. What commands and extensions print while a turn or command runs reaches the frontend as `output` messages; anything else goes to stderr, which the frontend may show or log. Output is flushed after every line. Lines are UTF-8 (output is escaped ASCII), separated by `\n`.

**Ending.** The session ends when the frontend sends `quit`, closes minimal's stdin, or a command asks to quit (`/exit`, `/quit`). minimal then sends `quit`, closes stdout and exits with status 0 after running the extensions' shutdown handlers. If the frontend goes away without a word, minimal notices the closed pipe and ends the same way.

## Messages

Every message is one JSON object on one line with its kind in `"t"`. Unknown kinds, fields that are not listed here and lines that are not JSON objects are ignored, in both directions, so the protocol can grow within a version. The version is `PROTOCOL` (`2`, in `hello.protocol`); a frontend refuses another one. (Version 1 was the same messages with the direction inverted: minimal spawned the frontend.)

**Styled lines** (`panels`, `segments`, `rendered`, `body`): a list of lines, each a list of `[text, style]` pieces; `style` is `""` or space-separated names from `minimal.api.STYLES` (`foreground accent secondary success warning error muted dim diff_added diff_removed bold italic underline reverse`). Extensions' panels, status segments and renderers are Python and run inside `minimal serve`; the frontend only draws the lines.

**Messages of the conversation** (`hello.messages`): minimal's provider-neutral format. `{"role": "user", "content": "text"}`, or `content` a list of blocks: `{"type": "text", "text"}`, `{"type": "tool_use", "id", "name", "input"}` (assistant) and `{"type": "tool_result", "tool_use_id", "content", "is_error"}` (user).

### minimal → frontend (stdout)

| `t` | Fields | Meaning |
| --- | ------ | ------- |
| `hello` | `protocol` int, `version` str (minimal's), `status` object, `messages` list, `hidden` int, `renderers` list of str, `bodies` object, `errors` list of str, `skipped` list of str, `notice` str or null | First line. `status`: `model`, `provider`, `cwd`, `session_id`, `context_tokens` (int or null), `extensions` (how many loaded), `untrusted` (project extensions and settings not applied for lack of `/trust`). `messages`: the last 200 messages of a resumed session, to draw; `hidden` is how many older ones were left out. `renderers`: the block kinds an extension renders (`fence:mermaid`, `blockquote:alert`, `tool:<name>`; see `render`). `bodies`: styled lines by `tool_use` id for the resumed tool calls that a `tool:<name>` renderer draws. `errors`: extensions and settings that failed to load. `skipped`: extensions skipped for needing another toolkit. `notice`: the text telling the user to run `/trust`, or null. |
| `text` | `chunk` str | A piece of the model's answer, as it streams. |
| `tool_call` | `call` object, `body` styled lines or null | A tool is about to run. `call` is the `tool_use` block (`id`, `name`, `input`). `body` is what a `tool:<name>` renderer draws for it, or null for the default drawing. |
| `tool_result` | `call` object, `result` object, `body` as above | The tool finished. `result` is the `tool_result` block (`content`, `is_error`, …). `body` is drawn again with the result. |
| `turn_end` | `error` str or null | The turn finished; `error` says why if it failed or was interrupted. |
| `output` | `text` str, `stderr` bool | What a command or an extension printed (the output of `/help`, `/tools`, an extension's `print`), in pieces as it was written: concatenate them. |
| `notify` | `text` str, `kind` `"info"`, `"warning"` or `"error"` | A short notice (`ctx.notify`, a failed panel). |
| `reloaded` | `renderers`, `errors`, `skipped`, `notice` as in `hello` | Extensions were reloaded (`/reload`, `reload_extensions`, `/ext`): the registry is new, so the renderer kinds, panels and segments may differ. A `panels` and a `segments` message follow if they changed. |
| `idle` | `status` object | A user line (a message or a command) has finished and minimal waits for the next. `status` as in `hello`. |
| `ask` | `id` str, `question` str | A yes/no question (a confirmation to run `write`, `edit` or `bash`, or an extension's `ctx.ui.ask`). Answer with `answer` and `value` `true` or `false`. |
| `choose` | `id` str, `title` str, `options` list of `{value, label}` | Pick one option. Answer with `value` the chosen `value` string, or null to cancel. |
| `choose_many` | `id` str, `title` str, `options` list of `{value, label, checked}` | Tick any options (`checked`: pre-ticked). Answer with `value` a list of the ticked `value` strings (an empty list ticks none), or null to cancel. |
| `ask_text` | `id` str, `title` str | One line of text. Answer with `value` a string (empty means cancelled), or null. |
| `suspend` | | A command needs the real terminal (`/setup`, `/login`). Stop drawing, give the terminal back (leave the alternate screen, cooked mode, show the cursor) and answer `suspended`. minimal runs the command on the controlling terminal (`/dev/tty`, `CONIN$`/`CONOUT$` on Windows) itself. |
| `resume` | | The command ended: take the terminal again and redraw. `status` may have changed: it comes with the next `idle`. |
| `panels` | `panels` list of `{name, area, lines}` | Every panel extensions registered (`register_panel`) that is showing, in registration order; `area` is `"sidebar"`, `"top"` or `"bottom"`; `lines` are styled lines. Sent when they change (and once at start); a panel that returned nothing is absent. |
| `segments` | `left`, `right`: lists of styled lines | The extensions' status-line segments (`register_status`) by side, one line of pieces each. Sent when they change. |
| `rendered` | `id`, `lines` styled lines or null | The answer to `render`; null means draw it the default way. |
| `quit` | | minimal is ending. Close the interface. |

`ask`, `choose`, `choose_many` and `ask_text` block the turn (or command) that asked until the frontend answers. A question that is never answered is cancelled when the frontend quits or closes the pipe. Question ids are strings, unique within the session; answer with the `id` you were sent.

### frontend → minimal (stdin)

| `t` | Fields | Meaning |
| --- | ------ | ------- |
| `submit` | `text` str | A line the user entered, stripped: a message to the model, or a `/command`. Empty lines are ignored. Lines are run one at a time, in order; minimal sends `idle` after each. A message queued by an extension (`ctx.queue_user_message`) runs before the next `submit`. |
| `interrupt` | | Stop the turn in progress at its next streamed chunk, tool call or model request. The turn ends with a `turn_end` carrying an error. |
| `answer` | `id` str, `value` | The answer to a question (see the table above). An unknown `id` is ignored. |
| `suspended` | | The frontend gave the terminal up after `suspend`. minimal waits for it up to 5 seconds, then runs the command anyway. |
| `render` | `id`, `kind` str, `block` object | Ask for the lines an extension's renderer draws for `block` (`kind` is one of `hello.renderers`): for `fence:<language>` `{kind, language, source, closed, width}`; for `blockquote:alert` `{kind, alert, text, width}`; for `tool:<name>` `{kind, call, result}`. Answered by `rendered` with the same `id`, from another thread, so a slow renderer does not hold up the rest. |
| `quit` | | End the session. |

## A session

```
→ minimal serve --provider anthropic
← {"t":"hello","protocol":2,"version":"0.4.0","status":{"model":"claude-sonnet-5-5","provider":"anthropic","cwd":"/work/app","session_id":"2026-11-14T13-36-21-854Z","context_tokens":null,"extensions":3,"untrusted":0},"messages":[],"hidden":0,"renderers":[],"bodies":{},"errors":[],"skipped":[],"notice":null}
← {"t":"panels","panels":[]}
← {"t":"segments","left":[],"right":[]}
→ {"t":"submit","text":"what does main.py do?"}
← {"t":"tool_call","call":{"type":"tool_use","id":"toolu_1","name":"read","input":{"path":"main.py"}},"body":null}
← {"t":"tool_result","call":{…},"result":{"type":"tool_result","tool_use_id":"toolu_1","content":"…","is_error":false},"body":null}
← {"t":"text","chunk":"It starts the "}
← {"t":"text","chunk":"server."}
← {"t":"turn_end","error":null}
← {"t":"idle","status":{…}}
→ {"t":"quit"}
← {"t":"quit"}
```

## Writing a frontend

Find minimal with `$MINIMAL_BIN` (default `minimal` on `PATH`) and run `<bin> serve [your pass-through arguments]` in the project directory, with stdin and stdout piped and stderr inherited (or captured). Read lines until `hello`, check `protocol`, draw `hello.messages`, then send `submit` for each line the user enters, drawing what comes back. Do not send a second `submit` while a turn runs unless you mean to queue it: lines wait their turn, but `interrupt` acts on the one running. Handle `suspend`/`resume` if the user may run `/setup` or `/login`; a frontend that cannot give the terminal up should tell the user to run them with `minimal` instead. Slash-command completion is yours to do: `/help` and `/tools` print the names.
