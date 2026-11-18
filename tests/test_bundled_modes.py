import json
import threading
import unittest
from unittest import mock

from minimal.core.app import App
from minimal.core.session import Session
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply

from tests.test_bundled_extensions import MARKETPLACE, BundledExtensionTest, RecordingProvider, text_reply

PLAN = "I read the code.\n\n1. Edit foo.py to add the flag.\n2. Update the tests.\n"
APPROVED = "The plan is approved. Execute it."
GATE_WAIT = 5


def tool_reply(name, input, id="c1"):
    return Reply(content=[{"type": "tool_use", "id": id, "name": name, "input": input}], stop_reason="tool_use")


class ModesTest(BundledExtensionTest):
    """The marketplace ``extensions/modes.py`` loaded into a real ``App`` whose frontend is a mock."""

    def setUp(self):
        super().setUp()
        self.frontend = mock.Mock()
        self.frontend.ask.return_value = True

    def make(self, replies=(), settings=None):
        if settings is not None:
            (self.tmp / "settings.json").write_text(json.dumps({"extensions": {"settings": {"modes": settings}}}))
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            trust_project=True,
            frontend=self.frontend,
            extra_extensions=[str(MARKETPLACE / "extensions" / "modes.py")],
        )
        _, errors = app.reload_extensions()
        self.assertEqual(errors, [])
        app.provider, app.model = RecordingProvider(replies), "m"
        self.changes = []
        app.registry.on("mode_change", lambda e: self.changes.append((e["old"], e["new"])))
        return app

    def propose_plan(self, app, text=PLAN):
        app.messages.extend([{"role": "user", "content": "plan it"}, {"role": "assistant", "content": text}])

    @staticmethod
    def wait_for_gate():
        """Join the gate's thread, if one is running."""
        for thread in threading.enumerate():
            if thread.name == "modes-gate":
                thread.join(GATE_WAIT)
                assert not thread.is_alive(), "the plan gate is still waiting"


class ModeRestrictionTest(ModesTest):
    def test_plan_is_read_only_for_every_tool_and_build_is_unrestricted(self):
        app = self.make()
        self.assertEqual(list(app.context.modes()), ["plan", "build"])
        plan, build = app.context.mode_info("plan"), app.context.mode_info("build")
        self.assertTrue(plan.read_only)
        self.assertIsNone(plan.tools)
        self.assertIn("numbered plan", plan.system_note)
        self.assertEqual((build.tools, build.exclude_tools, build.read_only), (None, (), False))
        self.assertIn("approved", build.system_note)

    def test_plan_offers_reads_and_a_later_tool_that_does_not_ask_but_not_writes(self):
        app = self.make([text_reply("ok")])
        app.registry.register_tool("ask_user", lambda input, ctx: "answer")
        app.registry.register_tool("deploy", lambda input, ctx: "deployed", confirm=True)
        app.context.set_mode("plan")
        self.run_turn(app, "go")
        offered = set(app.provider.requests[0].tools)
        self.assertLessEqual({"read", "grep", "glob", "ask_user"}, offered)
        self.assertTrue({"write", "edit", "bash", "deploy"}.isdisjoint(offered))
        self.assertIn("plan mode", app.system_prompt())

    def test_plan_refuses_write_edit_and_bash_calls_and_build_runs_them(self):
        target = self.tmp / "out.txt"
        calls = [
            ("write", {"path": str(target), "content": "x"}),
            ("edit", {"path": str(target), "old_text": "a", "new_text": "b"}),
            ("bash", {"command": "touch " + str(self.tmp / "ran")}),
        ]
        app = self.make()
        app.context.set_mode("plan")
        for index, (name, args) in enumerate(calls):
            app.provider.replies[:] = [tool_reply(name, args, f"c{index}"), text_reply("done")]
            self.run_turn(app, f"try {name}")
            result = app.messages[-2]["content"][0]
            self.assertTrue(result["is_error"], name)
            self.assertIn("plan mode", result["content"])
            self.assertIn("only the user can change the mode", result["content"])
        self.assertFalse(target.exists())
        self.assertFalse((self.tmp / "ran").exists())

        app.context.set_mode("build")
        app.provider.replies[:] = [tool_reply("write", calls[0][1], "w"), text_reply("done")]
        self.run_turn(app, "now write")
        self.assertFalse(app.messages[-2]["content"][0].get("is_error"))
        self.assertEqual(target.read_text(), "x")

    def test_plan_allows_reading(self):
        (self.tmp / "a.txt").write_text("hello")
        app = self.make([tool_reply("read", {"path": str(self.tmp / "a.txt")}), text_reply("done")])
        app.context.set_mode("plan")
        self.run_turn(app, "read it")
        result = app.messages[-2]["content"][0]
        self.assertFalse(result.get("is_error"))
        self.assertIn("hello", result["content"])


class PlanGateTest(ModesTest):
    def test_approving_a_plan_queues_the_execute_message(self):
        app = self.make()
        self.propose_plan(app)
        app.context.set_mode("plan")
        self.assertEqual(list(app.queued), [])
        app.context.set_mode("build")
        self.wait_for_gate()
        self.frontend.ask.assert_called_once()
        question = self.frontend.ask.call_args.args[0]
        self.assertIn("Approve", question)
        self.assertIn("Edit foo.py", question)
        self.assertEqual(list(app.queued), [APPROVED])
        self.assertEqual(app.mode, "build")

    def test_a_plan_in_content_blocks_counts_and_only_the_last_assistant_message_does(self):
        app = self.make()
        app.messages.extend(
            [
                {"role": "assistant", "content": [{"type": "text", "text": PLAN}]},
                {"role": "user", "content": "thanks"},
                {"role": "assistant", "content": [{"type": "text", "text": "Done, nothing else to plan."}]},
            ]
        )
        app.context.set_mode("plan")
        app.context.set_mode("build")
        self.wait_for_gate()
        self.frontend.ask.assert_not_called()
        self.assertEqual(list(app.queued), [])

    def test_a_plan_in_content_blocks_is_asked_about(self):
        app = self.make()
        app.messages.append({"role": "assistant", "content": [{"type": "text", "text": PLAN}]})
        app.context.set_mode("plan")
        app.context.set_mode("build")
        self.wait_for_gate()
        self.frontend.ask.assert_called_once()

    def test_denying_goes_back_to_plan_notifies_and_does_not_ask_again(self):
        self.frontend.ask.return_value = False
        app = self.make()
        self.propose_plan(app)
        app.context.set_mode("plan")
        app.context.set_mode("build")
        self.wait_for_gate()
        self.assertEqual(app.mode, "plan")
        self.assertEqual(list(app.queued), [])
        self.frontend.ask.assert_called_once()
        # The gate thread may report its switch before this recorder hears of the first one.
        self.assertCountEqual(self.changes, [(None, "plan"), ("plan", "build"), ("build", "plan")])
        message, kind = self.frontend.notify.call_args.args
        self.assertIn("not approved", message)
        self.assertEqual(kind, "info")

    def test_other_switches_never_ask(self):
        app = self.make()
        self.propose_plan(app)
        app.context.set_mode("build")  # from no mode
        app.context.set_mode("plan")
        app.context.set_mode(None)  # leaving plan for no mode
        self.wait_for_gate()
        self.frontend.ask.assert_not_called()
        self.assertEqual(list(app.queued), [])

    def test_a_second_switch_while_the_question_is_open_does_not_ask_again(self):
        release = threading.Event()
        asked = threading.Event()

        def slow_ask(question):
            asked.set()
            release.wait(GATE_WAIT)
            return True

        self.frontend.ask.side_effect = slow_ask
        app = self.make()
        self.propose_plan(app)
        app.context.set_mode("plan")
        app.context.set_mode("build")
        self.assertTrue(asked.wait(GATE_WAIT))
        app.context.set_mode("plan")
        app.context.set_mode("build")
        release.set()
        self.wait_for_gate()
        self.frontend.ask.assert_called_once()
        self.assertEqual(list(app.queued), [APPROVED])
        # The gate is free again once answered.
        self.frontend.ask.side_effect = None
        app.context.set_mode("plan")
        app.context.set_mode("build")
        self.wait_for_gate()
        self.assertEqual(self.frontend.ask.call_count, 2)

    def test_the_gate_asks_off_the_thread_that_switched_the_mode(self):
        asked_on = []
        self.frontend.ask.side_effect = lambda question: asked_on.append(threading.get_ident()) or True
        app = self.make()
        self.propose_plan(app)
        app.context.set_mode("plan")
        app.context.set_mode("build")
        self.wait_for_gate()
        self.assertEqual(len(asked_on), 1)
        self.assertNotEqual(asked_on[0], threading.get_ident())

    def test_nothing_is_queued_if_the_user_left_build_while_the_question_was_open(self):
        app = self.make()
        self.propose_plan(app)
        self.frontend.ask.side_effect = lambda question: app.context.set_mode(None) or True
        app.context.set_mode("plan")
        app.context.set_mode("build")
        self.wait_for_gate()
        self.assertEqual(list(app.queued), [])
        self.assertIsNone(app.mode)

    def test_a_failing_question_is_reported_and_the_switch_stands(self):
        self.frontend.ask.side_effect = RuntimeError("no terminal")
        app = self.make()
        self.propose_plan(app)
        app.context.set_mode("plan")
        app.context.set_mode("build")  # must not raise
        self.wait_for_gate()
        self.assertEqual(app.mode, "build")
        self.assertEqual(list(app.queued), [])
        message, kind = self.frontend.notify.call_args.args
        self.assertIn("plan gate failed (no terminal)", message)
        self.assertEqual(kind, "warning")
        # The lock was released: the gate works again.
        self.frontend.ask.side_effect = None
        app.context.set_mode("plan")
        app.context.set_mode("build")
        self.wait_for_gate()
        self.assertEqual(list(app.queued), [APPROVED])

    def test_a_handler_failure_is_reported_not_raised(self):
        app = self.make()
        self.propose_plan(app)
        app.context.set_mode("plan")
        with mock.patch("threading.Thread", side_effect=RuntimeError("cannot start thread")):
            app.context.set_mode("build")  # must not raise
        message, kind = self.frontend.notify.call_args.args
        self.assertIn("cannot start thread", message)
        self.assertEqual(kind, "warning")
        # The lock was released: the gate works again.
        app.context.set_mode("plan")
        app.context.set_mode("build")
        self.wait_for_gate()
        self.frontend.ask.assert_called_once()


class DefaultModeTest(ModesTest):
    def begin(self, settings, mode=None):
        app = self.make(settings=settings)
        if mode:
            app.context.set_mode(mode)
        self.changes.clear()
        app.begin()
        return app

    def test_default_plan_or_build_sets_the_mode_at_session_start(self):
        for wanted in ("plan", "build", " Plan "):
            with self.subTest(wanted):
                app = self.begin({"default": wanted})
                self.assertEqual(app.mode, wanted.strip().lower())

    def test_no_default_or_off_leaves_no_mode(self):
        for settings in (None, {"default": ""}, {"default": "off"}, {"default": "none"}):
            with self.subTest(settings):
                app = self.begin(settings)
                self.assertIsNone(app.mode)
        self.frontend.notify.assert_not_called()

    def test_default_does_not_override_a_mode_already_set(self):
        app = self.begin({"default": "plan"}, mode="build")
        self.assertEqual(app.mode, "build")
        self.assertEqual(self.changes, [])

    def test_a_bad_default_says_what_to_use_and_starts_with_no_mode(self):
        app = self.begin({"default": "review"})
        self.assertIsNone(app.mode)
        message, kind = self.frontend.notify.call_args.args
        self.assertIn("setting 'default' is 'review'", message)
        self.assertIn("'plan', 'build' or ''", message)
        self.assertIn("extensions.settings.modes", message)
        self.assertEqual(kind, "warning")

    def test_a_default_of_the_wrong_type_is_a_reload_error_and_keeps_no_mode(self):
        (self.tmp / "settings.json").write_text(json.dumps({"extensions": {"settings": {"modes": {"default": 3}}}}))
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            frontend=self.frontend,
            trust_project=True,
            extra_extensions=[str(MARKETPLACE / "extensions" / "modes.py")],
        )
        _, errors = app.reload_extensions()
        self.assertEqual(len(errors), 1)
        self.assertIn("setting 'default' must be str", errors[0])
        app.begin()
        self.assertIsNone(app.mode)


if __name__ == "__main__":
    unittest.main()
