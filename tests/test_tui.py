import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import Input, Markdown, SelectionList, Static

from minimal import extensions
from minimal.app import App
from minimal.providers.base import Reply
from minimal.registry import LogoSpec, Registry
from minimal.session import Session
from minimal.tui import (
    ChecklistScreen,
    ChoiceScreen,
    ConfirmScreen,
    MinimalTui,
    TextScreen,
    ToolBlock,
    _logo,
    _outside_event_loop,
)


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


class TuiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.object(extensions, "GLOBAL_EXTENSIONS_DIR", self.tmp / "global")
        patcher.start()
        self.addCleanup(patcher.stop)
        Counter.calls = 0
        self.core = App(
            registry=Registry(), session=Session("t", self.tmp / "t.jsonl"), cwd=str(self.tmp), auto_approve=False
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
        with mock.patch("minimal.tui.git_status", slow_git_status):
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
        self.core.registry.register_command("pick", lambda args, app: print(app.frontend.choose_many("Pick", options)))
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
        self.core.registry.register_command("name", lambda args, app: print(repr(app.frontend.ask_text("Name"))))
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
        content = _logo(LogoSpec(["ab.a", "a.bb", "b..."], {"a": "#ff0000", "b": "#00ff00"}))
        self.assertEqual(content.plain, "█▀▄▀\n▀   ")
        self.assertIn("#ff0000 on #00ff00", [str(span.style) for span in content.spans])

    async def test_default_header_is_wordmark(self):
        app = MinimalTui(self.core)
        with mock.patch("minimal.tui._version", return_value="9.9"):
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
