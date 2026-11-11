import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import Input, Markdown, SelectionList, Static

from minimal import config
from minimal.core.app import App
from minimal.core.session import Session
from minimal.extensions import loader
from minimal.extensions.registry import LogoSpec, Registry
from minimal.providers.base import Reply
from minimal.ui.commands import BUILTIN_COMMANDS
from minimal.ui.tui.app import CommandSuggester, MinimalTui, _outside_event_loop
from minimal.ui.tui.screens import ChecklistScreen, ChoiceScreen, ConfirmScreen, TextScreen
from minimal.ui.tui.widgets import ToolBlock, _edit_diff, logo_content


class StreamingFakeProvider:
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)

    def stream(self, **kw):
        reply = self.replies.pop(0)
        for block in reply.content:
            if block["type"] == "text":
                text = block["text"]
                for chunk in (text[: len(text) // 2], text[len(text) // 2 :]):
                    kw["on_text"](chunk)
        return reply


class BlockingFakeProvider:
    """Blocks inside ``stream`` until released, then streams one late chunk."""

    name = "fake"

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def stream(self, **kw):
        self.started.set()
        self.release.wait(5)
        kw["on_text"]("late")
        return Reply(content=[{"type": "text", "text": "late"}], stop_reason="end_turn")


class Counter(Static):
    calls = 0

    def refresh_state(self, ctx):
        Counter.calls += 1
        self.update(f"{len(ctx.messages)} messages")


class LiveRecorder(Static):
    kinds: list[str] = []

    def refresh_live(self, ctx, event):
        LiveRecorder.kinds.append(event["kind"])


class TuiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for patcher in (
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.tmp / "global"),
            mock.patch.object(config, "SETTINGS_FILE", self.tmp / "settings.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        Counter.calls = 0
        self.core = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=False,
            commands=BUILTIN_COMMANDS,
        )
        self.core.reload_extensions()
        self.core.registry.register_widget("counter", lambda ctx: Counter(), area="sidebar")
        self.core.provider_name, self.core.model = "fake", "m"

    def use_replies(self, *replies):
        self.core.provider = StreamingFakeProvider(replies)

    async def submit(self, pilot, text):
        pilot.app.query_one("#prompt", Input).value = text
        await pilot.press("enter")

    async def wait_until(self, pilot, condition):
        for _ in range(100):
            await pilot.pause(0.05)
            if condition():
                return
        self.fail("timed out")

    def idle(self, app):
        return lambda: not app.query_one("#prompt", Input).disabled

    async def test_turn_streams_and_widget_refreshes(self):
        self.use_replies(Reply(content=[{"type": "text", "text": "Hello **there**"}], stop_reason="end_turn"))
        app = MinimalTui(self.core)
        async with app.run_test() as pilot:
            await self.submit(pilot, "hi")
            await self.wait_until(pilot, self.idle(app))
            users = [str(s.render()) for s in app.query(".user").results(Static)]
            self.assertIn("hi", users)
            self.assertEqual([m.source for m in app.query(Markdown)], ["Hello **there**"])
            sidebar = app.query_one("#sidebar")
            self.assertTrue(sidebar.has_class("-has-widgets"))
            self.assertEqual(len(sidebar.query(Counter)), 1)
            self.assertGreaterEqual(Counter.calls, 2)

    async def test_context_widget_returns_the_mounted_extension_widget(self):
        app = MinimalTui(self.core)
        self.assertIsNone(self.core.context.widget("counter"))  # nothing is mounted yet
        async with app.run_test():
            widget = self.core.context.widget("counter")
            self.assertIsInstance(widget, Counter)
            self.assertIs(widget, app.query_one(Counter))
            self.assertIsNone(self.core.context.widget("missing"))

    async def test_escape_interrupts_turn(self):
        provider = self.core.provider = BlockingFakeProvider()
        app = MinimalTui(self.core)
        async with app.run_test() as pilot:
            await self.submit(pilot, "hi")
            await self.wait_until(pilot, provider.started.is_set)
            await pilot.press("escape")
            provider.release.set()
            await self.wait_until(pilot, self.idle(app))
            self.assertEqual(self.core.messages, [])
            errors = [str(s.render()) for s in app.query(".error").results(Static)]
            self.assertTrue(any("interrupted" in e for e in errors), errors)
            self.assertEqual([m.source for m in app.query(Markdown)], [])

    async def test_slow_git_status_does_not_block_the_ui(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_git_status(cwd):
            release.wait(5)
            return ("feature", True)

        app = MinimalTui(self.core)
        status = lambda: str(app.query_one("#status-left", Static).render())  # noqa: E731
        with mock.patch("minimal.ui.tui.app.git_status", slow_git_status):
            async with app.run_test() as pilot:
                await pilot.pause(0.1)
                self.assertIn("fake", status())  # drawn while git is still running
                self.assertNotIn("feature", status())
                release.set()
                await self.wait_until(pilot, lambda: "feature *" in status())

    async def test_resumed_session_is_replayed(self):
        self.core.session.messages += [
            {"role": "user", "content": "old question"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "old answer"},
                    {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "a.txt"}},
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1: hi", "is_error": False}],
            },
        ]
        app = MinimalTui(self.core)
        async with app.run_test():
            self.assertEqual([str(s.render()) for s in app.query(".user").results(Static)], ["old question"])
            self.assertEqual([m.source for m in app.query(Markdown)], ["old answer"])
            self.assertTrue(app.query_one(ToolBlock).has_class("-success"))

    async def test_refresh_live_reports_tool_events(self):
        (self.tmp / "a.txt").write_text("hi\n")
        LiveRecorder.kinds = []
        self.core.registry.register_widget("recorder", lambda ctx: LiveRecorder(), area="sidebar")
        self.use_replies(
            Reply(
                content=[{"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "a.txt"}}],
                stop_reason="tool_use",
            ),
            Reply(content=[{"type": "text", "text": "done"}], stop_reason="end_turn"),
        )
        app = MinimalTui(self.core)
        async with app.run_test() as pilot:
            await self.submit(pilot, "read it")
            await self.wait_until(pilot, self.idle(app))
            self.assertEqual(LiveRecorder.kinds, ["start", "tool_call", "tool_result"])

    async def test_status_segments_render_and_report_errors(self):
        def boom(ctx):
            raise RuntimeError("no")

        self.core.registry.register_status("cost", lambda ctx: "$1")
        self.core.registry.register_status("boom", boom, side="left")
        app = MinimalTui(self.core)
        async with app.run_test() as pilot:
            self.assertIn("$1", str(app.query_one("#status-right", Static).render()))
            for _ in range(2):
                await self.submit(pilot, "/tools")
                await self.wait_until(pilot, self.idle(app))
            errors = [str(s.render()) for s in app.query(".error").results(Static)]
            self.assertEqual(len([e for e in errors if "status boom" in e]), 1, errors)

    def test_edit_diff_numbers_and_highlights_changes(self):
        lines = _edit_diff({"old_string": "a\nb = 1\nc", "new_string": "a\nb = 2\nc"}, 10)
        self.assertEqual([line.plain for line in lines], ["10   a", "11 - b = 1", "11 + b = 2", "12   c"])
        removed = lines[1]
        changed = [removed.plain[s.start : s.end] for s in removed.spans if str(s.style) == "$diff-removed reverse"]
        self.assertEqual(changed, ["1"])

    async def test_tool_block_expands_on_click(self):
        output = "\n".join(f"line {i}" for i in range(20))
        self.core.session.messages += [
            {"role": "user", "content": "run"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "seq"}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": output, "is_error": False}],
            },
        ]
        app = MinimalTui(self.core)
        async with app.run_test(size=(100, 50)) as pilot:
            block = app.query_one(ToolBlock)
            self.assertIn("12 earlier lines · click to expand", str(block.render()))
            self.assertNotIn("line 0\n", str(block.render()))
            await pilot.click(ToolBlock)
            self.assertIn("line 0\n", str(block.render()))
            self.assertIn("click to collapse", str(block.render()))

    async def test_command_suggester(self):
        self.core.registry.register_command("thanks", lambda args, app: None)
        suggester = CommandSuggester(self.core)
        self.assertEqual(await suggester.get_suggestion("/th"), "/thanks")
        self.assertEqual(await suggester.get_suggestion("/the"), "/theme")
        self.assertIsNone(await suggester.get_suggestion("/theme x"))
        self.assertIsNone(await suggester.get_suggestion("hi"))

    def use_write_replies(self):
        self.use_replies(
            Reply(
                content=[{"type": "tool_use", "id": "t1", "name": "write", "input": {"path": "out.txt", "content": "x"}}],
                stop_reason="tool_use",
            ),
            Reply(content=[{"type": "text", "text": "done"}], stop_reason="end_turn"),
        )

    async def test_confirm_modal_allows_tool(self):
        self.use_write_replies()
        app = MinimalTui(self.core)
        async with app.run_test() as pilot:
            await self.submit(pilot, "write it")
            await self.wait_until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            self.assertTrue(app.query_one(ToolBlock).has_class("-pending"))
            await pilot.press("enter")  # Allow is preselected
            await self.wait_until(pilot, self.idle(app))
            self.assertTrue(app.query_one(ToolBlock).has_class("-success"))
        self.assertEqual((self.tmp / "out.txt").read_text(), "x")

    async def test_untrusted_project_extensions_notice_and_trust(self):
        patcher = mock.patch.object(config, "TRUST_FILE", self.tmp / "trusted.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        ext_dir = self.tmp / ".minimal" / "extensions"
        ext_dir.mkdir(parents=True)
        (ext_dir / "hello.py").write_text(
            '__api__ = "1"\ndef register(minimal):\n    minimal.register_tool(name="hello", run=lambda input, ctx: "hi")\n'
        )
        self.core.reload_extensions()
        app = MinimalTui(self.core)
        status = lambda: str(app.query_one("#status-right", Static).render())  # noqa: E731
        async with app.run_test() as pilot:
            self.assertIn("1 untrusted", status())
            self.assertTrue(any("/trust" in o for o in self.outputs(app)), self.outputs(app))
            await self.submit(pilot, "/trust")
            await self.wait_until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.pause(0.1)
            self.assertTrue(any("not trusted yet" in o for o in self.outputs(app)), self.outputs(app))
            await pilot.press("enter")  # Allow is preselected
            await self.wait_until(pilot, self.idle(app))
            self.assertNotIn("untrusted", status())
        self.assertIn("hello", self.core.registry.tools)

    async def test_denied_tool_shows_error(self):
        self.use_write_replies()
        app = MinimalTui(self.core)
        async with app.run_test() as pilot:
            await self.submit(pilot, "write it")
            await self.wait_until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("right", "enter")
            await self.wait_until(pilot, self.idle(app))
            block = app.query_one(ToolBlock)
            self.assertTrue(block.has_class("-error"))
            self.assertIn("denied", str(block.render()))
        self.assertFalse((self.tmp / "out.txt").exists())

    async def test_command_output_captured(self):
        app = MinimalTui(self.core)
        async with app.run_test() as pilot:
            await self.submit(pilot, "/tools")
            await self.wait_until(pilot, self.idle(app))
            await pilot.pause(0.1)
            outputs = [str(s.render()) for s in app.query(".output").results(Static)]
            self.assertTrue(any("reload_extensions" in o for o in outputs), outputs)

    def outputs(self, app):
        return [str(s.render()) for s in app.query(".output").results(Static)]

    async def test_checklist_dialog(self):
        options = [("a", "A", True), ("b", "B", False)]
        self.core.registry.register_command("pick", lambda args, ctx: print(ctx.ui.choose_many("Pick", options)))
        app = MinimalTui(self.core)
        async with app.run_test() as pilot:
            await self.submit(pilot, "/pick")
            await self.wait_until(pilot, lambda: isinstance(app.screen, ChecklistScreen))
            app.screen.query_one(SelectionList).select("b")
            await pilot.press("enter")
            await self.wait_until(pilot, self.idle(app))
            await pilot.pause(0.1)
            self.assertTrue(any("['a', 'b']" in o for o in self.outputs(app)), self.outputs(app))

    async def test_text_dialog_does_not_send_message(self):
        self.core.registry.register_command("name", lambda args, ctx: print(repr(ctx.ui.ask_text("Name"))))
        app = MinimalTui(self.core)
        async with app.run_test() as pilot:
            await self.submit(pilot, "/name")
            await self.wait_until(pilot, lambda: isinstance(app.screen, TextScreen))
            await pilot.press("h", "i", "enter")
            await self.wait_until(pilot, self.idle(app))
            await pilot.pause(0.1)
            self.assertTrue(any("'hi'" in o for o in self.outputs(app)), self.outputs(app))
            self.assertEqual(len(app.query(".user")), 0)

    async def test_theme_command_switches_and_reload_falls_back(self):
        self.core.registry.register_theme("ocean", dark=True, background="#0b1622", foreground="#d0e4f5", accent="#4fc3f7")
        app = MinimalTui(self.core)
        async with app.run_test() as pilot:
            self.assertEqual(app.theme, "minimal")
            await self.submit(pilot, "/theme")
            await self.wait_until(pilot, lambda: isinstance(app.screen, ChoiceScreen))
            await pilot.press("down", "enter")  # minimal, ocean
            await self.wait_until(pilot, self.idle(app))
            self.assertEqual(app.theme, "ocean")
            await self.submit(pilot, "/theme nord")  # not installed: keeps the current theme
            await self.wait_until(pilot, self.idle(app))
            self.assertEqual(app.theme, "ocean")
            await self.submit(pilot, "/reload")  # rebuilds the registry from disk, where ocean is not
            await self.wait_until(pilot, self.idle(app))
            self.assertEqual(app.theme, "minimal")
            self.assertEqual(list(app.available_themes), ["minimal"])

    def test_logo_half_blocks(self):
        content = logo_content(LogoSpec(["ab.a", "a.bb", "b..."], {"a": "#ff0000", "b": "#00ff00"}))
        self.assertEqual(content.plain, "█▀▄▀\n▀   ")
        self.assertIn("#ff0000 on #00ff00", [str(span.style) for span in content.spans])

    async def test_default_header_is_wordmark(self):
        app = MinimalTui(self.core)
        with mock.patch("minimal.ui.tui.widgets._version", return_value="9.9"):
            async with app.run_test():
                header = app.query_one("Header")
                self.assertTrue(header.has_class("-align-left"))
                wordmark = str(app.query_one("Header .wordmark", Static).render())
                self.assertIn("m i n i m a l", wordmark)
                self.assertTrue(wordmark.endswith("v9.9"), wordmark)
                self.assertNotIn("9.9", str(app.query_one("Header .info", Static).render()))

    async def test_logo_replaces_wordmark_until_reload(self):
        self.core.registry.register_logo(["aa"], {"a": "$accent"}, align="right")
        app = MinimalTui(self.core)
        async with app.run_test() as pilot:
            self.assertTrue(app.query_one("Header").has_class("-align-right"))
            self.assertEqual(len(app.query("Header .wordmark")), 0)
            self.assertEqual(str(app.query_one("Header .logo", Static).render()), "▀▀")
            info = str(app.query_one("Header .info", Static).render())
            self.assertNotIn("minimal", info)
            await self.submit(pilot, "/reload")  # rebuilds the registry from disk, where the logo is not
            await self.wait_until(pilot, self.idle(app))
            self.assertEqual(len(app.query("Header .logo")), 0)
            header = app.query_one("Header")
            self.assertTrue(header.has_class("-align-left"))
            self.assertFalse(header.has_class("-align-right"))
            self.assertIn("m i n i m a l", str(app.query_one("Header .wordmark", Static).render()))

    async def test_terminal_command_can_run_its_own_event_loop(self):
        # /setup's questionary menus call asyncio.run on the UI thread.
        async def answer():
            return 42

        loop = asyncio.get_running_loop()
        with _outside_event_loop():
            self.assertEqual(asyncio.run(answer()), 42)
        self.assertIs(asyncio.get_running_loop(), loop)


if __name__ == "__main__":
    unittest.main()
