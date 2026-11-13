import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from minimal import cli, config
from minimal.core import session as session_mod
from minimal.core.app import App
from minimal.core.host import Host
from minimal.extensions import loader
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply
from minimal.ui.commands import register_builtin_commands
from minimal.ui.frontends import register_builtin_frontends, run_console


class FakeProvider:
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)

    def stream(self, **kwargs):
        return self.replies.pop(0)


def text_reply(text):
    return Reply(content=[{"type": "text", "text": text}], stop_reason="end_turn")


class FrontendTest(unittest.TestCase):
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
        )
        self.app.reload_extensions()
        self.app.provider_name, self.app.model = "fake", "m"
        self.host = Host(self.app)

    def test_importing_the_api_does_not_import_textual(self):
        # A frontend without Textual must be able to use minimal.api.
        code = "import sys, minimal.api; print(any(m.split('.')[0] == 'textual' for m in sys.modules))"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "False")

    def test_console_sends_messages_runs_commands_and_quits_on_eof(self):
        self.app.provider = FakeProvider([text_reply("first answer"), text_reply("queued answer")])
        lines = iter(["hello", "", "/title Parser work"])

        def fake_input(prompt=""):
            line = next(lines, None)
            if line is None:
                raise EOFError
            if line.startswith("/title"):
                self.host.ctx.queue_user_message("from an extension")
            return line

        out = io.StringIO()
        with mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            run_console(self.host)
        self.assertIn("Title: Parser work", out.getvalue())
        self.assertEqual(self.app.session.title, "Parser work")
        self.assertEqual(  # the queued message was sent between turns, without typing
            [m["content"] for m in self.app.messages if m["role"] == "user"], ["hello", "from an extension"]
        )
        self.assertEqual(self.app.messages[-1]["content"][0]["text"], "queued answer")
        self.assertNotIn("theme", self.host.commands())  # /theme belongs to the terminal UI

    def test_frontend_commands_survive_a_reload_and_complete(self):
        self.host.register_command("zap", lambda args, ctx: print("zapped"))
        self.app.reload_extensions()
        self.assertIn("zap", self.host.commands())
        self.assertEqual(self.host.complete("/za"), ["/zap"])
        self.assertEqual(self.host.complete("/zap now"), [])

    def test_an_extension_frontend_is_picked_with_ui(self):
        (self.tmp / "extensions").mkdir()
        (self.tmp / "extensions" / "plain.py").write_text(
            '"""A frontend."""\n__version__ = "1.0"\n__api__ = "1.2"\n\n'
            "def register(minimal):\n"
            '    minimal.register_frontend("plain", lambda host: print("plain ran", host.status().model))\n'
        )
        out, err = io.StringIO(), io.StringIO()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp / "work")
        argv = ["minimal", "--ui", "plain", "--provider", "fake-provider"]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(cli.setup, "can_run", return_value=True),
            mock.patch.object(cli, "start_provider", lambda app, opts: setattr(app, "model", "m1")),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            cli.main()
        self.assertIn("plain ran m1", out.getvalue())
        with (
            mock.patch.object(sys, "argv", ["minimal", "--ui", "nope"]),
            mock.patch.object(cli.setup, "can_run", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as stopped,
        ):
            cli.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("unknown frontend 'nope'; available: console, plain, tui", err.getvalue())


if __name__ == "__main__":
    unittest.main()
