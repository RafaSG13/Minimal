import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from minimal import cli, config
from minimal.core.app import App
from minimal.core.session import Session
from minimal.extensions import loader, marketplace
from minimal.extensions.loader import load_extensions
from minimal.extensions.registry import Registry
from minimal.ui.commands import BUILTIN_COMMANDS

HELLO = '''"""Say hello."""

__version__ = "1.0"
__api__ = "1"


def register(minimal):
    minimal.register_tool(name="hello", run=lambda input, ctx: "hi", description="Say hi")
'''

OCEAN = '''"""Deep blue dark theme."""

__version__ = "1.0"
__api__ = "1"


def register(minimal):
    minimal.register_theme("ocean", dark=True, background="#0b1622", foreground="#d0e4f5", accent="#4fc3f7")
'''

HELLO_TOOL = '''"""Another hello, as a tool."""

__version__ = "1.0"
__api__ = "1"


def register(minimal):
    minimal.register_tool(name="hello_tool", run=lambda input, ctx: "hi", description="Say hi")
'''

PKG_INIT = '''"""A package extension."""

from .helpers import REPLY

__version__ = "1.0"
__api__ = "1"


def register(minimal):
    minimal.register_tool(name="pkg", run=lambda input, ctx: REPLY, description="Reply")
'''

RISKY = '''"""Run a command."""

__version__ = "1.0"
__api__ = "1"

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

    def head(self):
        """The commit the marketplace's main branch is at."""
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.work, check=True, capture_output=True, text=True
        ).stdout.strip()

    def lock(self, directory=None):
        """The parsed ``.lock.json`` of an extensions folder (default: the global one)."""
        return json.loads(((directory or self.global_dir) / ".lock.json").read_text())

    def entry(self, name):
        [entry] = [e for e in marketplace.catalog(str(self.bare), str(self.tmp)) if e.listing.name == name]
        return entry

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
            commands=BUILTIN_COMMANDS,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            app.run_command("/ext")
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

    def test_picker_labels_and_skips_an_extension_for_another_api(self):
        self.push_file("extensions/future.py", HELLO.replace('__api__ = "1"', '__api__ = "9"'))
        ui, out = self.run_ext(
            {"Marketplace": str(self.bare), "Category": "extensions", "Extensions": ["hello", "future"], "Install": "global", "Allow": True}
        )
        options = next(opts for title, opts in ui.asked if title.startswith("Extensions"))
        labels = {value: label for value, label, _ in options}
        self.assertIn("not for this minimal (API 9)", labels["future"])
        self.assertNotIn("not for this minimal", labels["hello"])
        self.assertIn("Skipped future: written for minimal API 9, this minimal has API 1.0", out)
        self.assertTrue((self.global_dir / "hello.py").exists())
        self.assertFalse((self.global_dir / "future.py").exists())

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
        path.write_text('"""Greet people."""\n\n__version__ = "1.0"\n__api__ = "1"\n\n\ndef register(minimal):\n    pass\n')
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
        good.write_text('"""Good."""\n\n__version__ = "1.0"\n__api__ = "1"\n\n\ndef register(minimal):\n    pass\n')
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
        unversioned = '"""No version."""\n__api__ = "1"\n\n\ndef register(minimal):\n    pass\n'
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
        self.push_file("extensions/future.py", HELLO.replace('__api__ = "1"', '__api__ = "9"'))
        [listing] = [x for x in marketplace.search() if x.name == "future"]
        self.assertFalse(listing.compatible)
        self.assertEqual(listing.api, "9")
        self.assertIn("written for minimal API 9, this minimal has API 1.0", listing.problem)
        with self.assertRaisesRegex(ValueError, "future: written for minimal API 9, this minimal has API 1.0"):
            marketplace.install("future")
        self.assertFalse((self.global_dir / "future.py").exists())

    def test_compatible_listing_has_no_problem(self):
        [listing] = marketplace.search("hello")
        self.assertEqual((listing.api, listing.problem, listing.compatible), ("1", None, True))

    def test_install_records_a_lock_entry_and_uninstall_removes_it(self):
        marketplace.install("hello")
        digest = hashlib.sha256(HELLO.encode()).hexdigest()
        self.assertEqual(
            self.lock(),
            {"hello": {"marketplace": str(self.bare), "commit": self.head(), "version": "1.0", "api": "1", "sha256": digest}},
        )
        self.assertEqual((self.global_dir / ".lock.json").stat().st_mode & 0o777, 0o600)
        registry = Registry()
        loaded, errors = load_extensions(registry, str(self.tmp), config.Settings())
        self.assertEqual((loaded, errors), ([self.global_dir / "hello.py"], []))  # the lock is not an extension

        self.push_file("themes/ocean.py", OCEAN)
        marketplace.install("ocean")
        self.assertEqual(sorted(self.lock()), ["hello", "themes/ocean"])
        marketplace.uninstall("ocean", category="themes")
        self.assertEqual(sorted(self.lock()), ["hello"])
        marketplace.uninstall("hello")
        self.assertFalse((self.global_dir / ".lock.json").exists())

    def test_project_scope_has_its_own_lock(self):
        marketplace.install("hello", "project", str(self.tmp))
        folder = self.tmp / ".minimal" / "extensions"
        self.assertEqual(self.lock(folder)["hello"]["commit"], self.head())
        self.assertFalse((self.global_dir / ".lock.json").exists())
        self.assertEqual(loader.extension_entries(folder), [folder / "hello.py"])  # the lock is not an extension

        # A copy installed from a local folder no longer comes from the marketplace.
        marketplace.install_from(self.work / "extensions" / "hello.py", "project", str(self.tmp))
        self.assertFalse((folder / ".lock.json").exists())

    def test_an_older_compatible_version_is_offered_and_installed(self):
        compatible = self.head()
        self.push_hello(HELLO.replace('"1.0"', '"2.0"').replace('__api__ = "1"', '__api__ = "9"'))

        [listing] = marketplace.search("hello")
        self.assertEqual((listing.version, listing.api, listing.compatible), ("1.0", "1", True))
        [release] = marketplace.releases("hello")
        self.assertEqual((release.commit, release.newer.api), (compatible, "9"))
        entry = self.entry("hello")
        self.assertEqual((entry.listing.version, entry.release.commit), ("1.0", compatible))
        self.assertIn("[latest for this minimal; newer needs API 9]", marketplace._ext_search({"query": "hello"}, None))

        dest, updated = marketplace.install("hello")
        self.assertEqual((dest.read_text(), updated), (HELLO, False))
        self.assertEqual(self.lock()["hello"]["commit"], compatible)
        self.assertEqual(self.entry("hello").outdated, [])  # installed the offered version

        ui, _ = self.run_ext({"Marketplace": str(self.bare), "Category": "extensions", "Extensions": ["hello"]})
        [(_, label, _)] = next(opts for title, opts in ui.asked if title.startswith("Extensions"))
        self.assertIn("latest for this minimal (newer needs API 9)", label)

    def test_global_review_scans_the_older_version_it_installs(self):
        self.push_file("extensions/risky.py", RISKY)
        self.push_file("extensions/risky.py", HELLO.replace('__api__ = "1"', '__api__ = "9"'))  # HEAD: harmless, API 9
        picked = {"Marketplace": str(self.bare), "Category": "extensions", "Extensions": ["risky"], "Install": "global"}
        _, out = self.run_ext({**picked, "Allow": False})
        self.assertIn("runs commands", out)  # the scan is of the API 1 version, not of HEAD
        self.assertFalse((self.global_dir / "risky.py").exists())

    def test_an_older_compatible_package_version_is_installed(self):
        self.push_file("tools/pkg/__init__.py", PKG_INIT)
        self.push_file("tools/pkg/helpers.py", 'REPLY = "v1"\n')
        old = self.head()
        self.push_file("tools/pkg/__init__.py", PKG_INIT.replace('__api__ = "1"', '__api__ = "9"'))
        self.push_file("tools/pkg/helpers.py", 'REPLY = "v2"\n')

        dest, _ = marketplace.install("pkg")
        self.assertEqual((dest / "helpers.py").read_text(), 'REPLY = "v1"\n')
        self.assertEqual(self.lock()["tools/pkg"]["commit"], old)
        self.assertEqual(self.entry("pkg").outdated, [])

    def test_an_extension_with_no_compatible_version_stays_listed_but_is_not_installed(self):
        self.push_file("extensions/future.py", HELLO.replace('__api__ = "1"', '__api__ = "9"'))
        self.push_file("extensions/future.py", HELLO.replace('__api__ = "1"', '__api__ = "8"'))
        [release] = marketplace.releases("future")
        self.assertEqual((release.listing.compatible, release.listing.api, release.commit), (False, "8", self.head()))
        with self.assertRaisesRegex(ValueError, "future: written for minimal API 8"):
            marketplace.install("future")
        self.assertFalse((self.global_dir / "future.py").exists())

    def test_a_new_version_is_outdated_by_commit(self):
        marketplace.install("hello")
        self.assertEqual((self.entry("hello").outdated, self.entry("hello").modified), ([], []))
        first = self.head()
        self.push_hello(HELLO.replace('"1.0"', '"1.1"'))
        entry = self.entry("hello")
        self.assertEqual((entry.outdated, entry.modified, entry.installed_version), (["global"], [], "1.0"))

        _, out = self.run_ext(
            {
                "Marketplace": str(self.bare),
                "Category": "extensions",
                "Extensions": ["hello"],
                "Update": ["hello"],
                "Allow": True,
            }
        )
        self.assertIn("Updated hello at", out)
        self.assertIn('"1.1"', (self.global_dir / "hello.py").read_text())
        self.assertNotEqual(self.lock()["hello"]["commit"], first)
        self.assertEqual(self.lock()["hello"]["commit"], self.head())
        self.assertEqual(self.entry("hello").outdated, [])

    def test_local_changes_are_detected_and_not_pre_ticked(self):
        hello = self.global_dir / "hello.py"
        marketplace.install("hello")
        hello.write_text(HELLO + "# mine\n")
        entry = self.entry("hello")
        self.assertEqual((entry.outdated, entry.modified), ([], ["global"]))  # a local edit is not an update

        self.push_hello(HELLO.replace('"1.0"', '"1.1"'))
        entry = self.entry("hello")
        self.assertEqual((entry.outdated, entry.modified), (["global"], ["global"]))
        ui, _ = self.run_ext(
            {"Marketplace": str(self.bare), "Category": "extensions", "Extensions": ["hello"], "Update": []}
        )
        [(_, label, checked)] = next(opts for title, opts in ui.asked if title.startswith("Update"))
        self.assertFalse(checked)
        self.assertIn("update available", label)
        self.assertIn("local changes will be overwritten", label)
        self.assertIn("# mine", hello.read_text())  # unticked: left alone

        self.run_ext(
            {
                "Marketplace": str(self.bare),
                "Category": "extensions",
                "Extensions": ["hello"],
                "Update": ["hello"],
                "Allow": True,
            }
        )
        self.assertEqual(hello.read_text(), HELLO.replace('"1.0"', '"1.1"'))  # ticking it updates anyway
        self.assertEqual(self.entry("hello").modified, [])

    def test_ext_install_refuses_to_overwrite_local_changes_without_force(self):
        hello = self.global_dir / "hello.py"
        marketplace.install("hello")
        hello.write_text(HELLO + "# mine\n")
        ctx = SimpleNamespace(cwd=str(self.tmp), ui=SimpleNamespace(ask=lambda question: True))
        with self.assertRaisesRegex(ValueError, "hello has local changes in .*hello.py.*force"):
            marketplace._ext_install({"name": "hello", "marketplace": str(self.bare)}, ctx)
        with self.assertRaisesRegex(ValueError, "local changes"):
            marketplace.install("hello")
        self.assertIn("# mine", hello.read_text())

        out = marketplace._ext_install({"name": "hello", "marketplace": str(self.bare), "force": True}, ctx)
        self.assertTrue(out.startswith("Updated hello at"), out)
        self.assertEqual(hello.read_text(), HELLO)
        self.assertEqual(self.entry("hello").modified, [])
        schema = Registry()
        marketplace.register_marketplace_tools(schema)
        self.assertEqual(schema.tools["ext_install"].input_schema["properties"]["force"]["type"], "boolean")

    def test_a_copy_without_a_lock_entry_is_compared_by_bytes(self):
        hello = self.global_dir / "hello.py"
        hello.parent.mkdir(parents=True)
        hello.write_text(HELLO)  # installed by hand
        entry = self.entry("hello")
        self.assertEqual((entry.installed, entry.outdated, entry.modified), (["global"], [], []))
        hello.write_text(HELLO + "# mine\n")
        entry = self.entry("hello")
        self.assertEqual((entry.outdated, entry.modified), (["global"], []))  # nothing to call a local change
        marketplace.install("hello")  # no lock entry, so nothing to protect
        self.assertEqual(hello.read_text(), HELLO)

        # An entry from another marketplace says nothing about this one: bytes again.
        lock = self.lock()
        lock["hello"]["marketplace"] = "https://example.com/other.git"
        lock["hello"]["commit"] = "0" * 40
        (self.global_dir / ".lock.json").write_text(json.dumps(lock))
        self.assertEqual(self.entry("hello").outdated, [])

    def test_describe_requires_an_api(self):
        path = self.tmp / "noapi.py"
        path.write_text(HELLO.replace('__api__ = "1"\n', ""))
        with self.assertRaisesRegex(ValueError, r'noapi\.py: set __api__ = "1"; the minimal API it is written for'):
            marketplace.describe(path)
        with self.assertRaisesRegex(ValueError, "set __api__"):
            marketplace.publish(path)

    def test_describe_rejects_a_malformed_api(self):
        path = self.tmp / "badapi.py"
        path.write_text(HELLO.replace('__api__ = "1"', '__api__ = "one"'))
        with self.assertRaisesRegex(ValueError, "badapi.py: invalid __api__ 'one'"):
            marketplace.describe(path)

    def test_listing_without_api_is_skipped_by_search(self):
        self.push_file("extensions/noapi.py", HELLO.replace('__api__ = "1"\n', ""))
        self.assertEqual([x.name for x in marketplace.search()], ["hello"])


class CheckTest(unittest.TestCase):
    def test_marketplace_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "extensions").mkdir()
            (root / "extensions" / "hello.py").write_text(HELLO)
            (root / "extensions" / "nodoc.py").write_text('__version__ = "1.0"\n__api__ = "1"\n\ndef register(minimal):\n    pass\n')
            (root / "extensions" / "future.py").write_text(HELLO.replace('__api__ = "1"', '__api__ = "9"'))
            (root / "extensions" / "needs.py").write_text('"""Needs a module."""\n__version__ = "1.0"\n__api__ = "1"\nimport not_installed_anywhere\ndef register(minimal):\n    pass\n')
            lines, ok = marketplace.check([root])
        self.assertFalse(ok)
        self.assertEqual(
            lines,
            [
                "FAIL extensions/future: written for minimal API 9, this minimal has API 1.0: install a version of it made for API 1",
                "ok   extensions/hello 1.0",
                "WARN extensions/needs: needs not_installed_anywhere",
                "FAIL extensions/nodoc: add a module docstring; its first line is the marketplace description",
            ],
        )

    def test_broken_import_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "broken.py")
            path.write_text('"""Broken."""\n__version__ = "1.0"\n__api__ = "1"\nfrom math import does_not_exist\ndef register(minimal):\n    pass\n')
            [line], ok = marketplace.check([path])
        self.assertFalse(ok)
        self.assertTrue(line.startswith("FAIL "), line)
        self.assertIn("does_not_exist", line)

    def test_cli_check_has_the_tools_the_app_has(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "wraps.py")
            path.write_text(
                '"""Wraps ext_search."""\n__version__ = "1.0"\n__api__ = "1"\n\n\ndef register(minimal):\n'
                '    search = minimal.tools["ext_search"]\n'
                '    minimal.register_tool("my_search", lambda input, ctx: search.run(input, ctx))\n'
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit:
                cli.ext_cli(["check", str(path)])
        self.assertEqual((exit.exception.code, out.getvalue()), (0, "ok   extensions/wraps 1.0\n"))


if __name__ == "__main__":
    unittest.main()
