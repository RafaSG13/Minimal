import contextlib
import io
import json
import threading
import unittest
from unittest import mock

from minimal.core.app import ConsoleFrontend
from minimal.core.host import Host
from minimal.extensions.registry import TurnInterrupted
from minimal.providers.base import Usage
from tests.test_agents import AgentTest, call, text

PAIR = [
    {"role": "user", "content": "first"},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "t", "input": {}}]},
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "out", "is_error": False}]},
    {"role": "assistant", "content": [{"type": "text", "text": "answer one"}]},
    {"role": "user", "content": "second"},
    {"role": "assistant", "content": [{"type": "text", "text": "answer two"}]},
]


class SessionEditTest(AgentTest):
    """``ctx.fork`` and ``ctx.truncate`` on a saved conversation with a tool call in it."""

    def setUp(self):
        super().setUp()
        self.app.messages.extend(json.loads(json.dumps(PAIR)))
        self.app.session.save()
        self.file = self.app.session.file

    def saved(self, file=None):
        records = [json.loads(line) for line in (file or self.file).read_text().splitlines()]
        return records[0], records[1:]

    def test_truncate_cuts_the_history_and_rewrites_the_file(self):
        dropped = self.ctx.truncate(4)
        self.assertEqual(dropped, 2)
        self.assertEqual(self.ctx.messages, PAIR[:4])
        header, saved = self.saved()
        self.assertEqual(saved, PAIR[:4])
        self.assertEqual(header["type"], "session")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith(".")), [])  # no temp files left
        self.app.messages.append({"role": "user", "content": "again"})
        self.app.session.save()
        self.assertEqual(self.saved()[1], [*PAIR[:4], {"role": "user", "content": "again"}])

    def test_truncate_keeps_the_title(self):
        self.app.session.set_title("my title")
        self.ctx.truncate(1)
        self.assertEqual(self.saved()[0]["title"], "my title")
        self.assertEqual(self.saved()[1], PAIR[:1])

    def test_truncate_never_splits_a_tool_call_from_its_result(self):
        with self.assertRaisesRegex(ValueError, "cannot cut at 2: .*cut at 1 .* or at 3"):
            self.ctx.truncate(2)
        self.assertEqual(self.ctx.messages, PAIR)
        self.assertEqual(self.saved()[1], PAIR)
        self.ctx.truncate(3)  # after the results
        self.ctx.truncate(1)  # before the call

    def test_truncate_rejects_an_index_out_of_range_and_leaves_the_session_alone(self):
        for index in (-1, 7, True, "2", None):
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "out of range: this session has 6 messages"):
                self.ctx.truncate(index)
        self.assertEqual(self.ctx.truncate(6), 0)
        self.assertEqual(self.saved()[1], PAIR)

    def test_truncate_handles_messages_that_were_not_saved_yet(self):
        self.app.messages.append({"role": "user", "content": "unsaved"})
        self.assertEqual(self.ctx.truncate(7), 0)
        self.assertEqual(self.saved()[1], PAIR)  # nothing dropped: nothing rewritten, the new message is still unsaved
        self.ctx.truncate(5)  # the cut leaves a saved message and drops the unsaved one
        self.assertEqual(self.saved()[1], PAIR[:5])
        self.app.messages.append({"role": "assistant", "content": [{"type": "text", "text": "x"}]})
        self.ctx.truncate(5)
        self.app.session.save()
        self.assertEqual(self.saved()[1], PAIR[:5])

    def test_fork_copies_the_first_messages_into_a_new_session_and_switches_to_it(self):
        switches = []
        self.registry.on("session_switch", lambda e: switches.append((e["old_id"], e["new_id"], len(e["ctx"].messages))))
        old = self.ctx.session_id
        new = self.ctx.fork(4)
        self.assertNotEqual(new, old)
        self.assertEqual(self.ctx.session_id, new)
        self.assertEqual(self.ctx.messages, PAIR[:4])
        self.assertEqual(switches, [(old, new, 4)])
        header, saved = self.saved(self.app.session.file)
        self.assertEqual((header["parent"], saved), (old, PAIR[:4]))
        self.assertEqual(self.saved()[1], PAIR)  # the old session stays as it was
        self.ctx.messages.append({"role": "user", "content": "other"})  # the copy shares nothing with the old one
        self.assertEqual(len(self.saved()[1]), 6)

    def test_fork_without_an_index_copies_everything(self):
        self.ctx.fork()
        self.assertEqual(self.ctx.messages, PAIR)
        self.assertIsNot(self.ctx.messages[0], PAIR[0])

    def test_fork_cannot_split_a_tool_call_and_does_not_switch(self):
        old = self.ctx.session_id
        with self.assertRaisesRegex(ValueError, "cannot cut at 2"):
            self.ctx.fork(2)
        with self.assertRaisesRegex(ValueError, "out of range"):
            self.ctx.fork(99)
        self.assertEqual(self.ctx.session_id, old)

    def test_neither_runs_while_a_turn_is_running(self):
        results = {}

        def cut(input, ctx):
            for name, act in (("truncate", lambda: ctx.truncate(0)), ("fork", lambda: ctx.fork())):
                try:
                    act()
                except ValueError as e:
                    results[name] = str(e)
            return "done"

        self.registry.register_tool(name="cut", description="", run=cut)
        self.script(call("cut"), text("ok"))
        self.app.run_turn("go")
        self.assertEqual(results, {name: f"cannot {what} while a turn is running" for name, what in (
            ("truncate", "rewind the session"), ("fork", "fork the session"))})
        self.assertEqual(self.ctx.messages[: len(PAIR)], PAIR)

    def test_rewind_and_fork_commands(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.app.run_command("/fork 1")
        self.assertEqual(self.ctx.messages, PAIR[:4])
        self.assertIn("with its first 4 messages", out.getvalue())
        self.assertIn("before your message 1 from the end: second", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.app.run_command("/rewind 1")
        self.assertEqual(self.ctx.messages, [])
        self.assertIn("dropped 4 messages", out.getvalue())
        self.assertIn("first", out.getvalue())

    def test_rewind_defaults_to_the_last_message_and_reports_a_bad_count(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.app.run_command("/rewind")
        self.assertEqual(self.ctx.messages, PAIR[:4])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.app.run_command("/rewind 3")
            self.app.run_command("/rewind x")
            self.app.run_command("/fork 0")
        self.assertIn("this session has only 1 of your message", err.getvalue())
        self.assertEqual(err.getvalue().count("usage: /"), 2)
        self.assertEqual(self.ctx.messages, PAIR[:4])


class CompleteAndUsageTest(AgentTest):
    def test_complete_text_is_one_request_without_tools_and_stays_out_of_the_history(self):
        provider = self.script(text("a title", Usage(10, 3, 4, 1)))
        self.assertEqual(self.ctx.complete_text("Name this", system="Be brief", max_tokens=32), "a title")
        request = provider.requests[0]
        self.assertEqual(request["messages"], [{"role": "user", "content": "Name this"}])
        self.assertEqual((request["system"], request["tools"], request["model"]), ("Be brief", [], "m"))
        self.assertEqual(request["options"], {"max_tokens": 32})
        self.assertEqual(self.ctx.messages, [])
        u = self.ctx.usage
        self.assertEqual((u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, u.requests), (10, 3, 4, 1, 1))

    def test_complete_text_defaults_to_no_system_prompt_and_can_use_another_model(self):
        provider = self.script(text("x"))
        self.ctx.complete_text("q", model="small")
        self.assertEqual((provider.requests[0]["system"], provider.requests[0]["model"]), ("", "small"))
        self.assertEqual(self.app.model, "m")
        for bad in ("  ", ""):
            with self.assertRaisesRegex(ValueError, "prompt is empty"):
                self.ctx.complete_text(bad)
        with self.assertRaisesRegex(ValueError, "max_tokens"):
            self.ctx.complete_text("q", max_tokens=0)

    def test_complete_retries_through_provider_error_handlers_and_counts_once(self):
        provider = self.script(RuntimeError("overloaded"), text("fine", Usage(5, 1)))
        seen = []
        self.registry.on("provider_error", lambda e: seen.append((e["attempt"], e["agent_id"])) or {"retry_after": 0})
        self.assertEqual(self.ctx.complete_text("q"), "fine")
        self.assertEqual((seen, len(provider.requests)), ([(1, "main")], 2))
        self.assertEqual((self.ctx.usage.requests, self.ctx.usage.input_tokens), (1, 5))

    def test_complete_without_a_retrying_handler_raises_the_error(self):
        self.script(RuntimeError("overloaded"))
        with self.assertRaisesRegex(RuntimeError, "overloaded"):
            self.ctx.complete_text("q")
        self.assertEqual(self.ctx.usage.requests, 0)

    def test_complete_stops_when_the_user_interrupts_while_the_reply_streams(self):
        class Streaming:
            name = "fake"

            def stream(inner, **kw):
                self.app.cancelled.set()
                kw["on_text"]("chunk")

        self.app.provider = Streaming()
        with self.assertRaises(TurnInterrupted):
            self.ctx.complete_text("q")

    def test_usage_totals_add_up_turns_agents_and_complete_calls(self):
        self.registry.register_tool(name="sub", description="", run=lambda i, ctx: ctx.run_agent("inner"))
        self.script(
            call("sub", usage=Usage(100, 10, 50, 5)),  # the turn's first request
            text("inner done", Usage(20, 2)),  # the nested agent
            text("all done", Usage(130, 4, 60)),  # the turn's second request
            text("title", Usage(7, 1)),  # ctx.complete_text
        )
        self.app.run_turn("go")
        self.ctx.complete_text("name it")
        u = self.ctx.usage
        self.assertEqual(
            (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens, u.requests), (257, 17, 110, 5, 4)
        )
        self.assertEqual(Host(self.app).status().usage, u)

    def test_a_provider_that_reports_no_usage_still_counts_requests(self):
        self.script(text("a"), text("b"))
        self.app.run_turn("hi")
        self.ctx.complete_text("q")
        u = self.ctx.usage
        self.assertEqual((u.requests, u.input_tokens), (2, 0))

    def test_usage_command_prints_the_totals(self):
        self.script(text("a", Usage(1200, 30, 1000, 0)))
        self.app.run_turn("hi")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.app.run_command("/usage")
        self.assertIn("1 model requests", out.getvalue())
        self.assertIn("1,200 tokens (1,000 read from the cache, 0 written to it)", out.getvalue())
        self.assertIn(f"output {30:>10,} tokens", out.getvalue())


class ProgressTest(AgentTest):
    def test_progress_reaches_the_frontend_while_a_tool_runs(self):
        seen = []
        self.frontend.progress.side_effect = seen.append

        def slow(input, ctx):
            ctx.progress("reading 1/3\nignored second line")
            ctx.progress("  reading   2/3  ")
            ctx.progress("x" * 300)
            ctx.progress(None)
            return "ok"

        self.registry.register_tool(name="slow", description="", run=slow)
        self.script(call("slow"), text("done"))
        self.app.run_turn("go")
        self.assertEqual(seen, ["reading 1/3", "reading 2/3", "x" * 120, None])

    def test_progress_from_a_command_and_from_another_thread(self):
        seen = []
        self.frontend.progress.side_effect = seen.append
        worker = threading.Thread(target=lambda: self.ctx.progress("from a thread"))
        worker.start()
        worker.join()
        self.ctx.progress("   ")  # blank text clears
        self.assertEqual(seen, ["from a thread", None])

    def test_progress_needs_text_or_none(self):
        with self.assertRaisesRegex(TypeError, "text must be a string or None"):
            self.ctx.progress(3)

    def test_console_prints_each_change_once_on_a_terminal_only(self):
        console = ConsoleFrontend()
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            err.isatty = lambda: True  # type: ignore[method-assign]
            for text_ in ("a", "a", "b\x1b[2J", None, "b\x1b[2J"):
                console.progress(text_)
        self.assertEqual(err.getvalue().count("…"), 3)
        self.assertNotIn("\x1b[2J", err.getvalue())
        quiet = io.StringIO()
        with mock.patch("sys.stderr", quiet):
            ConsoleFrontend().progress("hidden")
        self.assertEqual(quiet.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
