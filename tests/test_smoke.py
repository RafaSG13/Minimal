import contextlib
import io
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from minimal import extensions
from minimal import session as session_mod
from minimal.agent import run_agent
from minimal.app import App
from minimal.commands import run_command
from minimal.extensions import load_extensions
from minimal.providers.base import Reply, Usage
from minimal.registry import Registry, TurnInterrupted
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
        patcher = mock.patch.object(extensions, "GLOBAL_EXTENSIONS_DIR", Path(self.cwd) / "global")
        patcher.start()
        self.addCleanup(patcher.stop)
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
        self.run_tool("edit", path="a/b.txt", old_string="world", new_string="minimal")
        self.assertEqual((Path(self.cwd) / "a/b.txt").read_text(), "hello\nminimal\nhello\n")

    def test_bash(self):
        self.assertRegex(self.run_tool("bash", command="echo ok; exit 3"), r"ok[\s\S]*\[exit 3\]")
        self.assertIn("(timeout)", self.run_tool("bash", command="sleep 5", timeout_ms=100))

    def test_bash_interrupted(self):
        self.ctx.cancelled = threading.Event()
        timer = threading.Timer(0.3, self.ctx.cancelled.set)
        timer.start()
        self.addCleanup(timer.cancel)
        start = time.monotonic()
        self.assertIn("(interrupted)", self.run_tool("bash", command="sleep 5"))
        self.assertLess(time.monotonic() - start, 3)

    def test_search_tools(self):
        root = Path(self.cwd)
        for name, text in {
            "src/a.py": "def foo():\n    return 1\n",
            "src/sub/b.py": "FOO = 2\n",
            "notes.md": "foo\n",
            ".hidden/c.py": "foo\n",
        }.items():
            self.run_tool("write", path=name, content=text)
        (root / "bin.dat").write_bytes(b"foo\0bar")

        self.assertEqual(
            self.run_tool("grep", pattern="foo", ignore_case=True, glob="*.py"),
            "src/a.py:1:def foo():\nsrc/sub/b.py:1:FOO = 2",
        )
        out = self.run_tool("grep", pattern="foo")
        self.assertIn("notes.md:1:foo", out)
        self.assertNotIn("bin.dat", out)
        self.assertEqual(self.run_tool("grep", pattern="zzz"), "No matches")
        with self.assertRaises(re.error):
            self.run_tool("grep", pattern="(")

        self.assertEqual(self.run_tool("glob", pattern="src/*.py"), "src/a.py")
        self.assertEqual(self.run_tool("glob", pattern="src/**/*.py"), "src/a.py\nsrc/sub/b.py")
        self.assertEqual(self.run_tool("glob", pattern="*.{py,md}"), "notes.md\nsrc/a.py\nsrc/sub/b.py")
        self.assertEqual(self.run_tool("glob", pattern="*.rs"), "No files found")
        self.assertEqual(self.run_tool("glob", pattern="*.py", path="src/sub"), "src/sub/b.py")

    def test_search_tools_respect_gitignore(self):
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        subprocess.run(["git", "init", "-q"], cwd=self.cwd, check=True)
        for name, text in {".gitignore": "build/\n", "build/out.py": "foo\n", "main.py": "foo\n"}.items():
            self.run_tool("write", path=name, content=text)
        self.assertEqual(self.run_tool("glob", pattern="*.py"), "main.py")
        self.assertEqual(self.run_tool("grep", pattern="foo", glob="*.py"), "main.py:1:foo")

    def test_guardrail(self):
        r = self.registry.emit("tool_call", {"name": "bash", "input": {"command": "rm -rf /"}})
        self.assertIs(r[0]["block"], True)

    def test_emit_reports_a_failing_handler_and_continues(self):
        registry = Registry()
        registry.on("x", lambda p: 1 / 0)
        registry.on("x", lambda p: "after")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(registry.emit("x", {}), ["after"])
        self.assertIn("division by zero", err.getvalue())

    def test_emit_lets_turn_interrupted_through(self):
        registry, later = Registry(), mock.Mock()

        def stop(payload):
            raise TurnInterrupted("stop")

        registry.on("x", stop)
        registry.on("x", later)
        with self.assertRaises(TurnInterrupted):
            registry.emit("x", {})
        later.assert_not_called()

    def test_agent_loop(self):
        provider = FakeProvider(
            [
                Reply(
                    content=[{"type": "tool_use", "id": "t1", "name": "now", "input": {}}],
                    stop_reason="tool_use",
                    provider_data=["raw"],
                ),
                Reply(content=[{"type": "text", "text": "done"}], stop_reason="end_turn", usage=Usage(3, 4)),
            ]
        )
        messages = [{"role": "user", "content": "time?"}]
        run_agent(provider=provider, model="m", system="s", messages=messages, registry=self.registry, ctx=self.ctx)
        self.assertEqual(len(messages), 4)
        self.assertEqual(messages[1]["provider_data"], {"provider": "fake", "data": ["raw"]})
        self.assertEqual(messages[2]["content"][0]["type"], "tool_result")
        self.assertIs(messages[2]["content"][0]["is_error"], False)
        self.assertNotIn("provider_data", messages[3])
        self.assertNotIn("usage", messages[1])
        self.assertEqual(messages[3]["usage"]["input_tokens"], 3)

    def test_agent_loop_interrupted(self):
        self.ctx.cancelled = threading.Event()
        self.ctx.cancelled.set()
        messages = [{"role": "user", "content": "hi"}]
        with self.assertRaises(TurnInterrupted):  # FakeProvider([]) would raise IndexError if asked
            run_agent(
                provider=FakeProvider([]), model="m", system="s", messages=messages, registry=self.registry, ctx=self.ctx
            )
        self.assertEqual(len(messages), 1)

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

    def test_resume_command(self):
        with mock.patch.object(session_mod, "SESSIONS_DIR", Path(self.cwd) / "sessions"):
            saved = session_mod.open_session("a")
            saved.messages += [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": [{"type": "text", "text": "r"}]},
            ]
            saved.save()
            app = App(registry=self.registry, session=session_mod.open_session("b"), cwd=self.cwd, auto_approve=True)
            with contextlib.redirect_stdout(io.StringIO()):
                run_command("/resume a", app)
            self.assertEqual(app.session.id, "a")
            self.assertEqual(len(app.messages), 2)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                run_command("/resume zzz", app)
            self.assertEqual(app.session.id, "a")
            self.assertIn("no session 'zzz'", stderr.getvalue())

    def test_register_logo_validates(self):
        registry = Registry()
        with self.assertRaisesRegex(ValueError, "same length"):
            registry.register_logo(["ab", "a"], {"a": "#ff0000", "b": "#00ff00"})
        with self.assertRaisesRegex(ValueError, "no colour for pixel"):
            registry.register_logo(["ax"], {"a": "#ff0000"})
        with self.assertRaisesRegex(ValueError, r"not a #rrggbb colour or \$variable"):
            registry.register_logo(["a"], {"a": "red"})
        with self.assertRaisesRegex(ValueError, "align must be"):
            registry.register_logo(["a"], {"a": "#ff0000"}, align="middle")
        registry.register_logo(["a."], {"a": "$accent"})
        self.assertEqual(registry.logo.art, ["a."])
        self.assertEqual(registry.logo.align, "center")

    def test_register_status_validates(self):
        registry = Registry()
        with self.assertRaisesRegex(ValueError, "side must be"):
            registry.register_status("x", lambda ctx: "", side="middle")
        with self.assertRaisesRegex(ValueError, "name and render are required"):
            registry.register_status("x", "not callable")
        registry.register_status("x", lambda ctx: "")
        self.assertEqual(registry.status["x"].side, "right")


HELLO_EXT = 'def register(minimal):\n    minimal.register_tool(name="hello", run=lambda input, ctx: "{reply}")\n'


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
        self.app = App(
            registry=self.registry, session=session, cwd=str(self.tmp), auto_approve=True, trust_project=True
        )
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
        (self.ext_dir / "broken.py").write_text("def register(minimal):\n  minimal.register_tool(\n")
        with self.assertRaisesRegex(RuntimeError, "broken.py: SyntaxError"):
            self.registry.tools["reload_extensions"].run({}, self.app)


if __name__ == "__main__":
    unittest.main()
