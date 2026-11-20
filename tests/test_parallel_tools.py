import contextlib
import io
import json
import threading
import time
import unittest
from types import SimpleNamespace

from minimal import config
from minimal.core.agent import UNFINISHED, USER_DENIED, run_agent
from minimal.core.app import App
from minimal.core.session import Session
from minimal.extensions.registry import Registry, TurnInterrupted
from minimal.providers.base import Reply
from tests.test_security import TempDirTest

WAIT = 3  # seconds a test waits for something that must happen


def call(call_id, name, **input):
    return {"type": "tool_use", "id": call_id, "name": name, "input": input}


def tool_reply(*calls):
    return Reply(content=list(calls), stop_reason="tool_use")


def text_reply(text="done"):
    return Reply(content=[{"type": "text", "text": text}], stop_reason="end_turn")


class ScriptedProvider:
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = 0

    def stream(self, **kwargs):
        self.requests += 1
        return self.replies.pop(0)


class ParallelToolsTest(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        self.ctx = SimpleNamespace(cwd=".", cancelled=False)
        self.log: list[tuple[str, str]] = []  # ("start" | "end", tool name), in the order they happened
        self.lock = threading.Lock()
        self.running = 0
        self.most_running = 0

    def note(self, what, name):
        with self.lock:
            self.log.append((what, name))

    def add(self, name, run, read_only=True, **kwargs):
        def traced(input, ctx):
            self.note("start", name)
            with self.lock:
                self.running += 1
                self.most_running = max(self.most_running, self.running)
            try:
                return run(input, ctx)
            finally:
                with self.lock:
                    self.running -= 1
                self.note("end", name)

        self.registry.register_tool(name, traced, name, read_only=read_only, **kwargs)

    def run_loop(self, replies, **kwargs):
        messages = [{"role": "user", "content": "go"}]
        run_agent(
            provider=ScriptedProvider(replies), model="m", system="s", messages=messages, registry=self.registry,
            ctx=self.ctx, **kwargs,
        )
        return messages

    def test_read_only_calls_of_one_reply_run_together_and_answer_in_order(self):
        barrier = threading.Barrier(3, timeout=WAIT)  # only passes if all three are running at the same time
        delays = {"a": 0.15, "b": 0.05, "c": 0.0}

        def run(input, ctx):
            barrier.wait()
            time.sleep(delays[input["who"]])  # they finish in the order c, b, a
            return f"result {input['who']}"

        self.add("look", run)
        shown, finished = [], []
        messages = self.run_loop(
            [tool_reply(*(call(f"t{n}", "look", who=who) for n, who in enumerate("abc", 1))), text_reply()],
            on_tool=lambda c: shown.append(c["id"]),
            on_tool_result=lambda c, r: finished.append((c["id"], r["tool_use_id"])),
        )
        results = messages[2]["content"]
        self.assertEqual([(r["tool_use_id"], r["content"], r["is_error"]) for r in results],
                         [("t1", "result a", False), ("t2", "result b", False), ("t3", "result c", False)])
        self.assertEqual(shown, ["t1", "t2", "t3"])
        self.assertEqual(finished, [("t1", "t1"), ("t2", "t2"), ("t3", "t3")])
        self.assertEqual(self.most_running, 3)

    def test_a_call_that_is_not_read_only_is_a_barrier_and_runs_alone_in_order(self):
        pair = threading.Barrier(2, timeout=WAIT)

        def look(input, ctx):
            if input.get("pair"):
                pair.wait()  # the first two only pass if they run at the same time
            else:
                time.sleep(0.02)
            return input["who"]

        self.add("look", look)
        self.add("change", lambda input, ctx: time.sleep(0.05) or "changed", read_only=False)
        replies = [
            tool_reply(
                call("t1", "look", who="1", pair=True),
                call("t2", "look", who="2", pair=True),
                call("t3", "change"),
                call("t4", "look", who="4"),
                call("t5", "look", who="5"),
            ),
            text_reply(),
        ]
        messages = self.run_loop(replies)
        self.assertEqual([r["content"] for r in messages[2]["content"]], ["1", "2", "changed", "4", "5"])
        order = [event for event in self.log if event[1] == "change" or event[1] == "look"]
        first_two = order[:4]
        self.assertEqual(sorted(first_two), [("end", "look"), ("end", "look"), ("start", "look"), ("start", "look")])
        self.assertEqual(order[4:6], [("start", "change"), ("end", "change")])  # nothing runs next to the write
        self.assertEqual(sorted(order[6:]), [("end", "look"), ("end", "look"), ("start", "look"), ("start", "look")])

    def test_max_parallel_bounds_the_concurrency(self):
        self.add("look", lambda input, ctx: time.sleep(0.05) or "x")
        calls = [call(f"t{n}", "look") for n in range(6)]
        self.run_loop([tool_reply(*calls), text_reply()], max_parallel=2)
        self.assertEqual(self.most_running, 2)

    def test_max_parallel_one_runs_one_by_one(self):
        self.add("look", lambda input, ctx: time.sleep(0.03) or "x")
        self.run_loop([tool_reply(*(call(f"t{n}", "look") for n in range(3))), text_reply()], max_parallel=1)
        self.assertEqual(self.most_running, 1)

    def test_tools_that_are_not_read_only_never_overlap(self):
        self.add("change", lambda input, ctx: time.sleep(0.03) or "x", read_only=False)
        self.run_loop([tool_reply(*(call(f"t{n}", "change") for n in range(3))), text_reply()])
        self.assertEqual(self.most_running, 1)

    def test_errors_and_unknown_tools_answer_their_own_call_only(self):
        def run(input, ctx):
            if input.get("fail"):
                raise ValueError("boom")
            return "fine"

        self.add("look", run)
        messages = self.run_loop(
            [tool_reply(call("t1", "look"), call("t2", "look", fail=True), call("t3", "nope"), call("t4", "look")), text_reply()]
        )
        got = [(r["tool_use_id"], r["content"], r["is_error"]) for r in messages[2]["content"]]
        self.assertEqual(
            got,
            [("t1", "fine", False), ("t2", "Error: boom", True), ("t3", "Error: unknown tool: nope", True), ("t4", "fine", False)],
        )

    def test_confirmations_are_asked_one_by_one_in_order_before_any_call_starts(self):
        self.add("look", lambda input, ctx: input["who"], confirm=True)
        asked = []
        caller = threading.get_ident()

        def confirm(c):
            asked.append((c["id"], threading.get_ident() == caller, list(self.log)))
            return c["id"] != "t2"  # the user refuses the second call

        messages = self.run_loop(
            [tool_reply(*(call(f"t{n}", "look", who=str(n)) for n in (1, 2, 3))), text_reply()], confirm=confirm
        )
        self.assertEqual([a[0] for a in asked], ["t1", "t2", "t3"])
        self.assertTrue(all(a[1] for a in asked))  # on the thread that runs the turn
        self.assertTrue(all(a[2] == [] for a in asked))  # nothing had started yet
        got = [(r["tool_use_id"], r["content"], r["is_error"]) for r in messages[2]["content"]]
        self.assertEqual(got, [("t1", "1", False), ("t2", "Error: " + USER_DENIED, True), ("t3", "3", False)])

    def test_tool_events_come_in_call_order(self):
        events = []
        self.registry.on("tool_call", lambda e: events.append(("call", e["input"]["who"])))
        self.registry.on("tool_result", lambda e: events.append(("result", e["input"]["who"], e["output"])))
        self.add("look", lambda input, ctx: time.sleep(0.05 if input["who"] == "a" else 0) or input["who"].upper())
        self.run_loop([tool_reply(*(call(f"t{n}", "look", who=who) for n, who in enumerate("ab", 1))), text_reply()])
        self.assertEqual(
            events, [("call", "a"), ("call", "b"), ("result", "a", "A"), ("result", "b", "B")]
        )

    def test_a_tool_result_handler_can_rewrite_a_parallel_result(self):
        self.registry.on("tool_result", lambda e: {"output": e["output"] + "!"})
        self.add("look", lambda input, ctx: "x")
        messages = self.run_loop([tool_reply(call("t1", "look"), call("t2", "look")), text_reply()])
        self.assertEqual([r["content"] for r in messages[2]["content"]], ["x!", "x!"])

    def test_interrupting_a_batch_answers_every_call_and_does_not_wait_for_the_slow_ones(self):
        release = threading.Event()
        self.addCleanup(release.set)
        cancelled = threading.Event()
        started = threading.Barrier(3, timeout=WAIT)

        def run(input, ctx):
            started.wait()  # all of them are running...
            if input["who"] != "a":
                release.wait(WAIT)  # ...and two do not finish
            return input["who"]

        self.add("look", run)
        messages = [{"role": "user", "content": "go"}]
        calls = [call(f"t{n}", "look", who=who) for n, who in enumerate("abc", 1)]
        threading.Timer(0.3, cancelled.set).start()
        began = time.monotonic()
        with self.assertRaises(TurnInterrupted):
            run_agent(
                provider=ScriptedProvider([tool_reply(*calls)]), model="m", system="s", messages=messages,
                registry=self.registry, ctx=self.ctx, cancelled=cancelled,
            )
        self.assertLess(time.monotonic() - began, WAIT)
        results = messages[2]["content"]
        self.assertEqual([r["tool_use_id"] for r in results], ["t1", "t2", "t3"])
        self.assertEqual((results[0]["content"], results[0]["is_error"]), ("a", False))  # finished before the interrupt
        self.assertEqual([(r["content"], r["is_error"]) for r in results[1:]], [("Error: " + UNFINISHED, True)] * 2)

    def test_an_interrupt_before_the_batch_starts_leaves_every_call_answered(self):
        self.add("look", lambda input, ctx: "x")
        cancelled = threading.Event()
        messages = [{"role": "user", "content": "go"}]
        seen = []

        def on_tool(c):
            seen.append(c["id"])
            cancelled.set()  # Esc while the calls are being announced

        with self.assertRaises(TurnInterrupted):
            run_agent(
                provider=ScriptedProvider([tool_reply(call("t1", "look"), call("t2", "look"))]), model="m", system="s",
                messages=messages, registry=self.registry, ctx=self.ctx, cancelled=cancelled, on_tool=on_tool,
            )
        self.assertEqual(seen, ["t1"])
        self.assertEqual([(r["tool_use_id"], r["is_error"]) for r in messages[2]["content"]], [("t1", True), ("t2", True)])
        self.assertEqual(self.log, [])  # nothing ran


class BuiltinReadOnlyTest(unittest.TestCase):
    def test_read_grep_and_glob_are_read_only_and_the_others_are_not(self):
        from minimal.tools.builtin import BUILTIN_TOOLS

        self.assertEqual(
            {t.name for t in BUILTIN_TOOLS if t.read_only}, {"read", "grep", "glob"}
        )

    def test_register_tool_and_the_decorator_take_read_only(self):
        from minimal.extensions.facade import Extension

        registry = Registry()
        registry.register_tool("a", lambda i, c: "", read_only=True)
        self.assertTrue(registry.tools["a"].read_only)
        with self.assertRaisesRegex(ValueError, "read_only must be a bool"):
            registry.register_tool("b", lambda i, c: "", read_only="yes")
        ext = Extension.__new__(Extension)
        ext._registry = registry
        ext.tool("c", read_only=True)(lambda i, c: "")
        ext.tool("d")(lambda i, c: "")
        self.assertEqual((registry.tools["c"].read_only, registry.tools["d"].read_only), (True, False))


class TurnLimitTest(TempDirTest):
    """``agent.max_turns``, ``tools.max_parallel`` and ``reasoning_effort`` as an App uses them."""

    def make_app(self, settings=None):
        if settings is not None:
            folder = self.cwd / ".minimal"
            folder.mkdir()
            (folder / "settings.json").write_text(json.dumps(settings))
        app = App(registry=Registry(), session=Session("t", self.tmp / "t.jsonl"), cwd=str(self.cwd), auto_approve=True)
        with contextlib.redirect_stderr(io.StringIO()):
            app.reload_extensions()
        return app

    def run_turn(self, app, provider):
        app.provider, app.model = provider, "m"
        errors = []
        app.frontend = SimpleNamespace(
            text=lambda c: None, tool_call=lambda c: None, tool_result=lambda c, r: None, turn_end=errors.append,
            reloaded=lambda: None, notify=lambda t, k: None, queued=lambda: None, mode_changed=lambda: None,
        )
        app.run_turn("go")
        return errors[0]

    def test_the_default_limit_is_50_and_the_setting_changes_it_for_ctx_too(self):
        self.assertEqual(self.make_app().context.max_turns, 50)
        self.assertEqual(self.make_app({"agent": {"max_turns": 7}}).context.max_turns, 7)

    def test_max_turns_setting_stops_the_loop(self):
        app = self.make_app({"agent": {"max_turns": 2}})
        provider = ScriptedProvider([tool_reply(call(f"t{n}", "glob", pattern="*")) for n in range(5)])
        error = self.run_turn(app, provider)
        self.assertIn("exceeded the maximum of 2 turns", error)
        self.assertEqual(provider.requests, 2)

    def test_a_nested_agent_uses_the_setting_unless_it_asks_for_less(self):
        app = self.make_app({"agent": {"max_turns": 2}})
        app.provider, app.model = ScriptedProvider([tool_reply(call(f"t{n}", "glob", pattern="*")) for n in range(5)]), "m"
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "maximum of 2 turns"):
                app.context.run_agent("go")
            app.provider = ScriptedProvider([tool_reply(call(f"t{n}", "glob", pattern="*")) for n in range(5)])
            with self.assertRaisesRegex(RuntimeError, "maximum of 1 turns"):
                app.context.run_agent("go", max_turns=1)

    def test_max_parallel_setting_reaches_the_loop(self):
        self.assertEqual(self.make_app().settings.max_parallel, 4)
        self.assertEqual(self.make_app({"tools": {"max_parallel": 1}}).settings.max_parallel, 1)

    def test_reasoning_effort_comes_from_the_setting_and_ctx_overrides_it(self):
        app = self.make_app({"reasoning_effort": "low"})
        ctx = app.context
        self.assertEqual((ctx.reasoning_effort, app.request_options()), ("low", {"reasoning_effort": "low"}))
        ctx.set_reasoning_effort("high")
        self.assertEqual(ctx.reasoning_effort, "high")
        ctx.set_reasoning_effort(None)
        self.assertEqual((ctx.reasoning_effort, app.request_options()), (None, {}))
        with self.assertRaisesRegex(ValueError, "low, medium, high"):
            ctx.set_reasoning_effort("extreme")
        self.assertIsNone(ctx.reasoning_effort)  # a refused value changes nothing

    def test_reasoning_effort_reaches_the_provider_with_max_tokens(self):
        app = self.make_app({"reasoning_effort": "medium", "max_tokens": 9000})
        sent = []

        class Spy(ScriptedProvider):
            def stream(self, **kwargs):
                sent.append(kwargs.get("options"))
                return super().stream(**kwargs)

        provider = Spy([text_reply(), text_reply()])
        self.run_turn(app, provider)
        app.context.complete([{"role": "user", "content": "x"}], tools=False)
        expected = {"max_tokens": 9000, "reasoning_effort": "medium"}
        self.assertEqual(sent, [expected, expected])

    def test_status_shows_the_reasoning_effort(self):
        from minimal.core.host import Host

        app = self.make_app({"reasoning_effort": "high"})
        app.provider_name = "p"
        self.assertEqual(Host(app).status().reasoning_effort, "high")


class SettingsValidationTest(TempDirTest):
    def read(self, data):
        folder = self.cwd / ".minimal"
        folder.mkdir(exist_ok=True)
        (folder / "settings.json").write_text(json.dumps(data))
        layers, errors = config.read_layers(str(self.cwd))
        return config.Settings(tuple(layers)), errors

    def test_valid_values_apply(self):
        settings, errors = self.read(
            {"reasoning_effort": "high", "agent": {"max_turns": 9}, "tools": {"max_parallel": 2}}
        )
        self.assertEqual(errors, [])
        self.assertEqual((settings.reasoning_effort, settings.max_turns, settings.max_parallel), ("high", 9, 2))

    def test_defaults(self):
        settings = config.Settings()
        self.assertEqual((settings.reasoning_effort, settings.max_turns, settings.max_parallel), (None, 50, 4))

    def test_invalid_values_are_dropped_with_a_message(self):
        settings, errors = self.read(
            {"reasoning_effort": "max", "agent": {"max_turns": 0}, "tools": {"max_parallel": True}}
        )
        self.assertEqual(len(errors), 3)
        self.assertTrue(any("reasoning_effort must be one of low, medium, high" in e for e in errors))
        self.assertTrue(any("agent.max_turns must be a positive integer" in e for e in errors))
        self.assertTrue(any("tools.max_parallel must be a positive integer" in e for e in errors))
        self.assertEqual((settings.reasoning_effort, settings.max_turns, settings.max_parallel), (None, 50, 4))

    def test_layers_follow_the_highest_wins_rule(self):
        (self.home).mkdir(exist_ok=True)
        config.SETTINGS_FILE.write_text(json.dumps({"reasoning_effort": "low", "agent": {"max_turns": 5}}))
        settings, errors = self.read({"reasoning_effort": "high"})
        self.assertEqual(errors, [])
        self.assertEqual((settings.reasoning_effort, settings.max_turns), ("high", 5))


if __name__ == "__main__":
    unittest.main()
