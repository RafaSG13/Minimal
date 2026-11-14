import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from minimal import cli, config
from minimal.core import session as session_mod
from minimal.core.app import App
from minimal.core.host import Host
from minimal.extensions import loader
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply
from minimal.ui.commands import register_builtin_commands
from minimal.ui import frontends
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
            mock.patch.object(frontends, "shutil", SimpleNamespace(which=lambda binary: None)),  # no minimal-go
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

    def entry_points(self, *points):
        """Make ``minimal.frontends`` list ``points``: ``(name, register)`` pairs, ``register`` a callable or an exception to raise."""

        def load(register):
            def loader():
                if isinstance(register, Exception):
                    raise register
                return register

            return loader

        found = [SimpleNamespace(name=n, value=f"pkg_{n}:register", load=load(r)) for n, r in points]
        patcher = mock.patch.object(frontends, "entry_points", lambda group: found if group == "minimal.frontends" else [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def installed_app(self, **kwargs):
        return App(
            registry=Registry(),
            session=session_mod.open_session("s2", str(self.tmp / "work")),
            cwd=str(self.tmp / "work"),
            auto_approve=True,
            register_commands=register_builtin_commands,
            register_frontends=frontends.register_frontends,
            **kwargs,
        )

    def test_an_installed_package_registers_its_frontend_through_its_entry_point(self):
        calls = []

        def register(registry):
            calls.append(registry)
            registry.register_frontend("tui", print, "a Textual UI", toolkit="textual")

        self.entry_points(("tui", register))
        app = self.installed_app()
        self.assertEqual(calls, [app.registry])
        self.assertEqual(sorted(app.registry.frontends), ["console", "tui"])
        self.assertEqual(app.registry.frontends["tui"].toolkit, "textual")
        self.assertEqual(app.ui, "console")  # the CLI resolves which one to run (cli.resolve_ui), not the App
        _, errors = app.reload_extensions()  # the registry is reset, so they register again
        self.assertEqual((errors, len(calls), sorted(app.registry.frontends)), ([], 2, ["console", "tui"]))
        self.assertIs(calls[1], app.registry)

    def test_the_ui_setting_is_not_replaced_when_it_is_missing(self):
        self.entry_points()
        app = self.installed_app()
        app.reload_extensions()
        self.assertEqual((app.ui, sorted(app.registry.frontends)), ("console", ["console"]))
        (self.tmp / "settings.json").write_text('{"ui": "tui"}')
        app.reload_extensions()
        self.assertEqual(app.ui, "tui")  # asked for, so not replaced: the CLI explains that it is missing

    def test_a_broken_entry_point_is_reported_and_the_rest_still_register(self):
        self.entry_points(
            ("bad", ModuleNotFoundError("No module named 'textual'")),
            ("worse", lambda registry: 1 / 0),
            ("good", lambda registry: registry.register_frontend("good", print)),
        )
        app = self.installed_app()
        _, errors = app.reload_extensions()
        self.assertEqual(sorted(app.registry.frontends), ["console", "good"])
        self.assertEqual(
            errors,
            [
                "minimal.frontends bad (pkg_bad:register): ModuleNotFoundError: No module named 'textual'",
                "minimal.frontends worse (pkg_worse:register): ZeroDivisionError: division by zero",
            ],
        )

    def test_an_entry_point_may_replace_the_console_by_name(self):
        self.entry_points(("c", lambda registry: registry.register_frontend("console", print, "mine")))
        self.assertEqual(self.installed_app().registry.frontends["console"].description, "mine")

    def test_removed_textual_names_say_where_they_went(self):
        from minimal import api, glyphs

        with self.assertRaisesRegex(ImportError, r"minimal\.api\.logo_content was removed in minimal API 2\.0.*minimal_textual"):
            api.logo_content
        with self.assertRaisesRegex(AttributeError, r"theme\.DEFAULT was removed in minimal API 2\.0.*minimal_textual\.theme\.DEFAULT"):
            glyphs.DEFAULT
        with self.assertRaisesRegex(AttributeError, "no attribute 'nope'"):
            glyphs.nope

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

    def test_console_reports_the_extensions_it_skipped(self):
        folder = self.tmp / "extensions" / "console"
        folder.mkdir(parents=True)
        (folder / "pretty.py").write_text(
            '"""Needs Textual."""\n__version__ = "1.0"\n__api__ = "2"\n__frontend__ = "textual"\n\n'
            "def register(minimal):\n    raise AssertionError('must not load under console')\n"
        )
        (self.tmp / "settings.json").write_text('{"ui": "console"}')
        self.assertEqual(self.app.reload_extensions()[1], [])
        err = io.StringIO()
        with mock.patch("builtins.input", side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            run_console(self.host)
        self.assertIn(f"[extension] skipped {folder / 'pretty.py'}: needs a textual frontend; console is not one", err.getvalue())

    def test_frontend_commands_survive_a_reload_and_complete(self):
        self.host.register_command("zap", lambda args, ctx: print("zapped"))
        self.app.reload_extensions()
        self.assertIn("zap", self.host.commands())
        self.assertEqual(self.host.complete("/za"), ["/zap"])
        self.assertEqual(self.host.complete("/zap now"), [])

    def test_an_extension_frontend_is_picked_with_ui(self):
        (self.tmp / "extensions" / "plain").mkdir(parents=True)  # a frontend's folder holds the extension that defines it
        (self.tmp / "extensions" / "plain" / "plain.py").write_text(
            '"""A frontend."""\n__version__ = "1.0"\n__api__ = "2"\n\n'
            "def register(minimal):\n"
            '    minimal.register_frontend("plain", lambda host: print("plain ran", host.status().model))\n'
        )
        out, err = io.StringIO(), io.StringIO()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp / "work")
        self.entry_points()
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
        self.assertIn("unknown frontend 'nope'; available: console", err.getvalue())
        self.assertNotIn("minimal-textual", err.getvalue())
        err = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["minimal", "--ui", "tui"]),
            mock.patch.object(cli.setup, "can_run", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as stopped,
        ):
            cli.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("unknown frontend 'tui'; available: console; install minimal-textual: https://github.com/RafaSG13/minimal-marketplace/tree/main/textual", err.getvalue())

    def go_on_the_path(self, present=True):
        which = lambda binary: f"/usr/bin/{binary}" if present and binary == "minimal-go" else None  # noqa: E731
        patcher = mock.patch.object(frontends, "shutil", SimpleNamespace(which=which))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_go_binary_on_the_path_registers_the_go_frontend_with_its_own_folder(self):
        self.go_on_the_path()
        registry = Registry()
        register_builtin_frontends(registry)
        self.assertEqual(sorted(registry.frontends), ["console", "go"])
        self.assertEqual(registry.frontends["go"].toolkit, "go")
        self.assertEqual([loader.frontend_folder(registry, ui) for ui in ("console", "go", None)], ["console", "go", "console"])
        self.go_on_the_path(present=False)
        gone = Registry()
        register_builtin_frontends(gone)
        self.assertEqual(sorted(gone.frontends), ["console"])
        self.assertEqual(loader.frontend_folder(gone, "go"), "go")  # served without its binary: still its own folder

    def test_the_folder_of_a_frontend_is_its_toolkit_else_its_name(self):
        registry = Registry()
        registry.register_frontend("tui", print, toolkit="textual")
        registry.register_frontend("plain", print)
        self.assertEqual([loader.frontend_folder(registry, ui) for ui in ("tui", "plain")], ["textual", "plain"])


class ResolveUiTest(unittest.TestCase):
    """Which frontend ``minimal`` runs: ``--ui``, then the ``ui`` setting, then what is installed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        (self.tmp / "work").mkdir()
        for patcher in (
            mock.patch.object(config, "SETTINGS_FILE", self.tmp / "settings.json"),
            mock.patch.object(config, "HOME_DIR", self.tmp),
            mock.patch.object(config, "KEYS_FILE", self.tmp / "keys.json"),
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.tmp / "extensions"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def installed(self, *names):
        """Pretend these frontends (besides ``console``) are installed."""

        def scan():
            registry = Registry()
            registry.register_frontend("console", print, "the console")
            for name in names:
                registry.register_frontend(name, print, f"the {name}", toolkit={"tui": "textual"}.get(name, name))
            return registry

        patcher = mock.patch.object(cli, "scan_frontends", scan)
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve(self, explicit=None, choice=None):
        """``cli.resolve_ui`` with the menu answering ``choice``; returns ``(ui, menu options or None)``."""
        shown = []

        def choose(options):
            shown.append(list(options))
            return choice

        with mock.patch.object(cli.setup, "choose_frontend", choose), contextlib.redirect_stdout(io.StringIO()):
            ui = cli.resolve_ui(explicit, str(self.tmp / "work"))
        return ui, shown[0] if shown else None

    def test_the_option_beats_the_setting_which_beats_what_is_installed(self):
        self.installed("tui", "go")
        config.SETTINGS_FILE.write_text('{"ui": "tui"}')
        self.assertEqual(self.resolve("console"), ("console", None))
        self.assertEqual(self.resolve(), ("tui", None))  # no menu: the setting decides

    def test_without_any_frontend_installed_it_is_the_console(self):
        self.installed()
        self.assertEqual(self.resolve(), ("console", None))

    def test_one_installed_frontend_is_used_without_asking(self):
        self.installed("go")
        self.assertEqual(self.resolve(), ("go", None))
        self.assertFalse(config.SETTINGS_FILE.exists())

    def test_several_installed_frontends_ask_once_and_save_the_choice(self):
        self.installed("tui", "go")
        ui, options = self.resolve(choice="go")
        self.assertEqual((ui, list(options)), ("go", ["console", "tui", "go"]))
        self.assertEqual(json.loads(config.SETTINGS_FILE.read_text()), {"ui": "go"})
        self.assertEqual(self.resolve(), ("go", None))  # saved: not asked again

    def test_passthrough_drops_the_ui_option_and_setup_only(self):
        self.assertEqual(
            cli.passthrough_args(["--ui", "go", "-c", "--ui=go", "--setup", "-m", "x", "--provider", "p"]),
            ["-c", "-m", "x", "--provider", "p"],
        )

    def run_main(self, *args):
        """``cli.main()`` with ``minimal-go`` replacing the process (``exec_external`` recorded, ending the run like an exec does); returns what happened."""
        started = []

        def exec_external(name, rest):
            started.append((name, rest))
            raise SystemExit(0)

        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp / "work")
        with (
            mock.patch.object(sys, "argv", ["minimal", *args]),
            mock.patch.object(cli.setup, "can_run", return_value=True),
            mock.patch.object(cli, "exec_external", exec_external),
            mock.patch.object(cli, "ensure_provider", lambda opts: started.append(("provider", opts.setup))),
            mock.patch.object(cli, "open_session", side_effect=AssertionError("the session must not be opened before exec")),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            cli.main()
        return started

    def test_an_external_frontend_is_started_with_minimals_args_before_the_session_exists(self):
        self.installed("go")
        self.assertEqual(
            self.run_main("--ui", "go", "-c", "--setup", "--provider", "openai"),
            [("provider", True), ("go", ["-c", "--provider", "openai"])],
        )
        self.assertEqual(self.run_main("-y"), [("provider", False), ("go", ["-y"])])  # the only one installed


if __name__ == "__main__":
    unittest.main()
