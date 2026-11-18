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


if __name__ == "__main__":
    unittest.main()
