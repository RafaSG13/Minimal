import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from minimal.extensions import loader, marketplace, migrate


def source(doc, frontend=""):
    declared = f'__frontend__ = "{frontend}"\n' if frontend else ""
    return f'"""{doc}"""\n__version__ = "1.0"\n__api__ = "2"\n{declared}\ndef register(minimal):\n    pass\n'


class MigrateTest(unittest.TestCase):
    """The old flat layout of ``~/.minimal/extensions`` moves into one folder per frontend."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "extensions"
        self.base.mkdir()
        patcher = mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, relative, text):
        path = self.base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def migrate(self, folders=("console", "textual")):
        asked = []

        def available():
            asked.append(True)
            return folders

        return migrate.migrate_global(available), bool(asked)

    def test_nothing_to_move_asks_for_nothing(self):
        self.write("console/hello.py", source("Already moved."))
        self.write(".lock.json", "{}")
        self.assertEqual(self.migrate(), (None, False))  # the frontends are not even looked up

    def test_an_item_goes_to_every_available_frontend_and_one_with_a_frontend_only_to_its_own(self):
        self.write("hello.py", source("Hello."))
        self.write("themes/ocean.py", source("Ocean."))
        self.write("widgets/fancy.py", source("Fancy.", "textual"))
        self.write("tools/pkg/__init__.py", source("A package."))
        self.write("tools/pkg/data.txt", "resource")
        notice, _ = self.migrate(("console", "textual", "go"))
        for folder in ("console", "textual", "go"):
            self.assertEqual((self.base / folder / "hello.py").read_text(), source("Hello."))
            self.assertEqual((self.base / folder / "themes" / "ocean.py").read_text(), source("Ocean."))
            self.assertEqual((self.base / folder / "tools" / "pkg" / "data.txt").read_text(), "resource")
        self.assertEqual(sorted(p.name for p in self.base.glob("*/widgets/*.py")), ["fancy.py"])
        self.assertTrue((self.base / "textual" / "widgets" / "fancy.py").is_file())  # only where it says
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ["console", "go", "textual"])  # the old copies are gone
        for line in ("hello -> console/, textual/, go/", "themes/ocean -> console/, textual/, go/", "widgets/fancy -> textual/"):
            self.assertIn(line, notice)

    def test_an_item_for_a_frontend_that_is_not_installed_is_kept_in_its_folder(self):
        self.write("fancy.py", source("Fancy.", "textual"))
        self.migrate(("console",))
        self.assertTrue((self.base / "textual" / "fancy.py").is_file())
        self.assertFalse((self.base / "fancy.py").exists())

    def test_lock_entries_follow_their_items_and_the_old_lock_goes_when_empty(self):
        self.write("hello.py", source("Hello."))
        self.write("themes/ocean.py", source("Ocean.", "textual"))
        entry = {"marketplace": "u", "commit": "c", "version": "1.0", "api": "2", "sha256": "x"}
        self.write(".lock.json", json.dumps({"hello": entry, "themes/ocean": entry}))
        self.migrate()
        self.assertEqual(json.loads((self.base / "console" / ".lock.json").read_text()), {"hello": entry})
        self.assertEqual(
            json.loads((self.base / "textual" / ".lock.json").read_text()), {"hello": entry, "themes/ocean": entry}
        )
        self.assertFalse((self.base / ".lock.json").exists())

    def test_a_different_copy_already_in_a_folder_is_never_overwritten_nor_deleted_from_the_old_place(self):
        old = self.write("hello.py", source("Old hello."))
        mine = self.write("console/hello.py", source("Hello I changed."))
        self.write("other.py", source("Other."))
        notice, _ = self.migrate()
        self.assertEqual(mine.read_text(), source("Hello I changed."))
        self.assertEqual(old.read_text(), source("Old hello."))  # left in place
        self.assertTrue((self.base / "textual" / "hello.py").is_file())  # the other folder got its copy, the old one waits
        self.assertIn("hello: left in place, a different copy is already in console/", notice)
        self.assertTrue((self.base / "console" / "other.py").is_file())  # the rest still moved
        self.assertFalse((self.base / "other.py").exists())

    def test_running_it_again_after_an_interruption_finishes_the_job(self):
        old = self.write("hello.py", source("Hello."))
        self.write("console/hello.py", source("Hello."))  # copied before the run stopped
        self.write("console/.migrating-other.py", "half a copy")  # a staging file of the interrupted run
        self.write("other.py", source("Other."))
        self.migrate()
        self.assertFalse(old.exists())
        self.assertEqual((self.base / "textual" / "hello.py").read_text(), source("Hello."))
        self.assertEqual((self.base / "console" / "other.py").read_text(), source("Other."))
        self.assertEqual(self.migrate(), (None, False))

    def test_files_that_are_not_extensions_stay(self):
        self.write("notes.txt", "mine")
        self.write("_private.py", "x = 1")
        self.write("themes/readme.md", "mine too")
        self.write("hello.py", source("Hello."))
        self.migrate()
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ["_private.py", "console", "notes.txt", "textual", "themes"])
        self.assertEqual((self.base / "themes" / "readme.md").read_text(), "mine too")

    def test_the_project_is_only_reported_never_moved(self):
        with tempfile.TemporaryDirectory() as cwd:
            project = Path(cwd, ".minimal", "extensions")
            (project / "tools").mkdir(parents=True)
            (project / "hello.py").write_text(source("Hello."))
            (project / "tools" / "probe.py").write_text(source("Probe."))
            (project / "console").mkdir()
            (project / "console" / "fine.py").write_text(source("Fine."))
            notice = migrate.project_notice(cwd)
            self.assertIn("hello, tools/probe", notice)
            self.assertNotIn("fine", notice)
            self.assertTrue((project / "hello.py").is_file())
            self.assertIsNone(migrate.project_notice(str(Path(cwd, "elsewhere"))))


class ListingTest(unittest.TestCase):
    def test_frontend_folders_are_the_top_level_folders_holding_a_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in ("console/tools/a.py", "go/widgets/b.py", "textual/src/pkg/c.py", "docs/readme.md", ".git/tools/x.py", "_x/tools/y.py"):
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).write_text("")
            self.assertEqual(marketplace.frontend_folders(root), ["console", "go"])


if __name__ == "__main__":
    unittest.main()
