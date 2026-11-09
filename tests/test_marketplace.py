import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from minimal import commands, config, extensions, marketplace
from minimal.app import App
from minimal.extensions import load_extensions
from minimal.registry import Registry
from minimal.session import Session

HELLO = '''"""Say hello."""

__version__ = "1.0"


def register(minimal):
    minimal.register_tool(name="hello", run=lambda input, ctx: "hi", description="Say hi")
'''

OCEAN = '''"""Deep blue dark theme."""


def register(minimal):
    minimal.register_theme("ocean", dark=True, background="#0b1622", foreground="#d0e4f5", accent="#4fc3f7")
'''

HELLO_TOOL = '''"""Another hello, as a tool."""


def register(minimal):
    minimal.register_tool(name="hello_tool", run=lambda input, ctx: "hi", description="Say hi")
'''


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class ScriptedFrontend:
    """Answers dialogs from ``answers``, keyed by title prefix; records ``(title, options)``."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def _answer(self, title, options=None):
        self.asked.append((title, options))
        return next(value for prefix, value in self.answers.items() if title.startswith(prefix))

    choose = choose_many = _answer

    def ask_text(self, title):
        return self._answer(title)

    def reloaded(self):
        pass


class MarketplaceTest(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.bare = self.tmp / "market.git"
        self.global_dir = self.tmp / "global"
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "MINIMAL_MARKETPLACE": str(self.bare),
        }
        for patcher in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(marketplace, "CACHE_ROOT", self.tmp / "cache"),
            mock.patch.object(extensions, "GLOBAL_EXTENSIONS_DIR", self.global_dir),
            mock.patch.object(config, "MARKETPLACES_FILE", self.tmp / "marketplaces.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        git("init", "--quiet", "--bare", "--initial-branch=main", str(self.bare))
        self.work = self.tmp / "work"
        git("clone", "--quiet", str(self.bare), str(self.work))
        (self.work / "extensions").mkdir()
        git("checkout", "--quiet", "-b", "main", cwd=self.work)
        self.push_hello(HELLO)

    def push_hello(self, source):
        """Commit ``source`` as ``extensions/hello.py`` and push it to the marketplace."""
        self.push_file("extensions/hello.py", source)

    def push_file(self, relative, source):
        """Commit ``source`` at ``relative`` in the marketplace and push it."""
        path = self.work / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        git("add", ".", cwd=self.work)
        git("commit", "--quiet", "-m", relative, cwd=self.work)
        git("push", "--quiet", "origin", "main", cwd=self.work)

    def run_ext(self, answers):
        """Run ``/ext`` with scripted dialog answers; return the frontend and the printed output."""
        ui = ScriptedFrontend(answers)
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            frontend=ui,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            commands.run_command("/ext", app)
        return ui, out.getvalue()

    def test_search_and_install(self):
        listings = marketplace.search("hello")
        self.assertEqual([(x.name, x.description, x.version) for x in listings], [("hello", "Say hello.", "1.0")])
        self.assertEqual(marketplace.search("nothing-like-this"), [])

        self.assertEqual(marketplace.install("hello"), (self.global_dir / "hello.py", False))
        registry = Registry()
        loaded, errors = load_extensions(registry, str(self.tmp))
        self.assertEqual((loaded, errors), ([self.global_dir / "hello.py"], []))
        self.assertIn("hello", registry.tools)
        self.assertEqual(marketplace.install("hello"), (self.global_dir / "hello.py", True))

        with self.assertRaisesRegex(ValueError, "not in the marketplace"):
            marketplace.install("missing")

    def test_picker_installs_updates_removes(self):
        hello = self.global_dir / "hello.py"
        url = str(self.bare)
        _, out = self.run_ext({"Marketplace": url, "Category": "extensions", "Extensions": ["hello"], "Install": "global"})
        self.assertEqual(hello.read_text(), HELLO)
        self.assertIn("Installed hello at", out)

        self.push_hello(HELLO.replace('"1.0"', '"1.1"'))
        ui, out = self.run_ext(
            {"Marketplace": url, "Category": "extensions", "Extensions": ["hello"], "Update": ["hello"]}
        )
        [(value, label, checked)] = next(opts for title, opts in ui.asked if title.startswith("Extensions"))
        self.assertEqual((value, checked), ("hello", True))
        self.assertIn("update available (1.0 → 1.1)", label)
        self.assertIn('"1.1"', hello.read_text())
        self.assertIn("Updated hello at", out)

        _, out = self.run_ext({"Marketplace": url, "Category": "extensions", "Extensions": []})
        self.assertFalse(hello.exists())
        self.assertIn("Removed", out)

        _, out = self.run_ext({"Marketplace": None})
        self.assertIn("Cancelled.", out)

    def test_categories_install_into_subfolders(self):
        self.push_file("themes/ocean.py", OCEAN)
        self.push_file("tools/hello.py", HELLO_TOOL)
        ui, _ = self.run_ext(
            {"Marketplace": str(self.bare), "Category": "themes", "Themes": ["ocean"], "Install": "global"}
        )
        categories = next(opts for title, opts in ui.asked if title.startswith("Category"))
        self.assertEqual([value for value, _ in categories], ["extensions", "themes", "tools"])
        self.assertEqual((self.global_dir / "themes" / "ocean.py").read_text(), OCEAN)

        # The same name in two categories is ambiguous until the category is given.
        with self.assertRaisesRegex(ValueError, "several categories"):
            marketplace.install("hello")
        self.assertEqual(marketplace.install("hello", category="tools"), (self.global_dir / "tools" / "hello.py", False))
        marketplace.install("hello", category="extensions")
        registry = Registry()
        loaded, errors = load_extensions(registry, str(self.tmp))
        self.assertEqual(errors, [])
        self.assertIn("ocean", registry.themes)
        self.assertLessEqual({"hello", "hello_tool"}, set(registry.tools))  # both hello.py files loaded
        self.assertEqual(
            [(scope, category, path.name) for scope, category, path in marketplace.installed(str(self.tmp))],
            [("global", "extensions", "hello.py"), ("global", "themes", "ocean.py"), ("global", "tools", "hello.py")],
        )

    def test_saved_marketplaces(self):
        self.assertEqual(marketplace.marketplaces(), [str(self.bare), marketplace.DEFAULT_URL])
        other = self.tmp / "other.git"
        git("init", "--quiet", "--bare", str(other))
        url = marketplace.add_marketplace(str(other))
        self.assertEqual(marketplace.marketplaces()[-1], url)
        with self.assertRaisesRegex(ValueError, "already in the list"):
            marketplace.add_marketplace(str(other))
        with self.assertRaises(RuntimeError):
            marketplace.add_marketplace(str(self.tmp / "missing.git"))
        self.assertEqual(marketplace.marketplaces()[-1], url)
        with self.assertRaisesRegex(ValueError, "not a saved marketplace"):
            marketplace.remove_marketplace(marketplace.DEFAULT_URL)
        marketplace.remove_marketplace(url)
        self.assertNotIn(url, marketplace.marketplaces())

    def test_publish_pushes_branch(self):
        path = self.tmp / "greet.py"
        path.write_text('"""Greet people."""\n\n\ndef register(minimal):\n    pass\n')
        real_git = shutil.which("git")
        with mock.patch.object(marketplace.shutil, "which", lambda cmd: real_git if cmd == "git" else None):
            message = marketplace.publish(path)
        self.assertTrue(message.startswith("Pushed branch publish/greet-"), message)
        branches = subprocess.run(
            ["git", "-C", str(self.bare), "branch", "--list", "publish/*"], capture_output=True, text=True, check=True
        ).stdout.split()
        self.assertEqual(len(branches), 1)
        self.assertTrue(branches[0].startswith("publish/greet-"))

    def test_publish_rejects_invalid(self):
        no_register = self.tmp / "noreg.py"
        no_register.write_text('"""No register."""\n')
        with self.assertRaisesRegex(ValueError, r"register\(minimal\)"):
            marketplace.publish(no_register)
        good = self.tmp / "good.py"
        good.write_text('"""Good."""\n\n\ndef register(minimal):\n    pass\n')
        with self.assertRaisesRegex(ValueError, "invalid extension name"):
            marketplace.publish(good, "Bad-Name")


if __name__ == "__main__":
    unittest.main()
