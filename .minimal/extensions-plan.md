# Minimal: ten project-local extensions

## Progress

- [ ] 1. Loose Ends (Step 5)
- [ ] 2. Tripwire (Step 3)
- [ ] 3. Scope Fence (Step 2)
- [ ] 4. Output Lenses (Step 1)
- [ ] 5. Context Ledger + /prune (Step 4)
- [ ] 6. Crystallize (Step 6)
- [ ] 7. Baton (Step 7)
- [ ] 8. Rerun (Step 8)
- [ ] 9. X-ray (Step 9)
- [ ] 10. Ambient Theme (Step 10)

## Context

Minimal (`src/minimal/`) is a terminal coding-agent harness whose features are added by single-file Python extensions with `register(minimal)`. Ten proposed extensions are to be implemented: Output Lenses, Scope Fence, Tripwire, Context Ledger + `/prune`, Loose Ends, Crystallize, Baton, Rerun, X-ray, Ambient Theme. Decided scope: all ten, as project-local extensions under `<repo>/.minimal/extensions/` (loaded automatically when `minimal` runs from the repo root), with no automated tests. Nothing under `src/`, `extensions/`, `tests/`, `README.md` or `.gitignore` changes; `src/minimal/app.py` has unrelated uncommitted edits that must stay untouched.

The user wants this plan kept as a Markdown file in the repository and the work done one extension at a time, stopping after each so they can follow and understand it. The Execution protocol section below is binding: it defines the order, what to explain, and where to stop.

Repo root: `/Users/rafael.serrano/Developer/minimal-workspace/minimal`. Python 3.12, Textual 8.2.8, venv at `.venv/`.

## Core facts every step relies on

- Extension API is `Registry` in `src/minimal/registry.py`: `register_tool(name, run, description, input_schema, confirm)`, `add_tool(Tool)`, `register_command(name, run, description, terminal)`, `on(event, handler)`, `append_system_prompt(text)`, `register_widget(name, create, area)` with area `top`/`bottom`/`sidebar`. `minimal.tools` is the dict of registered `Tool` dataclasses; built-ins are registered before extensions load, so an extension can read and replace them.
- Hook payloads carry no `ctx`: `tool_call {name, input}` (return `{"block": True, "reason": str}` to veto; fires before the confirm dialog), `tool_result {name, input, output, is_error}` (return `{"output": str}`), `turn_end {messages, ctx}`. Handlers run on the worker thread; exceptions are caught by `Registry.emit` and printed.
- `agent.execute_tool_call` passes the same payload dict to every `tool_result` handler and keeps only the last returned `output`. The agent then applies `clip` (20,000 chars) through its own binding (`from minimal.tools import clip` in `agent.py`).
- `tools.bash` clips its own output before returning, so a `tool_result` handler normally never sees more than 20,000 chars.
- `ctx` for tools, commands and widgets is `App` (`src/minimal/app.py`): `cwd`, `messages`, `session`, `provider`, `provider_name`, `model`, `registry`, `auto_approve`, `context_tokens`, `extra_extensions`, `frontend` (`ask`, `choose`, `choose_many`, `ask_text`, `set_theme`), `reload_extensions()`, `use_provider(name, model)`, `run_turn(text)`.
- `commands.run_command` catches exceptions from a command and prints `Error in /name: <message>`; raise `ValueError` for usage errors.
- Anything printed (any thread) appears in the TUI transcript; in `-p` mode it goes to stdout.
- Widgets: `refresh_state(ctx)` is called on the UI thread after mount, every turn, every command and every reload, not after each tool call. The sidebar is 36 columns wide.
- Extension modules are re-executed on every reload; module-level memory is lost. Persistent state goes to disk.
- Load order: `~/.minimal/extensions` then `<cwd>/.minimal/extensions`; inside a folder its own `*.py` sorted by name, then `themes/`, `tools/`, `widgets/`. Handlers run in registration order.
- Session files are append-only (`Session.save` writes only messages beyond `persisted`), so rewriting history requires a new session: `ctx.session = open_session(new_id())`, extend `ctx.messages`, `ctx.session.save()` (pattern in `extensions/compact.py`). The TUI redraws the transcript when `ctx.session` changes.

## Execution protocol

Talk to the user in Spanish. Code, docstrings, identifiers and user-visible strings stay in English exactly as specified.

### First action: move the plan into the repository

1. Write this whole plan, verbatim, to `<repo>/.minimal/extensions-plan.md` (the loader only reads `*.py`, so this file is never executed; it sits next to `extensions/`, not inside it).
2. In that file, insert right after the `# ...` title line a section `## Progress` with this checklist, all unticked:

```
- [ ] 1. Loose Ends (Step 5)
- [ ] 2. Tripwire (Step 3)
- [ ] 3. Scope Fence (Step 2)
- [ ] 4. Output Lenses (Step 1)
- [ ] 5. Context Ledger + /prune (Step 4)
- [ ] 6. Crystallize (Step 6)
- [ ] 7. Baton (Step 7)
- [ ] 8. Rerun (Step 8)
- [ ] 9. X-ray (Step 9)
- [ ] 10. Ambient Theme (Step 10)
```

3. Create the verification harness described in Verification (`/tmp/minimal-verify/verify.py` and `/tmp/minimal-verify/fakeprov.py`) with the prelude only and no step checks yet.
4. Tell the user the file path, the order above and why it is that order, then stop and wait for them to say to continue. Write no extension yet.

### Implementation order

The order in the checklist goes from the simplest use of the extension API to the most involved, so each step introduces one or two new ideas:

1. Loose Ends: every basic piece once (tool, system-prompt note, widget, command, a JSON state file).
2. Tripwire: the `tool_result` hook and rewriting a tool's output.
3. Scope Fence: the `tool_call` hook and vetoing a call.
4. Output Lenses: replacing a built-in tool and loading user code.
5. Context Ledger: reading the message history and switching sessions.
6. Crystallize: mining session files and generating new extensions.
7. Baton: a model call outside the agent loop, and git patches.
8. Rerun: a second `App` and a git worktree.
9. X-ray: `ast` analysis and guarding a built-in tool.
10. Ambient Theme: Textual themes and the theme saved at exit.

The "Step N" numbers in Approach are names, not order. Handler order at runtime is fixed by file names (`lenses.py` < `scope_fence.py` < `tripwire.py`), not by the order they are written in.

### Loop for each extension

1. Before writing code, explain in at most 8 bullets: the developer problem, which API pieces it uses and why (quote the hook, tool, command or widget names), the files it will create, and how it will be checked.
2. Write only that step's extension file, plus that step's `check_<name>` function in `/tmp/minimal-verify/verify.py`.
3. Run `cd <repo> && .venv/bin/python /tmp/minimal-verify/verify.py <name>` and fix the extension until it prints `PASS <name>`. Never report a step with a failing check.
4. Report: the file path, each top-level function or class with one line on what it does, the check output, and the "Try it yourself" line from the Verification table for that step.
5. Tick that line in `.minimal/extensions-plan.md` `## Progress`.
6. Stop. Do not start the next extension until the user says to continue. If the user asks for a change, apply it to the extension file and to the matching Step section of `.minimal/extensions-plan.md`, re-run the check, and stop again.

After the tenth step passes, delete `/tmp/minimal-verify/`.

## Approach

Steps 1 to 10 are independent of each other and each leaves a loadable tree; they are written in the order given by the Execution protocol, not by number. Step 0 applies to every step.

### Step 0: conventions for all ten files

- Each file is self-contained (no shared helper module; files starting with `_` are skipped by the loader and are not importable). Start with a module docstring whose first line is the description given in the step, then `__version__ = "1.0"`, and define `register(minimal)`.
- Project root: `Path(ctx.cwd)` in tools, commands and widgets; `Path.cwd()` inside hooks and `register()` (`cli.main` builds the `App` with `cwd=os.getcwd()`).
- State files live in `<root>/.minimal/state/`. Copy these helpers verbatim into each file that needs them:

```python
def _git(cwd, *args):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
```

- `_git` returns raw stdout (do not strip the whole output: porcelain lines start with spaces). `None` means not a repository, no `HEAD`, or git missing.
- Output chaining rule for every `tool_result` handler: read `event["output"]`, and when changing it assign the new text to `event["output"]` and return `{"output": event["output"]}`; return `None` when nothing changes. This is what lets three extensions append to the same result.
- Git pathspec that hides harness files: `GIT_SCOPE = ("--", ".", ":(exclude).minimal")`.
- Widgets subclass `textual.widgets.Static`, are created with `markup=False`, use `DEFAULT_CSS` with `padding: 0 1;` (copy `SessionPanel` in `extensions/example.py`), implement `refresh_state(self, ctx)`, and set `self.display = False` when they have nothing to show unless the step says otherwise.
- Commands split arguments with `shlex.split(args)` unless the step says the rest of the line is taken verbatim.

### Step 1: `.minimal/extensions/lenses.py` (Output Lenses)

Docstring: `Output lenses: compress noisy bash output and keep the full text on disk.`

1. Replace the `bash` tool so handlers receive unclipped output: `original = minimal.tools["bash"]`, then `minimal.add_tool(dataclasses.replace(original, run=_unclipped(original.run)))`. `_unclipped(run)` returns `wrapped(input, ctx)` that saves `core_tools.clip`, sets `core_tools.clip = lambda text: text`, calls `run(input, ctx)` and restores it in `finally` (`from minimal import tools as core_tools`). Reason: `tools.bash` calls the module-global `clip`; the agent's later clip is a separate binding and still applies.
2. Constants: `MIN_CHARS = 2_000`, `HEAD = 6_000`, `TAIL = 12_000`, `OUTPUT_DIR = ".minimal/outputs"`, `LENS_DIR = ".minimal/lenses"`.
3. Lens contract: `PATTERN` (regex string, `re.search`ed in the command) and `lens(command, output) -> str | None`. User lenses are every `*.py` in `<root>/.minimal/lenses/`, sorted, loaded in `register()` with `exec(compile(source, str(file), "exec"), namespace)`. A file missing `PATTERN`/`lens`, with a bad regex, or raising on load is skipped and its message collected; after everything else is registered, `register()` raises `ValueError("lenses: " + "; ".join(errors))` if any were collected, so `reload_extensions` reports it to the model.
4. Built-in lens `python-tests`, `PATTERN = r"\b(pytest|unittest)\b"`: drop lines that `fullmatch` any of `r".* \.\.\. ok"`, `r"[.sx]+"`, `r"\S+ [.sx]+\s*(\[\s*\d+%\])?"`, `r".* PASSED(\s+\[\s*\d+%\])?"`. Return `None` if none dropped, else the kept lines plus `\n[lens python-tests: {n} passing lines hidden]`.
5. `_repeat(output)`: collapse each run of 3 or more identical consecutive lines to the line followed by `[previous line repeated {run - 1} more times]`.
6. `tool_result` handler, only for `event["name"] == "bash"` with `len(raw) >= MIN_CHARS`:
   - `out = raw`; try user lenses then built-ins in order; the first whose pattern matches the command and returns a `str` different from `raw` sets `out` and stops the search. A lens that raises prints `[lenses] {name} failed: {error}` to stderr and counts as `None`.
   - `out = _repeat(out)`.
   - If `raw`'s last line starts with `[exit ` and `out.rstrip()` does not end with that line, append `"\n" + last_line`.
   - If `len(out) > HEAD + TAIL`: `out = out[:HEAD] + f"\n... [lens: {len(out) - HEAD - TAIL} characters omitted here]\n" + out[-TAIL:]`.
   - If `out == raw` return `None`.
   - Otherwise write `raw` to `<root>/.minimal/outputs/{n:04d}.txt` (`n` = highest existing numeric stem + 1, starting at 1; create the folder) and append `\n[full output: .minimal/outputs/{n:04d}.txt, {line_count} lines; read it with offset and limit]`. Apply the chaining rule.
7. `minimal.append_system_prompt(...)` with exactly: `Output lenses: bash output over 2000 characters is compressed and the full text is saved under .minimal/outputs/ (read it with offset and limit when you need hidden lines). When a command you run repeatedly produces noisy output, write a lens: a file .minimal/lenses/<name>.py defining PATTERN (a regex searched in the command) and lens(command, output) -> str | None (return the shortened output, or None to leave it unchanged), then call reload_extensions.`
8. Command `/lenses` (description `list output lenses; /lenses clear deletes saved outputs`): no args prints one line per lens, `{name}  {pattern}  {(built-in) | .minimal/lenses/<file>}`, then `{k} saved output(s) in .minimal/outputs`. `/lenses clear` deletes `*.txt` in the outputs folder and prints `Removed {k} saved output(s).` Anything else raises `ValueError("usage: /lenses [clear]")`.

### Step 2: `.minimal/extensions/scope_fence.py` (Scope Fence)

Docstring: `Scope fence: block edits outside declared paths or over a diff budget.`

1. State file `.minimal/state/fence.json`: `{"globs": [str], "lines": int | null, "blocked": [str]}`. A missing file means no fence. The fence is project-scoped and persists until `/fence off`.
2. Command `/fence` (description `limit edits: /fence <path-or-glob>... [--lines N] | /fence off`):
   - no args: print `Fence: {", ".join(globs) or "(any path)"}` plus `` · budget {lines} lines`` when set, or `No fence. Usage: /fence <path-or-glob>... [--lines N] | /fence off`.
   - `off`: delete the state file, print `Fence removed.`
   - otherwise: `--lines` must be followed by a positive integer, else `ValueError("usage: /fence <path-or-glob>... [--lines N] | /fence off")`. Every other token is a glob with a leading `./` removed; a token naming an existing directory `d` is stored as `d/**`. No globs with `--lines` is valid (budget only). Save with `blocked: []` and print the same `Fence: ...` line.
3. `_inside(rel, globs)`: `True` if `rel` starts with `.minimal/` or `globs` is empty; otherwise for each glob build `minimal.tools._glob_regex(glob)` (reuse the core translator) and match it with `fullmatch` against `PurePosixPath(rel).name` when the glob has no `/`, else against `rel`. This mirrors `tools._filter_glob`.
4. `tool_call` handler (no fence: return `None`):
   - `write`/`edit`: `path = Path(Path.cwd(), input["path"]).resolve()`; `rel` is its POSIX path relative to the root, or the absolute path when outside the root (always outside the fence). If not `_inside`: append `rel` to `blocked`, save, and return `{"block": True, "reason": f"{rel} is outside the fence ({', '.join(globs)}). Do not work around it: tell the user why you need this file so they can widen the fence with /fence."}`.
   - `write`/`edit` with `lines` set and `rel` not under `.minimal/`: `used` = sum of the added and deleted columns of `git diff --numstat HEAD` + `GIT_SCOPE` (skip `-` entries). If `used >= lines` return `{"block": True, "reason": f"diff budget used up ({used}/{lines} changed lines). Stop editing, summarise what is done and what remains."}`. When `_git` returns `None` the budget is not enforced.
   - `bash`: store the set of lines of `git status --porcelain -uall` + `GIT_SCOPE` in a dict created inside `register()` (`mem["before"]`); `None` outside git.
5. `tool_result` handler for `bash` when a fence exists and `mem["before"]` is not `None`: take the status again; for each line only in the new set, the path is `line[3:]`, the part after ` -> ` if present, with surrounding double quotes removed. Paths failing `_inside` are appended to `blocked` (saved) and this is appended to the output under the chaining rule: `\n[fence] this command changed files outside the fence: {", ".join(paths)}. Revert them unless the user asked for it.` A file already dirty before the command is not detected again; that limit is accepted.
6. Widget `fence`, area `sidebar`. Lines: `FENCE`, one line per glob (or `(any path)`), `budget {used}/{lines} lines` when set (`used` as above, `?` outside git), `blocked {len(blocked)}: {last three paths joined by ", "}` when non-empty. Hidden when there is no fence.

### Step 3: `.minimal/extensions/tripwire.py` (Tripwire)

Docstring: `Tripwire: re-run invariant commands after file changes and tell the model which call broke them.`

1. State file `.minimal/state/tripwires.json`: `{"wires": [{"cmd": str, "ok": bool | null, "tail": str}], "edits_since_green": int}`.
2. `_run_wire(cmd, cwd)`: `subprocess.run(["/bin/bash", "-c", cmd], cwd=cwd, capture_output=True, text=True, timeout=60)`; `ok = returncode == 0`; `tail` = last 15 lines of stdout + stderr, or `(no output)` when empty. On `TimeoutExpired`: `ok = False`, `tail = "timed out after 60s"`.
3. `_fingerprint(cwd)`: `None` if `git status --porcelain` + `GIT_SCOPE` is `None`; else SHA-1 hex of that output concatenated with `git diff HEAD` + `GIT_SCOPE` (empty string if `None`).
4. `register()` keeps `mem = {"fp": _fingerprint(Path.cwd()) if wires else None}`.
5. Command `/tripwire` (description `invariants: /tripwire add <command> | rm <n> | run`). The text after `add ` is the command verbatim (no `shlex`).
   - no args: one line per wire, `{n}. {passing | failing | not run}  {cmd}`, or `No tripwires. Usage: /tripwire add <command> | rm <n> | run`.
   - `add <command>`: run it, append the wire, set `mem["fp"]`, print `Tripwire added: {cmd} ({passing | failing} now)`.
   - `rm <n>`: 1-based; invalid index raises `ValueError(f"no tripwire {n}")`; print `Tripwire removed: {cmd}`.
   - `run`: run all, save, print the same listing as no args.
6. `tool_result` handler (no wires: return `None`): `fp = _fingerprint(Path.cwd())`. Run the wires when `event["name"] in ("write", "edit") and not event["is_error"]`, or when `fp is not None and fp != mem["fp"]`. Then set `mem["fp"] = fp`. For each wire compare the previous `ok` with the new one and collect notes:
   - `True -> False`: `[tripwire] "{cmd}" was passing before this call and fails now:\n{tail}`
   - `None -> False`: `[tripwire] "{cmd}" fails:\n{tail}`
   - `False -> False`: `[tripwire] still failing: "{cmd}"`
   - `False -> True`: `[tripwire] "{cmd}" passes again`
   `edits_since_green` becomes 0 when every wire passes, else increases by 1. Save. Append the notes, each preceded by `\n`, under the chaining rule.
7. `minimal.append_system_prompt(...)` with exactly: `Tripwires: after a tool call changes files, the user's invariant commands run. A [tripwire] note at the end of a tool result means your last call broke an invariant: fix it before continuing.`
8. Widget `tripwire`, area `bottom`, CSS `height: 1; padding: 0 1;`. All passing: `● {n}/{n} wires · 0 edits since green`. Otherwise: `✘ {passing}/{n} wires · {edits_since_green} edits since green · failing: {first failing cmd}`. Hidden without wires.

### Step 4: `.minimal/extensions/ledger.py` (Context Ledger and `/prune`)

Docstring: `Context ledger: what fills the context window, stale reads, and /prune.`

1. `STUB_PREFIX = "[pruned by the user"`. Module-level `_read_at: dict[str, float]`.
2. `tool_result` handler: for `read` without error, `_read_at[str(Path(Path.cwd(), input["path"]).resolve())] = time.time()`. Returns `None`.
3. `_entries(messages, cwd)`: walk messages with list content. Record every `tool_use` by id (message index, name, input) and every `tool_result`'s `is_error` by `tool_use_id`. Each `tool_result` whose text (`minimal.providers.base.result_text(block["content"])`) does not start with `STUB_PREFIX` yields an entry: `key = f"{message_index}:{block_index}"`, `chars`, `label`, `stale`.
   - `label`: `read {path}`, `bash {first line of command}`, `grep {pattern}`, else the tool name; `?` when the `tool_use` is not found.
   - `stale` (only for `read`): the resolved file no longer exists; or a later `tool_use` named `write` or `edit` resolves to the same path and its result is not an error; or the file's mtime is greater than `_read_at` for that path.
4. `_totals(messages)`: `results` = chars of all tool-result text, `inputs` = `len(json.dumps(input))` of all `tool_use` blocks, `prose` = user string content plus assistant text blocks. Tokens per char = `ctx.context_tokens / total` when both are non-zero, else `0.25`.
5. Widget `ledger`, area `sidebar`, always visible:
   - no messages: `CONTEXT empty`
   - else first line `CONTEXT {tokens / 1000:.1f}k` (prefix the number with `~` when `ctx.context_tokens` is `None`), second line `results {r}% · prose {p}% · inputs {i}%`, then the six largest entries as `{tokens / 1000:>4.1f}k {label}` with the label cut to 26 chars (ending `…` when cut) and ` ⚠` appended when stale.
6. Command `/prune` (description `drop chosen tool results from the context (continues in a new session)`): candidates are entries with `chars >= 500`, largest first, at most 20. None: `ValueError("nothing to prune")`. Call `ctx.frontend.choose_many("Prune from context (heaviest first)", [(key, f"{tokens / 1000:.1f}k  {label[:60]}" + ("  (stale)" if stale else ""), stale)])`. `None`: print `Cancelled.`; empty list: print `Nothing pruned.` Otherwise:
   - `new = copy.deepcopy(ctx.messages)`; in each ticked block set `content` to `f"{STUB_PREFIX} to save context: {label}, {chars} characters. Run the tool again if you need it.]"`; delete the `usage` key from every copied message (the old token count no longer describes the context).
   - `old = ctx.session; old.save(); ctx.session = open_session(new_id()); ctx.messages.extend(new); ctx.session.save()`.
   - print `[prune] {k} results pruned (~{tokens / 1000:.1f}k tokens); now in session {ctx.session.id}; the full history stays in session {old.id}`.

### Step 5: `.minimal/extensions/loose_ends.py` (Loose Ends)

Docstring: `Loose ends: a note tool and backlog for unverified, noticed and follow-up items.`

1. State file `.minimal/state/loose_ends.json`: `{"items": [{"id": int, "kind": str, "text": str, "path": str | null, "session": str, "at": ISO-8601 UTC, "done": bool}]}`. `id` = highest existing id + 1.
2. Tool `note`, `confirm=False`. Schema: `kind` (enum `unverified`, `noticed`, `followup`), `text` (string), `path` (string, optional); `kind` and `text` required. Description exactly: `Record a loose end in the user's backlog: something you could not verify (unverified), a problem you noticed but were not asked to fix (noticed), or work that remains (followup). One call per item; still mention it in your answer.` `run` appends the item with `session = ctx.session.id` and returns `Noted #{id}`.
3. `minimal.append_system_prompt(...)` with exactly: `Whenever you tell the user that you could not verify something, that you noticed an unrelated problem, or that work remains, also record it with the note tool.`
4. Widget `loose_ends`, area `sidebar`: `LOOSE ENDS ({open count})` then the eight newest open items as `{glyph} {text}` cut to 32 chars; glyphs `?` unverified, `!` noticed, `→` followup. Hidden when none are open.
5. Command `/ends` (description `review loose ends and close the finished ones`): no open items prints `No loose ends.` Otherwise `ctx.frontend.choose_many("Loose ends · tick the ones that are done", [(str(id), f"[{kind}] {text}" + (f" ({path})" if path else ""), False)])`; `None` prints `Cancelled.`; else mark ticked items `done`, save, print `Closed {n} loose end(s).`

### Step 6: `.minimal/extensions/crystallize.py` (Crystallize)

Docstring: `Crystallize: turn repeated commands into tools and denials into AGENTS.md rules.`

1. State file `.minimal/state/crystallize.json`: `{"sessions": [id], "tools": {name: command}, "rules": [key]}`.
2. `turn_end` handler: skip unless `event["messages"] is getattr(ctx, "messages", None)` (same guard as `extensions/compact.py`); add `ctx.session.id` to `sessions` if absent.
3. Command `/crystallize [all]` (description `turn repeated commands into tools and denials into AGENTS.md rules`). Session ids: with `all`, every `*.jsonl` stem in `minimal.session.SESSIONS_DIR`; otherwise the indexed `sessions`, and when that list is empty print `No sessions indexed for this project yet; scanning all sessions.` and use all. Read each file line by line with `json.loads`, skipping unreadable lines and missing files.
4. Habit candidates: every `tool_use` named `bash`; command normalised with `" ".join(command.split())`. Keep commands with at least 3 runs, at most 200 chars, first word not in `{ls, cd, pwd, cat, echo, head, tail, grep, rg, find, sed, wc}`, and not already a value of `tools`. Ten most frequent.
5. Correction candidates: every `tool_result` with `is_error` whose text starts with `Error: the user denied the execution`, joined to its `tool_use`. `detail` = `input["command"]` for `bash`, else `input.get("path")` or the JSON of the input (same rule as `App.confirm`). Key `f"{name}:{detail}"`, skipped if in `rules`. Rule text: `Do not run \`{detail}\` without asking first.` for `bash`, `Do not modify {detail} without asking first.` for `write`/`edit`, `Do not call {name} without asking first.` otherwise. Hint = first 60 chars of the next user message with string content in that session, if any. Ten most frequent.
6. No candidates: print `Nothing to crystallize yet.` Otherwise one `ctx.frontend.choose_many("Crystallize · tick what to keep", options)` with values `tool:{i}` / `rule:{i}`, labels `tool  {runs}x in {n} session(s)  {command[:70]}` and `rule  {text}  (denied {count}x)` plus `` · then: "{hint}"`` when a hint exists; nothing pre-ticked. `None` prints `Cancelled.`
7. For each ticked tool: `name = ctx.frontend.ask_text(f"Tool name for: {command[:60]}")`. Skip with `Skipped: {reason}` when the name is `None`, does not match `[a-z][a-z0-9_]*`, is already in `ctx.registry.tools`, or `<root>/.minimal/extensions/tools/{name}.py` exists. Otherwise write that file with this content (values substituted with `repr`), record it in `tools`, print `Tool written: .minimal/extensions/tools/{name}.py`:

```python
"""{name}: runs `{command}` (crystallized from {runs} runs)."""

__version__ = "1.0"

COMMAND = {command!r}


def register(minimal):
    bash = minimal.tools["bash"].run
    minimal.register_tool(
        name={name!r},
        run=lambda input, ctx: bash({"command": COMMAND}, ctx),
        description=f"Run `{COMMAND}` in the working directory and return its output. Runs without asking: the user approved this exact command.",
    )
```

8. For ticked rules: read `<root>/AGENTS.md` (empty if missing); if it lacks the line `## Learned rules`, append a blank line and that heading; append one `- {text}` line per rule at the end of the file; record the keys in `rules`; print `Added {n} rule(s) to AGENTS.md`.
9. If any tool file was written: `print(reload_summary(*ctx.reload_extensions()))` (`from minimal.app import reload_summary`, as `/ext` does).

### Step 7: `.minimal/extensions/baton.py` (Baton)

Docstring: `Baton: hand a task off as one file with summary, base commit and patch.`

1. Command `/baton` (description `hand off: /baton pass [note] | /baton take <path>`); anything else raises `ValueError("usage: /baton pass [note] | /baton take <path>")`. For `pass`, the note is the rest of the line verbatim.
2. `pass`:
   - `base = git rev-parse HEAD`; `None` raises `ValueError("not a git repository with a commit")`. No messages, or the last message is not from the assistant, raises `ValueError("nothing to hand off")`.
   - Summary: `ctx.provider.stream(model=ctx.model, system=build_system_prompt(ctx.registry, ctx.cwd), messages=[*ctx.messages, {"role": "user", "content": HANDOFF_REQUEST}], tools=tool_specs(ctx.registry), on_text=None)` (same call shape as `compact.py`; imports `minimal.app.build_system_prompt`, `minimal.agent.tool_specs`). Empty text or any `tool_use` block raises `ValueError("the model did not return a summary")`.
   - `HANDOFF_REQUEST` exactly: `Stop here and do not call any tools. Write a handoff note for another engineer who will continue this task with a different agent and none of this conversation: the goal and the user's instructions, decisions made and why, files changed (with paths), commands run and their relevant results, the current state, and what remains to be done, in order. Be specific and complete; plain text.`
   - Patch: `git diff --binary HEAD` + `GIT_SCOPE`, then for each path from `git ls-files --others --exclude-standard` + `GIT_SCOPE` that is at most 1,000,000 bytes, the stdout of `subprocess.run(["git", "diff", "--binary", "--no-index", "--", "/dev/null", path], ...)` (exit status 1 is the normal result there, so do not use `_git`). Larger untracked files are listed in the printed message as `not included (over 1 MB): ...`.
   - Write `<root>/.minimal/batons/{ctx.session.id}.md`:

```
# Baton {session id}

- from: {provider_name} · {model}
- base: {full sha}
- branch: {git rev-parse --abbrev-ref HEAD}
- created: {UTC ISO-8601, seconds}
- note: {note or "-"}

## Summary

{summary}

<!-- baton:patch:begin -->
{patch}
<!-- baton:patch:end -->
```

   - Print `Baton written: .minimal/batons/{id}.md (base {sha[:7]}, {n} file(s) in the patch). Send or commit this file; the receiver runs /baton take <path>.` where `n` counts `diff --git` lines.
3. `take <path>` (resolved against `ctx.cwd`):
   - Missing file: `ValueError(f"no such file: {path}")`. Parse `- from:` and `- base:` lines, the text between `## Summary` and the begin sentinel (stripped), and the lines strictly between the two sentinel lines (exact line matches). Any missing part: `ValueError("not a baton file")`.
   - `git rev-parse HEAD` must equal the base, else `ValueError(f"HEAD is {head[:7]} but the baton was made on {base[:7]}; check out {base} first")`.
   - Non-empty patch: write it to a temporary file; `git apply --check <file>` failing raises `ValueError(f"the patch does not apply: {first line of stderr}")` (run with `subprocess.run` to read stderr); then `git apply <file>`; delete the temporary file.
   - If `ctx.messages` is non-empty: `ctx.session.save(); ctx.session = open_session(new_id())`. Extend `ctx.messages` with `{"role": "user", "content": f"Handoff from another engineer's agent session ({from}). The working tree already contains their changes.\n\n{summary}"}` and `{"role": "assistant", "content": [{"type": "text", "text": "Understood. I will continue from this handoff."}]}`; `ctx.session.save()`.
   - Print `Baton taken: {n} file(s) patched, session {ctx.session.id} starts from the summary.`

### Step 8: `.minimal/extensions/rerun.py` (Rerun)

Docstring: `Rerun: replay a past session's prompts on another provider in a throwaway worktree.`

1. State file `.minimal/state/rerun.json`: `{"sessions": {id: {"commit": sha | null, "provider": str, "model": str}}}`. `turn_end` handler (same own-loop guard as Step 6) records the entry the first time a session id is seen, with `commit = git rev-parse HEAD` stripped.
2. Command `/rerun` (description `replay a session on another provider: /rerun <session-id> [provider [model]]`):
   - no args: print one line per recorded session, `{id}  {commit[:7] or "-"}  {provider} · {model}  {minimal.session.preview(id)}`, or `No sessions recorded yet. Usage: /rerun <session-id> [provider [model]]`.
   - `id` not in `minimal.session.list_sessions()`: `ValueError(f"no session {id}")`.
3. Prompts: `open_session(id).messages` entries with role `user`, string content, not starting with `Summary of the earlier conversation (compacted):` or `Handoff from another engineer's agent session`. None: `ValueError("session has no user prompts")`.
4. Provider = second argument or `ctx.provider_name`; model = third argument, else `ctx.model` when the provider is the current one, else `None` (the provider's default). Commit = recorded commit; when absent use `git rev-parse HEAD` and print `no recorded commit for {id}; using HEAD`. `None` raises `ValueError("not a git repository with a commit")`.
5. Confirm with `ctx.frontend.ask(f"Replay {n} prompt(s) from session {id} on {provider} in a temporary worktree? Tools run without asking and are not sandboxed.")`; `False` prints `Cancelled.`
6. `tmp = tempfile.mkdtemp(prefix="minimal-rerun-")`; `git worktree add --detach {tmp}/wt {commit}`; failure raises `ValueError("could not create the worktree")`. Print `replaying from commit {commit[:7]} (uncommitted state at the time is not reproduced)`.
7. Build `replay = App(registry=Registry(), session=Session("rerun", Path(tmp, "rerun.jsonl")), cwd=str(Path(tmp, "wt")), auto_approve=True, extra_extensions=[str(Path(ctx.cwd, e).resolve()) for e in ctx.extra_extensions], frontend=_QuietFrontend())`; `replay.reload_extensions()`, printing `[rerun] extension not loaded: {e}` for each error; `replay.use_provider(provider, model)`. `_QuietFrontend` subclasses `minimal.app.ConsoleFrontend`: `text`, `tool_call`, `tool_result` do nothing, `turn_end(error)` stores `self.error = error`, `ask` returns `True`. The session is built directly so nothing is written to `~/.minimal/sessions`.
8. For each prompt: print `[rerun] {i}/{n}`, call `replay.run_turn(prompt)`, stop at the first `frontend.error` and print `[rerun] stopped: {error}`. The command cannot be interrupted with Esc (the TUI only interrupts model turns); this is accepted.
9. `_metrics(messages)`: `requests` = assistant messages; `calls` = `Counter` of `tool_use` names; `errors` = `tool_result` blocks with `is_error`; `output` = sum of `usage["output_tokens"]` or `None` when no message has usage; `context` = input + output of the last usage or `None`.
10. In the worktree run `git add -A` then `git diff --binary --cached {commit}`; save it to `<root>/.minimal/reruns/{id}-{provider}.patch`; `diffstat` = stripped `git diff --shortstat --cached {commit}` or `no changes`.
11. Print a table with a 16-char label column and 30-char value columns, header `original` / `replay`, rows: `provider` (`{provider} · {model}` from the state file or `not recorded`), `model requests`, `tool calls` (total), `tools` (`Counter.most_common()` as `bash 6, read 4`), `tool errors`, `output tokens`, `final context` (tokens as `{n / 1000:.1f}k`, `n/a` for `None`), `diff` (`n/a` for the original, `diffstat` for the replay). Then `replay patch: .minimal/reruns/{id}-{provider}.patch`.
12. `finally`: `git worktree remove --force {tmp}/wt` (run in `ctx.cwd`) and `shutil.rmtree(tmp, ignore_errors=True)`.

### Step 9: `.minimal/extensions/xray.py` (X-ray)

Docstring: `X-ray: capability summary of extension files before the model reloads them.`

1. State file `.minimal/state/xray.json`: `{"approved": {absolute path: sha256 hex}}`. Files considered: `extension_files(GLOBAL_EXTENSIONS_DIR) + extension_files(project_extensions_dir(str(root)))` from `minimal.extensions`.
2. `scan(source) -> set[str]` with `ast` (`SyntaxError` returns `{"unparseable"}`), walking every node:
   - `Import`/`ImportFrom` root module: `subprocess`, `pty`, `multiprocessing` give `process`; `socket`, `urllib`, `http`, `httpx`, `requests`, `aiohttp`, `ftplib`, `smtplib`, `websockets` give `network`; `importlib`, `ctypes`, `pickle`, `marshal` give `dynamic`. A full module name `minimal.config` or `minimal.providers.claude_auth` (or `from minimal import config`) gives `secrets`.
   - Call on an attribute: `register_tool`/`add_tool` give `tool`; `register_command` `command`; `register_provider`/`add_provider` `provider`; `register_widget` `widget`; `register_theme` `theme`; `register_logo` `logo`; `append_system_prompt` `prompt`; `on` gives `hook:{event}` when the first argument is a string constant, else `hook:?`. For `register_tool`, a constant `name=` keyword or first positional argument that is in `BUILTIN` gives `replaces:{name}`.
   - Subscript of an attribute named `tools` with a constant key in `BUILTIN` gives `wraps:{key}`.
   - `os.system`, `os.popen`, and `os.exec*`/`os.spawn*` calls give `process`; calls to the names `eval`, `exec`, `compile`, `__import__` give `dynamic`; any attribute named `environ` or a call to `os.getenv` gives `env`; a string constant containing `keys.json` or `claude-auth.json` gives `secrets`.
   - `BUILTIN = {"read", "write", "edit", "bash", "grep", "glob", "reload_extensions", "ext_search", "ext_install", "ext_publish"}`; `BENIGN = {"tool", "command", "widget", "theme", "logo", "prompt", "hook:turn_end"}`.
3. `register()`: if the state file does not exist, approve every current file (trust on first use) and save.
4. Replace the `reload_extensions` tool: `original = minimal.tools["reload_extensions"]`; add `dataclasses.replace(original, run=guarded, confirm=False)`. `guarded(input, ctx)`:
   - `pending` = files whose SHA differs from the approved one (or are new) and whose `scan` result minus `BENIGN` is non-empty, with that risky set.
   - If `pending`: print `[xray] new or changed extensions with capabilities beyond the benign set:` then `  {path}: {", ".join(sorted(risky))}` per file (path relative to the root when inside it, else with the home directory as `~`); `ok = ctx.frontend.ask(f"Allow reload_extensions with the {len(pending)} unreviewed extension(s) listed above?")`. This question is asked even when `ctx.auto_approve` is set.
   - Else `ok = True if ctx.auto_approve else ctx.frontend.ask("Allow reload_extensions?")`.
   - Not `ok`: `raise RuntimeError("the user denied the execution")`. Otherwise approve every current file, save, and `return original.run(input, ctx)`.
   - Only the model's tool is guarded. `/reload`, startup loading and `ext_install` (which copies a file without loading it) are not.
5. Command `/xray` (description `extension capabilities: /xray [path] | /xray approve`): no args prints one line per file, `{ok | new | changed}  {path}  {", ".join(sorted(caps)) or "-"}`; `approve` approves every current file and prints `Approved {n} extension file(s).`; any other argument is a path resolved against `ctx.cwd`, printed as `{path}: {caps}`; a missing file raises `ValueError(f"no such file: {path}")`.

### Step 10: `.minimal/extensions/themes/ambient.py` (Ambient Theme)

Docstring: `Ambient theme: recolour the UI while a risky state is active.`

1. `SEP = "__"`. Variant theme names are `{base}{SEP}{state}`.
2. `register()` first repairs a saved variant: `saved = config.load_theme()`; if `saved` contains `SEP`, `config.save_theme(saved.split(SEP, 1)[0])` (`from minimal import config`). This runs before `run_tui` reads the saved theme, and is needed because `run_tui` saves whatever theme is active at exit.
3. States in priority order, each with a banner text:
   - `risk`: `ctx.auto_approve` is true. `⚠ auto-approve is on: tools run without asking`
   - `main`: branch from `minimal.tui.git_status(ctx.cwd)` (import inside `refresh_state`) is `main` or `master`. `⚠ on branch {branch}`
   - `full`: `ctx.context_tokens` is at least `CONTEXT_WARN = 80_000`. `⚠ context at {tokens / 1000:.0f}k tokens`
   - `guest`: `config.load_config()` (treat `ValueError` and `None` as inactive) names a provider different from `ctx.provider_name`. `● answering: {provider_name} (default is {default})`
4. Colours by the base theme's `dark` flag: `ERR` `#e06c75` / `#cf222e`, `WARN` `#e5c07b` / `#9a6700`, `INFO` `#56b6c2` / `#0969da` (dark / light). Overrides: `risk` sets `border`, `focus`, `model` to `ERR`; `main` sets `border`, `focus` to `WARN`; `full` sets `model`, `path` to `WARN`; `guest` sets `model` to `INFO`.
5. Base spec: `ctx.registry.themes.get(base)`; for the built-in theme use `ThemeSpec("minimal", True, BUILTIN_COLORS)` where `BUILTIN_COLORS` is the colour dict of the `MINIMAL` spec copied verbatim from `src/minimal/theme.py` (the built-in is not exposed as a `ThemeSpec`).
6. Widget `ambient`, area `top`, class `AmbientBanner(Static)` with CSS `height: 1; padding: 0 1; color: $warning;`, instance attribute `_fresh = True`. `refresh_state(ctx)`:
   - `current = self.app.theme`. If it contains `SEP`, `base` is the part before it. Else if `self._fresh` and `current == "minimal"` and `getattr(self.app, "ambient_base", "minimal")` is in `ctx.registry.themes`, `base` is that attribute (a reload resets an unknown variant to `minimal`; this restores the user's theme). Else `base = current`. Then `self._fresh = False` and `self.app.ambient_base = base`.
   - `active` = states whose condition holds, in priority order. Banner = their texts joined by ` · `; `self.display = bool(active)`.
   - With an active state: build `ThemeSpec(f"{base}{SEP}{active[0]}", spec.dark, {**spec.colors, **overrides})`, `self.app.register_theme(minimal.theme.build(variant))`, and set `self.app.theme` to that name if different. Without one: set `self.app.theme = base` if different.
7. `on_unmount`: inside `try/except Exception: pass`, if `self.app.theme` contains `SEP`, set it to the part before `SEP` (so exit saves the base theme).
8. Variants are registered on the Textual app only, never in `minimal.themes`, so `/theme` does not list one entry per theme and state.

## Critical files and anchors

- `src/minimal/agent.py`, `execute_tool_call` and `_run_tool`: handler order, veto before confirm, the last-rewrite-wins loop that the chaining rule works around, and the agent's own `clip` binding.
- `src/minimal/tools.py`, `bash`, `clip`, `_glob_regex`, `_filter_glob`: why Step 1 swaps `clip`, and the glob semantics Step 2 reuses.
- `src/minimal/tui.py`, `sync_themes`, `extensions_reloaded`, `remount_widgets`, `run_tui`: why Step 10 keeps `ambient_base` on the app, restores on a fresh widget, and repairs the saved theme.
- `extensions/compact.py`, `compact`: the model-summary call and the new-session switch reused by Steps 4 and 7.
- `src/minimal/extensions.py`, `extension_files`, `_load_extension`: load order and per-reload re-execution.

## Verification

No permanent tests. A throwaway harness outside the repository proves each step on a fresh scratch repository.

### Harness

`/tmp/minimal-verify/verify.py`, run as `cd <repo> && .venv/bin/python /tmp/minimal-verify/verify.py <name>`, where `<name>` is the extension file stem (`loose_ends`, `tripwire`, `scope_fence`, `lenses`, `ledger`, `crystallize`, `baton`, `rerun`, `xray`, `ambient`). On every run it:

1. Records `REPO = Path.cwd()`, creates `scratch = tempfile.mkdtemp()` and `home = tempfile.mkdtemp()`, and sets `os.environ["HOME"] = home` before importing anything from `minimal` (`SESSIONS_DIR`, `GLOBAL_EXTENSIONS_DIR` and the config paths are computed from `Path.home()` at import time).
2. In `scratch`: `git init -q -b work`, writes `src/a.py` with `x = 1\n`, commits it (`git -c user.name=t -c user.email=t@t commit -qm init`), copies `REPO/.minimal/extensions` to `scratch/.minimal/extensions` (so only the extensions written so far are present), writes `.minimal/extensions/themes/ocean.py` registering `register_theme("ocean", dark=True, background="#0b1622", foreground="#d0e4f5", accent="#4fc3f7")`, and `os.chdir(scratch)`.
3. Builds `app = App(registry=Registry(), session=Session("t", Path(home, "t.jsonl")), cwd=scratch, auto_approve=True, extra_extensions=["/tmp/minimal-verify/fakeprov.py"])`, asserts `app.reload_extensions()[1] == []`, `app.use_provider("fake")`; sets `app.frontend.choose_many = lambda t, o: [x[0] for x in o]`, `app.frontend.ask_text = lambda t: "make_test"`, `app.frontend.ask = lambda q: True`.
4. Helpers: `tool(name, **input)` returns `execute_tool_call({"id": "1", "name": name, "input": input}, app.registry, app)["content"]`; `cmd(line)` runs `commands.run_command(line, app)` with stdout and stderr captured and returns the text; `git(*args)` returns stdout.
5. Calls `CHECKS[name]()`, prints `PASS <name>` on success (an `AssertionError` shows the failing value), and removes `scratch` and `home` in `finally`.

`/tmp/minimal-verify/fakeprov.py` is an extension registering provider `fake` (`default_model="m"`) whose `stream` returns text `SUMMARY` when the last message is a string starting with `Stop here`, a `tool_use` `write` of `hello.txt` with content `hi` (id `w1`, `stop_reason="tool_use"`) when the last message is any other string, and text `done` otherwise.

### Checks per extension

Each `check_<name>` first asserts that the step's command, tool or widget names are registered, then runs its rows in order (each an `assert`).

| Extension | Input | Expected |
| --- | --- | --- |
| loose_ends | registry | tool `note`, command `ends`, widget `loose_ends` |
| loose_ends | `tool("note", kind="unverified", text="did not run the tests")`, `cmd("/ends")`, `cmd("/ends")` | `Noted #1`; `Closed 1 loose end(s).`; `No loose ends.` |
| tripwire | registry | command `tripwire`, widget `tripwire` |
| tripwire | `cmd("/tripwire add test ! -f broken")` | contains `passing now` |
| tripwire | `tool("bash", command="touch broken")`, `tool("bash", command="rm broken")`, `tool("bash", command="true")` | `was passing before this call and fails now`; `passes again`; no `[tripwire]` |
| scope_fence | registry | command `fence`, widget `fence` |
| scope_fence | `cmd("/fence src --lines 5")` | `Fence: src/** · budget 5 lines` |
| scope_fence | `tool("write", path="other.txt", content="x")` | starts with `Error: other.txt is outside the fence`; file absent |
| scope_fence | `tool("edit", path="src/a.py", old_string="x = 1", new_string="x = 2")` | starts with `Edited` |
| scope_fence | `tool("bash", command="touch stray.txt")` | contains `[fence] this command changed files outside the fence: stray.txt` |
| scope_fence | `tool("write", path="src/a.py", content="1\n2\n3\n4\n5\n6\n")` then `tool("edit", path="src/a.py", old_string="6", new_string="7")` | first starts with `Wrote`; second starts with `Error: diff budget used up (`; `cmd("/fence off")` gives `Fence removed.` |
| lenses | registry | command `lenses` |
| lenses | `tool("bash", command="for i in $(seq 600); do echo same line; done")` | contains `[previous line repeated 599 more times]`, `[exit 0]`, `[full output: .minimal/outputs/0001.txt`; that file has 600 `same line` |
| lenses | `tool("bash", command="seq 1 30000")` | under 20,200 chars; contains `[lens: `, `30000`, `[exit 0]` |
| lenses | write `.minimal/lenses/five.py` (`PATTERN = "seq 1 5000"`, `lens` returns `"LENSED"`), `app.reload_extensions()`, `tool("bash", command="seq 1 5000")` | starts with `LENSED`, contains `[exit 0]` |
| ledger | registry | command `prune`, widget `ledger` |
| ledger | extend `app.messages` with a user prompt, an assistant `tool_use` `read` of `src/a.py` (id `r1`), a user message with a `tool_result` for `r1` of 5,000 chars, an assistant text; `cmd("/prune")` | contains `[prune] 1 results pruned`; `app.session.id != "t"`; that result's content starts with `[pruned by the user` |
| crystallize | registry | command `crystallize` |
| crystallize | write `$HOME/.minimal/sessions/s1.jsonl` with three assistant `tool_use` `bash` calls of `make test`; `cmd("/crystallize all")` | `.minimal/extensions/tools/make_test.py` exists; `make_test` in `app.registry.tools` |
| baton | registry | command `baton` |
| baton | `app.run_turn("go")`, `tool("edit", path="src/a.py", old_string="x = 1", new_string="x = 2")`, `tool("bash", command="touch stray.txt")`, `cmd("/baton pass try it")` | `.minimal/batons/t.md` contains `SUMMARY`, `- base: `, `diff --git a/src/a.py`, `stray.txt`, `hello.txt` |
| baton | `git checkout -- . && git clean -fdq -e .minimal`, then `cmd("/baton take .minimal/batons/t.md")` | `stray.txt` exists; `git diff --stat` lists `src/a.py`; `app.messages[0]["content"]` contains `SUMMARY` |
| rerun | registry | command `rerun` |
| rerun | write `$HOME/.minimal/sessions/s2.jsonl` (user `make hello`, assistant text); `cmd("/rerun s2 fake")` | contains `no recorded commit for s2; using HEAD` and a `tool calls` row; `.minimal/reruns/s2-fake.patch` contains `hello.txt`; `hello.txt` absent from the scratch root; `git worktree list` has one line |
| xray | registry | command `xray`; `app.registry.tools["reload_extensions"].confirm is False` |
| xray | `cmd("/xray")` | every line starts with `ok` |
| xray | write `.minimal/extensions/evil.py` (`import socket`; `register` calls `minimal.on("tool_call", lambda e: None)`); `app.frontend.ask = lambda q: False`; `tool("reload_extensions")` | `Error: the user denied the execution`; `cmd("/xray")` has a `new` line for `evil.py` with `hook:tool_call, network` |
| xray | `app.frontend.ask = lambda q: True`, `tool("reload_extensions")`, `cmd("/xray")` | first output names `evil.py`; its `/xray` line now starts with `ok` |
| ambient | registry | widget `ambient` |
| ambient | `tui = MinimalTui(app, "ocean")`; `async with tui.run_test() as pilot`, `await pilot.pause(0.2)` (run with `asyncio.run`) | `tui.theme == "ocean__risk"` |
| ambient | same block: `app.reload_extensions()`, `await pilot.pause(0.3)` | still `ocean__risk` |
| ambient | same block: `app.auto_approve = False`, `tui.refresh_widgets()`, `await pilot.pause(0.2)` | `tui.theme == "ocean"` |
| ambient | after the block: write `{"theme": "ocean__risk"}` to `$HOME/.minimal/theme.json`, `app.reload_extensions()` | `config.load_theme() == "ocean"` |

### Try it yourself (real UI, from the repo root)

These run against the user's real `~/.minimal` and repository; state files appear under `.minimal/state/`.

| Extension | Do | See |
| --- | --- | --- |
| loose_ends | `uv run minimal`, ask: "use the note tool to record that you did not run the tests" | sidebar `LOOSE ENDS (1)`; `/ends` closes it |
| tripwire | `/tripwire add uv run ruff check` | bottom line `● 1/1 wires · 0 edits since green`; `/tripwire rm 1` removes it |
| scope_fence | `/fence src/minimal/tui.py`, ask the model to create `notes.txt` | the write is blocked with the fence reason; sidebar `FENCE`; `/fence off` |
| lenses | ask the model to run `uv run python -m unittest discover -s tests` | the result ends with `[full output: .minimal/outputs/...]`; `/lenses` lists `python-tests` |
| ledger | ask the model to read `src/minimal/agent.py` | sidebar `CONTEXT` with a `read src/minimal/agent.py` row; `/prune` offers it |
| crystallize | `/crystallize all` | a checklist of repeated commands and denials; Esc cancels without changes |
| baton | `/baton pass trying baton` after a short conversation | `.minimal/batons/<session>.md` with summary, base and patch; delete it afterwards (do not run `/baton take` in this repo) |
| rerun | `/rerun` | the sessions recorded since Rerun was installed; replaying costs tokens on the chosen provider |
| xray | `/xray` | one `ok` line per installed extension with its capabilities |
| ambient | `uv run minimal -y`, then quit | top banner `⚠ auto-approve is on: tools run without asking`; afterwards `~/.minimal/theme.json` contains no `__` |

## Assumptions and contingencies

- Extensions stay project-local and untested by the suite. To ship them later they would move to `extensions/` with cases in `tests/test_bundled_extensions.py`; that is a separate change.
- `.minimal/` is not ignored by git and stays that way; the extensions and their state appear as untracked files in this repo.
- Crystallized tools run their fixed command without a confirmation dialog, because the user ticked that exact command. If confirmation is wanted, add `confirm=True` to the template in Step 6.
- X-ray trusts every extension present at its first load and guards only model-initiated reloads.
- Step 10 assumes Textual accepts `App.register_theme` for an already registered name and a theme change inside a widget's `on_unmount`. If re-registering raises, register only when the name is not in `self.app.available_themes`. If the `on_unmount` restore fails or has no effect, keep only the startup repair in `register()`; the saved theme is still corrected on the next start.
- Step 1 assumes no other code calls `minimal.tools.clip` while a `bash` command runs (tools run one at a time). If a later core change runs tools concurrently, replace the swap with a copy of `tools.bash` that returns the unclipped text.
- Step 8 on a machine without credentials for the chosen provider fails in `use_provider` with the core's own message; the worktree is still removed by the `finally` block.
- The plan copy lives at `.minimal/extensions-plan.md`. If the user wants another path, move it there and keep ticking `## Progress` in the moved file.
- If the user asks to do several extensions in one go, the per-extension loop still applies to each one, but the stop at the end of the loop happens only after the last of them.
