import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from minimal import API_VERSION, cli, config
from minimal.core.app import App
from minimal.core.session import Session
from minimal.extensions import loader, marketplace
from minimal.extensions.loader import load_extensions
from minimal.extensions.metadata import read_metadata
from minimal.extensions.registry import Registry
from minimal.ui.commands import _entry_label, register_builtin_commands

HELLO = '''"""Say hello."""

__version__ = "1.0"
__api__ = "2"


def register(minimal):
    minimal.register_tool(name="hello", run=lambda input, ctx: "hi", description="Say hi")
'''

OCEAN = '''"""Deep blue dark theme."""

__version__ = "1.0"
__api__ = "2"


def register(minimal):
    minimal.register_theme("ocean", dark=True, background="#0b1622", foreground="#d0e4f5", accent="#4fc3f7")
'''

HELLO_TOOL = '''"""Another hello, as a tool."""

__version__ = "1.0"
__api__ = "2"


def register(minimal):
    minimal.register_tool(name="hello_tool", run=lambda input, ctx: "hi", description="Say hi")
'''

PKG_INIT = '''"""A package extension."""

from .helpers import REPLY

__version__ = "1.0"
__api__ = "2"


def register(minimal):
    minimal.register_tool(name="pkg", run=lambda input, ctx: REPLY, description="Reply")
'''

RISKY = '''"""Run a command."""

__version__ = "1.0"
__api__ = "2"

import subprocess


def register(minimal):
    minimal.register_tool(name="risky", run=lambda input, ctx: subprocess.run(["ls"]), description="Run ls")
'''


def item(doc="An item.", frontend="", extra=""):
    """The source of a valid marketplace item (API 2), declaring ``__frontend__`` if ``frontend`` is given."""
    declared = f'__frontend__ = "{frontend}"\n' if frontend else ""
    return f'"""{doc}"""\n\n__version__ = "1.0"\n__api__ = "2"\n{declared}{extra}\n\ndef register(minimal):\n    pass\n'


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


def remove_tree(path):
    """Delete ``path``, retrying: on macOS git can still be writing into it when a test ends.

    Fails loudly if something keeps writing for over 5 s.
    """
    for _ in range(50):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.1)
    shutil.rmtree(path)


class MarketplaceTest(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(remove_tree, self.tmp)
        self.bare = self.tmp / "market.git"
        self.global_dir = self.tmp / "global"
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "MINIMAL_MARKETPLACE": str(self.bare),
            # No background gc/maintenance or fsmonitor daemon outliving a git command.
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "maintenance.auto",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_KEY_1": "gc.auto",
            "GIT_CONFIG_VALUE_1": "0",
            "GIT_CONFIG_KEY_2": "gc.autoDetach",
            "GIT_CONFIG_VALUE_2": "false",
            "GIT_CONFIG_KEY_3": "core.fsmonitor",
            "GIT_CONFIG_VALUE_3": "false",
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
        (self.work / "shared" / "extensions").mkdir(parents=True)
        git("checkout", "--quiet", "-b", "main", cwd=self.work)
        self.push_hello(HELLO)

    def push_hello(self, source):
        """Commit ``source`` as ``extensions/hello.py`` and push it to the marketplace."""
        self.push_file("shared/extensions/hello.py", source)

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

    def published_files(self):
        """The files of the one ``publish/*`` branch of the marketplace."""
        [branch] = subprocess.run(
            ["git", "-C", str(self.bare), "branch", "--list", "publish/*", "--format=%(refname:short)"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        return subprocess.run(
            ["git", "-C", str(self.bare), "ls-tree", "-r", "--name-only", branch],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()

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
            register_commands=register_builtin_commands,
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
        self.push_file("shared/extensions/future.py", HELLO.replace('__api__ = "2"', '__api__ = "9"'))
        ui, out = self.run_ext(
            {"Marketplace": str(self.bare), "Category": "extensions", "Extensions": ["hello", "future"], "Install": "global", "Allow": True}
        )
        options = next(opts for title, opts in ui.asked if title.startswith("Extensions"))
        labels = {value: label for value, label, _ in options}
        self.assertIn("not for this minimal (API 9)", labels["future"])
        self.assertNotIn("not for this minimal", labels["hello"])
        self.assertIn(f"Skipped future: written for minimal API 9, this minimal has API {API_VERSION}", out)
        self.assertTrue((self.global_dir / "hello.py").exists())
        self.assertFalse((self.global_dir / "future.py").exists())

    def test_global_install_shows_the_scan_and_needs_a_yes(self):
        self.push_file("shared/extensions/risky.py", RISKY)
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
        self.push_file("shared/extensions/risky.py", RISKY)
        picked = {"Marketplace": str(self.bare), "Category": "extensions", "Extensions": ["risky"], "Install": "project"}
        _, out = self.run_ext(picked)  # no "Allow" answer: asking would raise StopIteration
        self.assertIn("Installed risky at", out)

    def test_picker_offers_dependencies_after_installing(self):
        self.push_file("shared/extensions/deps.py", NEEDS_DEPS.replace("DEPS", '["unittest_no_such_pkg_zz"]'))
        picked = {"Marketplace": str(self.bare), "Category": "extensions", "Extensions": ["deps"]}
        with mock.patch.object(marketplace, "install_dependencies") as install:
            ui, out = self.run_ext({**picked, "Install unittest_no_such_pkg_zz": False, "Install": "project"})
        install.assert_not_called()
        self.assertTrue((self.tmp / ".minimal" / "extensions" / "deps.py").exists())
        self.assertIn("Not installed: unittest_no_such_pkg_zz. deps will not load until they are installed.", out)
        self.assertTrue(ui.asked[-1][0].endswith("Needed by deps."), ui.asked)

    def test_model_tool_installs_globally_only_after_the_user_says_yes(self):
        self.push_file("shared/extensions/risky.py", RISKY)
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
        self.push_file("shared/themes/ocean.py", OCEAN)
        self.push_file("shared/tools/hello.py", HELLO_TOOL)
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
        path.write_text('"""Greet people."""\n\n__version__ = "1.0"\n__api__ = "2"\n\n\ndef register(minimal):\n    pass\n')
        real_git = shutil.which("git")
        with mock.patch.object(marketplace.shutil, "which", lambda cmd: real_git if cmd == "git" else None):
            message = marketplace.publish(path)
        self.assertTrue(message.startswith("Pushed branch publish/greet-"), message)
        branches = subprocess.run(
            ["git", "-C", str(self.bare), "branch", "--list", "publish/*"], capture_output=True, text=True, check=True
        ).stdout.split()
        self.assertEqual(len(branches), 1)
        self.assertTrue(branches[0].startswith("publish/greet-"))
        self.assertIn("shared/extensions/greet.py", self.published_files())

    def test_publish_rejects_invalid(self):
        no_register = self.tmp / "noreg.py"
        no_register.write_text('"""No register."""\n')
        with self.assertRaisesRegex(ValueError, r"register\(minimal\)"):
            marketplace.publish(no_register)
        good = self.tmp / "good.py"
        good.write_text('"""Good."""\n\n__version__ = "1.0"\n__api__ = "2"\n\n\ndef register(minimal):\n    pass\n')
        with self.assertRaisesRegex(ValueError, "invalid extension name"):
            marketplace.publish(good, "Bad-Name")

    def test_publish_rejects_unchanged_file_under_another_name(self):
        path = self.tmp / "local.py"
        path.write_text(HELLO)  # setUp published it as extensions/hello.py
        with self.assertRaisesRegex(ValueError, "already published with identical content"):
            marketplace.publish(path, "hello")

    def test_extension_named_like_a_category_keeps_the_category_folder(self):
        self.push_file("shared/tools/hello.py", HELLO_TOOL)
        self.push_file("shared/extensions/tools.py", HELLO.replace("Say hello.", "Named tools."))
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
        unversioned = '"""No version."""\n__api__ = "2"\n\n\ndef register(minimal):\n    pass\n'
        self.push_file("shared/extensions/plain.py", unversioned)
        self.assertEqual([x.name for x in marketplace.search()], ["hello"])
        path = self.tmp / "plain.py"
        path.write_text(unversioned)
        with self.assertRaisesRegex(ValueError, "needs a version"):
            marketplace.publish(path)

    def test_package_folder_installs_updates_and_uninstalls(self):
        self.push_file("shared/tools/pkg/__init__.py", PKG_INIT)
        self.push_file("shared/tools/pkg/helpers.py", 'REPLY = "v1"\n')
        self.push_file("shared/tools/pkg/prompt.txt", "v1\n")
        dest, updated = marketplace.install("pkg")
        self.assertEqual((dest, updated), (self.global_dir / "tools" / "pkg", False))
        self.assertEqual((dest / "helpers.py").read_text(), 'REPLY = "v1"\n')
        registry = Registry()
        self.assertEqual(load_extensions(registry, str(self.tmp), config.Settings()), ([dest], []))
        self.assertEqual(registry.tools["pkg"].run({}, None), "v1")

        [entry] = [e for e in marketplace.catalog(str(self.bare), str(self.tmp)) if e.listing.name == "pkg"]
        self.assertEqual((entry.installed, entry.outdated), (["global"], []))
        self.push_file("shared/tools/pkg/prompt.txt", "v2\n")  # a resource-only change is an update too
        [entry] = [e for e in marketplace.catalog(str(self.bare), str(self.tmp)) if e.listing.name == "pkg"]
        self.assertEqual(entry.outdated, ["global"])

        self.assertEqual(marketplace.uninstall("pkg", "global", str(self.tmp), "tools"), dest)
        self.assertFalse(dest.exists())

    def test_incompatible_listing_is_not_installed(self):
        self.push_file("shared/extensions/future.py", HELLO.replace('__api__ = "2"', '__api__ = "9"'))
        [listing] = [x for x in marketplace.search() if x.name == "future"]
        self.assertFalse(listing.compatible)
        self.assertEqual(listing.api, "9")
        self.assertIn(f"written for minimal API 9, this minimal has API {API_VERSION}", listing.problem)
        with self.assertRaisesRegex(ValueError, f"future: written for minimal API 9, this minimal has API {API_VERSION}"):
            marketplace.install("future")
        self.assertFalse((self.global_dir / "future.py").exists())

    def test_compatible_listing_has_no_problem(self):
        [listing] = marketplace.search("hello")
        self.assertEqual((listing.api, listing.problem, listing.compatible), ("2", None, True))

    def test_install_records_a_lock_entry_and_uninstall_removes_it(self):
        marketplace.install("hello")
        digest = hashlib.sha256(HELLO.encode()).hexdigest()
        self.assertEqual(
            self.lock(),
            {"hello": {"marketplace": str(self.bare), "commit": self.head(), "version": "1.0", "api": "2", "sha256": digest}},
        )
        self.assertEqual((self.global_dir / ".lock.json").stat().st_mode & 0o777, 0o600)
        registry = Registry()
        loaded, errors = load_extensions(registry, str(self.tmp), config.Settings())
        self.assertEqual((loaded, errors), ([self.global_dir / "hello.py"], []))  # the lock is not an extension

        self.push_file("shared/themes/ocean.py", OCEAN)
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
        marketplace.install_from(self.work / "shared" / "extensions" / "hello.py", "project", str(self.tmp))
        self.assertFalse((folder / ".lock.json").exists())

    def test_an_older_compatible_version_is_offered_and_installed(self):
        compatible = self.head()
        self.push_hello(HELLO.replace('"1.0"', '"2.0"').replace('__api__ = "2"', '__api__ = "9"'))

        [listing] = marketplace.search("hello")
        self.assertEqual((listing.version, listing.api, listing.compatible), ("1.0", "2", True))
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
        self.push_file("shared/extensions/risky.py", RISKY)
        self.push_file("shared/extensions/risky.py", HELLO.replace('__api__ = "2"', '__api__ = "9"'))  # HEAD: harmless, API 9
        picked = {"Marketplace": str(self.bare), "Category": "extensions", "Extensions": ["risky"], "Install": "global"}
        _, out = self.run_ext({**picked, "Allow": False})
        self.assertIn("runs commands", out)  # the scan is of the API 2 version, not of HEAD
        self.assertFalse((self.global_dir / "risky.py").exists())

    def test_an_older_compatible_package_version_is_installed(self):
        self.push_file("shared/tools/pkg/__init__.py", PKG_INIT)
        self.push_file("shared/tools/pkg/helpers.py", 'REPLY = "v1"\n')
        old = self.head()
        self.push_file("shared/tools/pkg/__init__.py", PKG_INIT.replace('__api__ = "2"', '__api__ = "9"'))
        self.push_file("shared/tools/pkg/helpers.py", 'REPLY = "v2"\n')

        dest, _ = marketplace.install("pkg")
        self.assertEqual((dest / "helpers.py").read_text(), 'REPLY = "v1"\n')
        self.assertEqual(self.lock()["tools/pkg"]["commit"], old)
        self.assertEqual(self.entry("pkg").outdated, [])

    def test_an_extension_with_no_compatible_version_stays_listed_but_is_not_installed(self):
        self.push_file("shared/extensions/future.py", HELLO.replace('__api__ = "2"', '__api__ = "9"'))
        self.push_file("shared/extensions/future.py", HELLO.replace('__api__ = "2"', '__api__ = "8"'))
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
        path.write_text(HELLO.replace('__api__ = "2"\n', ""))
        with self.assertRaisesRegex(ValueError, r'noapi\.py: set __api__ = "2"; the minimal API it is written for'):
            marketplace.describe(path)
        with self.assertRaisesRegex(ValueError, "set __api__"):
            marketplace.publish(path)

    def test_describe_rejects_a_malformed_api(self):
        path = self.tmp / "badapi.py"
        path.write_text(HELLO.replace('__api__ = "2"', '__api__ = "one"'))
        with self.assertRaisesRegex(ValueError, "badapi.py: invalid __api__ 'one'"):
            marketplace.describe(path)

    def test_listing_without_api_is_skipped_by_search(self):
        self.push_file("shared/extensions/noapi.py", HELLO.replace('__api__ = "2"\n', ""))
        self.assertEqual([x.name for x in marketplace.search()], ["hello"])

    def test_shared_and_frontend_items_are_listed_with_their_frontend(self):
        self.push_file("shared/widgets/gauge.py", item("A gauge."))
        self.push_file("frontends/textual/widgets/fancy.py", item("A fancy widget.", "textual"))
        # Not items: a frontend's package, tests and other languages' files.
        self.push_file("frontends/textual/src/minimal_textual/__init__.py", item("The package."))
        self.push_file("frontends/textual/tests/widgets/test_x.py", item("A test."))
        self.push_file("frontends/go/cmd/widgets/x.py", item("Go."))
        self.push_file("frontends/go/main.go", "package main\n")
        self.push_file("widgets/old.py", item("The old flat layout."))

        listings = marketplace.search()
        self.assertEqual(
            [(x.name, x.category, x.frontend, x.frontend_dir) for x in listings],
            [
                ("hello", "extensions", "", ""),
                ("fancy", "widgets", "textual", "textual"),
                ("gauge", "widgets", "", ""),
            ],
        )
        fancy = marketplace.releases("fancy")[0]
        self.assertEqual(fancy.relpath, "frontends/textual/widgets/fancy.py")
        self.assertEqual(marketplace.releases("gauge")[0].relpath, "shared/widgets/gauge.py")
        self.assertEqual(marketplace.format_listing(listings[1]), "fancy 1.0 (textual only) — A fancy widget.")
        self.assertEqual(marketplace.format_listing(listings[2]), "gauge 1.0 — A gauge.")

        out = marketplace._ext_search({}, None)
        self.assertIn("widgets/fancy 1.0 (textual only) — A fancy widget.", out)
        self.assertIn("widgets/gauge 1.0 — A gauge.", out)
        self.assertNotIn("minimal_textual", out)
        self.assertNotIn("test_x", out)
        self.assertNotIn("widgets/old", out)

        self.assertIn("(textual only)", _entry_label(self.entry("fancy")))
        self.assertNotIn("only", _entry_label(self.entry("gauge")))

        # Installed where the category says, whichever folder it came from.
        self.assertEqual(marketplace.install("fancy"), (self.global_dir / "widgets" / "fancy.py", False))
        self.assertEqual(sorted(self.lock()), ["widgets/fancy"])

    def test_an_older_version_of_a_frontend_item_is_found_by_its_path(self):
        self.push_file("frontends/textual/widgets/fancy.py", item("A fancy widget.", "textual", "# v1\n"))
        old = self.head()
        newer = item("A fancy widget.", "textual", "# v2\n").replace('__api__ = "2"', '__api__ = "9"')
        self.push_file("frontends/textual/widgets/fancy.py", newer)
        [release] = marketplace.releases("fancy")
        self.assertEqual((release.commit, release.newer.api, release.listing.frontend_dir), (old, "9", "textual"))
        dest, _ = marketplace.install("fancy")
        self.assertIn("# v1", dest.read_text())
        self.assertEqual(self.lock()["widgets/fancy"]["commit"], old)

    def test_the_same_name_in_two_folders_of_a_category_is_ambiguous(self):
        self.push_file("shared/widgets/dup.py", item("Shared."))
        self.push_file("frontends/textual/widgets/dup.py", item("Textual.", "textual"))
        self.push_file("shared/widgets/fine.py", item("Fine."))
        message = (
            r"dup is in several folders of the marketplace "
            r"\(shared/widgets/dup.py, frontends/textual/widgets/dup.py\)"
        )
        with self.assertRaisesRegex(ValueError, message):
            marketplace.install("dup")
        with self.assertRaisesRegex(ValueError, message):
            marketplace.install("dup", category="widgets")
        ctx = SimpleNamespace(cwd=str(self.tmp), ui=SimpleNamespace(ask=lambda question: True))
        with self.assertRaisesRegex(ValueError, message):
            marketplace._ext_install({"name": "dup", "marketplace": str(self.bare)}, ctx)
        self.assertFalse((self.global_dir / "widgets").exists())

        # Both are still found by search, but the picker leaves them out and says why.
        self.assertEqual([(x.name, x.frontend_dir) for x in marketplace.search("dup")], [("dup", ""), ("dup", "textual")])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            names = [e.listing.name for e in marketplace.catalog(str(self.bare), str(self.tmp))]
        self.assertEqual(names, ["hello", "fine"])
        self.assertIn("skipped shared/widgets/dup.py", err.getvalue())
        self.assertIn("skipped frontends/textual/widgets/dup.py", err.getvalue())

        # Different categories keep their own error.
        self.push_file("shared/tools/dup.py", item("A tool."))
        with self.assertRaisesRegex(ValueError, r"dup is in several categories \(tools, widgets\)"):
            marketplace.install("dup")

    def test_publish_files_an_item_by_its_frontend(self):
        real_git = shutil.which("git")
        local = self.tmp / "widgets" / "fancy.py"
        local.parent.mkdir()
        local.write_text(item("A fancy widget.", "textual"))
        with mock.patch.object(marketplace.shutil, "which", lambda cmd: real_git if cmd == "git" else None):
            marketplace.publish(local)
        self.assertEqual(self.published_files(), ["frontends/textual/widgets/fancy.py", "shared/extensions/hello.py"])

    def test_publish_refuses_a_name_that_would_clash_across_folders(self):
        local = self.tmp / "hello.py"
        local.write_text(item("Textual hello.", "textual"))
        with self.assertRaisesRegex(ValueError, "hello is already in shared/extensions/hello.py"):
            marketplace.publish(local)
        local.write_text(item("Bad frontend.", "../x"))
        with self.assertRaisesRegex(ValueError, "invalid __frontend__ '../x'"):
            marketplace.publish(local)


class CheckTest(unittest.TestCase):
    def test_marketplace_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "shared" / "extensions"
            folder.mkdir(parents=True)
            (folder / "hello.py").write_text(HELLO)
            (folder / "nodoc.py").write_text('__version__ = "1.0"\n__api__ = "2"\n\ndef register(minimal):\n    pass\n')
            (folder / "future.py").write_text(HELLO.replace('__api__ = "2"', '__api__ = "9"'))
            (folder / "needs.py").write_text('"""Needs a module."""\n__version__ = "1.0"\n__api__ = "2"\nimport not_installed_anywhere\ndef register(minimal):\n    pass\n')
            lines, ok = marketplace.check([root])
        self.assertFalse(ok)
        self.assertEqual(
            lines,
            [
                f"FAIL extensions/future: written for minimal API 9, this minimal has API {API_VERSION}: install a version of it made for API 2",
                "ok   extensions/hello 1.0",
                "WARN extensions/needs: needs not_installed_anywhere",
                "FAIL extensions/nodoc: add a module docstring; its first line is the marketplace description",
            ],
        )

    def test_broken_import_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "broken.py")
            path.write_text('"""Broken."""\n__version__ = "1.0"\n__api__ = "2"\nfrom math import does_not_exist\ndef register(minimal):\n    pass\n')
            [line], ok = marketplace.check([path])
        self.assertFalse(ok)
        self.assertTrue(line.startswith("FAIL "), line)
        self.assertIn("does_not_exist", line)

    def test_check_fails_textual_import_without_frontend(self):
        body = '"""Widget."""\n__version__ = "1.0"\n__api__ = "2"\n{}\nfrom textual.widgets import Static\n\n\ndef register(minimal):\n    pass\n'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "widgets", "w.py")
            path.parent.mkdir()
            path.write_text(body.format(""))
            lines, ok = marketplace.check([path])
            self.assertEqual(
                (lines, ok), (['FAIL widgets/w: imports textual without __frontend__ = "textual"'], False)
            )
            path.write_text(body.format('__frontend__ = "textual"'))
            [line], ok = marketplace.check([path])
        self.assertTrue(ok, line)
        installed = importlib.util.find_spec("textual") is not None
        self.assertEqual(
            line, "ok   widgets/w 1.0" + ("" if installed else " (static checks only: textual is not installed)")
        )

    def test_import_error_naming_a_missing_module_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "extensions", "hint.py")
            path.parent.mkdir()
            path.write_text(
                '"""Needs a module."""\n__version__ = "1.0"\n__api__ = "2"\n\n\ndef register(minimal):\n'
                '    raise ImportError("install it", name="not_installed_anywhere")\n'
            )
            lines, ok = marketplace.check([path])
        self.assertEqual((lines, ok), (["WARN extensions/hint: needs not_installed_anywhere"], True))

    def test_cli_check_has_the_tools_the_app_has(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "wraps.py")
            path.write_text(
                '"""Wraps ext_search."""\n__version__ = "1.0"\n__api__ = "2"\n\n\ndef register(minimal):\n'
                '    search = minimal.tools["ext_search"]\n'
                '    minimal.register_tool("my_search", lambda input, ctx: search.run(input, ctx))\n'
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as exit:
                cli.ext_cli(["check", str(path)])
        self.assertEqual((exit.exception.code, out.getvalue()), (0, "ok   extensions/wraps 1.0\n"))

    def write_checkout(self, root, files):
        for relative, source in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)

    def test_checkout_checks_shared_and_frontend_items_and_ignores_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_checkout(
                root,
                {
                    "shared/extensions/hello.py": HELLO,
                    "shared/widgets/gauge.py": item("A gauge."),
                    "frontends/textual/widgets/fancy.py": item(
                        "Fancy.", "textual", "from textual.widgets import Static\n"
                    ),
                    "frontends/textual/widgets/pkg/__init__.py": item("A package.", "textual"),
                    # Not items: a Python package, tests, a Go module and a virtualenv.
                    "frontends/textual/src/minimal_textual/__init__.py": "this is not python (",
                    "frontends/textual/tests/widgets/test_x.py": "this is not python (",
                    "frontends/textual/.venv/widgets/z.py": "this is not python (",
                    "frontends/go/main.go": "package main\n",
                    "frontends/go/cmd/widgets/x.py": "this is not python (",
                    "README.md": "# marketplace\n",
                },
            )
            with mock.patch.object(marketplace, "_missing", return_value=True):
                lines, ok = marketplace.check([root])
        self.assertEqual(
            lines,
            [
                "ok   extensions/hello 1.0",
                "ok   textual/widgets/fancy 1.0 (static checks only: textual is not installed)",
                "ok   widgets/gauge 1.0",
                "ok   textual/widgets/pkg 1.0 (static checks only: textual is not installed)",
            ],
        )
        self.assertTrue(ok)

    def test_a_textual_item_is_loaded_when_textual_is_importable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # No textual import of its own: this runs the same wherever textual really is.
            self.write_checkout(root, {"frontends/textual/widgets/fancy.py": item("Fancy.", "textual")})
            with mock.patch.object(marketplace, "_missing", return_value=False):
                self.assertEqual(marketplace.check([root]), (["ok   textual/widgets/fancy 1.0"], True))

    def test_static_checks_of_a_textual_item_still_fail_on_a_syntax_error_in_a_package_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_checkout(
                root,
                {
                    "frontends/textual/widgets/pkg/__init__.py": item("A package.", "textual", "from . import more\n"),
                    "frontends/textual/widgets/pkg/more.py": "def broken(:\n",
                },
            )
            with mock.patch.object(marketplace, "_missing", return_value=True):
                [line], ok = marketplace.check([root])
        self.assertFalse(ok)
        self.assertTrue(line.startswith("FAIL textual/widgets/pkg: SyntaxError"), line)

    def test_items_must_sit_in_the_folder_of_their_frontend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_checkout(
                root,
                {
                    "frontends/textual/widgets/a.py": item("No frontend."),
                    "frontends/textual/widgets/b.py": item("Another frontend.", "go"),
                    "shared/widgets/c.py": item("Shared but textual.", "textual"),
                    "shared/widgets/d.py": item("Shared with a toolkit.", extra="import textual\n"),
                },
            )
            lines, ok = marketplace.check([root])
        self.assertFalse(ok)
        self.assertEqual(
            lines,
            [
                'FAIL textual/widgets/a: it is in frontends/textual/, so it must set __frontend__ = "textual" (it does not set it)',
                "FAIL textual/widgets/b: it is in frontends/textual/, so it must set __frontend__ = \"textual\" (it sets 'go')",
                "FAIL widgets/c: it is in shared/, so it must not set __frontend__ (it sets 'textual'); move it to frontends/textual/",
                'FAIL widgets/d: imports textual without __frontend__ = "textual"',
            ],
        )

    def test_a_single_item_path_is_checked_by_where_it_sits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_checkout(root, {"frontends/textual/widgets/a.py": item("No frontend.")})
            [line], ok = marketplace.check([root / "frontends" / "textual" / "widgets" / "a.py"])
        self.assertFalse(ok)
        self.assertTrue(line.startswith("FAIL textual/widgets/a: it is in frontends/textual/"), line)

    def test_a_name_in_two_folders_of_a_category_fails_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_checkout(
                root,
                {
                    "shared/tools/dup.py": item("Shared."),
                    "frontends/textual/tools/dup.py": item("Textual.", "textual"),
                },
            )
            with mock.patch.object(marketplace, "_missing", return_value=True):
                lines, ok = marketplace.check([root])
        self.assertFalse(ok)
        self.assertEqual(
            lines[0],
            "FAIL tools/dup: in several folders (shared/tools/dup.py, frontends/textual/tools/dup.py); "
            "it would install to the same place, so keep only one",
        )
        self.assertEqual(len(lines), 3)

    def test_the_old_flat_layout_is_not_a_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_checkout(Path(tmp), {"extensions/hello.py": HELLO})
            [line], ok = marketplace.check([Path(tmp)])
        self.assertFalse(ok)
        self.assertIn("not a marketplace checkout (no shared/ or frontends/ folder)", line)


NEEDS_DEPS = '''"""Needs packages."""

__version__ = "1.0"
__api__ = "2"
__dependencies__ = DEPS


def register(minimal):
    pass
'''


class DependenciesTest(unittest.TestCase):
    def test_read_metadata(self):
        meta = read_metadata(NEEDS_DEPS.replace("DEPS", '["x>=1", "y[extra] >= 2, < 3", "z"]').encode(), "deps.py")
        self.assertEqual(meta.dependencies, ("x>=1", "y[extra] >= 2, < 3", "z"))
        self.assertEqual(read_metadata(HELLO.encode(), "hello.py").dependencies, ())

    def test_malformed_dependencies_fail_the_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "shared", "extensions")
            root.mkdir(parents=True)
            (root / "option.py").write_text(NEEDS_DEPS.replace("DEPS", '["--index-url=evil"]'))
            (root / "url.py").write_text(NEEDS_DEPS.replace("DEPS", '["pkg @ https://example.com/pkg.zip"]'))
            (root / "string.py").write_text(NEEDS_DEPS.replace("DEPS", '"x"'))
            lines, ok = marketplace.check([Path(tmp)])
        self.assertFalse(ok)
        expected = 'FAIL extensions/{}: __dependencies__ must be a list of package requirements such as "termaid>=0.3"; got {}'
        self.assertEqual(
            lines,
            [
                expected.format("option", "['--index-url=evil']"),
                expected.format("string", "'x'"),
                expected.format("url", "['pkg @ https://example.com/pkg.zip']"),
            ],
        )

    def test_a_malformed_dependencies_is_a_load_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "bad.py")
            path.write_text(NEEDS_DEPS.replace("DEPS", '["-e ."]'))
            loaded, errors = loader.load_sources(
                Registry(), [(loader.read_extensions([path])[0], "cli")], config.Settings(), tmp
            )
        self.assertEqual(loaded, [])
        self.assertIn("__dependencies__ must be a list", errors[0])

    def test_missing_dependencies(self):
        self.assertEqual(
            marketplace.missing_dependencies(["unittest_no_such_pkg_zz", "anthropic>=0.1", "unittest_no_such_pkg_zz"]),
            ["unittest_no_such_pkg_zz"],
        )

    def listing(self, name, *dependencies):
        return marketplace.Listing(name, "d", "1.0", Path(name), "extensions", "2", None, dependencies)

    def test_ensure_dependencies_asks_first(self):
        listings = [self.listing("a", "anthropic"), self.listing("b", "unittest_no_such_pkg_zz>=1")]
        with mock.patch.object(marketplace, "install_dependencies") as install:
            questions = []
            msg = marketplace.ensure_dependencies(listings, lambda q: questions.append(q) or False)
            install.assert_not_called()
            self.assertEqual(msg, "Not installed: unittest_no_such_pkg_zz>=1. b will not load until they are installed.")
            self.assertIn("Install unittest_no_such_pkg_zz>=1 into minimal's Python", questions[0])
            self.assertTrue(questions[0].endswith("Needed by b."), questions[0])

            msg = marketplace.ensure_dependencies(listings, lambda q: True)
            install.assert_called_once_with(["unittest_no_such_pkg_zz>=1"])
            self.assertEqual(msg, "Installed unittest_no_such_pkg_zz>=1.")

            install.reset_mock()
            self.assertIsNone(marketplace.ensure_dependencies([self.listing("a", "anthropic")], lambda q: True))
            install.assert_not_called()

    def test_install_dependencies_reports_the_installer_error(self):
        failed = subprocess.CompletedProcess([], 1, "", "Resolving...\nerror: no such package\n\n")
        with mock.patch.object(marketplace.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "^installing a b>=1 failed: error: no such package$"):
                marketplace.install_dependencies(["a", "b>=1"])

    def test_check_install_deps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "deps.py")
            path.write_text(NEEDS_DEPS.replace("DEPS", '["unittest_no_such_pkg_zz"]'))
            with mock.patch.object(marketplace, "install_dependencies") as install:
                lines, ok = marketplace.check([path], install_deps=True)
            install.assert_called_once_with(["unittest_no_such_pkg_zz"])
            self.assertEqual((lines, ok), (["ok   extensions/deps 1.0"], True))

            err = io.StringIO()
            with (
                mock.patch.object(marketplace, "install_dependencies", side_effect=RuntimeError("installing x failed")),
                contextlib.redirect_stderr(err),
            ):
                self.assertEqual(marketplace.check([path], install_deps=True), ([], False))
            self.assertEqual(err.getvalue(), "installing x failed\n")


if __name__ == "__main__":
    unittest.main()
