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
from minimal.extensions.metadata import api_problem
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply
from minimal.ui.commands import BUILTIN_COMMANDS


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
            registry=Registry(),
            session=Session("first", self.tmp / "first.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            commands=BUILTIN_COMMANDS,
        )

    def extension(self, name, body, folder=None, api='"1"'):
        """Write ``body`` as ``folder/name`` after an ``__api__ = <api>`` line (``api=None``: no such line)."""
        path = (folder or self.global_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        header = "" if api is None else f"__api__ = {api}\n"
        path.write_text(header + textwrap.dedent(body))
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

    def load_errors(self, api, name="future.py"):
        """Write an extension declaring ``api`` (a Python literal; ``None``: no ``__api__``) whose ``register`` must not run."""
        self.extension(name, 'def register(minimal):\n    raise AssertionError("ran")\n', api=api)
        loaded, errors = self.app.reload_extensions()
        self.assertEqual(loaded, [])
        self.assertEqual(len(errors), 1)
        return errors[0]

    def test_extension_for_another_api_major_is_not_loaded(self):
        self.assertIn("written for minimal API 9, this minimal has API 1.0", self.load_errors('"9"'))

    def test_extension_without_api_is_not_loaded(self):
        error = self.load_errors(None)
        self.assertIn("declares no __api__", error)
        self.assertIn('add __api__ = "1"', error)

    def test_extension_needing_a_newer_minor_api_is_not_loaded(self):
        self.assertIn("needs minimal API 1.9, this minimal has API 1.0", self.load_errors('"1.9"'))

    def test_extension_with_a_malformed_api_is_not_loaded(self):
        self.assertIn("invalid __api__ 'x'", self.load_errors('"x"'))

    def test_extension_with_an_api_loads(self):
        for api in ('"1"', '"1.0"'):
            with self.subTest(api=api):
                self.extension("ok.py", 'def register(minimal):\n    minimal.register_tool("ok", print)\n', api=api)
                self.assertEqual(self.app.reload_extensions(), ([self.global_dir / "ok.py"], []))

    def test_api_problem_boundaries(self):
        self.assertIsNone(api_problem("1"))
        self.assertIsNone(api_problem("1.0"))
        self.assertIn("needs minimal API 1.1", api_problem("1.1", "1.0"))
        self.assertIsNone(api_problem("1.1", "1.2"))
        self.assertIsNone(api_problem("1.2", "1.2"))
        self.assertIn("written for minimal API 2, this minimal has API 1.5", api_problem("2", "1.5"))
        self.assertIn("made for API 1", api_problem("0", "1.5"))
        self.assertIn("declares no __api__", api_problem(""))
        for bad in ("x", "1.2.3", ">=1", "1."):
            self.assertIn("invalid __api__", api_problem(bad))

    def test_removed_api_names_explain_what_to_use(self):
        self.extension("old.py", "from minimal.api import BUILTIN_COMMANDS\n\ndef register(minimal):\n    pass\n")
        loaded, errors = self.app.reload_extensions()
        self.assertEqual(loaded, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("minimal.api.BUILTIN_COMMANDS was removed in minimal API 1.0", errors[0])
        self.assertIn("ctx.commands()", errors[0])

    def test_unknown_api_name_is_a_plain_import_error(self):
        with self.assertRaises(ImportError):
            from minimal.api import nothing_like_this  # noqa: F401

    def test_ctx_commands_lists_built_in_and_extension_commands(self):
        self.extension(
            "lister.py",
            """
            def register(minimal):
                @minimal.command()
                def listing(args, ctx):
                    '''Show them.'''
                    for name, description in ctx.commands().items():
                        print(f"{name}={description}")
            """,
        )
        self.assertEqual(self.app.reload_extensions()[1], [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.app.run_command("/listing")
        lines = out.getvalue().splitlines()
        self.assertIn("help=show the commands", lines)
        self.assertIn("listing=Show them.", lines)
        self.assertEqual(self.app.context.commands()["help"], "show the commands")

    def test_replaced_built_in_hands_over_without_recursing(self):
        self.extension(
            "model.py",
            """
            def register(minimal):
                @minimal.command()
                def model(args, ctx):
                    '''Log, then let the built-in switch.'''
                    print("custom model")
                    ctx.run_command("/model " + args, builtin=True)
            """,
        )
        self.assertEqual(self.app.reload_extensions()[1], [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.app.run_command("/model x")
        self.assertEqual(out.getvalue().splitlines(), ["custom model", "Model: x"])
        self.assertEqual(self.app.model, "x")
        self.assertEqual(self.app.context.commands()["model"], "Log, then let the built-in switch.")

    def test_ctx_run_command_reports_an_unknown_command(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.app.context.run_command("/nope")
        self.assertEqual(out.getvalue().strip(), "Unknown command: /nope")

    def test_ctx_run_command_builtin_skips_extension_commands(self):
        self.extension(
            "only.py",
            'def register(minimal):\n    minimal.register_command("only", lambda args, ctx: print("ext"))\n',
        )
        self.app.reload_extensions()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.app.context.run_command("/only", builtin=True)
        self.assertEqual(out.getvalue().strip(), "Unknown command: /only")

    def test_ctx_models_and_set_model(self):
        self.app.provider = mock.Mock(list_models=mock.Mock(return_value=["m1", "m2"]))
        self.app.model = "m2"
        self.assertEqual(self.app.context.models(), ["m1", "m2"])
        self.app.context.set_model("m1")
        self.assertEqual(self.app.model, "m1")

    def test_ctx_widget_is_none_without_a_widget_frontend(self):
        self.assertIsNone(self.app.context.widget("anything"))

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
