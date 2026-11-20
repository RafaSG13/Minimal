import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from minimal import config
from minimal.core.agent import execute_tool_call
from minimal.core.app import App, ConsoleFrontend
from minimal.core.permissions import Permissions, matches, parse_rule, suggest_rule
from minimal.core.session import Session
from minimal.extensions.registry import Registry, Tool
from minimal.tools.builtin import register_builtin_tools


def settings(allow=(), deny=(), scope="global", trusted=True):
    data = {"permissions.allow": list(allow), "permissions.deny": list(deny)}
    return config.Settings((config.Layer(scope, Path("settings.json"), data),), trusted)


class PermissionsBase(unittest.TestCase):
    """Built-in tools run through the agent loop's ``execute_tool_call`` with an App's ``permit``."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = Path(tmp.name).resolve()
        (self.cwd / "src" / "a").mkdir(parents=True)
        (self.cwd / ".env").write_text("SECRET=1\n")
        self.registry = Registry()
        register_builtin_tools(self.registry)
        self.app = App(
            registry=self.registry, session=Session("t", self.cwd / "t.jsonl"), cwd=str(self.cwd), auto_approve=False
        )
        self.confirm = mock.Mock(return_value=False)

    def call(self, name, rules, **input):
        self.app.permissions = Permissions(rules)
        with contextlib.redirect_stderr(io.StringIO()):
            return execute_tool_call(
                {"id": "t", "name": name, "input": input},
                self.registry,
                self.app.context,
                confirm=self.confirm,
                permit=self.app.permit,
            )


class PermissionsTest(PermissionsBase):
    """Rules checked through ``execute_tool_call``."""

    def test_allowed_command_runs_without_asking(self):
        result = self.call("bash", settings(allow=["bash(git status*)"]), command="git status -s")
        self.confirm.assert_not_called()
        self.assertNotIn("denied", result["content"])

    def test_chained_command_still_asks(self):
        result = self.call("bash", settings(allow=["bash(git status*)"]), command="git status && rm x")
        self.confirm.assert_called_once()
        self.assertTrue(result["is_error"])
        self.assertIn("the user denied the execution", result["content"])

    def test_deny_wins_over_auto_approve(self):
        self.app.auto_approve = True
        result = self.call("read", settings(deny=["read(.env)"], scope="project"), path=".env")
        self.assertTrue(result["is_error"])
        self.assertIn("denied by permission rule 'read(.env)' (project settings)", result["content"])
        self.assertNotIn("SECRET", result["content"])

    def test_deny_sees_every_command_inside_a_compound_one(self):
        rules = settings(deny=["bash(rm -rf*)"])
        for command in (
            "rm -rf x",
            "cd /tmp && rm -rf x",
            "echo a; rm -rf x",
            "true || rm -rf x",
            "ls | rm -rf x",
            "sleep 1 & rm -rf x",
            "echo a\nrm -rf x",
            "echo $(rm -rf x)",
            "echo `rm -rf x`",
            "(rm -rf x)",
            "{ rm -rf x; }",
            "if true; then rm -rf x; fi",
        ):
            with self.subTest(command=command):
                result = self.call("bash", rules, command=command)
                self.assertIn("denied by permission rule 'bash(rm -rf*)'", result["content"])

    def test_deny_ignores_a_command_that_only_mentions_the_pattern(self):
        result = self.call("bash", settings(deny=["bash(rm -rf*)"]), command="echo rm -rf is dangerous")
        self.confirm.assert_called_once()  # not denied: it asks as usual
        self.assertNotIn("denied by permission rule", result["content"])

    def test_path_glob_allows_inside_the_folder_only(self):
        rules = settings(allow=["write(src/**)"])
        result = self.call("write", rules, path="src/a/b.py", content="x")
        self.confirm.assert_not_called()
        self.assertFalse(result["is_error"])
        result = self.call("write", rules, path="docs/x.md", content="x")
        self.confirm.assert_called_once()
        self.assertTrue(result["is_error"])
        self.assertFalse((self.cwd / "docs" / "x.md").exists())

    def test_absolute_and_home_patterns_match_inside_the_folder(self):
        (self.cwd / ".ssh").mkdir()
        (self.cwd / ".ssh" / "id_rsa").write_text("KEY\n")
        with mock.patch.dict("os.environ", {"HOME": str(self.cwd)}):
            result = self.call("read", settings(deny=["read(~/.ssh/**)"]), path=".ssh/id_rsa")
        self.assertIn("denied by permission rule", result["content"])
        result = self.call("read", settings(deny=[f"read({self.cwd}/.env)"]), path=".env")
        self.assertIn("denied by permission rule", result["content"])
        self.assertNotIn("SECRET", result["content"])

    def test_tilde_in_the_path_is_resolved_as_the_tools_do(self):
        (self.cwd / "~").symlink_to(self.cwd)  # read("~/.env") opens <cwd>/~/.env, not the home folder's
        self.app.auto_approve = True
        result = self.call("read", settings(deny=["read(.env)"]), path="~/.env")
        self.assertIn("denied by permission rule 'read(.env)'", result["content"])
        self.assertNotIn("SECRET", result["content"])

    def test_extension_tool_subject(self):
        fetch = Tool("fetch", "", {}, lambda input, ctx: "ok", confirm=True, subject_key="url")
        self.assertTrue(matches("fetch(https://example.com/*)", fetch, {"url": "https://example.com/a/b"}, "/"))
        self.assertFalse(matches("fetch(https://example.com/*)", fetch, {"url": "https://evil.com/"}, "/"))
        no_subject = Tool("other", "", {}, lambda input, ctx: "ok")
        self.assertTrue(matches("other", no_subject, {}, "/"))
        self.assertFalse(matches("other(x)", no_subject, {"x": "x"}, "/"))

    def test_untrusted_project_allow_does_not_apply(self):
        result = self.call("bash", settings(allow=["bash"], scope="project", trusted=False), command="ls")
        self.confirm.assert_called_once()
        self.assertTrue(result["is_error"])

    def test_invalid_rules(self):
        with self.assertRaisesRegex(ValueError, "invalid permission rule"):
            parse_rule("bash git")
        self.assertEqual(parse_rule("read(src/**)"), ("read", "src/**"))


class SuggestRuleTest(PermissionsBase):
    """The rules the permission dialog offers for "this session" and "always" (``setUp`` as above)."""

    def suggest(self, name, **input):
        return suggest_rule(self.registry.tools[name], input, str(self.cwd))

    def test_bash_gets_the_command_and_subcommand_not_everything(self):
        self.assertEqual(self.suggest("bash", command="git status --short"), "bash(git status*)")
        self.assertEqual(self.suggest("bash", command="uv run pytest -q"), "bash(uv run*)")
        self.assertEqual(self.suggest("bash", command="ls"), "bash(ls)")

    def test_a_bash_command_that_runs_other_programs_or_has_options_matches_only_itself(self):
        for command in ("python script.py", "sudo ls", "xargs rm", "rm -rf build", "ls -la", "FOO=1 make"):
            with self.subTest(command=command):
                self.assertEqual(self.suggest("bash", command=command), f"bash({command})")

    def test_bash_wildcards_in_the_command_are_escaped(self):
        rule = self.suggest("bash", command="ls *.py")
        self.assertEqual(rule, "bash(ls [*].py)")
        self.assertTrue(matches(rule, self.registry.tools["bash"], {"command": "ls *.py"}, str(self.cwd)))
        self.assertFalse(matches(rule, self.registry.tools["bash"], {"command": "ls a.py"}, str(self.cwd)))

    def test_a_bash_command_with_shell_metacharacters_gets_no_rule_at_all(self):
        for command in ("git status; rm x", "ls && rm x", "ls | sh", "echo `id`", "echo $(id)", "ls > f", "cat < f", "ls\nrm x"):
            with self.subTest(command=command):
                self.assertIsNone(self.suggest("bash", command=command))

    def test_file_tools_get_the_exact_path_relative_to_the_project(self):
        self.assertEqual(self.suggest("write", path="src/a/b.py", content="x"), "write(src/a/b.py)")
        self.assertEqual(self.suggest("write", path=str(self.cwd / "src" / "b.py"), content="x"), "write(src/b.py)")
        outside = Path(tempfile.gettempdir()).resolve() / "elsewhere.txt"
        self.assertEqual(self.suggest("write", path=str(outside), content="x"), f"write({outside})")
        rule = self.suggest("write", path="a*{b}?.py", content="x")
        self.assertEqual(rule, "write(a[*][{]b}[?].py)")
        write = self.registry.tools["write"]
        self.assertTrue(matches(rule, write, {"path": "a*{b}?.py"}, str(self.cwd)))
        self.assertFalse(matches(rule, write, {"path": "axb.py"}, str(self.cwd)))
        self.assertIsNone(self.suggest("write", path="a[1].py", content="x"))  # a path glob cannot spell a literal [

    def test_a_tool_without_a_subject_gets_its_bare_name(self):
        self.assertEqual(self.suggest("reload_extensions"), "reload_extensions")

    def test_a_session_rule_allows_the_next_matching_call_but_deny_still_wins(self):
        self.app.permissions = Permissions(settings(deny=["bash(git push*)"]))
        self.app.permissions.allow_session("bash(git *)")
        with contextlib.redirect_stderr(io.StringIO()):
            for command, asked in (("git status", False), ("git push origin x", False), ("rm x", True)):
                self.confirm.reset_mock()
                result = execute_tool_call(
                    {"id": "t", "name": "bash", "input": {"command": command}},
                    self.registry,
                    self.app.context,
                    confirm=self.confirm,
                    permit=self.app.permit,
                )
                self.assertEqual(self.confirm.called, asked, command)
                if command == "git push origin x":
                    self.assertIn("denied by permission rule 'bash(git push*)'", result["content"])
        self.app.permissions.allow_session("bash(git *)")
        self.assertEqual(self.app.permissions.session, ["bash(git *)"])  # no duplicates
        with self.assertRaisesRegex(ValueError, "invalid permission rule"):
            self.app.permissions.allow_session("bash git")


class AllowAlwaysTest(unittest.TestCase):
    """The dialog's answers through ``App.confirm`` with a scripted frontend, and the file ``always`` writes."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.cwd = self.tmp / "work"
        self.cwd.mkdir()
        patcher = mock.patch.object(config, "TRUST_FILE", self.tmp / "trusted.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.frontend = mock.Mock()
        self.app = App(
            registry=Registry(), session=Session("t", self.tmp / "t.jsonl"), cwd=str(self.cwd), auto_approve=False,
            frontend=self.frontend,
        )
        register_builtin_tools(self.app.registry)
        self.local = self.cwd / ".minimal" / "settings.local.json"

    def confirm(self, answer, name="bash", **input):
        self.frontend.permission.return_value = answer
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            allowed = self.app.confirm({"id": "c", "name": name, "input": input})
        return allowed, out.getvalue(), err.getvalue()

    def permitted(self, command):
        return self.app.permit(self.app.registry.tools["bash"], {"command": command})

    def test_always_writes_the_rule_to_the_local_settings_and_the_next_call_runs(self):
        allowed, out, _ = self.confirm("always", command="git status -s")
        self.assertTrue(allowed)
        self.assertEqual(json.loads(self.local.read_text()), {"permissions": {"allow": ["bash(git status*)"]}})
        self.assertIn("Saved the rule bash(git status*)", out)
        self.assertIn(str(self.local), out)
        self.frontend.permission.assert_called_once_with(
            "Allow bash: git status -s?", "bash(git status*)", str(self.local)
        )
        self.assertTrue(self.permitted("git status"))
        self.assertFalse(self.permitted("git push"))
        self.assertEqual(self.local.stat().st_mode & 0o777, 0o600)

    def test_the_saved_rule_applies_after_a_restart_without_asking_to_trust_again(self):
        self.confirm("always", command="git status")
        self.app.reload_extensions()
        self.assertEqual([r for r, _ in self.app.settings.allow], ["bash(git status*)"])
        self.assertIsNone(self.app.untrusted)
        self.assertTrue(self.permitted("git status -s"))

    def test_always_keeps_what_the_file_already_has_and_does_not_repeat_a_rule(self):
        self.local.parent.mkdir()
        self.local.write_text(json.dumps({"theme": "x", "permissions": {"allow": ["read"], "deny": ["bash(rm*)"]}}))
        self.app.reload_extensions()  # the project asks to be trusted for "read": the user trusts that first
        snapshot = self.app.untrusted
        assert snapshot is not None
        config.save_trusted(snapshot.key, snapshot.digest)
        self.confirm("always", command="git status")
        self.confirm("always", command="git status -s")
        self.assertEqual(
            json.loads(self.local.read_text()),
            {"theme": "x", "permissions": {"allow": ["read", "bash(git status*)"], "deny": ["bash(rm*)"]}},
        )

    def test_this_session_allows_in_memory_only_and_says_so(self):
        allowed, out, _ = self.confirm("session", command="git status")
        self.assertTrue(allowed)
        self.assertIn("Allowing bash(git status*) for this session", out)
        self.assertFalse(self.local.exists())
        self.assertTrue(self.permitted("git status -s"))
        self.app.reload_extensions()  # a reload keeps it
        self.assertTrue(self.permitted("git status -s"))

    def test_once_and_deny_add_nothing(self):
        self.assertEqual(self.confirm("once", command="git status")[0], True)
        self.assertEqual(self.confirm("deny", command="git status")[0], False)
        self.assertFalse(self.permitted("git status"))
        self.assertFalse(self.local.exists())

    def test_a_command_with_metacharacters_is_offered_no_rule_and_a_wider_answer_adds_none(self):
        allowed, out, _ = self.confirm("always", command="git status; rm -rf x")
        self.assertTrue(allowed)
        self.frontend.permission.assert_called_once_with("Allow bash: git status; rm -rf x?", None, None)
        self.assertEqual(self.app.permissions.session, [])
        self.assertFalse(self.local.exists())
        self.assertEqual(out, "")

    def test_an_untrusted_project_is_offered_this_session_only(self):
        extensions = self.cwd / ".minimal" / "extensions"
        extensions.mkdir(parents=True)
        (extensions / "x.py").write_text('__api__ = "2"\ndef register(minimal):\n    pass\n')
        allowed, out, _ = self.confirm("always", command="git status")
        self.frontend.permission.assert_called_once_with("Allow bash: git status?", "bash(git status*)", None)
        self.assertTrue(allowed)
        self.assertFalse(self.local.exists())
        self.assertIn("for this session", out)

    def test_a_broken_settings_file_is_left_alone_and_the_rule_still_holds_for_the_session(self):
        self.local.parent.mkdir()
        self.local.write_text("{not json")
        allowed, _, err = self.confirm("always", command="git status")
        self.assertTrue(allowed)
        self.assertIn("not valid JSON", err)
        self.assertEqual(self.local.read_text(), "{not json")
        self.assertTrue(self.permitted("git status -s"))

    def test_add_allow_rule_validates_the_rule_and_the_file(self):
        with self.assertRaisesRegex(ValueError, "invalid permission rule"):
            config.add_allow_rule(str(self.cwd), "bash git")
        self.local.parent.mkdir()
        for text, problem in (
            ("[]", "not a JSON object"),
            ('{"permissions": []}', "permissions must be an object"),
            ('{"permissions": {"allow": "bash"}}', "permissions.allow must be a list of strings"),
        ):
            self.local.write_text(text)
            with self.assertRaisesRegex(ValueError, problem):
                config.add_allow_rule(str(self.cwd), "bash")
            self.assertEqual(self.local.read_text(), text)

    def test_the_console_asks_yes_or_no_only(self):
        with mock.patch.object(ConsoleFrontend, "ask", return_value=False):
            self.assertEqual(ConsoleFrontend().permission("Allow bash: ls?", "bash(ls)", "/x"), "deny")
        with mock.patch.object(ConsoleFrontend, "ask", return_value=True):
            self.assertEqual(ConsoleFrontend().permission("Allow bash: ls?", "bash(ls)", "/x"), "once")


if __name__ == "__main__":
    unittest.main()
