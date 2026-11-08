import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from minimal import extensions
from minimal import session as session_mod
from minimal.agent import run_agent
from minimal.app import App
from minimal.extensions import load_extensions
from minimal.providers.base import Reply
from minimal.registry import Registry
from minimal.session import Session
from minimal.tools import register_builtin_tools

EXAMPLE = Path(__file__).resolve().parent.parent / "extensions" / "example.py"


class FakeProvider:
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)

    def stream(self, **kwargs):
        return self.replies.pop(0)


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd = self._tmp.name
        self.registry = Registry()
        register_builtin_tools(self.registry)
        self.loaded, self.errors = load_extensions(self.registry, self.cwd, extra=[EXAMPLE])
        self.ctx = SimpleNamespace(cwd=self.cwd, messages=[], model="m", registry=self.registry, confirm=None)

    def run_tool(self, name, **inp):
        return self.registry.tools[name].run(inp, self.ctx)

    def test_extension_loaded(self):
        self.assertEqual(self.errors, [])
        self.assertEqual(len(self.loaded), 1)
        self.assertIn("now", self.registry.tools)
        self.assertIn("stats", self.registry.commands)

    def test_file_tools(self):
        self.run_tool("write", path="a/b.txt", content="hello\nworld\nhello\n")
        self.assertRegex(self.run_tool("read", path="a/b.txt"), r"1\thello")
        with self.assertRaisesRegex(ValueError, "appears 2 times"):
            self.run_tool("edit", path="a/b.txt", old_string="hello", new_string="x")
        self.run_tool("edit", path="a/b.txt", old_string="world", new_string="Pi")
        self.assertEqual((Path(self.cwd) / "a/b.txt").read_text(), "hello\nPi\nhello\n")

    def test_bash(self):
        self.assertRegex(self.run_tool("bash", command="echo ok; exit 3"), r"ok[\s\S]*\[exit 3\]")
        self.assertIn("(timeout)", self.run_tool("bash", command="sleep 5", timeout_ms=100))

    def test_guardrail(self):
        r = self.registry.emit("tool_call", {"name": "bash", "input": {"command": "rm -rf /"}})
        self.assertIs(r[0]["block"], True)

    def test_agent_loop(self):
        provider = FakeProvider(
            [
                Reply(
                    content=[{"type": "tool_use", "id": "t1", "name": "now", "input": {}}],
                    stop_reason="tool_use",
                    provider_data=["raw"],
                ),
                Reply(content=[{"type": "text", "text": "done"}], stop_reason="end_turn"),
            ]
        )
        messages = [{"role": "user", "content": "time?"}]
        run_agent(provider=provider, model="m", system="s", messages=messages, registry=self.registry, ctx=self.ctx)
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[1]["provider_data"], {"provider": "fake", "data": ["raw"]})
        self.assertEqual(messages[2]["content"][0]["type"], "tool_result")
        self.assertIs(messages[2]["content"][0]["is_error"], False)
        self.assertNotIn("provider_data", messages[3])

    def test_session_round_trip(self):
        with mock.patch.object(session_mod, "SESSIONS_DIR", Path(self.cwd) / "sessions"):
            s = session_mod.open_session("x")
            s.messages += [{"role": "user", "content": "a"}, {"role": "assistant", "content": [{"type": "text", "text": "é"}]}]
            s.save()
            s.messages.append({"role": "user", "content": "c"})
            s.save()
            again = session_mod.open_session("x")
            self.assertEqual(again.messages, s.messages)
            self.assertEqual(len(again.messages), 3)
            self.assertEqual(session_mod.list_sessions(), ["x"])


HELLO_EXT = 'def register(pi):\n    pi.register_tool(name="hello", run=lambda input, ctx: "{reply}")\n'


class ReloadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.object(extensions, "GLOBAL_EXTENSIONS_DIR", self.tmp / "global")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.registry = Registry()
        session = Session("t", self.tmp / "t.jsonl")
        self.app = App(registry=self.registry, session=session, cwd=str(self.tmp), auto_approve=True)
        self.ext_dir = self.tmp / ".minimal" / "extensions"
        self.ext_dir.mkdir(parents=True)

    def test_reload_picks_up_new_extension(self):
        tools = self.registry.tools
        self.app.reload_extensions()
        self.assertNotIn("hello", self.registry.tools)

        (self.ext_dir / "hello.py").write_text(HELLO_EXT.format(reply="v1"))
        self.app.reload_extensions()
        self.assertIn("hello", self.registry.tools)
        self.assertIn("reload_extensions", self.registry.tools)
        self.assertIs(self.registry.tools, tools)  # the agent loop holds this dict

        (self.ext_dir / "hello.py").write_text(HELLO_EXT.format(reply="v2"))  # same second, same size
        self.app.reload_extensions()
        self.assertEqual(self.registry.tools["hello"].run({}, self.app), "v2")

    def test_reload_reports_broken_extension(self):
        self.app.reload_extensions()
        (self.ext_dir / "broken.py").write_text("def register(pi):\n  pi.register_tool(\n")
        with self.assertRaisesRegex(RuntimeError, "broken.py: SyntaxError"):
            self.registry.tools["reload_extensions"].run({}, self.app)


if __name__ == "__main__":
    unittest.main()
