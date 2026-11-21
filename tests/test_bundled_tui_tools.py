"""The marketplace's tui_tools extension: /lazygit, /yazi, /btop... run external terminal UIs; fake executables on PATH stand in."""

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import unittest
from unittest import mock

from minimal.core.app import App
from minimal.core.host import Host
from minimal.core.session import Session
from minimal.extensions.registry import Registry
from minimal.ui.commands import register_builtin_commands
from minimal.ui.tui.app import MinimalTui
from minimal.ui.tui.composer import Composer
from tests.test_bundled_extensions import MARKETPLACE, BundledExtensionTest, RecordingProvider, text_reply

EXTENSION = MARKETPLACE / "extensions" / "tui_tools.py"

# Records how it was run (name, argv, directory, FAKE_VAR) in $FAKE_LOG, optionally writes the paths $FAKE_CHOOSE to the
# --chooser-file it is given (as yazi does), runs $FAKE_SH, and exits with $FAKE_EXIT.
FAKE = """#!/bin/sh
{
  echo "name=$(basename "$0")"
  echo "cwd=$(pwd -P)"
  echo "var=$FAKE_VAR"
  for a in "$@"; do echo "arg=$a"; done
  echo "--"
} >> "$FAKE_LOG"
for a in "$@"; do
  case "$a" in
    --chooser-file=*) chooser="${a#--chooser-file=}"; [ -n "$FAKE_CHOOSE" ] && printf '%s\\n' $FAKE_CHOOSE > "$chooser" ;;
  esac
done
[ -n "$FAKE_SH" ] && eval "$FAKE_SH"
exit ${FAKE_EXIT:-0}
"""


class ToolsTest(BundledExtensionTest):
    """The extension loaded into a real ``App`` (mock frontend) whose PATH holds only fake tools and git."""

    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "fake.log"
        self.project = self.tmp / "project"
        (self.project / "sub").mkdir(parents=True)
        git = shutil.which("git")
        self.assertIsNotNone(git, "these tests need git on PATH")
        env = mock.patch.dict(
            os.environ,
            {
                # system folders for the fake scripts' basename, touch...; git's own folder for the git tools
                "PATH": os.pathsep.join([str(self.bin), "/usr/bin", "/bin", os.path.dirname(git)]),
                "FAKE_LOG": str(self.log),
                "GIT_CONFIG_GLOBAL": os.devnull,  # no signing or hooks of the developer's own configuration
                "GIT_CONFIG_SYSTEM": os.devnull,
            },
        )
        env.start()
        self.addCleanup(env.stop)
        for name in ("FAKE_VAR", "FAKE_CHOOSE", "FAKE_SH", "FAKE_EXIT"):
            os.environ.pop(name, None)
        self.frontend = mock.Mock()

    def install(self, *names):
        for name in names:
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def make(self, settings=None, errors=False):
        if settings is not None:
            (self.tmp / "settings.json").write_text(json.dumps({"extensions": {"settings": {"tui_tools": settings}}}))
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.project),
            auto_approve=True,
            trust_project=True,
            frontend=self.frontend,
            extra_extensions=[str(EXTENSION)],
        )
        _, problems = app.reload_extensions()
        if not errors:
            self.assertEqual(problems, [])
        app.provider, app.model = RecordingProvider([text_reply("ok")]), "m"
        self.addCleanup(app.stop_git)
        self.problems = problems
        return app

    def run_command(self, app, line):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            app.run_command(line)
        return out.getvalue()

    def runs(self):
        """What the fake tools recorded, one dict per run (``args`` in order)."""
        if not self.log.exists():
            return []
        runs = []
        for block in self.log.read_text().split("--\n")[:-1]:
            run = {"args": []}
            for line in block.splitlines():
                key, _, value = line.partition("=")
                if key == "arg":
                    run["args"].append(value)
                else:
                    run[key] = value
            runs.append(run)
        return runs

    def notices(self):
        return [call.args for call in self.frontend.notify.call_args_list]

    def init_repo(self):
        for cmd in (["init", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", *cmd], cwd=self.project, check=True, capture_output=True)
        (self.project / "a.txt").write_text("a\n")
        subprocess.run(["git", "add", "."], cwd=self.project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.project, check=True, capture_output=True)


class RegistrationTest(ToolsTest):
    def test_only_the_tools_found_on_path_become_commands_and_they_suspend_the_ui(self):
        self.install("lazygit", "btop")
        app = self.make()
        commands = app.registry.commands
        self.assertEqual(sorted(commands), ["btop", "lazygit", "tui"])
        self.assertTrue(commands["lazygit"].terminal and commands["btop"].terminal)
        self.assertFalse(commands["tui"].terminal)
        self.assertIn("git UI", commands["lazygit"].description)
        self.assertTrue(Host(app).command("lazygit").terminal)

    def test_a_missing_tool_never_fails_the_load_and_tui_says_how_to_install_it(self):
        self.install("lazygit")
        app = self.make()
        self.assertNotIn("yazi", app.registry.commands)
        listing = self.run_command(app, "/tui")
        self.assertRegex(listing, r"lazygit\s+ready \(.*bin/lazygit\)")
        self.assertRegex(listing, r"yazi\s+missing: brew install yazi")
        self.assertRegex(listing, r"gh-dash\s+missing: brew install gh && gh extension install dlvhdr/gh-dash")
        for name in ("btop", "lazydocker", "k9s", "tig", "gitui"):
            self.assertIn(name, listing)
        self.assertEqual(self.run_command(app, "/yazi").strip(), "Unknown command: /yazi")
        said = self.run_command(app, "/tui yazi")
        self.assertIn("yazi is not installed ('yazi' is not on PATH): brew install yazi; then /reload.", said)

    def test_a_tool_installed_after_the_load_waits_for_a_reload(self):
        app = self.make()
        self.install("btop")
        self.assertRegex(self.run_command(app, "/tui"), r"btop\s+installed since the last load")
        self.assertIn("run /reload, then /btop", self.run_command(app, "/tui btop"))
        app.reload_extensions()
        self.assertIn("btop", app.registry.commands)

    def test_disabled_extension_registers_nothing(self):
        self.install("lazygit")
        app = self.make({"enabled": False})
        self.assertEqual(app.registry.commands, {})

    def test_tui_completes_tool_names(self):
        self.install("lazygit")
        app = self.make()
        complete = app.registry.commands["tui"].complete
        self.assertEqual(complete("lazyg", app.context), ["lazygit"])
        self.assertEqual(complete("la", app.context), ["lazygit", "lazydocker", "last"])
        self.assertEqual(complete("lazygit ", app.context), [])

    def test_every_setting_has_a_description(self):
        namespace = {}
        exec(compile(EXTENSION.read_text(), str(EXTENSION), "exec"), namespace)
        self.assertEqual(set(namespace["SCHEMA"]), set(namespace["DEFAULTS"]))
        for key, spec in namespace["SCHEMA"].items():
            self.assertTrue(spec["description"], key)

    def test_reserved_names_cover_every_command_of_minimal(self):
        registry = Registry()
        register_builtin_commands(registry)
        namespace = {}
        exec(compile(EXTENSION.read_text(), str(EXTENSION), "exec"), namespace)
        self.assertLessEqual({*registry.builtin_commands, "theme", "tui"}, namespace["RESERVED"])


class LaunchTest(ToolsTest):
    def test_lazygit_runs_in_the_project_with_its_path(self):
        self.install("lazygit")
        app = self.make()
        self.assertEqual(self.run_command(app, "/lazygit"), "")
        (run,) = self.runs()
        self.assertEqual(run["name"], "lazygit")
        self.assertEqual(run["args"], ["-p", str(self.project)])
        self.assertEqual(run["cwd"], os.path.realpath(self.project))
        self.assertEqual(self.notices(), [])

    def test_arguments_are_split_like_a_shell_and_appended(self):
        self.install("btop")
        app = self.make()
        self.run_command(app, '/btop --utf-force "a b"')
        self.assertEqual(self.runs()[0]["args"], ["--utf-force", "a b"])

    def test_unbalanced_quotes_are_reported_with_the_fix(self):
        self.install("btop")
        app = self.make()
        self.run_command(app, '/btop "oops')
        self.assertEqual(self.runs(), [])
        (text, kind), = self.notices()
        self.assertEqual(kind, "error")
        self.assertIn("close the quote or remove it", text)

    def test_a_configured_tool_gets_its_cwd_env_and_placeholders(self):
        self.install("fake-tui")
        app = self.make(
            {
                "tools": {
                    "fake": {
                        "command": ["fake-tui", "--in={cwd}", "x"],
                        "cwd": "sub",
                        "env": {"FAKE_VAR": "hello"},
                        "description": "my tool",
                    }
                }
            }
        )
        self.assertIn("my tool", app.registry.commands["fake"].description)
        self.run_command(app, "/fake y")
        (run,) = self.runs()
        sub = os.path.realpath(self.project / "sub")
        self.assertEqual((run["var"], run["cwd"]), ("hello", sub))
        self.assertEqual(run["args"], [f"--in={self.project / 'sub'}", "x", "y"])

    def test_a_builtin_is_overridden_field_by_field(self):
        self.install("lazygit")
        app = self.make({"tools": {"lazygit": {"command": ["lazygit", "--debug"]}}})
        self.run_command(app, "/lazygit")
        self.assertEqual(self.runs()[0]["args"], ["--debug"])
        self.assertIn("git UI", app.registry.commands["lazygit"].description)

    def test_a_disabled_tool_is_not_a_command(self):
        self.install("lazygit", "btop")
        app = self.make({"tools": {"lazygit": {"enabled": False}}})
        self.assertEqual(sorted(app.registry.commands), ["btop", "tui"])
        self.assertIn("lazygit", self.run_command(app, "/tui"))
        self.assertIn("lazygit is disabled", self.run_command(app, "/tui lazygit"))

    def test_a_missing_folder_is_reported_with_the_setting_to_fix(self):
        self.install("btop")
        app = self.make({"tools": {"btop": {"cwd": "nowhere"}}})
        self.run_command(app, "/btop")
        self.assertEqual(self.runs(), [])
        (text, kind), = self.notices()
        self.assertEqual(kind, "error")
        self.assertIn("fix extensions.settings.tui_tools.tools.btop.cwd", text)

    def test_a_failing_exit_status_is_a_warning(self):
        self.install("btop")
        app = self.make()
        os.environ["FAKE_EXIT"] = "3"
        self.run_command(app, "/btop")
        self.assertEqual(self.notices(), [("btop exited with status 3.", "warning")])

    def test_a_tool_that_disappeared_after_the_load_says_how_to_install_it(self):
        self.install("btop")
        app = self.make()
        (self.bin / "btop").unlink()
        self.run_command(app, "/btop")
        (text, kind), = self.notices()
        self.assertEqual(kind, "error")
        self.assertIn("brew install btop; then /reload", text)

    def test_tui_name_queues_the_tools_own_command_with_the_arguments(self):
        self.install("btop")
        app = self.make()
        self.assertEqual(self.run_command(app, "/tui btop -t  dark"), "")
        self.assertEqual(app.take_queued(), "/btop -t  dark")
        self.assertEqual(self.runs(), [])  # nothing ran: only a terminal command may take the terminal
        self.assertIn("Unknown tool 'nope'", self.run_command(app, "/tui nope"))
        self.assertIsNone(app.take_queued())

    def test_a_key_queues_the_command_and_a_bad_key_is_a_load_error(self):
        self.install("lazygit")
        app = self.make({"tools": {"lazygit": {"key": "ctrl+g"}}})
        spec = app.registry.keybindings["ctrl+g"]
        self.assertEqual(spec.id, "tui_tools:lazygit")
        spec.run(app.context)
        self.assertEqual(app.take_queued(), "/lazygit")
        self.assertRegex(self.run_command(app, "/tui"), r"lazygit\s+ready .*\[ctrl\+g\]")
        app = self.make({"tools": {"lazygit": {"key": "ctrl+c"}}}, errors=True)
        self.assertTrue(any("extensions.settings.tui_tools.tools.lazygit.key: " in p for p in self.problems), self.problems)

    def test_no_key_is_bound_by_default(self):
        self.install("lazygit")
        self.assertEqual(self.make().registry.keybindings, {})


class ChooserTest(ToolsTest):
    def choose(self, *names):
        os.environ["FAKE_CHOOSE"] = " ".join(names)

    def test_yazi_gets_a_chooser_file_and_the_chosen_paths_are_listed_as_mentions(self):
        self.install("yazi")
        app = self.make()
        self.choose(str(self.project / "a.txt"), str(self.project / "sub" / "b.py"), "/elsewhere/c.txt")
        self.assertEqual(self.run_command(app, "/yazi"), "")
        (run,) = self.runs()
        flag, folder = run["args"]
        self.assertRegex(flag, r"^--chooser-file=.+\.chooser$")
        self.assertEqual(folder, str(self.project))
        self.assertFalse(os.path.exists(flag.split("=", 1)[1]), "the temporary chooser file is removed")
        expected = ["yazi chose 3 paths: paste or type them in your message", "@a.txt", "@sub/b.py", "/elsewhere/c.txt"]
        self.assertEqual(self.notices(), [("\n".join(expected), "info")])
        self.assertEqual(self.run_command(app, "/tui last").splitlines(), expected)

    def test_choosing_nothing_says_nothing(self):
        self.install("yazi")
        app = self.make()
        self.run_command(app, "/yazi")
        self.assertEqual(self.notices(), [])
        self.assertIn("No tool has reported anything yet", self.run_command(app, "/tui last"))

    def test_paths_with_spaces_get_no_mention_prefix_and_nothing_is_queued_or_sent(self):
        self.install("yazi")
        app = self.make()
        os.environ["FAKE_SH"] = f'printf "%s\\n" "{self.project}/my file.txt" > "$chooser"'
        self.run_command(app, "/yazi")
        ((text, _),) = self.notices()
        self.assertEqual(text.splitlines()[1], "my file.txt")
        self.assertIsNone(app.take_queued())
        self.assertEqual(app.messages, [])

    def test_settings_choose_the_prefix_and_how_many_paths_are_shown(self):
        self.install("yazi")
        app = self.make({"chooser_prefix": "see ", "chooser_max": 2})
        self.choose(*(str(self.project / f"f{i}.txt") for i in range(4)))
        self.run_command(app, "/yazi")
        (text, _), = self.notices()
        self.assertEqual(
            text.splitlines(),
            ["yazi chose 4 paths: paste or type them in your message", "see f0.txt", "see f1.txt", "... and 2 more (/tui last lists all)"],
        )
        self.assertEqual(self.run_command(app, "/tui last").splitlines()[1:], [f"see f{i}.txt" for i in range(4)])

    def test_a_custom_tool_may_use_the_chooser_placeholder(self):
        self.install("pick")
        app = self.make({"tools": {"pick": {"command": ["pick", "--chooser-file={chooser}"]}}})
        self.choose(str(self.project / "a.txt"))
        self.run_command(app, "/pick")
        self.assertIn("@a.txt", self.notices()[0][0])


class GitToolTest(ToolsTest):
    def test_a_git_tool_that_changed_the_tree_says_so_in_one_line(self):
        self.install("lazygit")
        self.init_repo()
        app = self.make()
        os.environ["FAKE_SH"] = f"touch {self.project / 'new.txt'}"
        self.run_command(app, "/lazygit")
        self.assertEqual(self.notices(), [("lazygit changed the working tree: 0 -> 1 changed files.", "info")])
        self.frontend.invalidate.assert_called()  # the view refreshes

    def test_a_branch_switch_is_named(self):
        self.install("lazygit")
        self.init_repo()
        app = self.make()
        os.environ["FAKE_SH"] = f"git -C {self.project} checkout -q -b topic"
        self.run_command(app, "/lazygit")
        self.assertEqual(self.notices(), [("lazygit changed the working tree: branch main -> topic.", "info")])

    def test_nothing_is_said_when_nothing_changed_or_outside_a_repository(self):
        self.install("lazygit", "btop")
        app = self.make()
        self.run_command(app, "/lazygit")  # not a repository
        self.init_repo()
        self.run_command(app, "/lazygit")  # unchanged
        os.environ["FAKE_SH"] = f"touch {self.project / 'x.txt'}"
        self.run_command(app, "/btop")  # not a git tool
        self.assertEqual(self.notices(), [])

    def test_change_note_can_be_turned_off(self):
        self.install("lazygit")
        self.init_repo()
        app = self.make({"change_note": False})
        os.environ["FAKE_SH"] = f"touch {self.project / 'new.txt'}"
        self.run_command(app, "/lazygit")
        self.assertEqual(self.notices(), [])

    def test_any_configured_tool_can_be_a_git_tool(self):
        self.install("fake-tui")
        self.init_repo()
        app = self.make({"tools": {"fake": {"command": ["fake-tui"], "git": True}}})
        os.environ["FAKE_SH"] = f"touch {self.project / 'new.txt'}"
        self.run_command(app, "/fake")
        self.assertIn("fake changed the working tree", self.notices()[0][0])


class BadSettingsTest(ToolsTest):
    def assert_error(self, settings, *needles):
        self.make(settings, errors=True)
        text = "\n".join(self.problems)
        for needle in needles:
            self.assertIn(needle, text)

    def test_tool_names_are_validated_like_command_names(self):
        self.assert_error({"tools": {"Bad Name": {"command": ["x"]}}}, "'Bad Name' is not a tool name", "lowercase")

    def test_a_name_that_would_replace_a_builtin_command_is_refused(self):
        self.assert_error(
            {"tools": {"help": {"command": ["x"]}}},
            "extensions.settings.tui_tools.tools.help: /help is a command of minimal itself; give the tool another name",
        )
        self.assert_error({"tools": {"tui": {"command": ["x"]}}}, "/tui is a command of minimal itself")

    def test_a_new_tool_needs_a_command(self):
        self.assert_error({"tools": {"mine": {"description": "d"}}}, "tools.mine.command is required", '["mine"]')

    def test_fields_are_type_checked_with_the_fix(self):
        self.assert_error(
            {"tools": {"mine": {"command": "mine", "env": {"A": 1}, "enabled": "yes", "colour": 1}}},
            "unknown field 'colour' (known: command, description, cwd, env, enabled, install, git, key)",
        )
        self.assert_error(
            {"tools": {"mine": {"command": "mine", "env": {"A": 1}, "enabled": "yes"}}},
            "tools.mine.command must be a non-empty list of non-empty strings",
            "tools.mine.env must be an object of text values",
            "tools.mine.enabled must be true or false",
        )

    def test_an_entry_must_be_an_object(self):
        self.assert_error({"tools": {"mine": ["mine"]}}, "tools.mine must be an object")

    def test_top_level_settings_are_validated_by_minimal(self):
        self.assert_error({"chooser_max": 0}, "extensions.settings.tui_tools.chooser_max")
        self.assert_error({"change_note": "yes"}, "tui_tools: extensions.settings.tui_tools.change_note must be bool")

    def test_a_bad_setting_loads_nothing_but_does_not_crash(self):
        self.install("lazygit")
        app = self.make({"tools": {"help": {"command": ["x"]}}}, errors=True)
        self.assertNotIn("lazygit", app.registry.commands)
        self.assertIn("Unknown command: /lazygit", self.run_command(app, "/lazygit"))


class TuiTest(ToolsTest, unittest.IsolatedAsyncioTestCase):
    """Through the real terminal UI: a tool's command suspends it, a plain command does not."""

    async def submit(self, pilot, text):
        pilot.app.query_one("#prompt", Composer).set_text(text)
        await pilot.press("enter")

    async def wait_until(self, pilot, condition):
        for _ in range(100):
            await pilot.pause(0.05)
            if condition():
                return
        self.fail("timed out")

    async def run_ui(self, body, settings=None):
        self.install("btop", "lazygit")
        app = self.make(settings)
        suspended = []

        @contextlib.contextmanager
        def suspend(self_):
            suspended.append(True)
            yield

        tui = MinimalTui(Host(app))
        with mock.patch.object(MinimalTui, "suspend", suspend):
            async with tui.run_test() as pilot:
                await body(tui, pilot, app, suspended)

    async def test_the_tool_command_suspends_the_interface_and_tui_does_not(self):
        async def body(tui, pilot, app, suspended):
            await pilot.pause(0.1)
            await self.submit(pilot, "/tui")
            await self.wait_until(pilot, lambda: not tui.busy)
            self.assertEqual((suspended, self.runs()), ([], []))
            shown = " ".join(str(block.render()) for block in tui.query(".output"))
            self.assertIn("Terminal tools", shown)
            os.environ["FAKE_EXIT"] = "2"
            await self.submit(pilot, "/btop --x")
            await self.wait_until(pilot, lambda: not tui.busy and self.runs())
            self.assertEqual(suspended, [True])
            self.assertEqual(self.runs()[0]["args"], ["--x"])
            # what a terminal command prints is gone when the interface returns: the report is a notice
            await pilot.pause(0.1)
            self.assertIn("btop exited with status 2.", [n.message for n in tui._notifications])

        await self.run_ui(body)

    async def test_tui_name_ends_up_suspending_the_interface_too(self):
        async def body(tui, pilot, app, suspended):
            await pilot.pause(0.1)
            await self.submit(pilot, "/tui lazygit log")
            await self.wait_until(pilot, lambda: self.runs() and not tui.busy)
            self.assertEqual(suspended, [True])
            self.assertEqual(self.runs()[0]["args"], ["-p", str(self.project), "log"])

        await self.run_ui(body)

    async def test_the_key_opens_the_tool_through_the_same_suspending_command(self):
        async def body(tui, pilot, app, suspended):
            await pilot.pause(0.1)
            await pilot.press("ctrl+g")
            await self.wait_until(pilot, lambda: self.runs() and not tui.busy)
            self.assertEqual(suspended, [True])

        await self.run_ui(body, {"tools": {"lazygit": {"key": "ctrl+g"}}})


if __name__ == "__main__":
    unittest.main()
