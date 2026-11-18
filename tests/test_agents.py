import contextlib
import io
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from minimal import config
from minimal.core import session as session_mod
from minimal.core.agent import MaxTurnsExceeded
from minimal.core.agents import AgentResult
from minimal.core.app import App
from minimal.core.host import Host
from minimal.core.session import Session
from minimal.extensions import loader
from minimal.extensions.registry import Registry
from minimal.providers.base import ProviderSpec, Reply, Usage
from minimal.ui.commands import register_builtin_commands


class ScriptedProvider:
    """Replays ``replies`` in order (an exception instance is raised instead), recording each request."""

    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def stream(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def offered(self, index=-1):
        return [t["name"] for t in self.requests[index]["tools"]]


def text(t, usage=None):
    return Reply(content=[{"type": "text", "text": t}], stop_reason="end_turn", usage=usage)


def call(name, input=None, id="c1", usage=None):
    block = {"type": "tool_use", "id": id, "name": name, "input": input or {}}
    return Reply(content=[block], stop_reason="tool_use", usage=usage)


class AgentTest(unittest.TestCase):
    """An ``App`` in a temporary project with a temporary ``~/.minimal`` and a scripted provider."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for patcher in (
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.tmp / "home" / "extensions"),
            mock.patch.object(config, "SETTINGS_FILE", self.tmp / "home" / "settings.json"),
            mock.patch.object(config, "TRUST_FILE", self.tmp / "home" / "trusted.json"),
            mock.patch.object(session_mod, "SESSIONS_DIR", self.tmp / "home" / "sessions"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.frontend = mock.Mock()
        self.app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            frontend=self.frontend,
            register_commands=register_builtin_commands,
        )
        self.app.reload_extensions()
        self.app.provider_name, self.app.model = "fake", "m"
        self.registry = self.app.registry
        self.ctx = self.app.context
        self.events = []

    def script(self, *replies):
        self.app.provider = ScriptedProvider(replies)
        return self.app.provider

    def record(self, *names):
        """Record ``(event, agent_id, parent_id)`` of every event in ``names``."""
        for name in names:
            self.registry.on(
                name, lambda e, name=name: self.events.append((name, e["agent_id"], e["parent_id"]))
            )

    def spawn_tool(self, **kwargs):
        """Register ``spawn``: runs its ``prompt`` input as an agent with ``kwargs``; keeps the ``AgentResult``."""
        self.results = []

        def spawn(input, ctx):
            result = ctx.run_agent_result(input.get("prompt", "child task"), **kwargs)
            self.results.append(result)
            return f"{result.status}: {result.text}"

        self.registry.register_tool("spawn", spawn)

    def quiet(self):
        return contextlib.redirect_stderr(io.StringIO())


class AgentIdentityTest(AgentTest):
    def test_nested_agent_events_carry_its_id_and_its_parent(self):
        self.registry.register_tool("echo", lambda input, ctx: "echoed")
        self.spawn_tool(name="helper")
        self.record("tool_call", "tool_result", "turn_end", "before_request", "after_response")
        self.script(
            call("spawn", id="s1"),  # main
            call("echo", id="e1"),  # child
            text("child done"),  # child
            text("main done"),  # main
        )
        self.app.run_turn("go")

        child = self.results[0].agent_id
        self.assertNotEqual(child, "main")
        by_agent = {}
        for event, agent_id, parent_id in self.events:
            by_agent.setdefault((agent_id, parent_id), []).append(event)
        self.assertEqual(set(by_agent), {("main", None), (child, "main")})
        self.assertEqual(
            by_agent[(child, "main")],
            ["before_request", "after_response", "tool_call", "tool_result", "before_request", "after_response", "turn_end"],
        )
        self.assertEqual(by_agent[("main", None)].count("turn_end"), 1)
        self.assertEqual(by_agent[("main", None)].count("tool_call"), 1)  # spawn

    def test_agent_start_and_end_bracket_each_loop_in_order(self):
        self.spawn_tool(name="helper")
        self.record("agent_start", "agent_end", "turn_end", "tool_call", "tool_result")
        self.script(call("spawn"), text("child done"), text("main done"))
        self.app.run_turn("go")

        child = self.results[0].agent_id
        self.assertEqual(
            self.events,
            [
                ("agent_start", "main", None),
                ("tool_call", "main", None),
                ("agent_start", child, "main"),
                ("turn_end", child, "main"),
                ("agent_end", child, "main"),
                ("tool_result", "main", None),
                ("turn_end", "main", None),
                ("agent_end", "main", None),
            ],
        )

    def test_agent_start_and_end_payloads(self):
        self.spawn_tool(name="helper")
        starts, ends = [], []
        self.registry.on("agent_start", lambda e: starts.append(e))
        self.registry.on("agent_end", lambda e: ends.append(e))
        usage = Usage(input_tokens=10, output_tokens=2)
        self.script(call("spawn", input={"prompt": "do it"}), text("child done", usage), text("main done"))
        self.app.run_turn("go")

        child = self.results[0].agent_id
        self.assertEqual([(s["agent_id"], s["parent_id"], s["name"], s["prompt"]) for s in starts],
                         [("main", None, "main", "go"), (child, "main", "helper", "do it")])
        self.assertEqual(
            [(e["agent_id"], e["parent_id"], e["name"], e["status"], e["text"], e["error"]) for e in ends],
            [(child, "main", "helper", "done", "child done", None), ("main", None, "main", "done", "main done", None)],
        )
        self.assertEqual(ends[0]["usage"], usage)
        self.assertEqual((starts[1]["ctx"].agent_id, starts[1]["ctx"].parent_id), (child, "main"))

    def test_agent_end_fires_when_the_loop_fails(self):
        self.spawn_tool()
        ends = []
        self.registry.on("agent_end", lambda e: ends.append((e["agent_id"], e["status"], e["error"])))
        self.script(call("spawn"), RuntimeError("provider down"), text("main done"))
        self.app.run_turn("go")
        self.assertEqual(ends[0][1:], ("error", "provider down"))
        self.assertEqual(ends[1], ("main", "done", None))

        ends.clear()
        self.script(RuntimeError("main failed"))
        self.app.run_turn("again")
        self.assertEqual(ends, [("main", "error", "main failed")])

    def test_frontend_gets_the_agent_in_the_calls_but_the_history_does_not(self):
        self.registry.register_tool("echo", lambda input, ctx: "echoed")
        self.spawn_tool()
        self.script(call("spawn", id="s1"), call("echo", id="e1"), text("c"), text("m"))
        self.app.run_turn("go")

        shown = [c.args[0] for c in self.frontend.tool_call.call_args_list]
        child = self.results[0].agent_id
        self.assertEqual([(c["name"], c["agent_id"], c["parent_id"]) for c in shown],
                         [("spawn", "main", None), ("echo", child, "main")])
        results = [c.args for c in self.frontend.tool_result.call_args_list]
        self.assertEqual([(call_["id"], r["agent_id"], r["is_error"]) for call_, r in results],
                         [("e1", child, False), ("s1", "main", False)])
        for message in self.app.messages:  # the session's history is what the provider saw: no extra keys
            for block in message["content"] if isinstance(message["content"], list) else []:
                self.assertNotIn("agent_id", block)
        self.assertEqual([b["id"] for b in self.app.messages[1]["content"]], ["s1"])

    def test_the_nested_ctx_is_attributed_to_its_agent(self):
        seen = []
        self.registry.register_tool("who", lambda input, ctx: seen.append((ctx.agent_id, ctx.parent_id)) or "ok")
        self.script(call("who"))
        self.script(call("who"), text("done"))
        self.assertEqual(self.ctx.agent_id, "main")
        self.assertIsNone(self.ctx.parent_id)
        result = self.ctx.run_agent_result("go", name="x")
        self.assertEqual(seen, [(result.agent_id, "main")])

    def test_agents_lists_running_and_finished_agents_with_counters(self):
        snapshots = []

        def spawn(input, ctx):
            snapshots.append(ctx.agents())
            result = ctx.run_agent_result("child", name="helper")
            return result.text

        self.registry.register_tool("spawn", spawn)
        self.registry.register_tool("echo", lambda input, ctx: "ok")
        self.script(
            call("spawn", usage=Usage(input_tokens=5, output_tokens=1)),
            call("echo", usage=Usage(input_tokens=7, output_tokens=2)),
            text("child done", Usage(input_tokens=9, output_tokens=3)),
            text("main done", Usage(input_tokens=11, output_tokens=4)),
        )
        self.assertEqual(self.ctx.agents(), [])
        self.app.run_turn("go")

        self.assertEqual([(a.id, a.status) for a in snapshots[0]], [("main", "running")])
        main, child = self.ctx.agents()
        self.assertEqual((main.id, main.parent_id, main.name, main.status, main.tool_calls), ("main", None, "main", "done", 1))
        self.assertEqual((main.usage.input_tokens, main.usage.output_tokens), (16, 5))
        self.assertEqual((child.parent_id, child.name, child.status, child.tool_calls), ("main", "helper", "done", 1))
        self.assertEqual((child.usage.input_tokens, child.usage.output_tokens), (16, 5))
        self.assertEqual((child.model, child.provider, child.prompt), ("m", "fake", "child"))
        self.assertIsNotNone(child.finished)
        self.assertLessEqual(child.started, child.finished)

    def test_main_agent_keeps_how_its_last_turn_ended(self):
        self.script(RuntimeError("down"))
        self.app.run_turn("go")
        self.assertEqual([(a.id, a.status, a.error) for a in self.ctx.agents()], [("main", "error", "down")])
        self.script(text("fine"))
        self.app.run_turn("again")
        (main,) = self.ctx.agents()
        self.assertEqual((main.status, main.error, main.prompt), ("done", None, "again"))

    def test_ids_are_unique(self):
        self.script(*[text("x") for _ in range(30)])
        ids = {self.ctx.run_agent_result("go").agent_id for _ in range(30)}
        self.assertEqual(len(ids), 30)


class RunAgentResultTest(AgentTest):
    def test_done_result_has_text_usage_and_its_own_transcript(self):
        self.script(call("echo", id="e1", usage=Usage(1, 2)), text("all done", Usage(3, 4)))
        self.registry.register_tool("echo", lambda input, ctx: "echoed")
        result = self.ctx.run_agent_result("task")
        self.assertIsInstance(result, AgentResult)
        self.assertEqual((result.status, result.error, result.text), ("done", None, "all done"))
        self.assertEqual((result.usage.input_tokens, result.usage.output_tokens), (4, 6))
        self.assertEqual(result.messages[0], {"role": "user", "content": "task"})
        self.assertEqual([m["role"] for m in result.messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual(self.app.messages, [])  # the session's history is untouched
        with self.assertRaises(Exception):
            result.text = "x"  # frozen

    def test_provider_error_is_reported_in_the_result_and_raised_by_run_agent(self):
        self.script(RuntimeError("boom"), RuntimeError("boom"))
        result = self.ctx.run_agent_result("task")
        self.assertEqual((result.status, result.error, result.text), ("error", "boom", ""))
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.ctx.run_agent("task")

    def test_running_out_of_turns_is_max_turns(self):
        self.registry.register_tool("echo", lambda input, ctx: "echoed")
        self.script(*[call("echo", id=f"e{i}") for i in range(2)], text("unused"))
        result = self.ctx.run_agent_result("task", max_turns=2)
        self.assertEqual((result.status, result.error), ("max_turns", "exceeded the maximum of 2 turns"))
        self.assertEqual(len(self.app.provider.requests), 2)
        # every tool_use is answered, so the transcript stays valid
        self.assertEqual([m["role"] for m in result.messages], ["user", "assistant", "user", "assistant", "user"])
        self.assertEqual(self.ctx.agents()[0].status, "max_turns")

        self.script(*[call("echo", id=f"e{i}") for i in range(2)])
        with self.assertRaises(MaxTurnsExceeded):
            self.ctx.run_agent("task", max_turns=2)

    def test_user_interrupt_during_a_tool_is_interrupted_and_stops_the_turn_too(self):
        def stop(input, ctx):
            self.app.cancelled.set()  # the user presses Esc while the child's tool runs
            return "stopping"

        self.registry.register_tool("stop", stop)
        self.spawn_tool()
        ends = []
        self.registry.on("agent_end", lambda e: ends.append((e["agent_id"], e["status"])))
        self.script(call("spawn", id="s1"), call("stop", id="t1"))
        self.app.run_turn("go")

        (child,) = [a for a in self.ctx.agents() if a.id != "main"]
        self.assertEqual(self.results[0].status, "interrupted")
        self.assertEqual(ends, [(child.id, "interrupted"), ("main", "interrupted")])
        self.assertEqual([m["role"] for m in self.results[0].messages], ["user", "assistant", "user"])
        pending = self.results[0].messages[-1]["content"][0]  # tool_use t1 has its result
        self.assertEqual(pending["tool_use_id"], "t1")
        self.frontend.turn_end.assert_called_once()
        self.assertIn("interrupted", self.frontend.turn_end.call_args.args[0])

    def test_cancelling_one_agent_does_not_interrupt_its_parent(self):
        self.registry.register_tool("stop", lambda input, ctx: ctx.cancel_agent(ctx.agent_id) and "stopped")
        self.spawn_tool()
        self.script(call("spawn", id="s1"), call("stop", id="t1"), text("main carries on"))
        self.app.run_turn("go")

        self.assertEqual(self.results[0].status, "interrupted")
        self.assertFalse(self.app.cancelled.is_set())
        self.frontend.turn_end.assert_called_once_with(None)
        main, child = self.ctx.agents()
        self.assertEqual((main.status, child.status), ("done", "interrupted"))
        self.assertEqual(self.app.messages[-1]["content"][0]["text"], "main carries on")
        spawn_result = self.app.messages[2]["content"][0]  # the parent's tool call got the child's outcome
        self.assertFalse(spawn_result["is_error"])
        self.assertTrue(spawn_result["content"].startswith("interrupted"))

    def test_run_agent_of_a_cancelled_agent_raises_turn_interrupted_for_its_tool(self):
        self.registry.register_tool("stop", lambda input, ctx: ctx.cancel_agent(ctx.agent_id) and "stopped")

        def spawn(input, ctx):
            return ctx.run_agent("child")  # the text API: an interrupted child raises

        self.registry.register_tool("spawn", spawn)
        self.script(call("spawn", id="s1"), call("stop", id="t1"), text("main carries on"))
        self.app.run_turn("go")
        spawn_result = self.app.messages[2]["content"][0]
        self.assertTrue(spawn_result["is_error"])
        self.assertIn("interrupted", spawn_result["content"])
        self.frontend.turn_end.assert_called_once_with(None)

    def test_cancelling_an_agent_cancels_the_agents_it_started(self):
        def child(input, ctx):
            return ctx.run_agent_result("grandchild", name="grandchild").status

        self.registry.register_tool("child", child)
        self.registry.register_tool("stop_parent", lambda input, ctx: ctx.cancel_agent(ctx.parent_id) and "stopped")
        self.spawn_tool(name="child")
        # main -> spawn -> child agent -> child tool -> grandchild agent -> stop_parent (cancels the child agent)
        self.script(call("spawn", id="s1"), call("child", id="k1"), call("stop_parent", id="p1"), text("main done"))
        self.app.run_turn("go")

        statuses = {a.name: a.status for a in self.ctx.agents()}
        self.assertEqual(statuses, {"main": "done", "child": "interrupted", "grandchild": "interrupted"})
        self.assertFalse(self.app.cancelled.is_set())

    def test_cancel_agent_from_another_thread_stops_a_blocked_agent(self):
        started = threading.Event()

        def wait(input, ctx):
            started.set()
            deadline = time.monotonic() + 5
            while not ctx.cancelled and time.monotonic() < deadline:
                time.sleep(0.01)
            return "cancelled" if ctx.cancelled else "timed out"

        self.registry.register_tool("wait", wait)
        self.spawn_tool(name="slow")
        self.script(call("spawn", id="s1"), call("wait", id="w1"), text("main done"))
        worker = threading.Thread(target=self.app.run_turn, args=("go",))
        worker.start()
        self.assertTrue(started.wait(5))
        (child,) = [a for a in self.ctx.agents() if a.name == "slow"]
        self.assertEqual(child.status, "running")
        self.assertTrue(self.ctx.cancel_agent(child.id))
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.results[0].status, "interrupted")
        self.assertEqual(self.ctx.agents()[0].status, "done")

    def test_cancel_agent_is_false_for_unknown_and_finished_agents(self):
        self.script(text("x"))
        result = self.ctx.run_agent_result("go")
        self.assertFalse(self.ctx.cancel_agent(result.agent_id))
        self.assertFalse(self.ctx.cancel_agent("nobody"))
        self.assertFalse(self.ctx.cancel_agent("main"))  # no turn is running

    def test_an_agent_started_after_the_users_interrupt_is_interrupted_at_once(self):
        self.script(text("never asked"))
        self.app.cancelled.set()
        result = self.ctx.run_agent_result("go")
        self.assertEqual(result.status, "interrupted")
        self.assertEqual(self.app.provider.requests, [])


class RunAgentOptionsTest(AgentTest):
    def setUp(self):
        super().setUp()
        for name in ("a", "b", "c"):
            self.registry.register_tool(name, lambda input, ctx, name=name: f"{name} ran")

    def test_tools_is_an_allow_list_and_other_calls_fail(self):
        self.script(call("b", id="b1"), text("done"))
        result = self.ctx.run_agent_result("go", tools=["a"])
        self.assertEqual(self.app.provider.offered(0), ["a"])
        blocked = result.messages[2]["content"][0]
        self.assertTrue(blocked["is_error"])
        self.assertIn("b is not available to this agent", blocked["content"])

    def test_an_empty_allow_list_offers_no_tool(self):
        self.script(text("done"))
        self.ctx.run_agent_result("go", tools=[])
        self.assertEqual(self.app.provider.offered(), [])

    def test_unknown_names_in_tools_and_conflicting_arguments_raise_value_error(self):
        self.script(text("never"))
        with self.assertRaisesRegex(ValueError, "unknown tool.*nope"):
            self.ctx.run_agent_result("go", tools=["a", "nope"])
        with self.assertRaisesRegex(ValueError, "cannot be used together"):
            self.ctx.run_agent_result("go", tools=["a"], exclude_tools=["b"])
        with self.assertRaisesRegex(ValueError, "not a string"):
            self.ctx.run_agent("go", tools="a")
        with self.assertRaisesRegex(ValueError, "max_turns"):
            self.ctx.run_agent_result("go", max_turns=0)
        self.assertEqual(self.ctx.agents(), [])  # nothing started
        self.assertEqual(self.app.provider.requests, [])

    def test_exclude_tools_still_hides_tools_as_before(self):
        self.script(call("b", id="b1"), text("done"))
        result = self.ctx.run_agent_result("go", exclude_tools={"b"})
        self.assertNotIn("b", self.app.provider.offered(0))
        self.assertIn("unknown tool: b", result.messages[2]["content"][0]["content"])

    def test_system_replaces_the_prompt_and_system_note_is_still_appended(self):
        self.script(text("one"), text("two"))
        self.ctx.run_agent_result("go", system="You are a reviewer.", system_note="Be brief.")
        self.assertEqual(self.app.provider.requests[0]["system"], "You are a reviewer.\n\nBe brief.")
        self.ctx.run_agent_result("go", system_note="Extra.")
        self.assertEqual(self.app.provider.requests[1]["system"], self.ctx.system_prompt() + "\n\nExtra.")

    def test_model_applies_to_this_agent_only(self):
        self.script(text("x"))
        self.ctx.run_agent_result("go", model="small")
        self.assertEqual(self.app.provider.requests[0]["model"], "small")
        self.assertEqual((self.app.provider_name, self.app.model), ("fake", "m"))
        self.assertEqual(self.ctx.agents()[0].model, "small")

    def test_provider_uses_another_provider_for_this_agent_only(self):
        other = ScriptedProvider([text("from other")])
        other.name = "other"
        self.registry.add_provider(ProviderSpec(name="other", create=lambda: other, default_model="other-model"))
        main = self.script(text("from main"))
        result = self.ctx.run_agent_result("go", provider="other")
        self.assertEqual((result.text, other.requests[0]["model"]), ("from other", "other-model"))
        self.assertEqual(main.requests, [])
        self.assertIs(self.app.provider, main)
        self.assertEqual((self.app.provider_name, self.app.model), ("fake", "m"))
        other.replies.append(text("again"))
        self.ctx.run_agent_result("go", provider="other", model="o2")
        self.assertEqual(other.requests[1]["model"], "o2")
        agent = self.ctx.agents()[0]
        self.assertEqual((agent.provider, agent.model), ("other", "other-model"))
        with self.assertRaisesRegex(ValueError, "unknown provider: nowhere"):
            self.ctx.run_agent_result("go", provider="nowhere")

    def test_a_provider_error_handler_switching_provider_does_not_change_the_apps(self):
        other = ScriptedProvider([text("rescued")])
        other.name = "other"
        self.registry.add_provider(ProviderSpec(name="other", create=lambda: other, default_model="o"))
        self.registry.on("provider_error", lambda e: {"provider": "other"})
        self.script(RuntimeError("overloaded"))
        result = self.ctx.run_agent_result("go", model="small")
        self.assertEqual(result.text, "rescued")
        self.assertEqual((self.app.provider_name, self.app.model), ("fake", "m"))

    def test_max_turns_is_passed_to_the_loop_and_default_is_unchanged(self):
        self.script(call("a", id="1"), call("a", id="2"), call("a", id="3"), text("done"))
        self.assertEqual(self.ctx.run_agent_result("go", max_turns=4).status, "done")

    def test_old_signature_still_returns_the_text(self):
        self.script(text("plain"))
        self.assertEqual(self.ctx.run_agent("go", system_note="n", exclude_tools=("a",)), "plain")


class ModeTest(AgentTest):
    """Tools ``a`` (reads), ``b`` (reads) and ``w`` (writes: asks for confirmation) and these modes."""

    def setUp(self):
        super().setUp()
        self.ran = []
        for name, confirm in (("a", False), ("b", False), ("w", True)):
            self.registry.register_tool(
                name, lambda input, ctx, name=name: self.ran.append(name) or f"{name} ran", confirm=confirm
            )
        self.registry.register_mode("only_a", description="just a", tools=["a"], system_note="Mode note A.")
        self.registry.register_mode("no_b", description="all but b", exclude_tools=["b"])
        self.registry.register_mode("plan", description="read only", read_only=True, system_note="Plan note.")
        self.changes = []
        self.registry.on("mode_change", lambda e: self.changes.append((e["old"], e["new"])))


class ModeStateTest(ModeTest):
    def test_there_is_no_mode_by_default_and_modes_lists_descriptions_in_order(self):
        self.assertIsNone(self.ctx.mode)
        self.assertEqual(self.ctx.modes(), {"only_a": "just a", "no_b": "all but b", "plan": "read only"})
        self.assertEqual(list(self.ctx.modes()), ["only_a", "no_b", "plan"])

    def test_mode_info_returns_the_registration_of_a_mode(self):
        self.assertIsNone(self.ctx.mode_info())
        self.assertEqual(self.ctx.mode_info("only_a").tools, ("a",))
        self.assertIsNone(self.ctx.mode_info("nope"))
        self.ctx.set_mode("plan")
        info = self.ctx.mode_info()
        self.assertEqual((info.name, info.read_only, info.system_note), ("plan", True, "Plan note."))

    def test_set_mode_validates_and_fires_mode_change_only_on_a_change(self):
        self.ctx.set_mode("only_a")
        self.ctx.set_mode("only_a")
        self.ctx.set_mode(None)
        with self.assertRaisesRegex(ValueError, "no mode 'nope'.*only_a, no_b, plan"):
            self.ctx.set_mode("nope")
        self.assertEqual(self.changes, [(None, "only_a"), ("only_a", None)])
        self.assertIsNone(self.ctx.mode)

    def test_cycle_mode_goes_through_the_modes_and_back_to_none(self):
        order = [self.ctx.cycle_mode() for _ in range(5)]
        self.assertEqual(order, ["only_a", "no_b", "plan", None, "only_a"])
        self.assertEqual(self.changes[:4], [(None, "only_a"), ("only_a", "no_b"), ("no_b", "plan"), ("plan", None)])

    def test_cycle_mode_with_no_modes_registered_does_nothing(self):
        self.registry.modes.clear()
        self.assertIsNone(self.ctx.cycle_mode())
        self.assertEqual(self.changes, [])

    def test_register_mode_validates_its_arguments(self):
        register = self.registry.register_mode
        for bad in ("", "Has Space", "UPPER", "off", "-x"):
            with self.assertRaisesRegex(ValueError, "register_mode: name"):
                register(bad, description="d")
        with self.assertRaisesRegex(ValueError, "collection of tool names"):
            register("x", description="d", tools="read")
        with self.assertRaisesRegex(ValueError, "collection of tool names"):
            register("x", description="d", exclude_tools=[1])
        with self.assertRaisesRegex(ValueError, "read_only"):
            register("x", description="d", read_only="yes")
        register("x", description="d", tools=(t for t in ["a"]))  # any iterable of names
        self.assertEqual(self.registry.modes["x"].tools, ("a",))

    def test_registering_a_mode_again_replaces_it_in_place(self):
        self.registry.register_mode("no_b", description="changed")
        self.assertEqual(self.ctx.modes()["no_b"], "changed")
        self.assertEqual(list(self.ctx.modes()), ["only_a", "no_b", "plan"])

    def test_host_exposes_modes_and_the_status_shows_the_current_one(self):
        host = Host(self.app)
        self.assertEqual(host.modes(), self.ctx.modes())
        self.assertIsNone(host.status().mode)
        self.assertEqual(host.cycle_mode(), "only_a")
        self.assertEqual((host.mode, host.status().mode), ("only_a", "only_a"))
        host.set_mode(None)
        self.assertIsNone(host.mode)
        with self.assertRaises(ValueError):
            host.set_mode("nope")


class ModeEnforcementTest(ModeTest):
    def test_a_mode_hides_blocked_tools_from_the_model(self):
        self.ctx.set_mode("only_a")
        self.script(text("ok"))
        self.app.run_turn("go")
        self.assertEqual(self.app.provider.offered(), ["a"])
        self.ctx.set_mode("no_b")
        self.script(text("ok"))
        self.app.run_turn("again")
        self.assertNotIn("b", self.app.provider.offered())
        self.assertIn("a", self.app.provider.offered())
        self.assertIn("bash", self.app.provider.offered())

    def test_a_call_to_a_blocked_tool_is_an_error_result_naming_the_mode_and_how_to_switch(self):
        guard_calls = []
        self.registry.on("tool_call", lambda e: guard_calls.append(e["name"]))
        self.ctx.set_mode("only_a")
        self.script(call("b", id="b1"), call("a", id="a1"), text("ok"))
        self.app.run_turn("go")
        blocked = self.app.messages[2]["content"][0]
        self.assertTrue(blocked["is_error"])
        self.assertIn("b is blocked", blocked["content"])
        self.assertIn("only_a mode", blocked["content"])
        self.assertIn("/mode <name>", blocked["content"])
        self.assertIn("only_a, no_b, plan", blocked["content"])
        self.assertEqual(self.ran, ["a"])  # b never ran; a, the next call, did
        self.assertEqual(guard_calls, ["a"])  # the refusal of b comes before the tool_call handlers

    def test_an_allowed_tool_still_runs_in_a_mode(self):
        self.ctx.set_mode("only_a")
        self.script(call("a", id="a1"), text("ok"))
        self.app.run_turn("go")
        self.assertEqual(self.ran, ["a"])
        self.assertFalse(self.app.messages[2]["content"][0]["is_error"])

    def test_exclude_tools_blocks_only_the_listed_tools(self):
        self.ctx.set_mode("no_b")
        self.script(call("b", id="b1"), call("a", id="a1"), text("ok"))
        self.app.run_turn("go")
        self.assertIn("b is blocked", self.app.messages[2]["content"][0]["content"])
        self.assertEqual(self.ran, ["a"])  # the second call of the script
        self.script(call("a", id="a2"), text("ok"))
        self.app.run_turn("again")
        self.assertEqual(self.ran, ["a", "a"])

    def test_read_only_blocks_tools_that_ask_for_confirmation_unless_the_mode_lists_them(self):
        self.ctx.set_mode("plan")
        self.script(text("ok"))
        self.app.run_turn("go")
        offered = self.app.provider.offered()
        self.assertNotIn("w", offered)
        self.assertNotIn("bash", offered)
        self.assertNotIn("write", offered)
        self.assertIn("read", offered)
        self.assertIn("a", offered)
        self.script(call("w", id="w1"), text("ok"))
        self.app.run_turn("write")
        self.assertIn("read-only", self.app.messages[-2]["content"][0]["content"])
        self.assertEqual(self.ran, [])
        self.assertIn("grep", offered)  # tools with a confirm(input, ctx) function are reading tools

        self.registry.register_mode("plan_w", description="plan that may use w", read_only=True, tools=["a", "w"])
        self.ctx.set_mode("plan_w")
        self.script(call("w", id="w2"), text("ok"))
        self.app.run_turn("write")
        self.assertEqual(self.ran, ["w"])

    def test_the_mode_note_is_appended_to_the_system_prompt(self):
        plain = self.ctx.system_prompt()
        self.ctx.set_mode("only_a")
        self.assertEqual(self.ctx.system_prompt(), plain + "\n\nMode note A.")
        self.script(text("ok"))
        self.app.run_turn("go")
        self.assertTrue(self.app.provider.requests[0]["system"].endswith("\n\nMode note A."))
        self.ctx.set_mode("no_b")  # no note
        self.assertEqual(self.ctx.system_prompt(), plain)

    def test_a_mode_switched_in_the_middle_of_a_turn_applies_to_the_next_request(self):
        self.registry.register_tool("leave", lambda input, ctx: ctx.set_mode(None) or "left")
        self.ctx.set_mode("only_a")
        self.registry.register_mode("only_a", description="just a", tools=["a", "leave"], system_note="Note A.")
        self.script(call("leave", id="l1"), call("b", id="b1"), text("ok"))
        self.app.run_turn("go")
        self.assertEqual(self.app.provider.offered(0), ["a", "leave"])
        self.assertTrue(self.app.provider.requests[0]["system"].endswith("Note A."))
        self.assertIn("b", self.app.provider.offered(1))
        self.assertNotIn("Note A.", self.app.provider.requests[1]["system"])
        self.assertEqual(self.ran, ["b"])
        self.assertEqual(self.changes, [(None, "only_a"), ("only_a", None)])

    def test_agents_inherit_the_mode(self):
        self.ctx.set_mode("only_a")
        self.script(call("b", id="b1"), text("done"))
        result = self.ctx.run_agent_result("go")
        self.assertEqual(self.app.provider.offered(0), ["a"])
        self.assertIn("b is blocked", result.messages[2]["content"][0]["content"])
        self.assertTrue(self.app.provider.requests[0]["system"].endswith("Mode note A."))

    def test_tools_cannot_bypass_a_mode(self):
        self.ctx.set_mode("only_a")
        self.script(call("b", id="b1"), text("done"))
        result = self.ctx.run_agent_result("go", tools=["a", "b"])  # b is registered but the mode excludes it
        self.assertEqual(self.app.provider.offered(0), ["a"])
        self.assertIn("b is blocked", result.messages[2]["content"][0]["content"])
        self.assertEqual(self.ran, [])

        self.script(text("done"))
        self.ctx.run_agent_result("go", tools=["b"])  # nothing left of the allow-list
        self.assertEqual(self.app.provider.offered(), [])

    def test_system_replacement_keeps_the_restriction_and_the_note(self):
        self.ctx.set_mode("only_a")
        self.script(text("done"))
        self.ctx.run_agent_result("go", system="Custom.", system_note="Extra.")
        self.assertEqual(self.app.provider.requests[0]["system"], "Custom.\n\nExtra.\n\nMode note A.")

    def test_complete_offers_the_same_tools_as_a_turn(self):
        self.ctx.set_mode("only_a")
        self.script(text("ok"))
        self.ctx.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(self.app.provider.offered(), ["a"])

    def test_a_mode_that_is_not_registered_blocks_everything(self):
        self.app.mode = "gone"  # what a reload in progress looks like
        self.assertIn("not registered", self.app.mode_block_reason("a"))


class ModeReloadTest(ModeTest):
    def write_mode_extension(self, present=True):
        path = self.tmp / "home" / "extensions" / "mymodes.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = 'minimal.register_mode("review", description="review", tools=["a"])' if present else "pass"
        path.write_text('__api__ = "2.2"\ndef register(minimal):\n    ' + body + "\n")

    def test_the_mode_survives_a_reload_that_registers_it_again(self):
        self.write_mode_extension()
        self.app.reload_extensions()
        self.ctx.set_mode("review")
        self.app.reload_extensions()
        self.assertEqual(self.ctx.mode, "review")
        self.assertEqual(self.ctx.modes(), {"review": "review"})

    def test_a_mode_that_disappeared_falls_back_to_none_with_a_notice(self):
        self.write_mode_extension()
        self.app.reload_extensions()
        self.ctx.set_mode("review")
        self.write_mode_extension(present=False)
        self.app.reload_extensions()
        self.assertIsNone(self.ctx.mode)
        message, kind = self.frontend.notify.call_args.args
        self.assertIn("'review' is no longer registered", message)
        self.assertEqual(kind, "warning")

    def test_mode_change_fires_for_the_fallback_to_none(self):
        self.write_mode_extension()
        listener = self.tmp / "home" / "extensions" / "listen.py"
        listener.write_text(
            textwrap.dedent(
                """
                __api__ = "2.2"
                seen = []
                def register(minimal):
                    minimal.on("mode_change", lambda e: seen.append((e["old"], e["new"])))
                    minimal.register_tool("seen", lambda input, ctx: repr(seen))
                """
            )
        )
        self.app.reload_extensions()
        self.ctx.set_mode("review")
        self.write_mode_extension(present=False)
        self.app.reload_extensions()
        # the reload imports the listener again: its fresh handler sees the fallback the reload itself causes
        self.assertEqual(self.app.registry.tools["seen"].run({}, self.ctx), "[('review', None)]")


class ModeCommandTest(ModeTest):
    def run_command(self, line):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.app.run_command(line)
        return out.getvalue(), err.getvalue()

    def test_mode_without_arguments_lists_the_modes_and_marks_the_current_one(self):
        out, _ = self.run_command("/mode")
        self.assertIn("Mode: none", out)
        for name in ("only_a", "no_b", "plan"):
            self.assertIn(name, out)
        self.ctx.set_mode("no_b")
        out, _ = self.run_command("/mode")
        self.assertIn("Mode: no_b", out)
        self.assertIn("* no_b", out)

    def test_mode_with_a_name_switches_and_off_clears(self):
        out, _ = self.run_command("/mode plan")
        self.assertEqual((out.strip(), self.ctx.mode), ("Mode: plan", "plan"))
        out, _ = self.run_command("/mode off")
        self.assertEqual((out.strip(), self.ctx.mode), ("Mode: none", None))

    def test_unknown_mode_is_reported_and_changes_nothing(self):
        self.ctx.set_mode("plan")
        _, err = self.run_command("/mode nope")
        self.assertIn("Error in /mode: no mode 'nope'", err)
        self.assertEqual(self.ctx.mode, "plan")

    def test_mode_without_registered_modes_says_so(self):
        self.registry.modes.clear()
        out, _ = self.run_command("/mode")
        self.assertIn("No modes registered", out)
        self.assertIn("mode", self.ctx.commands())

    def test_mode_is_a_builtin_command(self):
        self.assertIn("mode", self.registry.builtin_commands)


if __name__ == "__main__":
    unittest.main()
