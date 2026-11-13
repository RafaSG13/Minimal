import contextlib
import io
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from minimal import API_VERSION, config
from minimal.core import session as session_mod
from minimal.core.app import App
from minimal.core.session import Session
from minimal.extensions import loader
from minimal.extensions.loader import reload_summary
from minimal.extensions.metadata import api_problem
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply, Usage
from minimal.ui.commands import register_builtin_commands
from minimal.ui.frontends import register_builtin_frontends


class ScriptedProvider:
    """Replays ``replies`` in order, recording the tool names offered with each request and the request itself."""

    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.offered = []
        self.requests = []

    def stream(self, **kw):
        self.offered.append([t["name"] for t in kw["tools"]])
        self.requests.append({**kw, "messages": list(kw["messages"])})
        return self.replies.pop(0)


def text_reply(text, usage=None):
    return Reply(content=[{"type": "text", "text": text}], stop_reason="end_turn", usage=usage)


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
            register_commands=register_builtin_commands,
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

    def test_frontend_only_extension_is_skipped_on_another_frontend(self):
        path = self.extension(
            "tuionly.py",
            '__frontend__ = "textual"\n\ndef register(minimal):\n    minimal.register_command("hi", lambda args, ctx: None)\n',
            api='"1.3"',
        )
        for ui, skipped in (("console", [f"{path}: needs a textual frontend; console is not one"]), ("tui", [])):
            with self.subTest(ui=ui):
                app = App(
                    registry=Registry(),
                    session=Session("first", self.tmp / "first.jsonl"),
                    cwd=str(self.tmp),
                    auto_approve=True,
                    register_commands=register_builtin_commands,
                    register_frontends=register_builtin_frontends,
                    ui=ui,
                )
                loaded, errors = app.reload_extensions()
                self.assertEqual((errors, app.extension_skipped), ([], skipped))
                self.assertEqual("hi" in app.registry.commands, ui == "tui")
                summary = reload_summary(loaded, errors, None, app.extension_skipped)
                self.assertEqual("\nSkipped: " in summary, ui == "console")
                self.assertNotIn("Failed:", summary)

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
        self.assertIn(f"written for minimal API 9, this minimal has API {API_VERSION}", self.load_errors('"9"'))

    def test_extension_without_api_is_not_loaded(self):
        error = self.load_errors(None)
        self.assertIn("declares no __api__", error)
        self.assertIn('add __api__ = "1"', error)

    def test_extension_needing_a_newer_minor_api_is_not_loaded(self):
        self.assertIn(f"needs minimal API 1.9, this minimal has API {API_VERSION}", self.load_errors('"1.9"'))

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

    def events(self, *names):
        """Record every payload of the events ``names`` as ``(name, payload)``."""
        seen = []
        for name in names:
            self.app.registry.on(name, lambda event, name=name: seen.append((name, event)))
        return seen

    def test_turn_start_rewrites_the_user_message(self):
        self.app.registry.on("turn_start", lambda event: {"text": event["text"].upper()})
        self.app.registry.on("turn_start", lambda event: {"text": ""})  # empty: ignored
        self.app.registry.on("turn_start", lambda event: 1 / 0)  # reported and skipped
        self.app.provider = ScriptedProvider([text_reply("ok")])
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.app.run_turn("rewritten")
        self.assertEqual(self.app.messages[0], {"role": "user", "content": "REWRITTEN"})
        self.assertIn('error in "turn_start" handler', err.getvalue())

    def test_before_request_changes_only_that_request(self):
        self.app.registry.on("before_request", lambda event: {"system": "X", "tools": "wrong"})
        self.app.registry.on(
            "before_request", lambda event: {"messages": [*event["messages"], {"role": "user", "content": "extra"}]}
        )
        seen = self.events("before_request", "after_response")
        usage = Usage(input_tokens=3, output_tokens=4)
        self.app.provider = ScriptedProvider([text_reply("ok", usage)])
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.app.run_turn("hi")
        [request] = self.app.provider.requests
        self.assertEqual(request["system"], "X")
        self.assertEqual(request["tools"], [])  # "wrong" was not a list
        self.assertEqual(request["messages"][-1], {"role": "user", "content": "extra"})
        self.assertIn('[extension] before_request: ignored "tools" (wrong type)', err.getvalue())
        self.assertEqual([m["role"] for m in self.app.messages], ["user", "assistant"])
        [(_, before), (_, after)] = seen
        self.assertFalse(before["nested"])
        self.assertEqual((after["usage"], after["reply"].content, after["nested"]), (usage, [{"type": "text", "text": "ok"}], False))

    def test_session_events(self):
        seen = self.events("session_start", "session_switch", "shutdown")
        self.app.begin()
        with contextlib.redirect_stdout(io.StringIO()):
            self.app.run_command("/new")
        new = self.app.session.id
        old = self.app.context.new_session([])
        self.app.shutdown()
        self.assertEqual(
            [(name, {k: v for k, v in e.items() if k != "ctx"}) for name, e in seen],
            [
                ("session_start", {"resumed": False}),
                ("session_switch", {"old_id": "first", "new_id": new}),
                ("session_switch", {"old_id": old, "new_id": self.app.session.id}),
                ("shutdown", {}),
            ],
        )
        self.assertTrue(all(e["ctx"] is self.app.context for _, e in seen))
        self.app.messages.append({"role": "user", "content": "x"})
        self.app.begin()
        self.assertEqual(seen[-1], ("session_start", {"ctx": self.app.context, "resumed": True}))

    def test_unknown_event_lists_every_event(self):
        with self.assertRaisesRegex(ValueError, "use one of tool_call, .*, before_request, after_response, shutdown"):
            self.app.registry.on("nope", print)

    def test_on_dispose_runs_before_a_reload_and_at_shutdown(self):
        log = self.tmp / "log"
        self.extension(
            "disposing.py",
            f"""
            import pathlib
            LOG = pathlib.Path({str(log)!r})

            def write(line):
                with LOG.open("a") as f:
                    f.write(line + "\\n")

            def register(minimal):
                write("register")
                minimal.on_dispose(lambda: write("dispose"))

                @minimal.on_dispose
                def boom():
                    raise RuntimeError("boom")
            """,
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.app.reload_extensions()[1], [])
            self.app.reload_extensions()
            self.assertEqual(log.read_text().split(), ["register", "dispose", "register"])
            self.app.shutdown()
            self.app.shutdown()  # disposers run once
        self.assertEqual(log.read_text().split(), ["register", "dispose", "register", "dispose"])
        self.assertEqual(err.getvalue().count("[extension] error in dispose: boom"), 2)

    def test_on_dispose_needs_a_callable(self):
        with self.assertRaisesRegex(ValueError, "on_dispose: fn must be callable"):
            self.app.registry.on_dispose("not callable")

    def test_data_dir(self):
        self.extension(
            "keeper.py",
            """
            def register(minimal):
                minimal.register_tool("dirs", lambda input, ctx: (minimal.data_dir(), minimal.data_dir("global")))
                minimal.register_tool("bad", lambda input, ctx: minimal.data_dir("repo"))
            """,
            folder=self.global_dir / "tools",
        )
        home = self.tmp / "data-home"
        with mock.patch.object(config, "HOME_DIR", home):
            self.assertEqual(self.app.reload_extensions()[1], [])
            project, global_ = self.app.registry.tools["dirs"].run({}, self.app.context)
            with self.assertRaisesRegex(ValueError, 'data_dir: scope must be "project" or "global"'):
                self.app.registry.tools["bad"].run({}, self.app.context)
        key = project.relative_to(home / "data" / "projects").parts[0]
        self.assertRegex(key, "^[0-9a-f]{16}$")
        self.assertEqual(project, home / "data" / "projects" / key / "tools" / "keeper")
        self.assertEqual(global_, home / "data" / "global" / "tools" / "keeper")
        for path in (project, global_, home / "data"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700, path)

    def test_request_options_are_sent_only_when_set(self):
        class Strict:
            """A provider written before request options existed."""

            name = "strict"

            def stream(self, *, model, system, messages, tools, on_text):
                return text_reply("ok")

        self.app.provider = Strict()
        with contextlib.redirect_stdout(io.StringIO()):
            self.app.run_turn("hi")
        self.assertEqual(self.app.context.complete([], tools=False).content, [{"type": "text", "text": "ok"}])

        self.app.registry.on("before_request", lambda event: {"options": {"temperature": 0.5}})
        self.app.registry.on("before_request", lambda event: {"options": {**event["options"], "max_tokens": 7}})
        self.app.provider = ScriptedProvider([text_reply("ok")])
        with contextlib.redirect_stdout(io.StringIO()):
            self.app.run_turn("again")
        self.assertEqual(self.app.provider.requests[0]["options"], {"temperature": 0.5, "max_tokens": 7})
        self.app.provider = mock.Mock(stream=mock.Mock(return_value=text_reply("ok")))
        self.app.context.complete([], options={"max_tokens": 3})
        self.assertEqual(self.app.provider.stream.call_args.kwargs["options"], {"max_tokens": 3})

    def test_ctx_notify(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.app.context.notify("hi", "warning")
            self.app.context.notify("plain")
        self.assertEqual(err.getvalue(), "\x1b[33mwarning: hi\x1b[0m\nplain\n")
        with self.assertRaisesRegex(ValueError, "kind must be one of info, warning, error"):
            self.app.context.notify("hi", "loud")

    def test_ctx_queue_user_message(self):
        self.app.context.queue_user_message("next")
        self.app.context.queue_user_message("/tools")
        self.assertEqual((self.app.take_queued(), self.app.take_queued(), self.app.take_queued()), ("next", "/tools", None))
        with self.assertRaisesRegex(ValueError, "text is empty"):
            self.app.context.queue_user_message("  ")

    def test_ctx_use_provider(self):
        self.app.reload_extensions()
        self.app.registry.register_provider("other", lambda: ScriptedProvider([]), "m1")
        self.app.context.use_provider("other", "m2")
        self.assertEqual((self.app.provider_name, self.app.model), ("other", "m2"))
        with self.assertRaisesRegex(ValueError, "unknown provider: nope"):
            self.app.context.use_provider("nope")

    def test_builtin_commands_get_ctx_and_can_be_replaced(self):
        self.extension(
            "myhelp.py",
            """
            def register(minimal):
                @minimal.command(description="mine")
                def help(args, ctx):
                    print("custom help")
            """,
        )
        self.app.reload_extensions()
        self.assertTrue(self.app.registry.builtin_commands["help"].builtin)
        self.assertEqual(self.app.context.commands()["help"], "mine")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.app.run_command("/help")
            self.app.context.run_command("/help", builtin=True)
            self.app.run_command("/tools")
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "custom help")
        self.assertIn("  /help       mine", lines)
        self.assertEqual(lines[-1], ", ".join(self.app.registry.tools))

    def test_max_tokens_setting(self):
        config.SETTINGS_FILE.write_text(json.dumps({"max_tokens": {"fake/m": 1234, "other": 99}}))
        self.assertEqual(self.app.reload_extensions()[1], [])
        self.app.provider_name, self.app.model = "fake", "m"
        self.app.provider = ScriptedProvider([text_reply("ok"), text_reply("ok")])
        with contextlib.redirect_stdout(io.StringIO()):
            self.app.run_turn("hi")
            self.app.model = "m2"  # no entry for fake/m2 nor fake: the provider's default
            self.app.run_turn("again")
        self.assertEqual(self.app.provider.requests[0]["options"], {"max_tokens": 1234})
        self.assertNotIn("options", self.app.provider.requests[1])
        self.assertEqual(self.app.settings.max_tokens_for("other", "x"), 99)

        config.SETTINGS_FILE.write_text(json.dumps({"max_tokens": 4000}))
        self.app.reload_extensions()
        self.assertEqual(self.app.request_options(), {"max_tokens": 4000})
        for bad in (0, True, "8k", {"fake": -1}):
            config.SETTINGS_FILE.write_text(json.dumps({"max_tokens": bad}))
            [error] = self.app.reload_extensions()[1]
            self.assertIn('max_tokens must be a positive integer or an object mapping "provider"', error)
            self.assertEqual(self.app.request_options(), {})

    def failing(self, *outcomes):
        """A provider that raises or returns each of ``outcomes`` in turn."""
        provider = mock.Mock(spec=["name", "stream"])
        provider.name = "fake"
        provider.stream.side_effect = list(outcomes)
        return provider

    def test_provider_error_retries(self):
        seen = []

        def retry_once(event):
            seen.append((type(event["error"]).__name__, event["attempt"], event["provider"], event["nested"]))
            return {"retry_after": 0} if event["attempt"] == 1 else None

        self.app.registry.on("provider_error", retry_once)
        self.app.provider = self.failing(RuntimeError("429"), text_reply("ok"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.app.run_turn("hi")
        self.assertEqual(self.app.provider.stream.call_count, 2)
        self.assertEqual(self.app.messages[-1]["content"], [{"type": "text", "text": "ok"}])
        self.assertEqual(seen, [("RuntimeError", 1, "fake", False)])

        self.app.provider = self.failing(RuntimeError("429"), RuntimeError("still 429"))
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.app.run_turn("again")
        self.assertEqual(self.app.provider.stream.call_count, 2)  # the handler gave up at attempt 2
        self.assertIn("Error: still 429", err.getvalue())

    def test_provider_error_without_a_handler_ends_the_turn(self):
        self.app.provider = self.failing(RuntimeError("boom"))
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.app.run_turn("hi")
        self.assertIn("Error: boom", err.getvalue())
        self.assertEqual(self.app.messages, [])

    def test_provider_error_switches_provider(self):
        self.app.reload_extensions()
        other = ScriptedProvider([text_reply("from other")])
        self.app.registry.register_provider("other", lambda: other, "m1")
        self.app.registry.on("provider_error", lambda event: {"provider": "other", "model": "m2"})
        self.app.provider_name, self.app.model = "fake", "m"
        self.app.provider = self.failing(RuntimeError("down"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.app.run_turn("hi")
        self.assertEqual((self.app.provider_name, self.app.model, self.app.provider), ("other", "m2", other))
        self.assertEqual(other.requests[0]["model"], "m2")
        self.assertEqual(self.app.messages[-1]["content"], [{"type": "text", "text": "from other"}])

    def test_interrupt_while_waiting_to_retry(self):
        def wait_long(event):
            self.app.cancelled.set()  # the user presses Esc during the wait
            return {"retry_after": 30}

        self.app.registry.on("provider_error", wait_long)
        self.app.provider = self.failing(RuntimeError("429"), text_reply("never"))
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.app.run_turn("hi")
        self.assertIn("Error: interrupted by the user", err.getvalue())
        self.assertEqual(self.app.provider.stream.call_count, 1)


if __name__ == "__main__":
    unittest.main()
