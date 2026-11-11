import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from minimal import config
from minimal.core.agent import execute_tool_call
from minimal.core.app import App
from minimal.core.permissions import Permissions, matches, parse_rule
from minimal.core.session import Session
from minimal.extensions.registry import Registry, Tool
from minimal.tools.builtin import register_builtin_tools


def settings(allow=(), deny=(), scope="global", trusted=True):
    data = {"permissions.allow": list(allow), "permissions.deny": list(deny)}
    return config.Settings((config.Layer(scope, Path("settings.json"), data),), trusted)


class PermissionsTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
