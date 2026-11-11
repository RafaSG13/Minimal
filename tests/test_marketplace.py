import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from minimal import cli, config
from minimal.core.app import App
from minimal.core.session import Session
from minimal.extensions import loader, marketplace
from minimal.extensions.loader import load_extensions
from minimal.extensions.registry import Registry
from minimal.ui import commands

HELLO = '''"""Say hello."""

__version__ = "1.0"


def register(minimal):
    minimal.register_tool(name="hello", run=lambda input, ctx: "hi", description="Say hi")
'''

OCEAN = '''"""Deep blue dark theme."""

__version__ = "1.0"


def register(minimal):
    minimal.register_theme("ocean", dark=True, background="#0b1622", foreground="#d0e4f5", accent="#4fc3f7")
'''

HELLO_TOOL = '''"""Another hello, as a tool."""

__version__ = "1.0"


def register(minimal):
    minimal.register_tool(name="hello_tool", run=lambda input, ctx: "hi", description="Say hi")
'''

PKG_INIT = '''"""A package extension."""

from .helpers import REPLY

__version__ = "1.0"


def register(minimal):
    minimal.register_tool(name="pkg", run=lambda input, ctx: REPLY, description="Reply")
'''

RISKY = '''"""Run a command."""

__version__ = "1.0"

import subprocess


def register(minimal):
    minimal.register_tool(name="risky", run=lambda input, ctx: subprocess.run(["ls"]), description="Run ls")
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

    def ask(self, question):
        return self._answer(question)

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
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.global_dir),
            mock.patch.object(config, "SETTINGS_FILE", self.tmp / "settings.json"),
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
        """Run ``/ext`` or ``/extensions`` with scripted dialog answers; return the frontend and the printed output."""
        ui = ScriptedFrontend(answers)
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            trust_project=True,
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
        loaded, errors = load_extensions(registry, str(self.tmp), config.Settings())
        self.assertEqual((loaded, errors), ([self.global_dir / "hello.py"], []))
        self.assertIn("hello", registry.tools)
        self.assertEqual(marketplace.install("hello"), (self.global_dir / "hello.py", True))

        with self.assertRaisesRegex(ValueError, "not in the marketplace"):
            marketplace.install("missing")

    def test_appledouble_sidecar_is_ignored(self):
        marketplace.install("hello")
        (self.global_dir / "._hello.py").write_bytes(b"\x00\x05\x16\x07\xb0")
        loaded, errors = load_extensions(Registry(), str(self.tmp), config.Settings())
        self.assertEqual((loaded, errors), ([self.global_dir / "hello.py"], []))

    def test_picker_installs_updates_removes(self):
        hello = self.global_dir / "hello.py"
        url = str(self.bare)
        _, out = self.run_ext(
            {"Marketplace": url, "Category": "extensions", "Extensions": ["hello"], "Install": "global", "Allow": True}
        )
        self.assertEqual(hello.read_text(), HELLO)
        self.assertIn("Installed hello at", out)

        self.push_hello(HELLO.replace('"1.0"', '"1.1"'))
        ui, out = self.run_ext(
            {"Marketplace": url, "Category": "extensions", "Extensions": ["hello"], "Update": ["hello"], "Allow": True}
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

    def test_global_install_shows_the_scan_and_needs_a_yes(self):
        self.push_file("extensions/risky.py", RISKY)
        risky = self.global_dir / "risky.py"
        picked = {"Marketplace": str(self.bare), "Category": "extensions", "Extensions": ["risky"], "Install": "global"}

        ui, out = self.run_ext({**picked, "Allow": False})
        self.assertFalse(risky.exists())
        self.assertIn("risky.py", out)
        self.assertIn("runs commands", out)
        self.assertIn("Cancelled.", out)
        self.assertEqual(len([title for title, _ in ui.asked if title.startswith("Allow")]), 1)

        self.run_ext({**picked, "Allow": True})
        self.assertEqual(risky.read_text(), RISKY)

    def test_global_update_shows_the_scan_and_needs_a_yes(self):
        picked = {"Marketplace": str(self.bare), "Category": "extensions", "Extensions": ["hello"]}
        self.run_ext({**picked, "Install": "global", "Allow": True})
        self.push_hello(RISKY.replace("Run a command.", "Say hello."))

        _, out = self.run_ext({**picked, "Update": ["hello"], "Allow": False})
        self.assertEqual((self.global_dir / "hello.py").read_text(), HELLO)
        self.assertIn("runs commands", out)

    def test_project_install_asks_nothing(self):
        self.push_file("extensions/risky.py", RISKY)
        picked = {"Marketplace": str(self.bare), "Category": "extensions", "Extensions": ["risky"], "Install": "project"}
        _, out = self.run_ext(picked)  # no "Allow" answer: asking would raise StopIteration
        self.assertIn("Installed risky at", out)

    def test_model_tool_installs_globally_only_after_the_user_says_yes(self):
        self.push_file("extensions/risky.py", RISKY)
        ui = ScriptedFrontend({"Allow": False})
        app = App(
            registry=Registry(), session=Session("t", self.tmp / "t.jsonl"), cwd=str(self.tmp), auto_approve=True, frontend=ui
        )
        app.reload_extensions()  # registers the marketplace tools
        install = app.registry.tools["ext_install"]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaisesRegex(ValueError, "not installed"):
                install.run({"name": "risky"}, app.context)
            self.assertFalse((self.global_dir / "risky.py").exists())
            self.assertIn("runs commands", out.getvalue())

            ui.answers["Allow"] = True
            self.assertIn("Installed risky", install.run({"name": "risky"}, app.context))
            self.assertTrue((self.global_dir / "risky.py").exists())

            self.assertIn("Installed risky", install.run({"name": "risky", "scope": "project"}, app.context))

    def test_categories_install_into_subfolders(self):
        self.push_file("themes/ocean.py", OCEAN)
        self.push_file("tools/hello.py", HELLO_TOOL)
        ui, _ = self.run_ext(
            {"Marketplace": str(self.bare), "Category": "themes", "Themes": ["ocean"], "Install": "global", "Allow": True}
        )
        categories = next(opts for title, opts in ui.asked if title.startswith("Category"))
        self.assertEqual([value for value, _ in categories], ["extensions", "themes", "tools"])
        self.assertEqual((self.global_dir / "themes" / "ocean.py").read_text(), OCEAN)

        # The same name in two categories is ambiguous until the category is given.
        with self.assertRaisesRegex(ValueError, "several categories"):
            marketplace.install("hello")
        self.assertEqual(
            marketplace.install("hello", category="tools"), (self.global_dir / "tools" / "hello.py", False)
        )
        marketplace.install("hello", category="extensions")
        registry = Registry()
        loaded, errors = load_extensions(registry, str(self.tmp), config.Settings())
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
        path.write_text('"""Greet people."""\n\n__version__ = "1.0"\n\n\ndef register(minimal):\n    pass\n')
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
        good.write_text('"""Good."""\n\n__version__ = "1.0"\n\n\ndef register(minimal):\n    pass\n')
        with self.assertRaisesRegex(ValueError, "invalid extension name"):
            marketplace.publish(good, "Bad-Name")

    def test_publish_rejects_unchanged_file_under_another_name(self):
        path = self.tmp / "local.py"
        path.write_text(HELLO)  # setUp published it as extensions/hello.py
        with self.assertRaisesRegex(ValueError, "already published with identical content"):
            marketplace.publish(path, "hello")

    def test_extension_named_like_a_category_keeps_the_category_folder(self):
        self.push_file("tools/hello.py", HELLO_TOOL)
        self.push_file("extensions/tools.py", HELLO.replace("Say hello.", "Named tools."))
        marketplace.install("hello", category="tools")
        category = self.global_dir / "tools"
        self.assertEqual(marketplace.install("tools", category="extensions"), (self.global_dir / "tools.py", False))
        self.assertTrue((category / "hello.py").is_file())

        package = self.tmp / "other" / "extensions" / "tools"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(HELLO.replace("Say hello.", "Named tools."))
        with self.assertRaisesRegex(ValueError, "cannot be named tools; that is the tools/ category folder"):
            marketplace.install_from(package, "global", str(self.tmp))
        self.assertTrue((category / "hello.py").is_file())

        data = self.global_dir / "data"
        data.mkdir()
        (data / "notes.txt").write_text("mine")
        package = self.tmp / "other" / "extensions" / "data"
        package.mkdir()
        (package / "__init__.py").write_text(HELLO.replace("Say hello.", "Data."))
        with self.assertRaisesRegex(ValueError, "not an extension package"):
            marketplace.install_from(package, "global", str(self.tmp))
        self.assertEqual((data / "notes.txt").read_text(), "mine")

        self.assertEqual(marketplace.uninstall("tools"), self.global_dir / "tools.py")
        self.assertTrue((category / "hello.py").is_file())
        with self.assertRaisesRegex(ValueError, "not installed"):
            marketplace.uninstall("tools")

    def test_files_without_py_are_not_extensions(self):
        resource = self.global_dir / "hello"
        resource.parent.mkdir(parents=True)
        resource.write_text("data")
        marketplace.install("hello")
        self.assertEqual(marketplace.uninstall("hello"), self.global_dir / "hello.py")
        with self.assertRaisesRegex(ValueError, "not installed"):
            marketplace.uninstall("hello")
        self.assertEqual(resource.read_text(), "data")

    def test_publish_refuses_package_symlinks_leaving_it(self):
        secret = self.tmp / "keys.json"
        secret.write_text('{"KEY": "secret"}')
        package = self.tmp / "leaky"
        package.mkdir()
        (package / "__init__.py").write_text(HELLO)
        (package / "inside.txt").write_text("ok")
        (package / "alias.txt").symlink_to(package / "inside.txt")  # stays inside: allowed
        self.assertEqual(marketplace.describe(package).name, "leaky")
        (package / "keys.json").symlink_to(secret)
        with self.assertRaisesRegex(ValueError, "links to .*keys.json; a package may only link to files inside it"):
            marketplace.publish(package)
        branches = subprocess.run(
            ["git", "-C", str(self.bare), "branch", "--list", "publish/*"], capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(branches, "")

    def test_listing_without_version_is_rejected_and_skipped(self):
        unversioned = '"""No version."""\n\n\ndef register(minimal):\n    pass\n'
        self.push_file("extensions/plain.py", unversioned)
        self.assertEqual([x.name for x in marketplace.search()], ["hello"])
        path = self.tmp / "plain.py"
        path.write_text(unversioned)
        with self.assertRaisesRegex(ValueError, "needs a version"):
            marketplace.publish(path)

    def test_package_folder_installs_updates_and_uninstalls(self):
        self.push_file("tools/pkg/__init__.py", PKG_INIT)
        self.push_file("tools/pkg/helpers.py", 'REPLY = "v1"\n')
        self.push_file("tools/pkg/prompt.txt", "v1\n")
        dest, updated = marketplace.install("pkg")
        self.assertEqual((dest, updated), (self.global_dir / "tools" / "pkg", False))
        self.assertEqual((dest / "helpers.py").read_text(), 'REPLY = "v1"\n')
        registry = Registry()
        self.assertEqual(load_extensions(registry, str(self.tmp), config.Settings()), ([dest], []))
        self.assertEqual(registry.tools["pkg"].run({}, None), "v1")

        [entry] = [e for e in marketplace.catalog(str(self.bare), str(self.tmp)) if e.listing.name == "pkg"]
        self.assertEqual((entry.installed, entry.outdated), (["global"], []))
        self.push_file("tools/pkg/prompt.txt", "v2\n")  # a resource-only change is an update too
        [entry] = [e for e in marketplace.catalog(str(self.bare), str(self.tmp)) if e.listing.name == "pkg"]
        self.assertEqual(entry.outdated, ["global"])

        self.assertEqual(marketplace.uninstall("pkg", "global", str(self.tmp), "tools"), dest)
        self.assertFalse(dest.exists())

    def test_incompatible_listing_is_not_installed(self):
        self.push_file("extensions/future.py", HELLO.replace("__version__", '__requires__ = ">=9"\n__version__'))
        [listing] = [x for x in marketplace.search() if x.name == "future"]
        self.assertFalse(listing.compatible)
        with self.assertRaisesRegex(ValueError, "future: requires minimal >=9, this is"):
            marketplace.install("future")


class CheckTest(unittest.TestCase):
    def test_marketplace_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "extensions").mkdir()
            (root / "extensions" / "hello.py").write_text(HELLO)
            (root / "extensions" / "nodoc.py").write_text('__version__ = "1.0"\n\ndef register(minimal):\n    pass\n')
            (root / "extensions" / "future.py").write_text(HELLO.replace("__version__", '__requires__ = ">=9"\n__version__'))
            (root / "extensions" / "needs.py").write_text('"""Needs a module."""\n__version__ = "1.0"\nimport not_installed_anywhere\ndef register(minimal):\n    pass\n')
            lines, ok = marketplace.check([root])
        self.assertFalse(ok)
        self.assertEqual(
            lines,
            [
                "FAIL extensions/future: requires minimal >=9, this is 0.2.0",
                "ok   extensions/hello 1.0",
                "WARN extensions/needs: needs not_installed_anywhere",
                "FAIL extensions/nodoc: add a module docstring; its first line is the marketplace description",
            ],
        )

    def test_broken_import_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "broken.py")
            path.write_text('"""Broken."""\n__version__ = "1.0"\nfrom math import does_not_exist\ndef register(minimal):\n    pass\n')
            [line], ok = marketplace.check([path])
        self.assertFalse(ok)
        self.assertTrue(line.startswith("FAIL "), line)
        self.assertIn("does_not_exist", line)

    def test_cli_check_has_the_tools_the_app_has(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "wraps.py")
            path.write_text(
                '"""Wraps ext_search."""\n__version__ = "1.0"\n\n\ndef register(minimal):\n'
                '    search = minimal.tools["ext_search"]\n'
                '    minimal.register_tool("my_search", lambda input, ctx: search.run(input, ctx))\n'
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit:
                cli.ext_cli(["check", str(path)])
        self.assertEqual((exit.exception.code, out.getvalue()), (0, "ok   extensions/wraps 1.0\n"))


if __name__ == "__main__":
    unittest.main()
