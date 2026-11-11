import contextlib
import io
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from minimal import config
from minimal.core import session as session_mod
from minimal.core.app import App
from minimal.core.session import Session
from minimal.extensions import loader
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply


class ScriptedProvider:
    """Replays ``replies`` in order, recording the tool names offered with each request."""

    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.offered = []

    def stream(self, **kw):
        self.offered.append([t["name"] for t in kw["tools"]])
        return self.replies.pop(0)


class ExtensionApiTest(unittest.TestCase):
    """An ``App`` in a temporary project with a temporary ``~/.minimal``."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.global_dir = self.tmp / "home" / "extensions"
        self.global_dir.mkdir(parents=True)
        for patcher in (
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.global_dir),
            mock.patch.object(config, "SETTINGS_FILE", self.tmp / "home" / "settings.json"),
            mock.patch.object(config, "TRUST_FILE", self.tmp / "home" / "trusted.json"),
            mock.patch.object(session_mod, "SESSIONS_DIR", self.tmp / "home" / "sessions"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = App(
            registry=Registry(), session=Session("first", self.tmp / "first.jsonl"), cwd=str(self.tmp), auto_approve=True
        )

    def extension(self, name, body, folder=None):
        path = (folder or self.global_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body))
        return path

    def test_tool_decorator_uses_the_docstring(self):
        self.extension(
            "deco.py",
            '''
            def register(minimal):
                @minimal.tool(input_schema={"type": "object", "properties": {}})
                def shout(input, ctx):
                    """Say it loud.

                    More text.
                    """
                    return "HI"

                @minimal.command()
                def hello(args, ctx):
                    """Greet."""
            ''',
        )
        self.assertEqual(self.app.reload_extensions()[1], [])
        tool = self.app.registry.tools["shout"]
        self.assertEqual((tool.description, tool.run({}, self.app.context)), ("Say it loud.", "HI"))
        self.assertEqual(self.app.registry.commands["hello"].description, "Greet.")

    def test_unknown_event_is_an_error(self):
        self.extension("bogus.py", 'def register(minimal):\n    minimal.on("bogus", print)\n')
        _, errors = self.app.reload_extensions()
        self.assertEqual(len(errors), 1)
        self.assertIn("ValueError: unknown event 'bogus'", errors[0])

    def test_declare_settings_merges_and_reports(self):
        self.extension(
            "tuned.py",
            """
            def register(minimal):
                settings = minimal.declare_settings({"limit": 10, "ratio": 0.5, "on": True, "name": "x"})
                minimal.register_tool("tuned", lambda input, ctx: dict(settings))
            """,
            folder=self.global_dir / "tools",
        )
        config.SETTINGS_FILE.write_text(json.dumps({
            "extensions": {"settings": {"tools/tuned": {"limit": 20, "ratio": 2, "on": 1, "colour": "red"}}}
        }))
        _, errors = self.app.reload_extensions()
        self.assertEqual(
            sorted(errors),
            [
                "tools/tuned: setting 'on' must be bool",
                "tools/tuned: unknown setting 'colour' (declared: limit, ratio, on, name)",
            ],
        )
        self.assertEqual(
            self.app.registry.tools["tuned"].run({}, self.app.context),
            {"limit": 20, "ratio": 2, "on": True, "name": "x"},
        )

    def test_new_session_switches_and_returns_the_old_id(self):
        self.app.messages.append({"role": "user", "content": "old"})
        old = self.app.context.new_session([{"role": "user", "content": "kept"}])
        self.assertEqual(old, "first")
        self.assertNotEqual(self.app.session.id, "first")
        self.assertEqual(self.app.context.messages, [{"role": "user", "content": "kept"}])
        self.assertIn(self.app.session.id, self.app.context.sessions())
        self.assertEqual(self.app.context.read_session(self.app.session.id), [{"role": "user", "content": "kept"}])
        with self.assertRaisesRegex(ValueError, "no session"):
            self.app.context.read_session("../x")

    def test_run_agent_hides_excluded_tools_and_marks_turn_end_nested(self):
        self.app.reload_extensions()
        self.app.registry.register_tool("x", lambda input, ctx: "x ran")
        seen = []
        self.app.registry.on("turn_end", lambda event: seen.append((event["nested"], event["ctx"])))
        self.app.provider = ScriptedProvider(
            [
                Reply(content=[{"type": "tool_use", "id": "1", "name": "x", "input": {}}], stop_reason="tool_use"),
                Reply(content=[{"type": "text", "text": "done"}], stop_reason="end_turn"),
            ]
        )
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.app.context.run_agent("go", exclude_tools={"x"}), "done")
        self.assertNotIn("x", self.app.provider.offered[0])
        self.assertIn("bash", self.app.provider.offered[0])
        self.assertEqual(seen, [(True, self.app.context)])

    def test_complete_keeps_an_empty_system_prompt(self):
        self.app.provider = mock.Mock(stream=mock.Mock(return_value=Reply(content=[], stop_reason="end_turn")))
        self.app.context.complete([], system="", tools=False)
        self.assertEqual(self.app.provider.stream.call_args.kwargs["system"], "")
        self.app.context.complete([], tools=False)
        self.assertEqual(self.app.provider.stream.call_args.kwargs["system"], self.app.context.system_prompt())

    def test_package_extension_reloads_its_helpers(self):
        pkg = self.global_dir / "pkg"
        self.extension(
            "__init__.py",
            """
            from .helpers import X

            def register(minimal):
                minimal.register_tool("pkg", lambda input, ctx: X)
            """,
            folder=pkg,
        )
        self.extension("helpers.py", "X = 'v1'\n", folder=pkg)
        self.assertEqual(self.app.reload_extensions(), ([pkg], []))
        self.assertEqual(self.app.registry.tools["pkg"].run({}, self.app.context), "v1")
        (pkg / "helpers.py").write_text("X = 'v2'\n")  # same size: a cached import would miss it
        self.app.reload_extensions()
        self.assertEqual(self.app.registry.tools["pkg"].run({}, self.app.context), "v2")

    def test_package_cannot_import_modules_added_after_the_snapshot(self):
        pkg = self.global_dir / "pkg"
        self.extension(
            "__init__.py",
            """
            import importlib

            def register(minimal):
                minimal.register_tool("lazy", lambda input, ctx: importlib.import_module(__name__ + "." + input["name"]).X)
            """,
            folder=pkg,
        )
        self.extension("sub/mod.py", "X = 'reviewed'\n", folder=pkg)
        self.assertEqual(self.app.reload_extensions(), ([pkg], []))
        tool = self.app.registry.tools["lazy"]
        self.assertEqual(tool.run({"name": "sub.mod"}, self.app.context), "reviewed")
        self.extension("late.py", "X = 'unreviewed'\n", folder=pkg)
        self.extension("sub/late.py", "X = 'unreviewed'\n", folder=pkg)
        for name in ("late", "sub.late"):
            with self.assertRaises(ModuleNotFoundError):
                tool.run({"name": name}, self.app.context)

    def test_copies_of_a_package_at_two_paths_keep_their_own_modules(self):
        body = """
            import importlib

            def register(minimal):
                minimal.register_tool("{tool}", lambda input, ctx: importlib.import_module(__name__ + ".helpers").X)
            """
        global_pkg, extra_pkg = self.global_dir / "tools" / "pkg", self.tmp / "extra" / "tools" / "pkg"
        for pkg, tool, value in ((global_pkg, "g", "global"), (extra_pkg, "e", "extra")):
            self.extension("__init__.py", body.replace("{tool}", tool), folder=pkg)
            self.extension("helpers.py", f"X = {value!r}\n", folder=pkg)
        self.app.extra_extensions = [str(extra_pkg)]
        self.assertEqual(self.app.reload_extensions(), ([global_pkg, extra_pkg.resolve()], []))
        tools = self.app.registry.tools
        self.assertEqual((tools["g"].run({}, None), tools["e"].run({}, None)), ("global", "extra"))

    def test_package_named_like_a_category_is_the_category_folder(self):
        self.extension("__init__.py", "", folder=self.global_dir / "tools")
        x = self.extension("x.py", 'def register(minimal):\n    minimal.register_tool("x", print)\n', self.global_dir / "tools")
        self.assertEqual(self.app.reload_extensions(), ([x], []))

    def test_file_and_folder_with_the_same_name(self):
        self.extension("dup.py", "def register(minimal):\n    pass\n")
        self.extension("__init__.py", "def register(minimal):\n    pass\n", folder=self.global_dir / "dup")
        loaded, errors = self.app.reload_extensions()
        self.assertEqual(loaded, [self.global_dir / "dup.py"])
        self.assertEqual(errors, [f"{self.global_dir / 'dup'}: both dup.py and dup/ exist; remove one"])

    def test_requires_a_newer_minimal(self):
        self.extension("future.py", '__requires__ = ">=9"\ndef register(minimal):\n    raise AssertionError("ran")\n')
        _, errors = self.app.reload_extensions()
        self.assertEqual(len(errors), 1)
        self.assertIn("requires minimal >=9, this is 0.2.0", errors[0])

    def test_disabled_extension_loads_only_from_ext(self):
        path = self.extension("quiet.py", 'def register(minimal):\n    minimal.register_tool("quiet", print)\n')
        config.SETTINGS_FILE.write_text(json.dumps({"extensions": {"disabled": ["quiet"]}}))
        self.assertEqual(self.app.reload_extensions(), ([], []))
        self.assertNotIn("quiet", self.app.registry.tools)
        self.app.extra_extensions = [str(path)]
        self.app.reload_extensions()
        self.assertIn("quiet", self.app.registry.tools)


if __name__ == "__main__":
    unittest.main()
