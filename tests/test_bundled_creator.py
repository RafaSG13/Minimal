import json
import os
import unittest
from unittest import mock

from minimal.core.app import App
from minimal.core.session import Session
from minimal.extensions import marketplace
from minimal.extensions.registry import Registry

from tests.test_bundled_extensions import MARKETPLACE, BundledExtensionTest

README_ROW_TOOL = "| `tools/{name}` | `tools/{name}.py` | "


class CreatorTest(BundledExtensionTest):
    """The marketplace ``extensions/creator.py`` loaded into a real ``App`` with a throwaway HOME and project."""

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.home.mkdir()
        self.project.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.project),
            auto_approve=True,
            trust_project=True,
            extra_extensions=[str(MARKETPLACE / "extensions" / "creator.py")],
        )
        _, errors = self.app.reload_extensions()
        self.assertEqual(errors, [])
        self.extensions = self.home / ".minimal" / "extensions"

    def call(self, tool, **input):
        return self.app.registry.tools[tool].run(input, self.app.context)

    def test_ext_new_writes_a_valid_skeleton_of_each_kind_and_never_overwrites(self):
        expected = {
            "extension": self.extensions / "demo.py",
            "tool": self.extensions / "tools" / "demo.py",
            "widget": self.extensions / "widgets" / "demo.py",
            "theme": self.extensions / "themes" / "demo.py",
        }
        for kind, path in expected.items():
            self.assertIn(str(path), self.call("ext_new", name="demo", kind=kind))
            lines, ok = marketplace.check([path])
            self.assertTrue(ok, lines)
            self.assertIn('"demo', path.read_text())  # the name replaced __NAME__
            self.assertNotIn("__NAME__", path.read_text())
            with self.assertRaisesRegex(ValueError, "exists; edit it instead"):
                self.call("ext_new", name="demo", kind=kind)

    def test_ext_new_in_the_project_scope_and_with_bad_arguments(self):
        self.call("ext_new", name="local_tool", kind="tool", scope="project")
        self.assertTrue((self.project / ".minimal" / "extensions" / "tools" / "local_tool.py").is_file())
        for bad in ({"name": "Bad-Name", "kind": "tool"}, {"name": "_x", "kind": "tool"}, {"name": "ok", "kind": "bogus"}):
            with self.assertRaises(ValueError):
                self.call("ext_new", **bad)
        with self.assertRaises(ValueError):
            self.call("ext_new", name="ok", kind="tool", scope="elsewhere")

    def test_ext_template_returns_the_skeleton_without_touching_disk(self):
        source = self.call("ext_template", kind="tool")
        self.assertIn("__NAME__", source)
        self.assertIn("register_tool", source)
        self.assertFalse(self.extensions.exists())
        with self.assertRaisesRegex(ValueError, "unknown kind"):
            self.call("ext_template", kind="bogus")

    def test_ext_check_passes_a_valid_file_and_raises_with_the_reason_for_a_broken_one(self):
        self.call("ext_new", name="good", kind="theme")
        self.assertIn("ok", self.call("ext_check", path=str(self.extensions / "themes" / "good.py")))
        broken = self.project / "broken.py"
        broken.write_text('"""Broken."""\n\n__version__ = "1.0"\n\n\ndef register(minimal):\n    pass\n')
        with self.assertRaisesRegex(RuntimeError, "__api__"):
            self.call("ext_check", path="broken.py")  # relative paths are the project's
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.call("ext_check", path="missing.py")

    def test_ext_api_is_the_api_source_with_its_version(self):
        out = self.call("ext_api")
        self.assertTrue(out.startswith("# minimal extension API 2."), out[:40])
        self.assertIn("API_VERSION", out)

    def test_ext_list_shows_created_items_of_both_scopes_and_skips_private_names(self):
        self.assertEqual(self.call("ext_list"), "No extensions found.")
        self.call("ext_new", name="demo", kind="extension")
        self.call("ext_new", name="shiny", kind="theme", scope="project")
        (self.extensions / "_helper.py").write_text('"""Private."""\n')
        lines = self.call("ext_list").splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith("global extensions/demo 1.0 — demo: one line saying what it does."), lines)
        self.assertTrue(lines[1].startswith("project themes/shiny 1.0 — shiny: a dark theme"), lines)
        self.assertEqual(self.call("ext_list", scope="global").count("\n"), 0)
        self.assertTrue(self.call("ext_list", scope="project").startswith("project themes/shiny"))

    def test_ext_bump_raises_minor_and_major_and_refuses_a_file_without_a_version(self):
        self.call("ext_new", name="demo", kind="tool")
        path = self.extensions / "tools" / "demo.py"
        self.assertEqual(self.call("ext_bump", path=str(path)), "1.0 -> 1.1")
        self.assertIn('__version__ = "1.1"', path.read_text())
        self.assertEqual(self.call("ext_bump", path=str(path), part="major"), "1.1 -> 2.0")
        self.assertEqual(self.call("ext_bump", path=str(path)), "2.0 -> 2.1")
        bare = self.project / "bare.py"
        bare.write_text('"""No version."""\n')
        with self.assertRaisesRegex(ValueError, "__version__"):
            self.call("ext_bump", path=str(bare))

    def test_ext_bump_edits_a_packages_init(self):
        package = self.extensions / "pkg"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text('"""A package."""\n__version__ = "3.9"\n')
        self.assertEqual(self.call("ext_bump", path=str(package)), "3.9 -> 3.10")

    def test_ext_remove_deletes_items_of_the_two_roots_and_nothing_else(self):
        self.call("ext_new", name="demo", kind="tool")
        package = self.project / ".minimal" / "extensions" / "pkg"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text('"""A package."""\n')
        outside = self.tmp / "x.py"
        outside.write_text('"""Outside."""\n')

        self.assertIn("Removed", self.call("ext_remove", path=str(self.extensions / "tools" / "demo.py")))
        self.assertFalse((self.extensions / "tools" / "demo.py").exists())
        self.assertIn("Removed", self.call("ext_remove", path=".minimal/extensions/pkg"))
        self.assertFalse(package.exists())

        for refused in (str(outside), str(self.extensions), str(self.extensions / "tools"), "../x.py"):
            with self.assertRaisesRegex(ValueError, "only extensions under"):
                self.call("ext_remove", path=refused)
        self.assertTrue(outside.exists())
        self.assertTrue(self.extensions.exists())

    def test_ext_catalogue_row_names_the_id_the_path_and_the_description(self):
        self.call("ext_new", name="demo", kind="tool")
        row = self.call("ext_catalogue_row", path=str(self.extensions / "tools" / "demo.py"))
        self.assertTrue(row.startswith(README_ROW_TOOL.format(name="demo")), row)
        self.assertTrue(row.endswith("one line saying what the model gets from it. |"), row)

        self.call("ext_new", name="plain", kind="extension")
        row = self.call("ext_catalogue_row", path=str(self.extensions / "plain.py"))
        self.assertTrue(row.startswith("| `plain` | `extensions/plain.py` | "), row)

        package = self.project / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text('"""A package."""\n')
        self.assertEqual(
            self.call("ext_catalogue_row", path="pkg"), "| `pkg` | `extensions/pkg/` | A package. |"
        )

    def test_create_command_queues_a_message_and_rejects_a_bad_kind(self):
        queued = []
        context = self.app.context
        with mock.patch.object(type(context), "queue_user_message", lambda self, text: queued.append(text)):
            self.app.registry.commands["create"].run("tool word_count counts words", context)
            self.app.registry.commands["create"].run("theme sunset", context)
        self.assertEqual(
            queued,
            [
                "Create a minimal tool named word_count: counts words. Follow the creator workflow.",
                "Create a minimal theme named sunset: ask me what it should do. Follow the creator workflow.",
            ],
        )
        with self.assertRaisesRegex(ValueError, "unknown kind"):
            self.app.registry.commands["create"].run("bogus x", context)
        with self.assertRaisesRegex(ValueError, "usage: /create"):
            self.app.registry.commands["create"].run("tool", context)


    def test_skeletons_teach_api_2_3_and_pass_the_lint_without_a_note(self):
        for kind in ("extension", "tool", "widget"):
            with self.subTest(kind):
                source = self.call("ext_template", kind=kind)
                self.assertIn('__api__ = "2.3"', source)
                self.assertIn("declare_settings(DEFAULTS, SCHEMA)", source)
                self.assertIn("__tags__", source)
        self.assertIn("read_only=True", self.call("ext_template", kind="tool"))
        widget = self.call("ext_template", kind="widget")
        self.assertIn("collapsible=True", widget)
        self.assertIn("order=settings", widget)
        self.assertIn("title=settings", widget)
        self.assertIn("register_keybinding", self.call("ext_template", kind="extension"))
        self.assertIn('__tags__ = ["theme"]', self.call("ext_template", kind="theme"))
        for kind in ("extension", "tool", "widget", "theme"):
            self.call("ext_new", name="demo", kind=kind)
            path = {"extension": "", "tool": "tools/", "widget": "widgets/", "theme": "themes/"}[kind]
            report = self.call("ext_check", path=str(self.extensions / f"{path}demo.py"))
            self.assertEqual([x for x in report.splitlines() if x.startswith(("note", "LINT"))], [], report)

    def test_the_skeleton_settings_are_listed_validated_and_change_the_behaviour(self):
        self.call("ext_new", name="demo", kind="tool")
        tool_file = self.extensions / "tools" / "demo.py"
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t2.jsonl"),
            cwd=str(self.project),
            auto_approve=True,
            trust_project=True,
            extra_extensions=[str(tool_file)],
        )
        _, errors = app.reload_extensions()
        self.assertEqual(errors, [])
        tool = app.registry.tools["demo"]
        self.assertTrue(tool.read_only)
        self.assertEqual(tool.run({"text": "abc"}, app.context), "ABC")
        with self.assertRaisesRegex(ValueError, "over 2000 characters .setting max_chars"):
            tool.run({"text": "x" * 2001}, app.context)

    def test_ext_api_starts_with_the_version_then_the_conventions_then_the_source(self):
        out = self.call("ext_api")
        head, _, rest = out.partition("\n")
        self.assertRegex(head, r"^# minimal extension API 2\.\d+$")
        self.assertIn("declare_settings(DEFAULTS, SCHEMA)", rest)
        self.assertIn("register_css", rest)
        self.assertIn("read_only=True", rest)
        self.assertLess(rest.index("declare_settings"), rest.index("API_VERSION"))

    def write(self, name, source):
        path = self.project / name
        path.write_text(source)
        return path

    def test_ext_check_fails_on_unknown_tags_and_on_an_api_older_than_the_features_used(self):
        self.write(
            "tagged.py",
            '"""Tagged."""\n__version__ = "1.0"\n__api__ = "2"\n__tags__ = ["gadgets"]\n\n\n'
            "def register(minimal):\n    pass\n",
        )
        with self.assertRaisesRegex(RuntimeError, "LINT __tags__ must be a list of marketplace tags .git ui safety"):
            self.call("ext_check", path="tagged.py")
        self.write(
            "old.py",
            '"""Old."""\n__version__ = "1.0"\n__api__ = "2"\n__tags__ = ["ui"]\n\n\n'
            "def register(minimal):\n"
            '    minimal.register_panel("p", lambda ctx: "x", title="P")\n',
        )
        with self.assertRaisesRegex(RuntimeError, r'LINT __api__ is "2" but .*register_panel\(title\).*__api__ = "2.3"'):
            self.call("ext_check", path="old.py")

    def test_ext_check_adds_notes_for_missing_metadata_and_unschemed_settings(self):
        self.write(
            "plain.py",
            '"""Plain."""\n__version__ = "1.0"\n__api__ = "2.3"\n\n\n'
            "def register(minimal):\n"
            '    minimal.declare_settings({"a": 1})\n'
            '    minimal.register_tool(name="t", run=lambda i, c: "x")\n'
            '    minimal.register_panel("p", lambda ctx: "x")\n',
        )
        report = self.call("ext_check", path="plain.py")
        for needle in (
            "note add __tags__",
            "note declare_settings has no schema",
            "note a tool says neither read_only=True",
            "note register_panel has no title=",
        ):
            self.assertIn(needle, report)

    def test_lint_setting_turns_the_extra_checks_off(self):
        (self.tmp / "settings.json").write_text(
            json.dumps({"extensions": {"settings": {"creator": {"lint": False}}}})
        )
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t2.jsonl"),
            cwd=str(self.project),
            auto_approve=True,
            trust_project=True,
            extra_extensions=[str(MARKETPLACE / "extensions" / "creator.py")],
        )
        _, errors = app.reload_extensions()
        self.assertEqual(errors, [])
        self.write("old.py", '"""Old."""\n__version__ = "1.0"\n__api__ = "2"\n__tags__ = ["nope"]\n\n\ndef register(minimal):\n    pass\n')
        report = app.registry.tools["ext_check"].run({"path": "old.py"}, app.context)
        self.assertNotIn("LINT", report)

    def test_default_scope_check_timeout_and_prompt_are_settings(self):
        (self.tmp / "settings.json").write_text(
            json.dumps(
                {"extensions": {"settings": {"creator": {"default_scope": "project", "check_timeout": 7, "prompt": ""}}}}
            )
        )
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t2.jsonl"),
            cwd=str(self.project),
            auto_approve=True,
            trust_project=True,
            extra_extensions=[str(MARKETPLACE / "extensions" / "creator.py")],
        )
        _, errors = app.reload_extensions()
        self.assertEqual(errors, [])
        app.registry.tools["ext_new"].run({"name": "p", "kind": "theme"}, app.context)
        self.assertTrue((self.project / ".minimal" / "extensions" / "themes" / "p.py").is_file())
        self.assertNotIn("ext_search first", app.context.system_prompt())
        self.assertIn("ext_search first", self.app.context.system_prompt())
        with mock.patch("subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("x", 7)) as run:
            with self.assertRaisesRegex(RuntimeError, "timed out after 7s; raise the check_timeout setting"):
                app.registry.tools["ext_check"].run({"path": "."}, app.context)
        self.assertEqual(run.call_args.kwargs["timeout"], 7)

    def test_bad_settings_are_load_errors_with_the_name(self):
        for settings in ({"default_scope": "everywhere"}, {"check_timeout": 0}, {"lint": "yes"}):
            with self.subTest(settings):
                (self.tmp / "settings.json").write_text(json.dumps({"extensions": {"settings": {"creator": settings}}}))
                app = App(
                    registry=Registry(),
                    session=Session("t", self.tmp / "t2.jsonl"),
                    cwd=str(self.project),
                    auto_approve=True,
                    trust_project=True,
                    extra_extensions=[str(MARKETPLACE / "extensions" / "creator.py")],
                )
                _, errors = app.reload_extensions()
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(f"creator.{next(iter(settings))}", errors[0])

    def test_the_read_only_tools_are_marked_and_the_writing_ones_are_not(self):
        tools = self.app.registry.tools
        for name in ("ext_template", "ext_check", "ext_api", "ext_list", "ext_catalogue_row"):
            self.assertTrue(tools[name].read_only, name)
        for name in ("ext_new", "ext_bump", "ext_remove"):
            self.assertFalse(tools[name].read_only, name)


if __name__ == "__main__":
    unittest.main()
