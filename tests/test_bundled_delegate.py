import json
from unittest import mock
from types import SimpleNamespace

from minimal.core.app import App
from minimal.core.session import Session
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply
from tests.test_bundled_extensions import MARKETPLACE, BundledExtensionTest, RecordingProvider, text_reply

TOOL_ID = "tools/delegate"


def use(name, input=None, id="c1", say=None):
    """A reply that calls tool ``name`` (after saying ``say``, if given)."""
    content = [{"type": "text", "text": say}] if say else []
    content.append({"type": "tool_use", "id": id, "name": name, "input": input or {}})
    return Reply(content=content, stop_reason="tool_use")


class ScriptedProvider(RecordingProvider):
    """RecordingProvider that also keeps each request's system prompt and raises a scripted exception."""

    def __init__(self, replies):
        super().__init__(replies)
        self.systems = []
        self.efforts = []

    def stream(self, **kw):
        self.systems.append(kw["system"])
        self.efforts.append(kw.get("options", {}).get("reasoning_effort"))
        if isinstance(self.replies[0], Exception):
            self.requests.append(SimpleNamespace(messages=list(kw["messages"]), tools=[t["name"] for t in kw["tools"]]))
            raise self.replies.pop(0)
        return super().stream(**kw)


class DelegateTest(BundledExtensionTest):
    def make(self, replies, *extensions, settings=None, errors=()):
        """An ``App`` with the delegate (and ``extensions``) loaded and tools ``echo`` and ``other`` registered."""
        if settings is not None:
            (self.tmp / "settings.json").write_text(json.dumps({"extensions": {"settings": {TOOL_ID: settings}}}))
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            trust_project=True,
            extra_extensions=[str(MARKETPLACE / "tools" / f"{name}.py") for name in ("delegate", *extensions)],
        )
        _, found = app.reload_extensions()
        self.assertEqual(found, list(errors))
        app.provider, app.model, app.provider_name = ScriptedProvider(replies), "m", "fake"
        app.registry.register_tool("echo", lambda input, ctx: "echoed")
        app.registry.register_tool("other", lambda input, ctx: "other ran")
        return app

    def tool_result(self, app, index=2):
        """The first tool result in the main history's message ``index``: ``(content, is_error)``."""
        block = app.messages[index]["content"][0]
        return block["content"], block.get("is_error", False)


class DelegateRunTest(DelegateTest):
    def test_the_delegate_starts_empty_returns_only_its_final_text_and_is_named_by_its_role(self):
        app = self.make(
            [
                use("delegate", {"task": "find x", "role": "scout"}),
                use("echo", id="e1"),
                text_reply("found x in a.py"),
                text_reply("main done"),
            ]
        )
        self.run_turn(app, "go")
        outer, first, inner, final = app.provider.requests
        self.assertEqual(self.tool_result(app), ("found x in a.py", False))
        self.assertEqual(first.messages, [{"role": "user", "content": "find x"}])
        self.assertEqual(inner.messages[-1]["content"][0]["content"], "echoed")
        self.assertNotIn("delegate", first.tools)
        self.assertIn("echo", first.tools)
        self.assertIn("delegate", outer.tools)
        self.assertIn("delegate", final.tools)
        self.assertIn("role: scout", app.provider.systems[1])
        (child,) = [a for a in app.context.agents() if a.id != "main"]
        self.assertEqual((child.name, child.parent_id, child.status, child.tool_calls), ("scout", "main", "done", 1))

    def test_the_role_defaults_to_delegate_and_an_empty_reply_says_so(self):
        app = self.make([use("delegate", {"task": "t"}), text_reply(""), text_reply("ok")])
        self.run_turn(app, "go")
        self.assertEqual(self.tool_result(app), ("(the delegate returned no text)", False))
        child = [a for a in app.context.agents() if a.id != "main"][0]
        self.assertEqual(child.name, "delegate")
        self.assertNotIn("role:", app.provider.systems[1])

    def test_model_is_passed_to_the_delegate_only(self):
        app = self.make([use("delegate", {"task": "t", "model": "small"}), text_reply("r"), text_reply("ok")])
        self.run_turn(app, "go")
        (child,) = [a for a in app.context.agents() if a.id != "main"]
        self.assertEqual((child.model, app.model), ("small", "m"))

    def test_an_empty_task_is_an_error_without_starting_an_agent(self):
        app = self.make([use("delegate", {"task": "  "}), text_reply("ok")])
        self.run_turn(app, "go")
        content, is_error = self.tool_result(app)
        self.assertTrue(is_error)
        self.assertIn("task must be a non-empty string", content)
        self.assertEqual([a.id for a in app.context.agents()], ["main"])

    def test_cwd_runs_the_delegate_in_a_folder_of_the_project_and_defaults_to_the_projects_own(self):
        (self.tmp / "sub" / "deep").mkdir(parents=True)
        app = self.make(
            [
                use("delegate", {"task": "t", "cwd": "sub/deep"}),
                use("where", id="w1"),
                text_reply("r1"),
                use("delegate", {"task": "t"}, id="c2"),
                use("where", id="w2"),
                text_reply("r2"),
                text_reply("ok"),
            ]
        )
        app.registry.register_tool("where", lambda input, ctx: ctx.cwd)
        self.run_turn(app, "go")
        results = [
            block["content"]
            for message in app.provider.requests[2].messages + app.provider.requests[5].messages
            if isinstance(message["content"], list)
            for block in message["content"]
            if block["type"] == "tool_result" and block["tool_use_id"] in ("w1", "w2")
        ]
        self.assertEqual(results, [str((self.tmp / "sub" / "deep").resolve()), str(self.tmp)])
        self.assertEqual(app.context.cwd, str(self.tmp))  # the caller's own folder is untouched

    def test_cwd_must_exist_and_stay_inside_the_project(self):
        (self.tmp / "sub").mkdir()
        app = self.make([text_reply("unused")])
        tool = app.registry.tools["delegate"]
        for value, message in (
            ("nowhere", "is not an existing folder of the project"),
            ("..", "is outside the project"),
            ("/", "is outside the project"),
            (3, "cwd must be a folder of the project"),
        ):
            with self.subTest(cwd=value):
                with self.assertRaisesRegex(ValueError, message):
                    tool.run({"task": "t", "cwd": value}, app.context)
        self.assertEqual(app.provider.requests, [])  # no agent was started

    def test_it_registers_no_before_request_handler_any_more(self):
        app = self.make([text_reply("unused")])
        self.assertFalse(app.registry._handlers.get("before_request"))
        self.assertIn("cwd", app.registry.tools["delegate"].input_schema["properties"])


class DelegateToolsTest(DelegateTest):
    def test_the_allow_list_is_the_only_tools_the_delegate_is_offered(self):
        app = self.make([use("delegate", {"task": "t", "tools": ["echo", "echo"]}), text_reply("r"), text_reply("ok")])
        self.run_turn(app, "go")
        _, inner, _ = app.provider.requests
        self.assertEqual(inner.tools, ["echo"])

    def test_a_tool_outside_the_allow_list_is_refused_when_the_delegate_calls_it(self):
        app = self.make(
            [
                use("delegate", {"task": "t", "tools": ["echo"]}),
                use("other", id="o1"),
                text_reply("r"),
                text_reply("ok"),
            ]
        )
        self.run_turn(app, "go")
        _, _, after, _ = app.provider.requests
        result = after.messages[-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("other is not available to this agent", result["content"])

    def test_an_unknown_tool_lists_the_valid_ones_and_starts_no_agent(self):
        app = self.make([use("delegate", {"task": "t", "tools": ["echo", "nope"]}), text_reply("ok")])
        self.run_turn(app, "go")
        content, is_error = self.tool_result(app)
        self.assertTrue(is_error)
        self.assertIn("unknown tool(s): nope", content)
        self.assertIn("Valid tools:", content)
        self.assertIn("echo", content.split("Valid tools:")[1])
        self.assertEqual(len(app.provider.requests), 2)  # main, then main again: no delegate request
        self.assertEqual([a.id for a in app.context.agents()], ["main"])

    def test_tools_must_be_an_array_of_names(self):
        app = self.make([text_reply("unused")])
        run = app.registry.tools["delegate"].run
        for bad in ("echo", [1], {"echo": 1}):
            with self.assertRaisesRegex(ValueError, "tools must be an array of tool names"):
                run({"task": "t", "tools": bad}, app.context)

    def test_the_mode_restriction_applies_to_the_delegate_whatever_its_tools_say(self):
        app = self.make(
            [
                use("delegate", {"task": "t", "tools": ["echo", "other"]}),
                use("other", id="o1"),
                text_reply("r"),
                text_reply("ok"),
            ]
        )
        app.registry.register_mode("no_other", description="all but other", exclude_tools=["other"])
        app.context.set_mode("no_other")
        self.run_turn(app, "go")
        _, first, after, _ = app.provider.requests
        self.assertEqual(first.tools, ["echo"])
        result = after.messages[-1]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("other is blocked", result["content"])

    def test_a_read_only_mode_keeps_the_delegate_from_unlisted_tools(self):
        app = self.make([use("delegate", {"task": "t"}), text_reply("r"), text_reply("ok")])
        app.registry.register_mode("only_echo", description="echo only", tools=["echo", "delegate"])
        app.context.set_mode("only_echo")
        self.run_turn(app, "go")
        _, inner, _ = app.provider.requests
        self.assertEqual(inner.tools, ["echo"])


class DelegateFailureTest(DelegateTest):
    def test_running_out_of_turns_is_an_error_with_the_status_and_the_partial_text(self):
        app = self.make(
            [
                use("delegate", {"task": "t", "max_turns": 1}),
                use("echo", id="e1", say="half way there"),
                text_reply("main recovers"),
            ]
        )
        self.run_turn(app, "go")
        content, is_error = self.tool_result(app)
        self.assertTrue(is_error)
        self.assertIn("status: max_turns", content)
        self.assertIn("exceeded the maximum of 1 turns", content)
        self.assertIn("Its last message:\nhalf way there", content)
        self.assertEqual(len(app.provider.requests), 3)  # the delegate made exactly one request
        self.assertEqual(app.messages[-1]["content"][0]["text"], "main recovers")
        child = [a for a in app.context.agents() if a.id != "main"][0]
        self.assertEqual(child.status, "max_turns")

    def test_a_failing_request_is_an_error_with_the_status_and_the_reason(self):
        app = self.make([use("delegate", {"task": "t"}), RuntimeError("provider down"), text_reply("main goes on")])
        self.run_turn(app, "go")
        content, is_error = self.tool_result(app)
        self.assertTrue(is_error)
        self.assertIn("The delegate failed (status: error: provider down).", content)

    def test_a_delegate_stopped_with_cancel_agent_is_an_error_but_the_turn_goes_on(self):
        def stop(input, ctx):
            ctx.cancel_agent(ctx.agent_id)
            return "stopping"

        app = self.make(
            [use("delegate", {"task": "t"}), use("stop", id="s1", say="partial"), text_reply("main goes on")]
        )
        app.registry.register_tool("stop", stop)
        self.run_turn(app, "go")
        content, is_error = self.tool_result(app)
        self.assertTrue(is_error)
        self.assertIn("was interrupted before it finished (status: interrupted)", content)
        self.assertIn("partial", content)
        self.assertEqual(app.messages[-1]["content"][0]["text"], "main goes on")

    def test_max_turns_must_be_an_integer_from_1_to_50(self):
        app = self.make([text_reply("unused")])
        run = app.registry.tools["delegate"].run
        for bad in (0, 51, -1, True, 2.5, "3"):
            with self.assertRaisesRegex(ValueError, "max_turns must be an integer from 1 to 50"):
                run({"task": "t", "max_turns": bad}, app.context)
        self.assertEqual([a.id for a in app.context.agents()], [])
        self.assertEqual(app.provider.requests, [])


class DelegateDepthTest(DelegateTest):
    def test_by_default_delegates_cannot_delegate_and_calling_it_anyway_is_refused(self):
        seen = []

        def probe(input, ctx):
            try:
                app.registry.tools["delegate"].run({"task": "deeper"}, ctx)
            except ValueError as e:
                seen.append(str(e))
            return "probed"

        app = self.make([use("delegate", {"task": "t"}), use("probe", id="p1"), text_reply("r"), text_reply("ok")])
        app.registry.register_tool("probe", probe)
        self.run_turn(app, "go")
        _, inner, _, _ = app.provider.requests
        self.assertNotIn("delegate", inner.tools)
        self.assertEqual(len(seen), 1)
        self.assertIn("at most 1 level(s) deep", seen[0])
        self.assertEqual(len(app.context.agents()), 2)  # main and the first delegate only

    def test_max_depth_allows_nesting_down_to_the_limit_and_no_further(self):
        app = self.make(
            [
                use("delegate", {"task": "outer", "role": "a"}),  # main
                use("delegate", {"task": "inner", "role": "b"}),  # a
                text_reply("b done"),  # b
                text_reply("a done"),  # a
                text_reply("main done"),  # main
            ],
            settings={"max_depth": 2},
        )
        self.run_turn(app, "go")
        _, a, b, a_again, _ = app.provider.requests
        self.assertIn("delegate", a.tools)
        self.assertNotIn("delegate", b.tools)
        self.assertEqual(self.tool_result(app), ("a done", False))
        self.assertEqual(a_again.messages[-1]["content"][0]["content"], "b done")
        by_name = {info.name: info for info in app.context.agents()}
        self.assertEqual((by_name["a"].parent_id, by_name["b"].parent_id), ("main", by_name["a"].id))

    def test_the_allow_list_loses_delegate_only_at_the_depth_limit(self):
        app = self.make(
            [
                use("delegate", {"task": "outer", "tools": ["echo", "delegate"]}),
                use("delegate", {"task": "inner", "tools": ["echo", "delegate"]}),
                text_reply("b done"),
                text_reply("a done"),
                text_reply("main done"),
            ],
            settings={"max_depth": 2},
        )
        self.run_turn(app, "go")
        _, a, b, _, _ = app.provider.requests
        self.assertEqual((sorted(a.tools), b.tools), (["delegate", "echo"], ["echo"]))

    def test_a_depth_below_one_is_a_load_error_that_keeps_the_default(self):
        app = self.make([text_reply("unused")], settings={"max_depth": 0}, errors=[mock.ANY])
        _, errors = app.reload_extensions()
        self.assertIn("tools/delegate.max_depth must be at least 1", errors[0])
        self.assertEqual(app.registry.tools["delegate"].run({"task": "t"}, app.context), "unused")


class DelegateSettingsTest(DelegateTest):
    def test_default_max_turns_applies_when_the_model_passes_none_and_the_model_may_override_it(self):
        app = self.make(
            [use("delegate", {"task": "t"}), use("echo", id="e1"), text_reply("main goes on")],
            settings={"default_max_turns": 1},
        )
        self.run_turn(app, "go")
        content, is_error = self.tool_result(app)
        self.assertTrue(is_error)
        self.assertIn("status: max_turns", content)
        self.assertEqual(len(app.provider.requests), 3)

        app = self.make(
            [
                use("delegate", {"task": "t", "max_turns": 2}),
                use("echo", id="e1"),
                text_reply("done in two"),
                text_reply("main goes on"),
            ],
            settings={"default_max_turns": 1},
        )
        self.run_turn(app, "go")
        self.assertEqual(self.tool_result(app), ("done in two", False))

    def test_without_the_setting_the_session_limit_applies(self):
        (self.tmp / "settings.json").write_text(json.dumps({"agent": {"max_turns": 1}}))
        app = self.make([use("echo"), text_reply("never")])
        with self.assertRaisesRegex(RuntimeError, "status: max_turns"):
            app.registry.tools["delegate"].run({"task": "t"}, app.context)
        self.assertEqual(len(app.provider.requests), 1)

    def test_max_turns_limit_bounds_what_the_model_may_ask_and_shows_in_the_schema(self):
        app = self.make([text_reply("ok")], settings={"max_turns_limit": 5})
        tool = app.registry.tools["delegate"]
        self.assertEqual(tool.input_schema["properties"]["max_turns"]["maximum"], 5)
        with self.assertRaisesRegex(ValueError, "max_turns must be an integer from 1 to 5 .setting max_turns_limit"):
            tool.run({"task": "t", "max_turns": 6}, app.context)
        self.assertEqual(tool.run({"task": "t", "max_turns": 5}, app.context), "ok")

    def test_reasoning_effort_reaches_only_the_delegates_requests(self):
        app = self.make(
            [use("delegate", {"task": "t"}), text_reply("found"), text_reply("main done")],
            settings={"reasoning_effort": "low"},
        )
        self.run_turn(app, "go")
        self.assertEqual(app.provider.efforts, [None, "low", None])

    def test_the_model_can_pick_the_effort_per_call_and_a_bad_one_is_refused(self):
        app = self.make(
            [use("delegate", {"task": "t", "reasoning_effort": "high"}), text_reply("found"), text_reply("done")],
            settings={"reasoning_effort": "low"},
        )
        self.run_turn(app, "go")
        self.assertEqual(app.provider.efforts, [None, "high", None])
        with self.assertRaisesRegex(ValueError, "reasoning_effort must be one of: low, medium, high"):
            app.registry.tools["delegate"].run({"task": "t", "reasoning_effort": "max"}, app.context)

    def test_a_bad_effort_setting_is_a_load_error_naming_the_choices(self):
        app = self.make([], settings={"reasoning_effort": "max"}, errors=[mock.ANY])
        _, errors = app.reload_extensions()
        self.assertIn("tools/delegate.reasoning_effort", errors[0])

    def test_progress_names_the_delegate_and_the_tool_it_runs_then_clears(self):
        app = self.make(
            [use("delegate", {"task": "t", "role": "scout"}), use("echo", id="e1"), text_reply("found"), text_reply("ok")]
        )
        notes = []
        app.frontend.progress = notes.append
        self.run_turn(app, "go")
        self.assertEqual(notes, ["scout: started", "scout: echo", None])

    def test_progress_can_be_turned_off(self):
        app = self.make([use("delegate", {"task": "t"}), text_reply("found"), text_reply("ok")], settings={"progress": False})
        notes = []
        app.frontend.progress = notes.append
        self.run_turn(app, "go")
        self.assertEqual(notes, [])

    def test_role_name_note_and_length_are_settings(self):
        app = self.make(
            [use("delegate", {"task": "t", "role": "a very long role"}), text_reply("found"), text_reply("ok")],
            settings={"default_role": "helper", "role_chars": 6, "note": "Be brief{role}."},
        )
        self.run_turn(app, "go")
        self.assertEqual(app.provider.systems[1].count("Be brief with the role: a very."), 1)
        self.assertEqual([a.name for a in app.context.agents() if a.id != "main"], ["a very"])
        app = self.make([use("delegate", {"task": "t"}), text_reply("found"), text_reply("ok")], settings={"default_role": "helper"})
        self.run_turn(app, "go")
        self.assertEqual([a.name for a in app.context.agents() if a.id != "main"], ["helper"])

    def test_defaults_are_unchanged(self):
        app = self.make([text_reply("x")])
        tool = app.registry.tools["delegate"]
        self.assertFalse(tool.read_only)
        self.assertEqual(tool.input_schema["properties"]["max_turns"]["maximum"], 50)
        app.registry.tools["delegate"].run({"task": "t"}, app.context)
        self.assertIn("You are a delegate started by another agent. Do the task", app.provider.systems[0])


class DelegateParallelTest(DelegateTest):
    def make_read_only(self, replies, **kw):
        app = self.make(replies, **kw)
        app.registry.register_tool("peek", lambda input, ctx: "peeked", read_only=True)
        return app

    def test_parallel_registers_the_tool_read_only_and_confines_delegates_to_read_only_tools(self):
        app = self.make_read_only(
            [use("delegate", {"task": "t"}), text_reply("found"), text_reply("ok")], settings={"parallel": True}
        )
        self.assertTrue(app.registry.tools["delegate"].read_only)
        self.run_turn(app, "go")
        _, child, _ = app.provider.requests
        self.assertEqual(child.tools, ["read", "grep", "glob", "peek"])  # read_only ones only: not echo, other

    def test_parallel_refuses_a_writing_tool_and_says_how_to_fix_it(self):
        app = self.make_read_only([text_reply("unused")], settings={"parallel": True})
        with self.assertRaisesRegex(ValueError, "echo is not one. Drop it from tools, or set parallel to false"):
            app.registry.tools["delegate"].run({"task": "t", "tools": ["peek", "echo"]}, app.context)
        self.assertEqual(app.provider.requests, [])

    def test_parallel_allows_an_explicit_read_only_list(self):
        app = self.make_read_only(
            [use("delegate", {"task": "t", "tools": ["peek"]}), text_reply("found"), text_reply("ok")],
            settings={"parallel": True},
        )
        self.run_turn(app, "go")
        self.assertEqual(app.provider.requests[1].tools, ["peek"])
