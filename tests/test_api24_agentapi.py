"""API 2.4: ``run_agent(cwd=, reasoning_effort=)``, usage by model, ``session_info``, keyed ``progress``, the
``notify`` event and a callable ``read_only``."""

import json
import os
import threading
import time
import unittest
from unittest import mock

from minimal.core import session as session_mod
from minimal.core.agents import ProgressSlots
from minimal.providers.base import Reply, Usage
from tests.test_agents import AgentTest, ScriptedProvider, call, text

WAIT = 5  # seconds a test waits for something that must happen


class ByPromptProvider:
    """Answers every agent alike, whatever the thread: first request calls ``script``'s tool, the next one ends."""

    name = "fake"

    def __init__(self, tool="where", answer="done", usage=None):
        self.tool, self.answer, self.usage = tool, answer, usage
        self.requests = []
        self.lock = threading.Lock()

    def stream(self, **kw):
        with self.lock:
            self.requests.append({**kw, "messages": list(kw["messages"])})
        last = kw["messages"][-1]
        if last["role"] == "user" and isinstance(last["content"], str):
            return call(self.tool, id="c-" + kw["model"], usage=self.usage)
        return text(self.answer, usage=self.usage)


class RunAgentCwdTest(AgentTest):
    def setUp(self):
        super().setUp()
        self.sub = self.tmp / "sub"
        self.sub.mkdir()
        (self.sub / "a.txt").write_text("inside\n")
        (self.tmp / "other.txt").write_text("outside\n")
        self.seen = []

        def where(input, ctx):
            self.seen.append(ctx.cwd)
            return ctx.cwd

        self.registry.register_tool("where", where)

    def test_the_agents_ctx_cwd_follows_and_the_parent_keeps_its_own(self):
        self.app.provider = ByPromptProvider()
        result = self.ctx.run_agent_result("go", cwd="sub")
        self.assertEqual(result.status, "done")
        self.assertEqual(self.seen, [str(self.sub)])
        self.assertEqual(self.ctx.cwd, str(self.tmp))
        self.assertEqual(self.app.cwd, str(self.tmp))

    def test_the_system_prompt_names_the_agents_directory(self):
        provider = self.app.provider = ByPromptProvider()
        self.ctx.run_agent_result("go", cwd=str(self.sub))
        self.assertIn(f"Working directory: {self.sub}", provider.requests[0]["system"])
        self.assertNotIn(f"Working directory: {self.tmp}\n", provider.requests[0]["system"])
        self.ctx.run_agent_result("again")
        self.assertIn(f"Working directory: {self.tmp}\n", provider.requests[-1]["system"])

    def test_project_instructions_come_from_the_agents_directory(self):
        (self.sub / "AGENTS.md").write_text("sub rules")
        provider = self.app.provider = ByPromptProvider()
        self.ctx.run_agent_result("go", cwd="sub")
        self.assertIn("sub rules", provider.requests[0]["system"])

    def test_bash_runs_in_the_agents_directory(self):
        self.app.provider = ByPromptProvider(tool="bash")
        provider = self.app.provider
        provider.stream = lambda **kw: (  # type: ignore[method-assign]
            call("bash", {"command": "pwd"}, id="b1")
            if kw["messages"][-1]["role"] == "user" and isinstance(kw["messages"][-1]["content"], str)
            else text("ok")
        )
        result = self.ctx.run_agent_result("go", cwd="sub")
        output = result.messages[2]["content"][0]["content"]
        self.assertEqual(output.splitlines()[0], str(self.sub.resolve()))

    def test_agents_inherit_the_directory_and_can_go_deeper(self):
        deeper = self.sub / "deeper"
        deeper.mkdir()
        results = []

        def spawn(input, ctx):
            results.append(ctx.cwd)
            ctx.run_agent_result("child", **input)
            return "ok"

        self.registry.register_tool("spawn", spawn)
        replies = iter(
            [
                call("spawn", {}, id="s1"),  # agent in sub: the grandchild inherits sub
                call("where", id="w1"),  # grandchild
                text("gc"),
                text("child"),
                call("spawn", {"cwd": "deeper"}, id="s2"),  # same, but the grandchild goes to sub/deeper
                call("where", id="w2"),
                text("gc"),
                text("child"),
            ]
        )
        self.app.provider = mock.Mock(name="fake", stream=lambda **kw: next(replies))
        self.app.provider.name = "fake"
        self.ctx.run_agent_result("go", cwd="sub", tools=["spawn", "where"])
        self.ctx.run_agent_result("go", cwd="sub", tools=["spawn", "where"])
        self.assertEqual(self.seen, [str(self.sub), str(deeper)])

    def test_confirmation_for_paths_outside_the_agents_directory(self):
        self.app.auto_approve = False
        self.frontend.permission.return_value = "once"
        replies = iter(
            [
                call("read", {"path": str(self.sub / "a.txt")}, id="r1"),
                text("done"),
                call("read", {"path": str(self.tmp / "other.txt")}, id="r2"),
                text("done"),
            ]
        )
        self.app.provider = mock.Mock(stream=lambda **kw: next(replies))
        self.app.provider.name = "fake"
        self.ctx.run_agent_result("inside", cwd="sub")
        self.assertEqual(self.frontend.permission.call_count, 0)
        self.ctx.run_agent_result("outside", cwd="sub")
        self.assertEqual(self.frontend.permission.call_count, 1)

    def test_the_parent_directory_still_reads_it_without_asking(self):
        self.app.auto_approve = False
        replies = iter([call("read", {"path": str(self.tmp / "other.txt")}, id="r1"), text("done")])
        self.app.provider = mock.Mock(stream=lambda **kw: next(replies))
        self.app.provider.name = "fake"
        self.ctx.run_agent_result("go")
        self.assertEqual(self.frontend.permission.call_count, 0)

    def test_parallel_agents_in_different_folders_do_not_mix(self):
        folders = [self.tmp / name for name in ("one", "two", "three")]
        for folder in folders:
            folder.mkdir()
        barrier = threading.Barrier(len(folders), timeout=WAIT)
        found = {}

        def meet(input, ctx):
            barrier.wait()  # every agent is inside its tool at once
            time.sleep(0.05)
            found[ctx.agent_id] = ctx.cwd
            return ctx.cwd

        self.registry.register_tool("where", meet)
        self.app.provider = ByPromptProvider()
        results = {}

        def run(folder):
            results[folder.name] = self.ctx.run_agent_result("go", cwd=str(folder), model=folder.name)

        threads = [threading.Thread(target=run, args=(f,)) for f in folders]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT)
        self.assertEqual(
            {results[f.name].agent_id: str(f) for f in folders}, found
        )
        self.assertEqual(self.ctx.cwd, str(self.tmp))

    def test_bad_cwd_is_a_value_error(self):
        self.app.provider = ByPromptProvider()
        with self.assertRaisesRegex(ValueError, "not a folder"):
            self.ctx.run_agent_result("go", cwd="missing")
        with self.assertRaisesRegex(ValueError, "not a folder"):
            self.ctx.run_agent_result("go", cwd=str(self.sub / "a.txt"))
        with self.assertRaisesRegex(ValueError, "cwd"):
            self.ctx.run_agent("go", cwd="")
        self.assertEqual(self.ctx.agents(), [])

    def test_mentions_are_resolved_against_the_agents_directory(self):
        provider = self.app.provider = ScriptedProvider([text("a"), text("b")])
        self.ctx.run_agent_result("read @a.txt", cwd="sub", mentions=True)
        first = provider.requests[0]["messages"][0]["content"]
        self.assertIn('<file path="a.txt">', first)
        self.assertIn("inside", first)
        self.ctx.run_agent_result("read @a.txt", cwd="sub")
        self.assertEqual(provider.requests[1]["messages"][0]["content"], "read @a.txt")

    def test_a_mention_outside_the_agents_directory_is_not_attached(self):
        provider = self.app.provider = ScriptedProvider([text("a")])
        self.ctx.run_agent_result("read @../other.txt", cwd="sub", mentions=True)
        self.assertNotIn("<file", provider.requests[0]["messages"][0]["content"])


class RunAgentEffortTest(AgentTest):
    def test_effort_applies_to_that_agent_only(self):
        provider = self.script(text("a"), text("b"), text("c"))
        self.ctx.run_agent_result("one", reasoning_effort="high")
        self.ctx.run_agent_result("two")
        self.app.set_reasoning_effort("low")
        self.ctx.run_agent_result("three", reasoning_effort="medium")
        self.assertEqual(provider.requests[0]["options"]["reasoning_effort"], "high")
        self.assertNotIn("options", provider.requests[1])
        self.assertEqual(provider.requests[2]["options"]["reasoning_effort"], "medium")
        self.assertEqual(self.ctx.reasoning_effort, "low")

    def test_the_agents_ctx_sees_it_and_so_do_its_children(self):
        seen = []

        def spawn(input, ctx):
            seen.append(ctx.reasoning_effort)
            if input.get("child"):
                return "leaf"
            return ctx.run_agent("child", tools=["spawn"])

        self.registry.register_tool("spawn", spawn)
        provider = self.script(
            call("spawn", {}, id="s1"), call("spawn", {"child": 1}, id="s2"), text("leaf"), text("mid")
        )
        self.ctx.run_agent("go", reasoning_effort="high")
        self.assertEqual(seen, ["high", "high"])
        self.assertEqual([r.get("options", {}).get("reasoning_effort") for r in provider.requests], ["high"] * 4)

    def test_invalid_effort_is_rejected_before_anything_starts(self):
        self.script(text("never"))
        with self.assertRaisesRegex(ValueError, "reasoning_effort"):
            self.ctx.run_agent_result("go", reasoning_effort="extreme")
        self.assertEqual(self.ctx.agents(), [])

    def test_complete_takes_effort_options_and_messages(self):
        provider = self.script(text("one"), text("two"), text("three"))
        history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
        self.assertEqual(self.ctx.complete_text("summarise", messages=history, system="be brief", reasoning_effort="low"), "one")
        sent = provider.requests[0]
        self.assertEqual([m["role"] for m in sent["messages"]], ["user", "assistant", "user"])
        self.assertEqual(sent["messages"][-1]["content"], "summarise")
        self.assertEqual(sent["system"], "be brief")
        self.assertEqual(sent["options"], {"max_tokens": 1024, "reasoning_effort": "low"})
        self.ctx.complete_text("x", options={"max_tokens": 50, "temperature": 0.2})
        self.assertEqual(provider.requests[1]["options"], {"max_tokens": 50, "temperature": 0.2})
        # the keyword beats options; an empty prompt sends the messages as they are
        self.ctx.complete_text("", messages=[{"role": "user", "content": "q"}], options={"reasoning_effort": "low"}, reasoning_effort="high")
        self.assertEqual(provider.requests[2]["options"]["reasoning_effort"], "high")
        self.assertEqual(len(provider.requests[2]["messages"]), 1)

    def test_complete_validates(self):
        self.script(text("never"))
        with self.assertRaisesRegex(ValueError, "reasoning_effort"):
            self.ctx.complete([{"role": "user", "content": "x"}], reasoning_effort="max")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.ctx.complete_text("  ")
        with self.assertRaisesRegex(ValueError, "empty"):
            self.ctx.complete_text("", messages=[])


class UsageByModelTest(AgentTest):
    def test_per_model_counts_sum_to_the_totals_with_an_agent_model_override(self):
        self.registry.register_tool("noop", lambda input, ctx: "ok")
        self.script(
            call("noop", usage=Usage(10, 1, 2, 3)),
            text("main", usage=Usage(20, 2)),
            text("agent", usage=Usage(100, 5)),
            text("complete", usage=Usage(7, 7)),
            text("complete2", usage=None),
        )
        self.app.run_turn("go")
        self.ctx.run_agent_result("sub", model="big")
        self.ctx.complete([{"role": "user", "content": "q"}], model="big")
        self.ctx.complete([{"role": "user", "content": "q"}])
        usage = self.ctx.usage
        self.assertEqual(set(usage.by_model), {"fake/m", "fake/big"})
        main, big = usage.by_model["fake/m"], usage.by_model["fake/big"]
        self.assertEqual((main.input_tokens, main.output_tokens, main.requests), (30, 3, 3))
        self.assertEqual((main.cache_read_tokens, main.cache_write_tokens), (2, 3))
        self.assertEqual((big.input_tokens, big.output_tokens, big.requests), (107, 12, 2))
        self.assertEqual(
            (usage.input_tokens, usage.output_tokens, usage.requests), (137, 15, 5)
        )
        self.assertEqual(sum(t.input_tokens for t in usage.by_model.values()), usage.input_tokens)
        self.assertEqual(main.by_model, {})

    def test_a_snapshot_does_not_change_later(self):
        self.script(text("a", usage=Usage(1, 1)), text("b", usage=Usage(1, 1)))
        self.ctx.complete([{"role": "user", "content": "q"}])
        before = self.ctx.usage
        self.ctx.complete([{"role": "user", "content": "q"}])
        self.assertEqual(before.by_model["fake/m"].requests, 1)
        self.assertEqual(self.ctx.usage.by_model["fake/m"].requests, 2)
        with self.assertRaises(TypeError):
            before.by_model["x"] = before  # type: ignore[index]

    def test_a_provider_switch_by_provider_error_is_counted_where_it_answered(self):
        class Other:
            name = "other"

            def stream(self, **kw):
                return text("ok", usage=Usage(4, 4))

        self.app.provider = ScriptedProvider([RuntimeError("down")])
        self.registry.on("provider_error", lambda e: {"provider": "other", "model": "o1"})
        with mock.patch.object(self.app, "create_provider", return_value=(Other(), "o1")):
            self.ctx.complete([{"role": "user", "content": "q"}], model="x")
        self.assertEqual(list(self.ctx.usage.by_model), ["other/o1"])

    def test_after_response_carries_model_provider_and_source(self):
        events = []
        self.registry.on("after_response", events.append)
        self.registry.register_tool("agentic", lambda input, ctx: ctx.complete([{"role": "user", "content": "q"}]).stop_reason)
        self.script(
            call("agentic"),  # main loop
            text("inside the tool"),  # complete from the tool
            text("main done"),
            text("complete outside"),
        )
        self.app.run_turn("go")
        self.ctx.complete([{"role": "user", "content": "q"}], model="big")
        summary = [(e["source"], e["model"], e["provider"], e["nested"], e["agent_id"]) for e in events]
        self.assertEqual(
            summary,
            [
                ("loop", "m", "fake", False, "main"),  # the reply that calls the tool
                ("complete", "m", "fake", False, "main"),  # from inside the tool
                ("loop", "m", "fake", False, "main"),
                ("complete", "big", "fake", False, "main"),
            ],
        )
        self.assertIs(events[1]["usage"], events[1]["reply"].usage)

    def test_a_crashing_after_response_handler_does_not_break_complete(self):
        def boom(event):
            raise RuntimeError("bad handler")

        self.registry.on("after_response", boom)
        self.script(text("fine"))
        with self.quiet():
            reply = self.ctx.complete([{"role": "user", "content": "q"}])
        self.assertEqual(reply.content[0]["text"], "fine")

    def test_complete_inside_an_agent_is_nested(self):
        events = []
        self.registry.on("after_response", events.append)
        self.registry.register_tool("ask", lambda input, ctx: ctx.complete_text("q"))
        self.script(call("ask"), text("answer"), text("done"))
        result = self.ctx.run_agent_result("go")
        self.assertEqual(
            [(e["source"], e["nested"], e["agent_id"]) for e in events],
            [("loop", True, result.agent_id), ("complete", True, result.agent_id), ("loop", True, result.agent_id)],
        )


class SessionInfoTest(AgentTest):
    def write(self, id, lines, *, raw=None):
        folder = session_mod.SESSIONS_DIR
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{id}.jsonl"
        path.write_text(raw if raw is not None else "".join(json.dumps(line) + "\n" for line in lines))
        return path

    def header(self, **extra):
        return {"type": "session", "cwd": os.path.realpath(self.tmp), **extra}

    def test_header_preview_count_and_creation(self):
        self.write(
            "2026-11-01T10-00-00-123Z",
            [
                self.header(title="My work", parent="2026-10-31T09-00-00-000Z"),
                {"role": "user", "content": "  fix   the\nbug  "},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "r"}]},
            ],
        )
        info = self.ctx.session_info("2026-11-01T10-00-00-123Z")
        self.assertEqual(
            (info.id, info.title, info.parent, info.cwd, info.preview, info.n_messages, info.locked),
            ("2026-11-01T10-00-00-123Z", "My work", "2026-10-31T09-00-00-000Z", os.path.realpath(self.tmp), "fix the bug", 3, False),
        )
        self.assertEqual(info.created, 1_793_527_200.123)
        self.assertGreater(info.mtime, 0)

    def test_the_preview_skips_tool_results_and_reads_block_text(self):
        self.write(
            "p1",
            [
                self.header(),
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "r"}]},
                {"role": "user", "content": [{"type": "text", "text": "the real question"}]},
            ],
        )
        info = self.ctx.session_info("p1")
        self.assertEqual((info.preview, info.created, info.title, info.parent), ("the real question", None, "", ""))

    def test_missing_and_invalid_ids_raise(self):
        with self.assertRaisesRegex(ValueError, "no session"):
            self.ctx.session_info("nope")
        with self.assertRaisesRegex(ValueError, "invalid session id"):
            self.ctx.session_info("../escape")

    def test_corrupt_files_give_what_can_be_read(self):
        self.write("junk", None, raw="not json at all\n{broken\n\n")
        info = self.ctx.session_info("junk")
        self.assertEqual((info.title, info.cwd, info.preview, info.n_messages), ("", "", "", 2))
        self.write("empty", None, raw="")
        self.assertEqual(self.ctx.session_info("empty").n_messages, 0)
        self.write("headerless", [{"role": "user", "content": "hi"}])
        info = self.ctx.session_info("headerless")
        self.assertEqual((info.cwd, info.preview, info.n_messages), ("", "hi", 1))

    def test_a_cut_off_last_line_is_not_a_message(self):
        good = json.dumps(self.header()) + "\n" + json.dumps({"role": "user", "content": "q"}) + "\n"
        self.write("torn", None, raw=good + '{"role": "assistant", "con')
        self.assertEqual(self.ctx.session_info("torn").n_messages, 1)
        self.write("whole", None, raw=good + json.dumps({"role": "assistant", "content": []}))  # no final newline
        self.assertEqual(self.ctx.session_info("whole").n_messages, 2)

    def test_a_non_utf8_file_is_not_an_error(self):
        path = self.write("bin", [self.header()])
        path.write_bytes(path.read_bytes() + b'{"role": "user", "content": "\xff\xfe"}\n')
        self.assertEqual(self.ctx.session_info("bin").n_messages, 1)

    def test_a_locked_session_says_so(self):
        self.write("busy", [self.header(), {"role": "user", "content": "q"}])
        held = session_mod.open_session("busy", str(self.tmp))
        held.lock()
        self.addCleanup(held.unlock)
        self.assertTrue(self.ctx.session_info("busy").locked)
        self.write("free", [self.header()])
        self.assertFalse(self.ctx.session_info("free").locked)
        self.assertFalse((session_mod.SESSIONS_DIR / "free.lock").exists())  # asking does not create the lock file

    def test_the_current_session_is_read_live(self):
        info = self.ctx.session_info()
        self.assertEqual((info.id, info.n_messages, info.preview, info.cwd), ("t", 0, "", str(self.tmp)))
        self.app.messages.append({"role": "user", "content": "unsaved question"})
        self.app.session.title = "named"
        info = self.ctx.session_info()
        self.assertEqual((info.n_messages, info.preview, info.title), (1, "unsaved question", "named"))
        self.assertEqual(self.ctx.session_info("t").n_messages, 1)

    def test_sessions_info_is_newest_first_filtered_and_limited(self):
        other = {"type": "session", "cwd": str(self.tmp / "elsewhere")}
        for n, (id, head) in enumerate([("a", self.header()), ("b", other), ("c", self.header()), ("d", self.header())]):
            path = self.write(id, [head, {"role": "user", "content": f"msg {id}"}])
            os.utime(path, (1000 + n * 10, 1000 + n * 10))
        self.write("junk", None, raw="garbage\n")
        os.utime(session_mod.SESSIONS_DIR / "junk.jsonl", (1005, 1005))
        mine = self.ctx.sessions_info()
        self.assertEqual([i.id for i in mine], ["d", "c", "a"])  # junk has no header: not this project's
        self.assertEqual([i.id for i in self.ctx.sessions_info(2)], ["d", "c"])
        self.assertEqual([i.id for i in self.ctx.sessions_info(all_projects=True)], ["d", "c", "b", "junk", "a"])
        self.assertEqual(self.ctx.sessions_info(0), [])
        self.assertEqual(self.ctx.sessions_info(1)[0].preview, "msg d")

    def test_no_sessions_folder_means_an_empty_list(self):
        self.assertEqual(self.ctx.sessions_info(), [])

    def test_a_long_history_is_not_parsed(self):
        lines = [self.header(), {"role": "user", "content": "first"}]
        raw = "".join(json.dumps(x) + "\n" for x in lines) + "{not json}\n" * 5000
        self.write("long", None, raw=raw)
        info = self.ctx.session_info("long")
        self.assertEqual((info.preview, info.n_messages), ("first", 5001))


class ProgressSlotsTest(AgentTest):
    def shown(self):
        return [c.args[0] for c in self.frontend.progress.call_args_list]

    def test_two_parallel_calls_keep_separate_slots(self):
        a_set, b_set, b_cleared = (threading.Event() for _ in range(3))

        def a(input, ctx):
            ctx.progress("A working")
            a_set.set()
            assert b_set.wait(WAIT)
            self.shown_with_both = self.shown()[-1]
            ctx.progress(None)  # clears only A's own slot
            b_cleared.set()
            return "a"

        def b(input, ctx):
            assert a_set.wait(WAIT)
            ctx.progress("B working")
            b_set.set()
            assert b_cleared.wait(WAIT)
            self.shown_after_a_cleared = self.shown()[-1]
            return "b"

        self.registry.register_tool("a", a, read_only=True)
        self.registry.register_tool("b", b, read_only=True)
        self.script(
            Reply([{"type": "tool_use", "id": "ta", "name": "a", "input": {}}, {"type": "tool_use", "id": "tb", "name": "b", "input": {}}], "tool_use"),
            text("done"),
        )
        self.app.run_turn("go")
        self.assertEqual(self.shown_with_both, "B working (+1 more)")
        self.assertEqual(self.shown_after_a_cleared, "B working")
        self.assertIsNone(self.shown()[-1])  # both gone when the calls ended
        self.assertEqual(self.app.progress_slots._slots, {})

    def test_a_slot_is_cleared_when_its_call_ends_even_if_the_tool_fails(self):
        def boom(input, ctx):
            ctx.progress("about to fail")
            raise RuntimeError("no")

        self.registry.register_tool("boom", boom)
        self.script(call("boom"), text("done"))
        self.app.run_turn("go")
        self.assertEqual(self.shown(), ["about to fail", None])

    def test_the_newest_update_is_the_one_shown(self):
        slots = []
        pool = ProgressSlots(slots.append)
        pool.set("a", "first")
        pool.set("b", "second")
        pool.set("a", "first again")
        self.assertEqual(slots[-1], "first again (+1 more)")
        pool.set("a", None)
        self.assertEqual(slots[-1], "second")
        pool.set("b", None)
        self.assertIsNone(slots[-1])

    def test_an_agent_and_the_user_turn_have_their_own_slots(self):
        seen = []

        def work(input, ctx):
            ctx.progress(f"{ctx.agent_id} busy")
            seen.append(self.app.progress_slots._slots.copy())
            return "ok"

        self.registry.register_tool("work", work)
        self.registry.register_tool("spawn", lambda input, ctx: (ctx.progress("parent waits"), ctx.run_agent("go", tools=["work"]))[1])
        self.script(call("spawn", id="p1"), call("work", id="w1"), text("child"), text("main"))
        self.app.run_turn("go")
        self.assertEqual(len(seen), 1)
        self.assertEqual(sorted(seen[0].values()), [next(v for v in seen[0].values() if v.endswith("busy")), "parent waits"])
        self.assertEqual(len(seen[0]), 2)
        self.assertEqual(self.app.progress_slots._slots, {})

    def test_an_explicit_key_stays_until_cleared_and_the_turn_end_drops_it(self):
        def job(input, ctx):
            ctx.progress("indexing", key="index")
            return "started"

        self.registry.register_tool("job", job)
        self.script(call("job"), text("done"))
        self.app.run_turn("go")
        # the turn ended: nothing stays on screen
        self.assertIsNone(self.shown()[-1])
        self.ctx.progress("later", key="k")
        self.assertEqual(self.shown()[-1], "later")
        self.ctx.progress(None, key="k")
        self.assertIsNone(self.shown()[-1])

    def test_a_command_reports_in_the_main_slot_and_it_is_dropped_at_its_end(self):
        self.registry.register_command("scan", lambda args, ctx: ctx.progress("scanning"))
        self.app.run_command("/scan")
        self.assertEqual(self.shown(), ["scanning", None])

    def test_text_is_cleaned_and_types_are_checked(self):
        self.ctx.progress("  one\ntwo  ")
        self.assertEqual(self.shown()[-1], "one")
        self.ctx.progress("x" * 300)
        self.assertEqual(len(self.shown()[-1]), 120)
        self.ctx.progress("   ")  # blank clears
        self.assertIsNone(self.shown()[-1])
        with self.assertRaises(TypeError):
            self.ctx.progress(5)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            self.ctx.progress("x", key=5)  # type: ignore[arg-type]


class NotifyEventTest(AgentTest):
    def test_ctx_notify_fires_the_event_before_showing(self):
        order = []
        self.registry.on("notify", lambda e: order.append(("event", e["text"], e["kind"], e["source"])))
        self.frontend.notify.side_effect = lambda text, kind: order.append(("shown", text, kind))
        self.ctx.notify("saved", "warning")
        self.ctx.notify("plain")
        self.assertEqual(
            order,
            [
                ("event", "saved", "warning", "extension"),
                ("shown", "saved", "warning"),
                ("event", "plain", "info", "extension"),
                ("shown", "plain", "info"),
            ],
        )

    def test_an_invalid_kind_fires_nothing(self):
        events = []
        self.registry.on("notify", events.append)
        with self.assertRaises(ValueError):
            self.ctx.notify("x", "loud")
        self.assertEqual(events, [])
        self.frontend.notify.assert_not_called()

    def test_a_crashing_handler_is_skipped_and_the_notice_still_shows(self):
        later = []

        def boom(event):
            raise RuntimeError("bad history")

        self.registry.on("notify", boom)
        self.registry.on("notify", later.append)
        with self.quiet():
            self.ctx.notify("hello")
        self.frontend.notify.assert_called_once_with("hello", "info")
        self.assertEqual(len(later), 1)

    def test_a_handler_that_notifies_does_not_recurse(self):
        seen = []

        def handler(event):
            seen.append(event["text"])
            if event["text"] == "first":
                event["ctx"].notify("echo")

        self.registry.on("notify", handler)
        self.ctx.notify("first")
        self.assertEqual(seen, ["first"])
        self.assertEqual([c.args[0] for c in self.frontend.notify.call_args_list], ["echo", "first"])

    def test_it_is_a_known_event_with_a_public_payload_type(self):
        from minimal import api

        self.assertIn("notify", api.EVENTS)
        self.assertEqual(set(api.NotifyEvent.__annotations__), {"ctx", "text", "kind", "source"})

    def test_a_mode_lost_in_a_reload_notifies_as_core(self):
        events = []
        self.registry.on("notify", events.append)
        self.registry.register_mode("review", description="d")
        self.app.set_mode("review")
        self.registry.modes.clear()
        self.app._validate_mode()
        self.assertEqual([(e["kind"], e["source"]) for e in events], [("warning", "core")])


class CallableReadOnlyTest(AgentTest):
    def setUp(self):
        super().setUp()
        self.running = 0
        self.most = 0
        self.lock = threading.Lock()
        self.asked = []

    def register(self, read_only):
        def probe(input, ctx):
            with self.lock:
                self.running += 1
                self.most = max(self.most, self.running)
            time.sleep(0.15)
            with self.lock:
                self.running -= 1
            return "ok"

        self.registry.register_tool("probe", probe, read_only=read_only)

    def turn(self, *inputs):
        blocks = [{"type": "tool_use", "id": f"p{i}", "name": "probe", "input": i_} for i, i_ in enumerate(inputs)]
        self.script(Reply(blocks, "tool_use"), text("done"))
        self.app.run_turn("go")

    def test_the_function_decides_per_call(self):
        def decide(input, ctx):
            self.asked.append(input["n"])
            return input["safe"]

        self.register(decide)
        self.turn({"n": 0, "safe": True}, {"n": 1, "safe": True})
        self.assertEqual(self.most, 2)
        self.assertEqual(self.asked, [0, 1])  # asked once per call

    def test_an_unsafe_call_is_a_barrier(self):
        self.register(lambda input, ctx: input["safe"])
        self.turn({"safe": True}, {"safe": False}, {"safe": True})
        self.assertEqual(self.most, 1)

    def test_each_call_is_asked_once_even_when_it_ends_a_run(self):
        def decide(input, ctx):
            self.asked.append(input["n"])
            return input["safe"]

        self.register(decide)
        self.turn({"n": 0, "safe": True}, {"n": 1, "safe": True}, {"n": 2, "safe": False}, {"n": 3, "safe": True})
        self.assertEqual(sorted(self.asked), [0, 1, 2, 3])

    def test_a_function_that_raises_means_not_read_only(self):
        def broken(input, ctx):
            raise RuntimeError("cannot tell")

        self.register(broken)
        self.turn({}, {})
        self.assertEqual(self.most, 1)
        self.assertEqual(self.app.messages[-2]["content"][0]["content"], "ok")  # the calls still ran

    def test_a_bool_still_works_and_other_values_are_refused(self):
        self.register(True)
        self.turn({}, {})
        self.assertEqual(self.most, 2)
        with self.assertRaisesRegex(ValueError, "read_only"):
            self.registry.register_tool("bad", lambda i, c: "", read_only="yes")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
