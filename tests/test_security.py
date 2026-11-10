import contextlib
import io
import json
import os
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

from minimal import cli, config, extensions, marketplace
from minimal import session as session_mod
from minimal.agent import execute_tool_call
from minimal.app import App, ConsoleFrontend
from minimal.registry import Registry, TurnInterrupted
from minimal.session import Session
from minimal.tools import clip, register_builtin_tools

HELLO_EXT = 'def register(minimal):\n    minimal.register_tool(name="hello", run=lambda input, ctx: "{reply}")\n'


class TempDirTest(unittest.TestCase):
    """A temp folder as working directory; minimal's files in ``~/.minimal`` are redirected into it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.cwd = self.tmp / "work"
        self.cwd.mkdir()
        for patcher in (
            mock.patch.object(
                extensions, "GLOBAL_EXTENSIONS_DIR", self.home / "extensions"
            ),
            mock.patch.object(config, "TRUST_FILE", self.home / "trusted.json"),
            mock.patch.object(config, "KEYS_FILE", self.home / "keys.json"),
            mock.patch.object(session_mod, "SESSIONS_DIR", self.home / "sessions"),
            mock.patch.dict(os.environ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class PrivateFilesTest(TempDirTest):
    def test_saved_key_is_private_despite_a_planted_tmp_file(self):
        self.home.mkdir(mode=0o755)
        planted = self.home / "keys.tmp"
        planted.write_text("")
        planted.chmod(0o644)
        config.save_key("X_KEY", "s")
        self.assertEqual(mode(config.KEYS_FILE), 0o600)
        self.assertEqual(mode(self.home), 0o700)
        self.assertEqual(planted.read_text(), "")
        self.assertEqual(
            sorted(p.name for p in self.home.iterdir()), ["keys.json", "keys.tmp"]
        )

    def test_sessions_are_private(self):
        session_mod.SESSIONS_DIR.mkdir(parents=True, mode=0o755)
        session = session_mod.open_session("x")
        session.messages.append({"role": "user", "content": "a"})
        session.save()
        self.assertEqual(mode(session_mod.SESSIONS_DIR), 0o700)
        self.assertEqual(mode(session.file), 0o600)

    def test_folders_created_for_sessions_are_private(self):
        self.tmp.chmod(0o755)
        session_mod.open_session("x")
        self.assertEqual(mode(self.home), 0o700)  # ~/.minimal, created on the way
        self.assertEqual(mode(session_mod.SESSIONS_DIR), 0o700)
        self.assertEqual(mode(self.tmp), 0o755)  # an existing parent is left alone


class ProjectTrustTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.ext_dir = self.cwd / ".minimal" / "extensions"
        self.ext_dir.mkdir(parents=True)
        (self.ext_dir / "hello.py").write_text(HELLO_EXT.format(reply="v1"))
        self.frontend = mock.Mock()
        self.app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.cwd),
            auto_approve=True,  # -y answers tool confirmations, not the trust question
            frontend=self.frontend,
        )

    def test_refused_project_extensions_do_not_run(self):
        self.frontend.ask.return_value = False
        self.app.reload_extensions()
        self.assertNotIn("hello", self.app.registry.tools)
        self.assertEqual(len(self.app.extension_errors), 1)
        self.assertIn("not trusted", self.app.extension_errors[0])
        self.frontend.ask.assert_called_once()
        self.assertFalse(config.TRUST_FILE.exists())

    def test_accepted_project_extensions_are_remembered_until_they_change(self):
        self.frontend.ask.return_value = True
        self.app.reload_extensions()
        self.assertIn("hello", self.app.registry.tools)
        self.assertEqual(self.app.extension_errors, [])
        digest = extensions.files_digest([self.ext_dir / "hello.py"])
        self.assertEqual(
            json.loads(config.TRUST_FILE.read_text()),
            {"projects": {str(self.ext_dir): digest}},
        )

        self.app.reload_extensions()
        self.app.reload_extensions()
        self.assertEqual(self.frontend.ask.call_count, 1)

        (self.ext_dir / "hello.py").write_text(HELLO_EXT.format(reply="v2"))
        self.app.reload_extensions()
        self.assertEqual(self.frontend.ask.call_count, 2)
        self.assertEqual(self.app.registry.tools["hello"].run({}, self.app), "v2")

    def test_trust_project_option_does_not_ask(self):
        self.app.trust_project = True
        self.app.reload_extensions()
        self.assertIn("hello", self.app.registry.tools)
        self.frontend.ask.assert_not_called()


class Started(Exception):
    """Raised by the fake ``App`` so ``cli.main`` stops right after building it."""


class CliTest(TempDirTest):
    def main(self, *argv, terminal=True):
        """Run ``cli.main`` with a fake ``App`` that stops it; returns the stderr text and the ``App`` mock."""
        err = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["minimal", *argv]),
            mock.patch.object(cli.setup, "can_run", return_value=terminal),
            mock.patch.object(cli, "App", side_effect=Started) as app,
            contextlib.redirect_stderr(err),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises((Started, SystemExit)) as stopped,
        ):
            cli.main()
        self.stopped = stopped.exception
        return err.getvalue(), app

    def test_without_a_terminal_nothing_starts(self):
        err, app = self.main(terminal=False)
        self.assertEqual(getattr(self.stopped, "code", None), 2)
        self.assertIn("needs a terminal", err)
        app.assert_not_called()

    def test_tool_confirmations_are_skipped_only_with_yes(self):
        self.assertIs(self.main()[1].call_args.kwargs["auto_approve"], False)
        self.assertIs(self.main("-y")[1].call_args.kwargs["auto_approve"], True)

    def test_console_denies_without_a_terminal(self):
        err = io.StringIO()
        with (
            mock.patch.object(sys, "stdin", io.StringIO("y\n")),
            mock.patch("builtins.input") as ask_input,
            contextlib.redirect_stderr(err),
        ):
            self.assertIs(ConsoleFrontend().ask("q"), False)
        ask_input.assert_not_called()
        self.assertIn("denied", err.getvalue())


class ToolTest(TempDirTest):
    """The built-in tools, called the way the agent loop does, with a user who refuses every confirmation."""

    def setUp(self):
        super().setUp()
        self.registry = Registry()
        register_builtin_tools(self.registry)
        self.confirm = mock.Mock(return_value=False)
        self.ctx = SimpleNamespace(cwd=str(self.cwd), confirm=self.confirm)

    def call(self, name, **input):
        with contextlib.redirect_stderr(io.StringIO()):
            return execute_tool_call(
                {"id": "t", "name": name, "input": input}, self.registry, self.ctx
            )

    def run_tool(self, name, **input):
        """Run the tool directly: its exceptions propagate."""
        return self.registry.tools[name].run(input, self.ctx)


class OutsideWorkspaceTest(ToolTest):
    def test_reading_outside_the_workspace_asks_first(self):
        (self.cwd / "in.txt").write_text("inside")
        secret = self.tmp / "secret.txt"
        secret.write_text("hunter2")
        (self.cwd / "link").symlink_to(secret)

        result = self.call("read", path="in.txt")
        self.assertEqual((result["content"], result["is_error"]), ("1\tinside", False))
        self.confirm.assert_not_called()

        for name, input in (
            ("read", {"path": str(secret)}),
            ("read", {"path": "link"}),
            ("grep", {"pattern": "hunter", "path": ".."}),
            ("glob", {"pattern": "*.txt", "path": str(self.tmp)}),
        ):
            with self.subTest(name=name, input=input):
                result = self.call(name, **input)
                self.assertTrue(result["is_error"])
                self.assertIn("denied", result["content"])
                self.assertNotIn("hunter2", result["content"])


class BashEnvironmentTest(ToolTest):
    def test_saved_keys_are_not_in_the_command_environment(self):
        self.confirm.return_value = True
        config.save_key("X_KEY", "s3cret")
        os.environ["SHELL_ONLY"] = "kept"
        self.assertEqual(os.environ["X_KEY"], "s3cret")  # providers still read it
        result = self.call("bash", command='echo "[$X_KEY][$SHELL_ONLY]"')
        self.assertTrue(result["content"].startswith("[][kept]"), result["content"])


class GuardHookTest(ToolTest):
    def test_a_failing_tool_call_handler_blocks_the_call(self):
        run = mock.Mock(return_value="ran")
        self.registry.register_tool(name="probe", run=run)
        self.registry.on("tool_call", lambda event: 1 / 0)
        result = self.call("probe")
        self.assertTrue(result["is_error"])
        self.assertIn("tool_call handler failed", result["content"])
        run.assert_not_called()


class PathValidationTest(TempDirTest):
    def test_session_id_cannot_be_a_path(self):
        for id in ("../x", "a/b", ".hidden", "-s", ""):
            with (
                self.subTest(id=id),
                self.assertRaisesRegex(ValueError, "invalid session id"),
            ):
                session_mod.open_session(id or "/")
        self.assertEqual(
            session_mod.open_session("2026-10-28T21-19-43-183Z").messages, []
        )
        self.assertFalse((self.home / "x.jsonl").exists())

    def test_cli_rejects_a_session_path(self):
        err = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["minimal", "-s", "../../x"]),
            mock.patch.object(cli.setup, "can_run", return_value=True),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as exit,
        ):
            cli.main()
        self.assertEqual(exit.exception.code, 2)
        self.assertIn("invalid session id", err.getvalue())

    def test_marketplace_category_cannot_be_a_path(self):
        with mock.patch.object(marketplace, "sync") as sync:
            with self.assertRaisesRegex(ValueError, "unknown category"):
                marketplace.search("", category="../..")
            with self.assertRaisesRegex(ValueError, "unknown category"):
                marketplace.install("hello", category="../../tools")
        sync.assert_not_called()


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class GitTest(ToolTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            marketplace, "CACHE_ROOT", self.home / "marketplace"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_marketplace_path_starting_with_a_dash_is_not_a_git_option(self):
        subprocess.run(
            ["git", "init", "--quiet", "--bare", "./-m"], cwd=self.cwd, check=True
        )
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.cwd)
        self.assertTrue((marketplace.sync("-m") / ".git").is_dir())

    def test_a_git_command_that_hangs_is_given_up(self):
        fake = self.tmp / "bin"
        fake.mkdir()
        (fake / "git").write_text("#!/bin/sh\nexec sleep 30\n")
        (fake / "git").chmod(0o755)
        os.environ["PATH"] = f"{fake}{os.pathsep}{os.environ['PATH']}"
        start = time.monotonic()
        with (
            mock.patch.object(marketplace, "GIT_TIMEOUT", 0.3),
            self.assertRaisesRegex(RuntimeError, "git clone timed out"),
        ):
            marketplace.sync("https://example.invalid/market.git")
        self.assertLess(time.monotonic() - start, 5)

    def test_repository_config_cannot_make_git_run_a_command(self):
        from minimal.tui import git_status

        marker = self.tmp / "ran"
        hook = self.tmp / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
        hook.chmod(0o755)
        subprocess.run(["git", "init", "--quiet"], cwd=self.cwd, check=True)
        subprocess.run(
            ["git", "config", "core.fsmonitor", str(hook)], cwd=self.cwd, check=True
        )
        (self.cwd / "a.py").write_text("x = 1\n")

        self.assertEqual(self.call("glob", pattern="*.py")["content"], "a.py")
        self.assertIsNotNone(git_status(str(self.cwd)))
        self.assertFalse(marker.exists())


class ConsoleOutputTest(unittest.TestCase):
    def test_tool_text_cannot_send_escape_sequences_to_the_terminal(self):
        err = io.StringIO()
        call = {"name": "bash", "input": {"command": "printf '\x9b2J'"}}
        with contextlib.redirect_stderr(err):
            ConsoleFrontend().tool_call(call)
            ConsoleFrontend().tool_result(
                call, {"is_error": True, "content": "\x1b[2Jboom\rAllow?"}
            )
        self.assertEqual(
            err.getvalue(),
            '\n\x1b[2m› bash {"command": "printf \'\\x9b2J\'"}\x1b[0m\n'
            "\x1b[31m✘ bash: \\x1b[2Jboom\\x0dAllow?\x1b[0m\n",
        )

    def test_answer_is_escaped_on_a_terminal_and_exact_in_a_pipe(self):
        for tty, shown in ((True, "a\\x1b[2J\tb\n"), (False, "a\x1b[2J\tb\n")):
            out = io.StringIO()
            out.isatty = lambda tty=tty: tty
            with contextlib.redirect_stdout(out):
                ConsoleFrontend().text("a\x1b[2J\tb\n")
            self.assertEqual(out.getvalue(), shown)


class ReadTest(ToolTest):
    def read(self, text, **input):
        (self.cwd / "f.txt").write_text(text, newline="")
        return self.run_tool("read", path="f.txt", **input)

    def test_lines_are_numbered_as_split_on_newlines(self):
        self.assertEqual(self.read(""), "1\t")
        self.assertEqual(self.read("a"), "1\ta")
        self.assertEqual(self.read("a\nb\n"), "1\ta\n2\tb\n3\t")
        self.assertEqual(self.read("a\r\nb"), "1\ta\r\n2\tb")
        self.assertEqual(self.read("a\nb\n", offset=10), "")
        self.assertEqual(
            self.read("a\nb\nc", offset=2, limit=1), "2\tb\n... [1 more lines]"
        )

    def test_long_files_are_cut(self):
        short = "x\n" * 2999  # 2999 lines and an empty last one
        self.assertTrue(
            self.read(short).endswith("1999\tx\n2000\tx\n... [1000 more lines]")
        )
        numbered = "".join(
            f"line {i}\n" for i in range(1, 3000)
        )  # the window alone is over MAX_CHARS
        window = (
            "\n".join(f"{i}\tline {i}" for i in range(1, 2001))
            + "\n... [1000 more lines]"
        )
        self.assertEqual(self.read(numbered), clip(window))
        long_line = "x" * 30_000
        self.assertEqual(
            self.read(long_line),
            "1\t" + "x" * 19_998 + "\n... [truncated: 10002 more characters]",
        )

    def test_only_regular_files_are_read(self):
        os.mkfifo(self.cwd / "fifo")
        for path in (".", "fifo"):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(ValueError, "not a regular file"),
            ):
                self.run_tool("read", path=path)


class GrepTest(ToolTest):
    def test_grep_stops_when_the_turn_is_interrupted(self):
        (self.cwd / "a.txt").write_text("foo\n")
        self.ctx.cancelled = threading.Event()
        self.assertEqual(self.run_tool("grep", pattern="foo"), "a.txt:1:foo")
        self.ctx.cancelled.set()
        with self.assertRaises(TurnInterrupted):
            self.run_tool("grep", pattern="foo")

    def test_grep_skips_fifos_outside_git(self):
        (self.cwd / "a.txt").write_text("foo\n")
        os.mkfifo(self.cwd / "fifo")  # opening it would block until a writer appears
        self.assertEqual(self.run_tool("grep", pattern="foo"), "a.txt:1:foo")


class BashCaptureTest(ToolTest):
    def test_output_kept_is_bounded(self):
        out = self.run_tool("bash", command="head -c 3000000 /dev/zero | tr '\\0' a")
        # 1 000 000 captured bytes + "\n[exit 0]" - 20 000 shown; without the cap it would be 2 980 009
        self.assertEqual(
            out, "a" * 20_000 + "\n... [truncated: 980009 more characters]"
        )

    def test_background_child_holding_the_pipe_runs_into_the_timeout(self):
        start = time.monotonic()
        out = self.run_tool("bash", command="sleep 30 & echo hi", timeout_ms=500)
        self.assertIn("hi", out)
        self.assertIn("(timeout)", out)
        self.assertLess(time.monotonic() - start, 3)

    def test_child_that_left_the_process_group_does_not_block_the_result(self):
        escape = f"{sys.executable} -c 'import os, time; os.setsid(); time.sleep(5)' & echo hi"
        start = time.monotonic()
        out = self.run_tool("bash", command=escape, timeout_ms=300)
        self.assertIn("hi", out)
        self.assertIn("(timeout)", out)
        self.assertLess(time.monotonic() - start, 4)


if __name__ == "__main__":
    unittest.main()
