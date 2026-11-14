import json
import os
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock

from minimal import __version__, cli, config
from minimal.core import session as session_mod
from minimal.core.app import App
from minimal.core.host import Host
from minimal.extensions import loader
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply
from minimal.ui import serve
from minimal.ui.commands import register_builtin_commands
from minimal.ui.frontends import register_builtin_frontends


class ScriptedProvider:
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)

    def stream(self, *, on_text, **kwargs):
        reply = self.replies.pop(0)
        for block in reply.content:
            if block["type"] == "text" and on_text is not None:
                on_text(block["text"])
        return reply


def text_reply(text):
    return Reply(content=[{"type": "text", "text": text}], stop_reason="end_turn")


def tool_reply(name, id="t1", input=None):
    return Reply(content=[{"type": "tool_use", "id": id, "name": name, "input": input or {}}], stop_reason="tool_use")


class Client:
    """The frontend's side of a served minimal: JSON lines over two pipes."""

    def __init__(self, test: unittest.TestCase, host: Host) -> None:
        to_server_r, self.to_server = os.pipe()
        from_server_r, from_server_w = os.pipe()
        self.writer = os.fdopen(self.to_server, "w", encoding="utf-8")
        self.lines: queue.Queue = queue.Queue()
        self.seen: list[dict] = []
        self.inp = os.fdopen(to_server_r, "r", encoding="utf-8")
        out = os.fdopen(from_server_w, "w", encoding="utf-8", buffering=1)
        self.server = threading.Thread(target=serve.run, args=(host, self.inp, out), daemon=True)
        self.reader_file = os.fdopen(from_server_r, "r", encoding="utf-8")
        threading.Thread(target=self.pump, daemon=True).start()
        self.server.start()
        test.addCleanup(self.close)

    def pump(self) -> None:
        try:
            for line in self.reader_file:
                self.lines.put(line)
        except (OSError, ValueError):  # closed by the test
            pass
        self.lines.put(None)

    def send(self, **message) -> None:
        self.writer.write(json.dumps(message) + "\n")
        self.writer.flush()

    def next(self, timeout: float = 10) -> dict:
        line = self.lines.get(timeout=timeout)
        self.assertion(line is not None, "the server closed its output")
        message = json.loads(line)
        self.seen.append(message)
        return message

    def assertion(self, ok: bool, text: str) -> None:
        if not ok:
            raise AssertionError(f"{text}; messages so far: {self.seen}")

    def until(self, kind: str, answer=None) -> list[dict]:
        """Read messages up to and including the first of ``kind``; ``answer(message)`` replies to questions on the way."""
        got = []
        while True:
            message = self.next()
            got.append(message)
            if message["t"] == kind:
                return got
            if answer is not None and "id" in message:
                self.send(t="answer", id=message["id"], value=answer(message))

    def close(self) -> None:
        self.writer.close()  # the server's reader sees the end of input and the server stops
        self.server.join(10)
        self.inp.close()
        self.reader_file.close()


class ServeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        (self.tmp / "work").mkdir()
        for patcher in (
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.tmp / "extensions"),
            mock.patch.object(config, "SETTINGS_FILE", self.tmp / "settings.json"),
            mock.patch.object(config, "TRUST_FILE", self.tmp / "trusted.json"),
            mock.patch.object(session_mod, "SESSIONS_DIR", self.tmp / "sessions"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = App(
            registry=Registry(),
            session=session_mod.open_session("s", str(self.tmp / "work")),
            cwd=str(self.tmp / "work"),
            auto_approve=True,
            register_commands=register_builtin_commands,
            register_frontends=register_builtin_frontends,
            ui=serve.TOOLKIT_UI,
        )
        self.app.reload_extensions()
        self.app.provider_name, self.app.model = "fake", "m"
        self.host = Host(self.app)

    def provider(self, *replies):
        self.app.provider = ScriptedProvider(replies)

    def start(self) -> Client:
        client = Client(self, self.host)
        hello = client.next()
        self.assertEqual(hello["t"], "hello")
        return client

    def test_hello_describes_minimal(self):
        self.app.messages.append({"role": "user", "content": "earlier"})
        client = Client(self, self.host)
        hello = client.next()
        self.assertEqual(
            sorted(hello),
            ["bodies", "errors", "hidden", "messages", "notice", "protocol", "renderers", "skipped", "status", "t", "version"],
        )
        self.assertEqual((hello["protocol"], hello["version"]), (2, __version__))
        self.assertEqual(hello["messages"], [{"role": "user", "content": "earlier"}])
        self.assertEqual(hello["status"]["model"], "m")
        self.assertEqual(hello["status"]["provider"], "fake")
        self.assertEqual(sorted(hello["status"]), sorted(Host(self.app).status().__dict__))
        self.assertEqual((hello["hidden"], hello["errors"], hello["skipped"], hello["notice"]), (0, [], [], None))
        client.send(t="quit")
        self.assertEqual(client.until("quit")[-1], {"t": "quit"})
        client.server.join(10)
        self.assertFalse(client.server.is_alive())
        self.assertIsNone(client.lines.get(timeout=10))  # the output is closed: the client sees the end

    def test_a_turn_streams_text_and_tool_calls_then_goes_idle(self):
        self.app.registry.register_tool("ping", lambda input, ctx: "pong", "Say pong.")
        self.provider(tool_reply("ping"), text_reply("all done"))
        client = self.start()
        client.send(t="submit", text="  go  ")
        got = client.until("idle")
        kinds = [m["t"] for m in got]
        self.assertEqual(kinds[: kinds.index("tool_call")], [k for k in kinds[: kinds.index("tool_call")] if k in ("panels", "segments")])
        call = next(m for m in got if m["t"] == "tool_call")
        self.assertEqual(call["call"]["name"], "ping")
        self.assertIsNone(call["body"])
        result = next(m for m in got if m["t"] == "tool_result")
        self.assertEqual((result["call"]["name"], result["result"]["content"], result["result"]["is_error"]), ("ping", "pong", False))
        self.assertEqual("".join(m["chunk"] for m in got if m["t"] == "text"), "all done")
        self.assertEqual(kinds.index("tool_call") < kinds.index("tool_result") < kinds.index("text") < kinds.index("turn_end"), True)
        self.assertEqual(next(m for m in got if m["t"] == "turn_end"), {"t": "turn_end", "error": None})
        self.assertEqual(got[-1]["status"]["session_id"], "s")
        self.assertEqual([m["content"] for m in self.app.messages if m["role"] == "user"][0], "go")  # stripped
        client.send(t="quit")
        client.until("quit")

    def test_a_failing_turn_ends_with_the_error(self):
        self.provider()  # no replies: the provider raises
        client = self.start()
        client.send(t="submit", text="hi")
        got = client.until("idle")
        end = next(m for m in got if m["t"] == "turn_end")
        self.assertIsInstance(end["error"], str)

    def test_questions_are_answered_by_the_client(self):
        answers = {}

        def ask(input, ctx):
            answers["ask"] = ctx.ui.ask("Proceed?")
            answers["choose"] = ctx.ui.choose("Pick", [("a", "Alpha"), ("b", "Beta")])
            answers["many"] = ctx.ui.choose_many("Tick", [("x", "X", False), ("y", "Y", True), ("z", "Z", False)])
            answers["text"] = ctx.ui.ask_text("Name?")
            return "asked"

        self.app.registry.register_tool("ask", ask, "Ask the user.")
        self.provider(tool_reply("ask"), text_reply("thanks"))
        client = self.start()
        client.send(t="submit", text="ask me")
        replies = {"ask": True, "choose": "b", "choose_many": ["z", "x", "nope"], "ask_text": "  Ada  "}
        got = client.until("idle", answer=lambda m: replies[m["t"]])
        questions = [m for m in got if "id" in m]
        self.assertEqual([m["t"] for m in questions], ["ask", "choose", "choose_many", "ask_text"])
        self.assertEqual(len({m["id"] for m in questions}), 4)
        self.assertEqual(questions[0]["question"], "Proceed?")
        self.assertEqual(questions[1]["options"], [{"value": "a", "label": "Alpha"}, {"value": "b", "label": "Beta"}])
        self.assertEqual(questions[1]["title"], "Pick")
        self.assertEqual(
            questions[2]["options"],
            [{"value": "x", "label": "X", "checked": False}, {"value": "y", "label": "Y", "checked": True}, {"value": "z", "label": "Z", "checked": False}],
        )
        self.assertEqual(questions[3]["title"], "Name?")
        self.assertEqual(answers, {"ask": True, "choose": "b", "many": ["x", "z"], "text": "Ada"})
        client.send(t="quit")
        client.until("quit")

    def test_a_question_nobody_answers_is_cancelled_when_the_client_quits(self):
        asked = threading.Event()
        answers = []

        def ask(input, ctx):
            asked.set()
            answers.append(ctx.ui.ask("Proceed?"))
            return "done"

        self.app.registry.register_tool("ask", ask, "Ask the user.")
        self.provider(tool_reply("ask"), text_reply("x"))
        client = self.start()
        client.send(t="submit", text="go")
        self.assertTrue(asked.wait(10))
        client.send(t="quit")
        client.server.join(10)
        self.assertFalse(client.server.is_alive())
        self.assertEqual(answers, [False])

    def test_commands_print_to_the_client_as_output(self):
        client = self.start()
        client.send(t="submit", text="/title Parser work")
        got = client.until("idle")
        text = "".join(m["text"] for m in got if m["t"] == "output")
        self.assertIn("Title: Parser work", text)
        self.assertTrue(all(m["stderr"] is False for m in got if m["t"] == "output"))
        self.assertEqual(self.app.session.title, "Parser work")
        client.send(t="submit", text="/exit")
        client.until("quit")
        client.server.join(10)
        self.assertFalse(client.server.is_alive())

    def test_panels_segments_and_renderers_are_drawn_by_python(self):
        registry = self.app.registry
        registry.register_panel("notes", lambda ctx: [["note ", ("bold", "bold")]], area="sidebar")
        registry.register_status("clock", lambda ctx: ("12:00", "muted"), side="left")
        registry.register_renderer("tool:ping", lambda ctx, block: f"{block['call']['name']} -> {(block['result'] or {}).get('content')}")
        registry.register_renderer("fence:demo", lambda ctx, block: "demo!")
        registry.register_tool("ping", lambda input, ctx: "pong", "Say pong.")
        self.provider(tool_reply("ping"), text_reply("ok"))
        client = Client(self, self.host)
        hello = client.next()
        self.assertEqual(hello["renderers"], ["fence:demo", "tool:ping"])
        panels = client.until("panels")[-1]
        self.assertEqual(panels["panels"], [{"name": "notes", "area": "sidebar", "lines": [[["note ", ""], ["bold", "bold"]]]}])
        segments = client.until("segments")[-1]
        self.assertEqual(segments, {"t": "segments", "left": [[["12:00", "muted"]]], "right": []})
        client.send(t="submit", text="go")
        got = client.until("idle")
        call = next(m for m in got if m["t"] == "tool_call")
        result = next(m for m in got if m["t"] == "tool_result")
        self.assertEqual(call["body"], [[["ping -> None", ""]]])
        self.assertEqual(result["body"], [[["ping -> pong", ""]]])
        client.send(t="render", id=7, kind="fence:demo", block={"kind": "fence:demo"})
        rendered = client.until("rendered")[-1]
        self.assertEqual(rendered, {"t": "rendered", "id": 7, "lines": [[["demo!", ""]]]})
        client.send(t="render", id=8, kind="fence:unknown", block={})
        self.assertEqual(client.until("rendered")[-1], {"t": "rendered", "id": 8, "lines": None})
        client.send(t="quit")
        client.until("quit")

    def test_interrupt_stops_the_turn(self):
        started = threading.Event()
        release = threading.Event()

        def slow(input, ctx):
            started.set()
            release.wait(10)
            return "late"

        self.app.registry.register_tool("slow", slow, "Wait.")
        self.provider(tool_reply("slow"), text_reply("never"))
        client = self.start()
        client.send(t="submit", text="go")
        self.assertTrue(started.wait(10))
        client.send(t="interrupt")
        deadline = threading.Timer(0.5, release.set)
        deadline.start()
        self.addCleanup(deadline.cancel)
        got = client.until("idle")
        end = next(m for m in got if m["t"] == "turn_end")
        self.assertIsNotNone(end["error"])
        client.send(t="quit")
        client.until("quit")

    def test_garbage_lines_are_ignored(self):
        self.provider(text_reply("fine"))
        client = self.start()
        client.writer.write("not json\n[1, 2]\n{}\n")
        client.send(t="nope")
        client.send(t="submit", text="hi")
        got = client.until("idle")
        self.assertEqual("".join(m["chunk"] for m in got if m["t"] == "text"), "fine")
        client.send(t="quit")
        client.until("quit")

    def test_a_terminal_command_suspends_the_client(self):
        ran = []
        self.host.register_command("tty", lambda args, ctx: ran.append(args), "Needs the terminal.", terminal=True)
        client = self.start()
        with mock.patch.object(serve, "on_terminal", mock.MagicMock()):
            client.send(t="submit", text="/tty now")
            self.assertEqual(client.until("suspend")[-1], {"t": "suspend"})
            client.send(t="suspended")
            self.assertEqual(client.until("resume")[-1], {"t": "resume"})
            client.until("idle")
        self.assertEqual(ran, ["now"])
        self.assertIsInstance(self.app.frontend, serve.BridgeFrontend)  # the client shows turns again
        client.send(t="quit")
        client.until("quit")

    def test_a_terminal_command_without_a_terminal_tells_the_client(self):
        self.host.register_command("tty", lambda args, ctx: self.fail("ran"), "Needs the terminal.", terminal=True)
        client = self.start()
        with mock.patch.object(serve, "on_terminal", side_effect=OSError("no tty")):
            client.send(t="submit", text="/tty")
            got = client.until("idle")
        note = next(m for m in got if m["t"] == "notify")
        self.assertEqual((note["kind"], note["text"]), ("error", "/tty needs a terminal: no tty"))
        self.assertNotIn("suspend", [m["t"] for m in got])
        client.send(t="quit")
        client.until("quit")


def kinds(stdout: str) -> list[str]:
    """The kind of every protocol line in ``stdout`` (each must be JSON), without the empty-panel updates at start."""
    return [m["t"] for m in map(json.loads, stdout.splitlines()) if m["t"] not in ("panels", "segments")]


EXTENSION = """\
    __api__ = "2"
    import sys

    class Provider:
        name = "scripted"

        def __init__(self):
            self.calls = 0

        def stream(self, *, on_text, **kwargs):
            from minimal.providers.base import Reply

            self.calls += 1
            print("noise from the provider on stdout")
            if self.calls == 1:
                return Reply(
                    content=[{"type": "tool_use", "id": "t1", "name": "ping", "input": {}}], stop_reason="tool_use"
                )
            on_text("hel")
            on_text("lo")
            return Reply(content=[{"type": "text", "text": "hello"}], stop_reason="end_turn")

    def register(minimal):
        print("noise from register on stdout")
        minimal.register_provider("scripted", Provider, "m1")
        minimal.register_tool(name="ping", run=lambda input, ctx: (print("noise from a tool"), "pong")[1])
"""


class ServeProcessTest(unittest.TestCase):
    """``minimal serve`` as a child process: what a real frontend starts."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        (self.tmp / "work").mkdir()
        (self.tmp / "ext.py").write_text(textwrap.dedent(EXTENSION))
        self.env = {**os.environ, "HOME": str(self.tmp), "USERPROFILE": str(self.tmp)}
        for name in ("MINIMAL_PROVIDER", "MINIMAL_MODEL", "MINIMAL_MARKETPLACE"):
            self.env.pop(name, None)

    def serve(self, stdin: str, *args: str, timeout: float = 60):
        return subprocess.run(
            [sys.executable, "-m", "minimal", "serve", *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.tmp / "work",
            env=self.env,
            timeout=timeout,
        )

    def test_stdout_carries_only_protocol_lines(self):
        stdin = json.dumps({"t": "submit", "text": "hi"}) + "\n" + json.dumps({"t": "quit"}) + "\n"
        done = self.serve(stdin, "--provider", "scripted", "-e", str(self.tmp / "ext.py"), "-y")
        self.assertEqual(done.returncode, 0, done.stderr)
        messages = [json.loads(line) for line in done.stdout.splitlines()]  # every line is JSON
        kinds = [m["t"] for m in messages]
        self.assertEqual(kinds[0], "hello")
        self.assertEqual(messages[0]["protocol"], 2)
        self.assertEqual(messages[0]["status"]["provider"], "scripted")
        self.assertEqual(kinds[-1], "quit")
        for kind in ("tool_call", "tool_result", "text", "turn_end", "idle"):
            self.assertIn(kind, kinds)
        self.assertEqual("".join(m["chunk"] for m in messages if m["t"] == "text"), "hello")
        self.assertEqual(kinds.count("hello"), 1)
        # What extensions and tools printed went to stderr (or to the client as output), never into the stream.
        self.assertIn("noise from register on stdout", done.stderr)
        self.assertNotIn("noise", done.stdout.replace('"noise from', ""))
        outputs = "".join(m["text"] for m in messages if m["t"] == "output")
        self.assertIn("noise from the provider on stdout", outputs)

    def test_a_child_process_cannot_write_into_the_protocol(self):
        # Children of minimal inherit descriptor 1, which is stderr under serve.
        (self.tmp / "ext2.py").write_text(
            textwrap.dedent(
                """\
                __api__ = "2"
                import subprocess, sys

                def register(minimal):
                    subprocess.run([sys.executable, "-c", "print('from a child')"])
                """
            )
        )
        done = self.serve(
            json.dumps({"t": "quit"}) + "\n", "--provider", "scripted", "-e", str(self.tmp / "ext.py"), "-e", str(self.tmp / "ext2.py")
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(kinds(done.stdout), ["hello", "quit"])
        self.assertIn("from a child", done.stderr)

    def test_end_of_input_ends_the_session(self):
        done = self.serve("", "--provider", "scripted", "-e", str(self.tmp / "ext.py"))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(kinds(done.stdout), ["hello", "quit"])

    def test_without_a_provider_it_stops_before_the_protocol_starts(self):
        done = self.serve(json.dumps({"t": "quit"}) + "\n")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "")
        self.assertIn("No default provider yet", done.stderr)

    def test_help_and_unknown_options(self):
        done = self.serve("", "--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("minimal serve", done.stdout)
        self.assertIn("docs/serve.md", done.stdout)
        done = self.serve("", "--ui", "console")  # an interface option: serve has none
        self.assertEqual(done.returncode, 2)
        self.assertIn("Unknown option: --ui", done.stderr)

    def test_extensions_needing_textual_are_skipped(self):
        (self.tmp / "ext3.py").write_text(
            '__api__ = "2"\n__frontend__ = "textual"\n\ndef register(minimal):\n    raise AssertionError("must not load")\n'
        )
        done = self.serve(
            json.dumps({"t": "quit"}) + "\n", "--provider", "scripted", "-e", str(self.tmp / "ext.py"), "-e", str(self.tmp / "ext3.py")
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        hello = json.loads(done.stdout.splitlines()[0])
        self.assertEqual(hello["errors"], [])
        self.assertEqual(len(hello["skipped"]), 1)
        self.assertIn("needs a textual frontend", hello["skipped"][0])


class ParseArgsTest(unittest.TestCase):
    def test_serve_takes_the_session_options_but_no_interface_ones(self):
        opts = cli.parse_args(["-c", "-s", "abc", "--provider", "p", "-m", "m", "-e", "x.py", "-y", "--trust-project"], True)
        self.assertEqual(
            (opts.cont, opts.session, opts.provider, opts.model, opts.ext, opts.yes, opts.trust_project, opts.ui),
            (True, "abc", "p", "m", ["x.py"], True, True, None),
        )
        for flag in ("--ui", "--setup", "--login", "--logout"):
            with self.subTest(flag=flag), mock.patch("builtins.print"), self.assertRaises(SystemExit):
                cli.parse_args([flag], True)


if __name__ == "__main__":
    unittest.main()
