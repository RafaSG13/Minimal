import contextlib
import io
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from minimal.core import session as session_mod
from minimal.core.agent import run_agent
from minimal.core.app import App
from minimal.core.session import Session
from minimal import config
from minimal.extensions import loader
from minimal.extensions.loader import load_extensions
from minimal.extensions.registry import PanelSpec, Registry, TurnInterrupted, styled_line, styled_lines
from minimal.providers.base import Reply, Usage
from minimal.tools.builtin import register_builtin_tools
from minimal.ui.commands import register_builtin_commands

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
        for patcher in (
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", Path(self.cwd) / "global"),
            mock.patch.object(config, "SETTINGS_FILE", Path(self.cwd) / "settings.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.registry = Registry()
        register_builtin_tools(self.registry)
        self.loaded, self.errors = load_extensions(self.registry, self.cwd, config.Settings(), extra=[EXAMPLE])
        self.ctx = SimpleNamespace(cwd=self.cwd, messages=[], model="m", cancelled=False)

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

    def test_edit_replace_all_and_several_edits_all_or_none(self):
        file = Path(self.cwd) / "f.txt"
        file.write_text("a = 1\nb = a\nc = a\n")
        out = self.run_tool("edit", path="f.txt", old_string="a", new_string="x", replace_all=True)
        self.assertEqual(file.read_text(), "x = 1\nb = x\nc = x\n")
        self.assertIn("3 replacements", out)
        self.assertIn("2\tb = x", out)  # the changed lines come back numbered like read
        with self.assertRaisesRegex(ValueError, "edit 2: old_string not found"):
            self.run_tool(
                "edit",
                path="f.txt",
                edits=[{"old_string": "x = 1", "new_string": "x = 2"}, {"old_string": "nope", "new_string": "y"}],
            )
        self.assertEqual(file.read_text(), "x = 1\nb = x\nc = x\n")  # the first edit was not written either
        self.run_tool(
            "edit",
            path="f.txt",
            edits=[{"old_string": "x = 1", "new_string": "x = 2"}, {"old_string": "c = x", "new_string": "c = x + 1"}],
        )
        self.assertEqual(file.read_text(), "x = 2\nb = x\nc = x + 1\n")
        with self.assertRaisesRegex(ValueError, "not both"):
            self.run_tool("edit", path="f.txt", old_string="b", new_string="y", edits=[])

    def test_changes_made_since_the_last_read_are_not_overwritten(self):
        file = Path(self.cwd) / "f.txt"
        file.write_text("one\n")
        self.run_tool("read", path="f.txt")
        file.write_text("one, changed by the user\n")
        with self.assertRaisesRegex(ValueError, "changed on disk since you last read it"):
            self.run_tool("edit", path="f.txt", old_string="one", new_string="two")
        with self.assertRaisesRegex(ValueError, "changed on disk"):
            self.run_tool("write", path="f.txt", content="two\n")
        self.run_tool("read", path="f.txt")
        self.run_tool("edit", path="f.txt", old_string="one", new_string="two")
        self.run_tool("edit", path="f.txt", old_string="two", new_string="three")  # its own edit is known
        self.assertEqual(file.read_text(), "three, changed by the user\n")
        file.unlink()
        self.run_tool("write", path="f.txt", content="new\n")  # nothing left to overwrite

    def test_read_shows_text_that_is_not_utf8_and_skips_binaries(self):
        (Path(self.cwd) / "latin1.txt").write_bytes("caf\xe9\nok\n".encode("latin-1"))
        out = self.run_tool("read", path="latin1.txt")
        self.assertIn("1\tcaf\ufffd", out)
        self.assertIn("2\tok", out)
        self.assertIn("not valid UTF-8", out)
        with self.assertRaisesRegex(ValueError, "not UTF-8"):
            self.run_tool("edit", path="latin1.txt", old_string="ok", new_string="no")
        (Path(self.cwd) / "img.bin").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0")
        self.assertIn("is a binary file (11 bytes): not shown", self.run_tool("read", path="img.bin"))

    def test_bash(self):
        self.assertRegex(self.run_tool("bash", command="echo ok; exit 3"), r"ok[\s\S]*\[exit 3\]")
        self.assertIn("(timeout)", self.run_tool("bash", command="sleep 5", timeout_ms=100))

    def test_bash_interrupted(self):
        timer = threading.Timer(0.3, lambda: setattr(self.ctx, "cancelled", True))
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
        registry.on("turn_end", lambda p: 1 / 0)
        registry.on("turn_end", lambda p: "after")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(registry.emit("turn_end", {}), ["after"])
        self.assertIn("division by zero", err.getvalue())

    def test_emit_lets_turn_interrupted_through(self):
        registry, later = Registry(), mock.Mock()

        def stop(payload):
            raise TurnInterrupted("stop")

        registry.on("turn_end", stop)
        registry.on("turn_end", later)
        with self.assertRaises(TurnInterrupted):
            registry.emit("turn_end", {})
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
        cancelled = threading.Event()
        cancelled.set()
        messages = [{"role": "user", "content": "hi"}]
        with self.assertRaises(TurnInterrupted):  # FakeProvider([]) would raise IndexError if asked
            run_agent(
                provider=FakeProvider([]), model="m", system="s", messages=messages, registry=self.registry,
                ctx=self.ctx, cancelled=cancelled,
            )
        self.assertEqual(len(messages), 1)

    def test_agent_loop_answers_a_tool_call_cut_off_by_max_tokens(self):
        ran = []
        self.registry.on("tool_call", ran.append)
        cut = Reply(
            content=[{"type": "tool_use", "id": "t1", "name": "now", "input": {}}],
            stop_reason="max_tokens",
        )
        provider = FakeProvider([cut, Reply(content=[{"type": "text", "text": "split"}], stop_reason="end_turn")])
        messages = [{"role": "user", "content": "go"}]
        run_agent(provider=provider, model="m", system="s", messages=messages, registry=self.registry, ctx=self.ctx)
        self.assertEqual(ran, [])  # a call cut off mid-way may have partial input: it is not run
        result = messages[2]["content"][0]
        self.assertEqual((result["type"], result["tool_use_id"], result["is_error"]), ("tool_result", "t1", True))
        self.assertIn("output token limit", result["content"])
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant"])

    def test_agent_loop_ends_on_max_tokens_without_tool_calls(self):
        cut = Reply(content=[{"type": "text", "text": "long answ"}], stop_reason="max_tokens")
        messages = [{"role": "user", "content": "go"}]
        run_agent(  # FakeProvider([cut]) would raise IndexError on a second request
            provider=FakeProvider([cut]), model="m", system="s", messages=messages, registry=self.registry, ctx=self.ctx
        )
        self.assertEqual(len(messages), 2)

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

    def test_session_with_a_cut_off_last_line_opens_and_keeps_saving(self):
        with mock.patch.object(session_mod, "SESSIONS_DIR", Path(self.cwd) / "sessions"):
            s = session_mod.open_session("x")
            s.messages += [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
            s.save()
            with open(s.file, "a", encoding="utf-8") as f:
                f.write('{"role": "user", "content": "interrupted wri')  # a crash in the middle of a save

            reopened = session_mod.open_session("x")
            self.assertEqual(reopened.messages, s.messages)
            self.assertIsNotNone(reopened.torn_at)
            reopened.messages.append({"role": "user", "content": "c"})
            reopened.save()

            final = session_mod.open_session("x")
            self.assertEqual([m["content"] for m in final.messages], ["a", "b", "c"])
            self.assertIsNone(final.torn_at)

    def test_session_with_a_corrupt_line_in_the_middle_still_fails(self):
        with mock.patch.object(session_mod, "SESSIONS_DIR", Path(self.cwd) / "sessions"):
            s = session_mod.open_session("x")
            s.messages += [{"role": "user", "content": "a"}]
            s.save()
            with open(s.file, "a", encoding="utf-8") as f:
                f.write('{"role": "user", "content": "broken\n{"role": "user", "content": "c"}\n')
            with self.assertRaises(ValueError):
                session_mod.open_session("x")

    def test_resume_command(self):
        with mock.patch.object(session_mod, "SESSIONS_DIR", Path(self.cwd) / "sessions"):
            saved = session_mod.open_session("a")
            saved.messages += [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": [{"type": "text", "text": "r"}]},
            ]
            saved.save()
            app = App(
                registry=self.registry,
                session=session_mod.open_session("b"),
                cwd=self.cwd,
                auto_approve=True,
                register_commands=register_builtin_commands,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                app.run_command("/resume a")
            self.assertEqual(app.session.id, "a")
            self.assertEqual(len(app.messages), 2)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                app.run_command("/resume zzz")
            self.assertEqual(app.session.id, "a")
            self.assertIn("no session 'zzz'", stderr.getvalue())

    def make_sessions(self, **projects):
        """Save one session per ``id=project`` keyword, in that order, each holding one message."""
        for id, project in projects.items():
            folder = Path(self.cwd) / project
            folder.mkdir(exist_ok=True)
            s = session_mod.open_session(id, str(folder))
            s.messages.append({"role": "user", "content": f"hello {id}"})
            s.save()
        return {p: str(Path(self.cwd) / p) for p in set(projects.values())}

    def test_sessions_belong_to_the_project_they_were_started_in(self):
        with mock.patch.object(session_mod, "SESSIONS_DIR", Path(self.cwd) / "sessions"):
            where = self.make_sessions(a="one", b="two", c="one")
            self.assertEqual(session_mod.list_sessions(where["one"]), ["a", "c"])
            self.assertEqual(session_mod.last_session(where["one"]), "c")
            self.assertEqual(session_mod.last_session(where["two"]), "b")
            self.assertIsNone(session_mod.last_session(str(Path(self.cwd) / "elsewhere")))
            self.assertEqual(session_mod.list_sessions(), ["a", "b", "c"])
            reopened = session_mod.open_session("a", where["two"])  # an existing session keeps its own project
            self.assertEqual(session_mod.list_sessions(where["one"]), ["a", "c"])
            self.assertEqual(reopened.messages, [{"role": "user", "content": "hello a"}])
            self.assertEqual(session_mod.preview("a"), "hello a")

    def test_sessions_and_resume_commands_default_to_this_project(self):
        with mock.patch.object(session_mod, "SESSIONS_DIR", Path(self.cwd) / "sessions"):
            where = self.make_sessions(a="one", b="two", c="one")
            app = App(
                registry=self.registry,
                session=session_mod.open_session("new", where["one"]),
                cwd=where["one"],
                auto_approve=True,
                register_commands=register_builtin_commands,
            )
            offered = []
            app.frontend = SimpleNamespace(choose=lambda title, options: offered.append([v for v, _ in options]))

            def output(line):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    app.run_command(line)
                return out.getvalue()

            listing = output("/sessions")
            self.assertIn("a  hello a", listing)
            self.assertNotIn("hello b", listing)
            self.assertIn("hello b", output("/sessions --all"))
            output("/resume")
            output("/resume --all")
            self.assertEqual(offered, [["c", "a"], ["c", "b", "a"]])  # newest first
            self.assertIn(f"from {Path(where['two']).resolve()}", output("/resume b"))  # an explicit id works anywhere
            self.assertEqual(app.session.id, "b")

    def test_a_session_is_held_by_one_minimal_at_a_time(self):
        sessions = Path(self.cwd) / "sessions"
        script = (
            "import sys; from pathlib import Path; from minimal.core import session as s; "
            f"s.SESSIONS_DIR = Path({str(sessions)!r}); s.open_session('a').lock(); print('locked', flush=True); "
            "sys.stdin.read()"
        )
        other = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(other.wait)
        self.addCleanup(other.stdout.close)
        self.addCleanup(other.stdin.close)
        self.assertEqual(other.stdout.readline(), "locked\n")
        with mock.patch.object(session_mod, "SESSIONS_DIR", sessions):
            with self.assertRaisesRegex(session_mod.SessionLocked, f"open in another minimal \\(pid {other.pid}\\)"):
                session_mod.open_session("a").lock()
            app = App(
                registry=self.registry,
                session=session_mod.open_session("b"),
                cwd=self.cwd,
                auto_approve=True,
                register_commands=register_builtin_commands,
            )
            with self.assertRaises(session_mod.SessionLocked):
                app.switch_session(session_mod.open_session("a"))
            self.assertEqual(app.session.id, "b")
            other.stdin.close()
            other.wait()  # it exits without unlocking, as a crash would: the lock goes with the process
            app.switch_session(session_mod.open_session("a"))
            self.assertEqual(app.session.id, "a")
            app.shutdown()
            self.assertFalse((sessions / "a.lock").exists())

    def test_title_and_parent_are_kept_in_the_header(self):
        with mock.patch.object(session_mod, "SESSIONS_DIR", Path(self.cwd) / "sessions"):
            app = App(
                registry=self.registry,
                session=session_mod.open_session("a", self.cwd),
                cwd=self.cwd,
                auto_approve=True,
                register_commands=register_builtin_commands,
            )
            app.messages.append({"role": "user", "content": "first question"})
            app.session.save()
            with contextlib.redirect_stdout(io.StringIO()):
                app.run_command("/title Parser rewrite")
            reopened = session_mod.open_session("a")
            self.assertEqual(reopened.title, "Parser rewrite")
            self.assertEqual(reopened.messages, [{"role": "user", "content": "first question"}])
            self.assertEqual(session_mod.preview("a"), "Parser rewrite")
            app.context.new_session([{"role": "user", "content": "summary"}])
            self.assertEqual(session_mod.open_session(app.session.id).parent, "a")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                app.run_command("/sessions")
            self.assertIn(f"{app.session.id}  summary  (from a)", out.getvalue())

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

    def test_register_panel_validates(self):
        registry = Registry()
        render = lambda ctx: None  # noqa: E731
        with self.assertRaisesRegex(ValueError, "area must be"):
            registry.register_panel("p", render, area="middle")
        with self.assertRaisesRegex(ValueError, "live must be callable"):
            registry.register_panel("p", render, live=1)
        for bad in (-1, True, "1"):
            with self.assertRaisesRegex(ValueError, "interval must be"):
                registry.register_panel("p", render, interval=bad)
        with self.assertRaisesRegex(ValueError, "name and render are required"):
            registry.register_panel("p", "not callable")
        registry.register_panel("p", render)
        self.assertEqual(registry.panels["p"], PanelSpec("p", render, "sidebar", None, 0.0))

    def test_register_renderer_validates_kind(self):
        registry = Registry()
        render = lambda ctx, block: None  # noqa: E731
        for kind in ("fence:mermaid", "blockquote:alert", "tool:edit"):
            registry.register_renderer(kind, render)
            self.assertIn(kind, registry.renderers)
        for kind in ("fence:", "blockquote:note", "Fence:x", "widget:x"):
            with self.assertRaisesRegex(ValueError, "unknown kind"):
                registry.register_renderer(kind, render)
        with self.assertRaisesRegex(ValueError, "render is required"):
            registry.register_renderer("fence:x", None)

    def test_styled_lines(self):
        self.assertEqual(styled_lines("a\nb"), [[("a", "")], [("b", "")]])
        self.assertEqual(
            styled_lines([["x", ("y", "bold muted")], ("z", "error")]),
            [[("x", ""), ("y", "bold muted")], [("z", "error")]],
        )
        self.assertIsNone(styled_lines(None))
        self.assertEqual(styled_line("x"), [("x", "")])
        self.assertEqual(styled_line(("x", "bold")), [("x", "bold")])
        with self.assertRaisesRegex(ValueError, "unknown style 'blink'"):
            styled_line(("x", "blink"))
        with self.assertRaises(ValueError):
            styled_line(3)
        with self.assertRaises(ValueError):
            styled_lines(3)


class FailingProvider:
    """Replays ``replies`` in order; an ``Exception`` in the list is raised instead of returned."""

    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)

    def stream(self, **kwargs):
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def now_call(call_id):
    return {"type": "tool_use", "id": call_id, "name": "now", "input": {}}


class FailedTurnTest(unittest.TestCase):
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
        self.registry = Registry()
        register_builtin_tools(self.registry)
        load_extensions(self.registry, str(self.tmp), config.Settings(), extra=[EXAMPLE])
        self.app = App(
            registry=self.registry, session=Session("t", self.tmp / "t.jsonl"), cwd=str(self.tmp), auto_approve=True
        )

    def run_turn(self, replies):
        self.app.provider, self.app.model = FailingProvider(replies), "m"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.app.run_turn("go")

    def assertEveryToolUseIsAnswered(self):
        messages = self.app.messages
        for i, message in enumerate(messages):
            if message["role"] != "assistant" or isinstance(message["content"], str):
                continue
            ids = [b["id"] for b in message["content"] if b["type"] == "tool_use"]
            answered = [b["tool_use_id"] for b in messages[i + 1]["content"]] if ids else []
            self.assertEqual(ids, answered)

    def test_error_after_tool_work_keeps_the_work_and_says_why(self):
        self.run_turn([Reply(content=[now_call("t1")], stop_reason="tool_use"), RuntimeError("429 rate limited\nbody")])
        messages = self.app.messages
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant"])
        self.assertIs(messages[2]["content"][0]["is_error"], False)
        self.assertEqual(messages[3]["content"][0]["text"], "[turn interrupted: 429 rate limited]")
        self.assertEveryToolUseIsAnswered()
        self.assertEqual(self.app.session.messages, messages)

    def test_interrupt_between_calls_keeps_finished_results_and_answers_the_rest(self):
        self.registry.on("tool_result", lambda event: self.app.cancelled.set())  # Esc after the first call ran
        self.run_turn([Reply(content=[now_call("t1"), now_call("t2")], stop_reason="tool_use")])
        messages = self.app.messages
        first, second = messages[2]["content"]
        self.assertEqual((first["tool_use_id"], first["is_error"]), ("t1", False))
        self.assertEqual((second["tool_use_id"], second["is_error"]), ("t2", True))
        self.assertIn("did not run", second["content"])
        self.assertEqual(messages[3]["content"][0]["text"], "[turn interrupted: interrupted by the user]")
        self.assertEveryToolUseIsAnswered()

    def test_error_before_any_reply_discards_the_turn_so_the_prompt_can_be_resent(self):
        self.run_turn([RuntimeError("401")])
        self.assertEqual(self.app.messages, [])

    def test_interrupt_after_a_complete_answer_keeps_it_without_a_note(self):
        def interrupt(event):  # what an extension does at turn_end, e.g. an interrupted compaction
            raise TurnInterrupted("interrupted by the user")

        self.registry.on("turn_end", interrupt)
        self.run_turn([Reply(content=[{"type": "text", "text": "done"}], stop_reason="end_turn")])
        self.assertEqual([m["role"] for m in self.app.messages], ["user", "assistant"])


HELLO_EXT = '__api__ = "1"\ndef register(minimal):\n    minimal.register_tool(name="hello", run=lambda input, ctx: "{reply}")\n'


class ReloadTest(unittest.TestCase):
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
            self.registry.tools["reload_extensions"].run({}, self.app.context)


if __name__ == "__main__":
    unittest.main()
