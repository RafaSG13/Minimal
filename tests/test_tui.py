import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import Input, Markdown, Static

from minimal import extensions
from minimal.app import App
from minimal.providers.base import Reply
from minimal.registry import Registry
from minimal.session import Session
from minimal.tui import ConfirmScreen, MinimalTui, _outside_event_loop


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
            self.assertIn("> hi", users)
            self.assertEqual([m.source for m in app.query(Markdown)], ["Hello **there**"])
            sidebar = app.query_one("#sidebar")
            self.assertTrue(sidebar.has_class("-has-widgets"))
            self.assertEqual(len(sidebar.query(Counter)), 1)
            self.assertGreaterEqual(Counter.calls, 2)

    async def test_confirm_modal(self):
        self.use_replies(
            Reply(
                content=[{"type": "tool_use", "id": "t1", "name": "write", "input": {"path": "out.txt", "content": "x"}}],
                stop_reason="tool_use",
            ),
            Reply(content=[{"type": "text", "text": "written"}], stop_reason="end_turn"),
        )
        app = MinimalTui(self.core)
        async with app.run_test() as pilot:
            await self.submit(pilot, "write it")
            await self.wait_until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")
            await self.wait_until(pilot, self.idle(app))
        self.assertEqual((self.tmp / "out.txt").read_text(), "x")

    async def test_command_output_captured(self):
        app = MinimalTui(self.core)
        async with app.run_test() as pilot:
            await self.submit(pilot, "/tools")
            await self.wait_until(pilot, self.idle(app))
            await pilot.pause(0.1)
            outputs = [str(s.render()) for s in app.query(".output").results(Static)]
            self.assertTrue(any("reload_extensions" in o for o in outputs), outputs)

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
