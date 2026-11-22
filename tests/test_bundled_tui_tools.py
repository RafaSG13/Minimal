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
        # only the few system tools the fake scripts use (basename, touch, git), linked into a folder of their own: the
        # real PATH may hold gh, lazygit... and must not make a tool "found"
        self.sysbin = self.tmp / "sysbin"
        self.sysbin.mkdir()
        for tool in ("basename", "touch", "git"):
            found = shutil.which(tool)
            self.assertIsNotNone(found, f"these tests need {tool} on PATH")
            (self.sysbin / tool).symlink_to(found)
        env = mock.patch.dict(
            os.environ,
            {
                "PATH": os.pathsep.join([str(self.bin), str(self.sysbin)]),
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
        self.draft = ""  # the prompt box of the mock frontend
        self.frontend.draft.side_effect = lambda: self.draft
        self.frontend.set_draft.side_effect = lambda text, append: setattr(self, "draft", self.draft + text if append else text)

    def install(self, *names):
        for name in names:
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def make(self, settings=None, errors=False, warnings=False, project=None, local=None, trusted=True):
        """The app with the extension loaded; ``settings`` are the global ones, ``project`` and ``local`` those of the
        project's two files (the project is trusted unless ``trusted`` is false)."""
        if settings is not None:
            (self.tmp / "settings.json").write_text(json.dumps({"extensions": {"settings": {"tui_tools": settings}}}))
        for name, layer in (("settings.json", project), ("settings.local.json", local)):
            if layer is not None:
                (self.project / ".minimal").mkdir(exist_ok=True)
                (self.project / ".minimal" / name).write_text(json.dumps({"extensions": {"settings": {"tui_tools": layer}}}))
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.project),
            auto_approve=True,
            trust_project=trusted,
            frontend=self.frontend,
            extra_extensions=[str(EXTENSION)],
            register_commands=register_builtin_commands,  # as the CLI does: minimal's own commands exist at register time
        )
        _, problems = app.reload_extensions()
        if not errors:
            self.assertEqual(problems, [])
        if not warnings:
            self.assertEqual(app.extension_problems, [])
        app.provider, app.model = RecordingProvider([text_reply("ok")]), "m"
        self.addCleanup(app.stop_git)
        self.problems = problems
        self.warnings = app.extension_problems
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
        """The toasts (``report_with: notify``)."""
        return [call.args for call in self.frontend.notify.call_args_list]

    def reports(self):
        """The transcript blocks the tools added with ``ctx.show``, as ``(text, kind)``."""
        return [call.args for call in self.frontend.show.call_args_list]

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

    def test_a_tool_may_not_take_the_name_of_a_command_of_minimal_or_of_the_extension(self):
        namespace = {}
        exec(compile(EXTENSION.read_text(), str(EXTENSION), "exec"), namespace)
        app = self.make(warnings=True)
        builtin = app.registry.builtin_commands
        self.assertIn("help", builtin)
        # /theme is added by the terminal UI, so builtin_commands() lacks it at the first load: it is reserved explicitly
        self.assertNotIn("theme", builtin)
        self.assertEqual(namespace["ALWAYS_RESERVED"], {"theme", "tui"})
        tools = {name: {"command": ["x"]} for name in (*builtin, "theme", "tui")}
        app = self.make({"tools": tools}, warnings=True)
        reserved = [name for name in tools if any(f"tools.{name}: /{name} is a command of minimal itself" in w for w in self.warnings)]
        self.assertEqual(sorted(reserved), sorted(tools))


class LaunchTest(ToolsTest):
    def test_lazygit_runs_in_the_project_with_its_path(self):
        self.install("lazygit")
        app = self.make()
        self.assertEqual(self.run_command(app, "/lazygit"), "")
        (run,) = self.runs()
        self.assertEqual(run["name"], "lazygit")
        self.assertEqual(run["args"], ["-p", str(self.project)])
        self.assertEqual(run["cwd"], os.path.realpath(self.project))
        self.assertEqual((self.reports(), self.notices()), ([], []))

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
        (text, kind), = self.reports()
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
        (text, kind), = self.reports()
        self.assertEqual(kind, "error")
        self.assertIn("fix extensions.settings.tui_tools.tools.btop.cwd", text)

    def test_a_failing_exit_status_is_a_warning(self):
        self.install("btop")
        app = self.make()
        os.environ["FAKE_EXIT"] = "3"
        self.run_command(app, "/btop")
        self.assertEqual(self.reports(), [("btop exited with status 3.", "output")])

    def test_a_tool_that_disappeared_after_the_load_says_how_to_install_it(self):
        self.install("btop")
        app = self.make()
        (self.bin / "btop").unlink()
        self.run_command(app, "/btop")
        (text, kind), = self.reports()
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

    def test_a_key_is_a_terminal_key_that_runs_the_program_itself_and_a_bad_key_is_a_warning(self):
        self.install("lazygit")
        app = self.make({"tools": {"lazygit": {"key": "ctrl+g"}}})
        spec = app.registry.keybindings["ctrl+g"]
        self.assertEqual(spec.id, "tui_tools:lazygit")
        self.assertTrue(spec.terminal)
        spec.run(app.context)
        self.assertIsNone(app.take_queued())  # no /lazygit queued: the key ran the tool under the suspended UI
        (run,) = self.runs()
        self.assertEqual((run["name"], run["args"]), ("lazygit", ["-p", str(self.project)]))
        self.assertRegex(self.run_command(app, "/tui"), r"lazygit\s+ready .*\[ctrl\+g\]")
        app = self.make({"tools": {"lazygit": {"key": "ctrl+c"}}}, warnings=True)
        (warning,) = self.warnings
        self.assertIn("extensions.settings.tui_tools.tools.lazygit.key: ", warning)
        self.assertIn("/lazygit works without the key", warning)
        self.assertIn("lazygit", app.registry.commands)  # the tool itself still loads
        self.assertEqual(app.registry.keybindings, {})

    def test_a_key_reports_through_the_transcript_and_refreshes_git_like_the_command(self):
        self.install("lazygit")
        self.init_repo()
        app = self.make({"tools": {"lazygit": {"key": "ctrl+g"}}})
        os.environ["FAKE_SH"] = f"touch {self.project / 'new.txt'}"
        with mock.patch.object(app, "poke_git") as poke:
            app.registry.keybindings["ctrl+g"].run(app.context)
        poke.assert_called()
        self.assertEqual(self.reports(), [("lazygit changed the working tree: 0 -> 1 changed files.", "output")])

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
        expected = ["yazi chose 3 paths: added to your message", "@a.txt", "@sub/b.py", "/elsewhere/c.txt"]
        self.assertEqual(self.reports(), [("\n".join(expected), "output")])
        self.assertEqual(self.notices(), [])
        self.assertEqual(self.run_command(app, "/tui last").splitlines(), expected)
        self.assertEqual(self.draft, "@a.txt @sub/b.py /elsewhere/c.txt ")

    def test_a_fake_fzf_flow_ends_in_the_composer_after_what_was_typed(self):
        self.install("fzf")
        app = self.make({"tools": {"fzf": {"command": ["fzf", "--chooser-file={chooser}"], "key": "ctrl+f"}}})
        self.draft = "explain"
        os.environ["FAKE_SH"] = f'printf "%s\\n" "{self.project}/a.txt" "{self.project}/my file.txt" > "$chooser"'
        app.registry.keybindings["ctrl+f"].run(app.context)
        self.assertEqual(self.draft, 'explain @a.txt "my file.txt" ')
        self.assertIsNone(app.take_queued())
        self.assertEqual(app.messages, [])  # filled, never sent
        self.assertIn("fzf chose 2 paths: added to your message", self.reports()[0][0])
        self.draft = "see "
        self.run_command(app, "/fzf")
        self.assertEqual(self.draft, 'see @a.txt "my file.txt" ')  # no extra space after one that is there

    def test_chooser_to_draft_can_be_turned_off_and_the_report_keeps_the_old_wording(self):
        self.install("yazi")
        app = self.make({"chooser_to_draft": False})
        self.choose(str(self.project / "a.txt"))
        self.run_command(app, "/yazi")
        self.assertEqual(self.draft, "")
        self.assertEqual(self.reports(), [("yazi chose 1 path: paste or type them in your message\n@a.txt", "output")])

    def test_without_a_prompt_box_the_report_says_to_paste_them(self):
        self.install("yazi")
        app = self.make()
        self.frontend.set_draft.side_effect = None  # the plain terminal ignores set_draft
        self.choose(str(self.project / "a.txt"))
        self.run_command(app, "/yazi")
        self.assertEqual(self.reports()[0][0].splitlines()[0], "yazi chose 1 path: paste or type them in your message")

    def test_report_with_notify_shows_a_toast_instead_of_a_block(self):
        self.install("yazi", "btop")
        app = self.make({"report_with": "notify"})
        self.choose(str(self.project / "a.txt"))
        self.run_command(app, "/yazi")
        os.environ["FAKE_EXIT"] = "3"
        self.run_command(app, "/btop")
        self.assertEqual(
            self.notices(),
            [("yazi chose 1 path: added to your message\n@a.txt", "info"), ("btop exited with status 3.", "warning")],
        )
        self.assertEqual(self.reports(), [])
        self.assertEqual(self.run_command(app, "/tui last"), "btop exited with status 3.\n")
        self.make({"report_with": "toast"}, errors=True)
        self.assertTrue(any("report_with" in p for p in self.problems), self.problems)

    def test_choosing_nothing_says_nothing(self):
        self.install("yazi")
        app = self.make()
        self.run_command(app, "/yazi")
        self.assertEqual((self.reports(), self.draft), ([], ""))
        self.assertIn("No tool has reported anything yet", self.run_command(app, "/tui last"))

    def test_paths_with_spaces_get_no_mention_prefix_and_nothing_is_queued_or_sent(self):
        self.install("yazi")
        app = self.make()
        os.environ["FAKE_SH"] = f'printf "%s\\n" "{self.project}/my file.txt" > "$chooser"'
        self.run_command(app, "/yazi")
        ((text, _),) = self.reports()
        self.assertEqual(text.splitlines()[1], "my file.txt")
        self.assertIsNone(app.take_queued())
        self.assertEqual(app.messages, [])

    def test_settings_choose_the_prefix_and_how_many_paths_are_shown(self):
        self.install("yazi")
        app = self.make({"chooser_prefix": "see ", "chooser_max": 2})
        self.choose(*(str(self.project / f"f{i}.txt") for i in range(4)))
        self.run_command(app, "/yazi")
        (text, _), = self.reports()
        self.assertEqual(
            text.splitlines(),
            ["yazi chose 4 paths: added to your message", "see f0.txt", "see f1.txt", "... and 2 more (/tui last lists all)"],
        )
        self.assertEqual(self.run_command(app, "/tui last").splitlines()[1:], [f"see f{i}.txt" for i in range(4)])

    def test_a_custom_tool_may_use_the_chooser_placeholder(self):
        self.install("pick")
        app = self.make({"tools": {"pick": {"command": ["pick", "--chooser-file={chooser}"]}}})
        self.choose(str(self.project / "a.txt"))
        self.run_command(app, "/pick")
        self.assertIn("@a.txt", self.reports()[0][0])


class GitToolTest(ToolsTest):
    def test_a_git_tool_that_changed_the_tree_says_so_in_one_line(self):
        self.install("lazygit")
        self.init_repo()
        app = self.make()
        os.environ["FAKE_SH"] = f"touch {self.project / 'new.txt'}"
        self.run_command(app, "/lazygit")
        self.assertEqual(self.reports(), [("lazygit changed the working tree: 0 -> 1 changed files.", "output")])
        self.frontend.invalidate.assert_called()  # the view refreshes

    def test_a_branch_switch_is_named(self):
        self.install("lazygit")
        self.init_repo()
        app = self.make()
        os.environ["FAKE_SH"] = f"git -C {self.project} checkout -q -b topic"
        self.run_command(app, "/lazygit")
        self.assertEqual(self.reports(), [("lazygit changed the working tree: branch main -> topic.", "output")])

    def test_nothing_is_said_when_nothing_changed_or_outside_a_repository(self):
        self.install("lazygit", "btop")
        app = self.make()
        self.run_command(app, "/lazygit")  # not a repository
        self.init_repo()
        self.run_command(app, "/lazygit")  # unchanged
        os.environ["FAKE_SH"] = f"touch {self.project / 'x.txt'}"
        self.run_command(app, "/btop")  # not a git tool
        self.assertEqual(self.reports(), [])

    def test_change_note_can_be_turned_off(self):
        self.install("lazygit")
        self.init_repo()
        app = self.make({"change_note": False})
        os.environ["FAKE_SH"] = f"touch {self.project / 'new.txt'}"
        self.run_command(app, "/lazygit")
        self.assertEqual(self.reports(), [])

    def test_any_configured_tool_can_be_a_git_tool(self):
        self.install("fake-tui")
        self.init_repo()
        app = self.make({"tools": {"fake": {"command": ["fake-tui"], "git": True}}})
        os.environ["FAKE_SH"] = f"touch {self.project / 'new.txt'}"
        self.run_command(app, "/fake")
        self.assertIn("fake changed the working tree", self.reports()[0][0])


class DeepMergeTest(ToolsTest):
    GLOBAL = {"tools": {"one": {"command": ["one"], "description": "first", "env": {"A": "1"}}, "lazygit": {"key": "ctrl+g"}}}

    def test_a_project_adds_or_changes_one_tool_without_replacing_the_global_ones(self):
        self.install("one", "two", "lazygit")
        app = self.make(
            self.GLOBAL,
            project={"tools": {"two": {"command": ["two"]}, "lazygit": {"command": ["lazygit", "--debug"]}, "one": {"description": "mine"}}},
            local={"tools": {"two": {"description": "personal"}}},
        )
        self.assertEqual(sorted(app.registry.commands), ["lazygit", "one", "tui", "two"])
        self.assertIn("mine", app.registry.commands["one"].description)  # the project's field...
        self.run_command(app, "/one")
        self.assertEqual(self.runs()[-1]["name"], "one")  # ...next to the global command
        self.assertIn("personal", app.registry.commands["two"].description)  # local over project
        self.run_command(app, "/lazygit")
        self.assertEqual(self.runs()[-1]["args"], ["--debug"])
        self.assertIn("ctrl+g", app.registry.keybindings)  # the global key survives the project's command
        self.assertEqual(app.context.settings.get("extensions.settings.tui_tools.tools.one.env"), {"A": "1"})

    def test_the_layers_merge_nested_objects_and_a_higher_layer_replaces_lists_and_scalars(self):
        self.install("one")
        app = self.make(
            self.GLOBAL, project={"tools": {"one": {"command": ["one", "--p"], "env": {"B": "2"}}}}
        )
        self.assertEqual(
            app.context.settings.get("extensions.settings.tui_tools.tools.one"),
            {"command": ["one", "--p"], "description": "first", "env": {"A": "1", "B": "2"}},
        )
        self.run_command(app, "/one")
        self.assertEqual(self.runs()[0]["args"], ["--p"])

    def test_an_untrusted_project_adds_nothing(self):
        self.install("one", "two")
        app = self.make(self.GLOBAL, project={"tools": {"two": {"command": ["two"]}}}, trusted=False)
        self.assertEqual(sorted(app.registry.commands), ["one", "tui"])

    def test_the_setting_declares_the_deep_merge(self):
        app = self.make()
        spec = app.registry.declared_settings["tui_tools"]
        self.assertEqual(spec.schema["tools"]["merge"], "deep")


class BadSettingsTest(ToolsTest):
    def assert_warning(self, settings, *needles):
        """Loads: the problem is a warning of the extension, not a failed load."""
        app = self.make(settings, warnings=True)
        text = "\n".join(self.warnings)
        for needle in needles:
            self.assertIn(needle, text)
        self.assertIn("tui", app.registry.commands)
        return app

    def test_tool_names_are_validated_like_command_names(self):
        self.assert_warning({"tools": {"Bad Name": {"command": ["x"]}}}, "'Bad Name' is not a tool name", "lowercase")

    def test_a_name_that_would_replace_a_builtin_command_is_refused(self):
        self.assert_warning(
            {"tools": {"help": {"command": ["x"]}}},
            "extensions.settings.tui_tools.tools.help: /help is a command of minimal itself; give the tool another name",
        )
        self.assert_warning({"tools": {"tui": {"command": ["x"]}}}, "/tui is a command of minimal itself")
        self.assert_warning({"tools": {"theme": {"command": ["x"]}}}, "/theme is a command of minimal itself")

    def test_a_new_tool_needs_a_command(self):
        self.assert_warning({"tools": {"mine": {"description": "d"}}}, "tools.mine.command is required", '["mine"]')

    def test_fields_are_type_checked_with_the_fix(self):
        self.assert_warning(
            {"tools": {"mine": {"command": "mine", "env": {"A": 1}, "enabled": "yes", "colour": 1}}},
            "unknown field 'colour' (known: command, description, cwd, env, enabled, install, git, key)",
        )
        self.assert_warning(
            {"tools": {"mine": {"command": "mine", "env": {"A": 1}, "enabled": "yes"}}},
            "tools.mine.command must be a non-empty list of non-empty strings",
            "tools.mine.env must be an object of text values",
            "tools.mine.enabled must be true or false",
        )

    def test_an_entry_must_be_an_object(self):
        self.assert_warning({"tools": {"mine": ["mine"]}}, "tools.mine must be an object")

    def test_top_level_settings_are_validated_by_minimal(self):
        self.make({"chooser_max": 0}, errors=True)
        self.assertIn("extensions.settings.tui_tools.chooser_max", "\n".join(self.problems))
        self.make({"change_note": "yes"}, errors=True)
        self.assertIn("tui_tools: extensions.settings.tui_tools.change_note must be bool", "\n".join(self.problems))

    def test_a_bad_entry_is_skipped_with_a_warning_and_the_other_tools_load(self):
        self.install("lazygit", "btop", "good")
        app = self.make(
            {
                "tools": {
                    "help": {"command": ["x"]},
                    "mine": {"description": "no command"},
                    "btop": {"enabled": "yes"},  # a built-in with a bad field keeps its own definition
                    "good": {"command": ["good"], "description": "fine"},
                }
            },
            warnings=True,
        )
        self.assertEqual(len(self.warnings), 3, self.warnings)
        self.assertTrue(all(w.startswith("tui_tools: ") and w.endswith("skipped)") for w in self.warnings), self.warnings)
        self.assertEqual(sorted(app.registry.commands), ["btop", "good", "lazygit", "tui"])
        self.assertIn("resource monitor", app.registry.commands["btop"].description)
        self.run_command(app, "/lazygit")
        self.assertEqual(self.runs()[0]["name"], "lazygit")
        self.assertIn("Warning: tui_tools: ", app.reload_summary())  # shown on every reload, no "Failed:"
        self.assertNotIn("Failed", app.reload_summary())


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
        self.install("btop", "lazygit", "yazi")
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

    @staticmethod
    def outputs(tui):
        return " ".join(str(block.render()) for block in tui.query(".output"))

    async def test_the_tool_command_suspends_the_interface_and_tui_does_not(self):
        async def body(tui, pilot, app, suspended):
            await pilot.pause(0.1)
            await self.submit(pilot, "/tui")
            await self.wait_until(pilot, lambda: not tui.busy)
            self.assertEqual((suspended, self.runs()), ([], []))
            self.assertIn("Terminal tools", self.outputs(tui))
            os.environ["FAKE_EXIT"] = "2"
            await self.submit(pilot, "/btop --x")
            await self.wait_until(pilot, lambda: not tui.busy and self.runs())
            self.assertEqual(suspended, [True])
            self.assertEqual(self.runs()[0]["args"], ["--x"])
            # what a terminal command prints is gone when the interface returns: the report is a block that stays
            await self.wait_until(pilot, lambda: "btop exited with status 2." in self.outputs(tui))
            self.assertEqual(list(tui._notifications), [])

        await self.run_ui(body)

    async def test_tui_name_ends_up_suspending_the_interface_too(self):
        async def body(tui, pilot, app, suspended):
            await pilot.pause(0.1)
            await self.submit(pilot, "/tui lazygit log")
            await self.wait_until(pilot, lambda: self.runs() and not tui.busy)
            self.assertEqual(suspended, [True])
            self.assertEqual(self.runs()[0]["args"], ["-p", str(self.project), "log"])

        await self.run_ui(body)

    async def test_the_key_runs_the_tool_directly_under_the_suspended_interface(self):
        async def body(tui, pilot, app, suspended):
            await pilot.pause(0.1)
            os.environ["FAKE_EXIT"] = "4"
            await pilot.press("ctrl+g")
            await self.wait_until(pilot, lambda: self.runs() and not tui.busy)
            self.assertEqual(suspended, [True])
            self.assertEqual(self.runs()[0]["args"], ["-p", str(self.project)])
            await self.wait_until(pilot, lambda: "lazygit exited with status 4." in self.outputs(tui))
            self.assertIsNone(app.take_queued())

        await self.run_ui(body, {"tools": {"lazygit": {"key": "ctrl+g"}}})

    async def test_what_yazi_picked_lands_in_the_prompt_box_after_the_resume(self):
        async def body(tui, pilot, app, suspended):
            await pilot.pause(0.1)
            composer = tui.query_one("#prompt", Composer)
            composer.set_text("look at")
            os.environ["FAKE_CHOOSE"] = f"{self.project / 'a.txt'} {self.project / 'sub' / 'b.py'}"
            await pilot.press("alt+y")
            await self.wait_until(pilot, lambda: self.runs() and not tui.busy)
            self.assertEqual(suspended, [True])
            await self.wait_until(pilot, lambda: "yazi chose 2 paths: added to your message" in self.outputs(tui))
            self.assertEqual(composer.text, "look at @a.txt @sub/b.py ")
            self.assertEqual(app.messages, [])

        await self.run_ui(body, {"tools": {"yazi": {"key": "alt+y"}}})


if __name__ == "__main__":
    unittest.main()
