"""API 2.4 settings slice: ``Extension.ctx``, ``ctx.settings``, ``ctx.set_theme``, ``normalize_key``,
``declare_settings`` ``"merge": "deep"`` and ``Extension.report_problem``."""

import asyncio
import contextlib
import io
import json
import os
import threading
import unittest
from unittest import mock

from minimal import api, config
from minimal.core.app import App, ConsoleFrontend
from minimal.core.session import Session
from minimal.extensions import marketplace
from minimal.extensions.registry import Registry
from minimal.tools.reload import reload_extensions
from minimal.ui.commands import register_builtin_commands
from tests import test_customisation as base


class Api24Test(base.CustomisationTest):
    """A temp ``~/.minimal`` (settings and trust files) with a real ``App`` over the plain-terminal frontend."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(config, "TRUST_FILE", self.home / "trusted.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.marks = self.tmp / "marks.txt"
        patcher = mock.patch.dict(os.environ, {"MARKS": str(self.marks)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def app(self, **kwargs) -> App:
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.cwd),
            auto_approve=True,
            frontend=ConsoleFrontend(),
            register_commands=register_builtin_commands,
            **kwargs,
        )
        app.provider_name, app.model = "fake", "m"
        app.reload_extensions()
        return app

    def marks_lines(self) -> list[str]:
        return self.marks.read_text().splitlines() if self.marks.exists() else []

    def read(self, scope: str = "global") -> dict:
        return json.loads(config.settings_files(str(self.cwd))[scope].read_text())


class ExtensionCtxTest(Api24Test):
    WATCHER = """
        import os

        def register(minimal):
            seen = [minimal.ctx]  # still registering

            @minimal.command(description="probe")
            def probe(args, ctx):
                with open(os.environ["MARKS"], "a") as f:
                    f.write(f"during={seen[0]} after={minimal.ctx is not None and minimal.ctx.agent_id}\\n")
        """

    def test_ctx_is_none_while_register_runs_and_available_after_it_also_across_reload(self):
        self.write_ext("watcher", self.WATCHER)
        app = self.app()
        app.run_command("/probe")
        app.reload_extensions()  # a new Extension object, a new closure
        app.run_command("/probe")
        self.assertEqual(self.marks_lines(), ["during=None after=main"] * 2)

    def test_a_thread_started_in_register_can_use_it_once_loaded(self):
        self.write_ext(
            "ticker",
            """
            import os, threading

            def register(minimal):
                go = threading.Event()
                stop = threading.Event()

                def run():
                    go.wait(5)
                    ctx = minimal.ctx
                    with open(os.environ["MARKS"], "a") as f:
                        f.write(f"{ctx.cwd if ctx else None}\\n")

                t = threading.Thread(target=run)
                t.start()
                minimal.on_dispose(lambda: t.join())
                minimal.on("session_start")(lambda event: go.set())
                minimal.register_command("go", lambda args, ctx: go.set(), "go")
            """,
        )
        app = self.app()
        app.run_command("/go")
        app.registry.dispose()  # joins the thread
        self.assertEqual(self.marks_lines(), [str(self.cwd)])

    def test_ctx_is_none_under_ext_check(self):
        path = self.write_ext(
            "checked",
            """
            import os

            def register(minimal):
                with open(os.environ["MARKS"], "a") as f:
                    f.write(f"{minimal.ctx}\\n")
            """,
        )
        lines, ok = marketplace.check([path])
        self.assertTrue(ok, lines)
        self.assertEqual(self.marks_lines(), ["None"])


class RunCommandInterruptTest(Api24Test):
    def setUp(self):
        super().setUp()
        self.write_ext(
            "flag",
            """
            import os

            def register(minimal):
                @minimal.command(description="record the interrupt flag")
                def flag(args, ctx):
                    with open(os.environ["MARKS"], "a") as f:
                        f.write(f"{ctx.cancelled}\\n")

                @minimal.command(description="run flag from inside")
                def outer(args, ctx):
                    ctx.run_command("/flag")
            """,
        )
        self.app_ = self.app()

    def test_idle_run_command_still_clears_a_stale_interrupt(self):
        self.app_.cancelled.set()
        self.app_.context.run_command("/flag")
        self.assertEqual(self.marks_lines(), ["False"])

    def test_run_command_from_another_thread_during_a_turn_keeps_the_users_esc(self):
        self.app_.agents.begin_main("hi", "m", "fake")  # a turn is running
        self.app_.cancelled.set()
        worker = threading.Thread(target=lambda: self.app_.context.run_command("/flag"))
        worker.start()
        worker.join()
        self.assertEqual(self.marks_lines(), ["True"])
        self.assertTrue(self.app_.cancelled.is_set())

    def test_a_command_running_another_does_not_clear_it_either(self):
        self.app_.cancelled.set()
        self.app_.run_command("/outer")  # the outer one clears (idle), the nested one must not
        self.assertEqual(self.marks_lines(), ["False"])
        self.marks.unlink()
        self.app_.cancelled.clear()

        entered = threading.Event()
        release = threading.Event()
        self.app_.registry.register_command("slow", lambda args, ctx: (entered.set(), release.wait(5)), "slow")
        slow = threading.Thread(target=lambda: self.app_.run_command("/slow"))
        slow.start()
        self.assertTrue(entered.wait(5))
        self.app_.cancelled.set()
        self.app_.context.run_command("/flag")
        release.set()
        slow.join()
        self.assertEqual(self.marks_lines(), ["True"])


class SettingsReadTest(Api24Test):
    def test_get_merges_layers_like_the_loader_and_defaults_when_unset(self):
        self.write_settings(
            {
                "theme": "dark",
                "prompt": {"append": "g"},
                "ui": {"widgets": {"a": {"area": "top", "order": 1}}, "sidebar": "left"},
                "extensions": {"settings": {"x": {"n": 1, "tools": {"a": 1}}}},
            }
        )
        self.write_settings(
            {
                "theme": "light",
                "prompt": {"append": "p"},
                "ui": {"widgets": {"a": {"order": 2}, "b": {"enabled": False}}},
                "extensions": {"settings": {"x": {"tools": {"b": 2}}}},
            },
            "project",
        )
        self.write_settings({"prompt": {"append": ["l"]}}, "local")
        app = self.app(trust_project=True)
        settings = app.context.settings
        self.assertEqual(settings.get("theme"), "light")
        self.assertEqual(settings.get("prompt.append"), ["g", "p", "l"])
        self.assertEqual(settings.get("ui.sidebar"), "left")
        self.assertEqual(settings.get("ui.widgets"), {"a": {"area": "top", "order": 2}, "b": {"enabled": False}})
        self.assertEqual(settings.get("ui.widgets.a.area"), "top")
        self.assertEqual(settings.get("extensions.settings.x.n"), 1)
        self.assertEqual(settings.get("extensions.settings.x.tools"), {"b": 2})  # replace by default
        self.assertEqual(settings.get("reasoning_effort", "unset"), "unset")
        self.assertIsNone(settings.get("ui.widgets.zzz.area"))
        with self.assertRaisesRegex(ValueError, "unknown setting 'nope'"):
            settings.get("nope")

    def test_get_returns_a_copy(self):
        self.write_settings({"ui": {"widgets": {"a": {"order": 1}}}, "prompt": {"append": ["x"]}})
        settings = self.app().context.settings
        settings.get("ui.widgets")["a"]["order"] = 9
        settings.get("prompt.append").append("y")
        self.assertEqual(settings.get("ui.widgets"), {"a": {"order": 1}})
        self.assertEqual(settings.get("prompt.append"), ["x"])

    def test_untrusted_project_trust_gated_keys_do_not_count(self):
        self.write_settings({"permissions": {"allow": ["read"]}})
        self.write_settings({"permissions": {"allow": ["bash"]}, "ui": {"density": "compact"}}, "project")
        settings = self.app().context.settings
        self.assertEqual(settings.get("permissions.allow"), ["read"])
        self.assertEqual(settings.layers("permissions.allow"), {"global": ["read"]})
        self.assertEqual(settings.get("ui.density"), "compact")  # not trust-gated
        trusted = self.app(trust_project=True).context.settings
        self.assertEqual(trusted.get("permissions.allow"), ["read", "bash"])

    def test_layers_says_which_file_sets_a_key_lowest_first(self):
        self.write_settings({"theme": "a", "ui": {"widgets": {"w": {"order": 1}}}})
        self.write_settings({"theme": "b"}, "local")
        settings = self.app().context.settings
        self.assertEqual(list(settings.layers("theme").items()), [("global", "a"), ("local", "b")])
        self.assertEqual(settings.layers("ui.widgets.w.order"), {"global": 1})
        self.assertEqual(settings.layers("ui.widgets.w.area"), {})
        self.assertEqual(settings.layers("model"), {})


class SettingsWriteTest(Api24Test):
    def counting_extension(self) -> None:
        self.write_ext(
            "counter",
            """
            import os

            def register(minimal):
                with open(os.environ["MARKS"], "a") as f:
                    f.write("loaded\\n")
            """,
        )

    def test_update_writes_all_keys_to_the_scope_file_and_reloads_once(self):
        self.counting_extension()
        app = self.app()
        self.assertEqual(self.marks_lines(), ["loaded"])
        app.context.settings.update({"ui.sidebar": "left", "ui.density": "compact", "reasoning_effort": "low"})
        self.assertEqual(self.marks_lines(), ["loaded", "loaded"])
        self.assertEqual(self.read(), {"ui": {"sidebar": "left", "density": "compact"}, "reasoning_effort": "low"})
        self.assertEqual(oct(config.SETTINGS_FILE.stat().st_mode & 0o777), "0o600")
        self.assertEqual(app.context.settings.get("ui.sidebar"), "left")  # the reload read it
        self.assertEqual(app.settings.ui_density, "compact")

    def test_apply_false_writes_without_reloading_and_unset_removes_and_cleans_groups(self):
        self.counting_extension()
        app = self.app()
        settings = app.context.settings
        settings.update({"ui.sidebar": "left"}, apply=False)
        self.assertEqual(self.marks_lines(), ["loaded"])
        self.assertEqual(self.read(), {"ui": {"sidebar": "left"}})
        self.assertIsNone(app.settings.scalar("ui.sidebar")[0])
        settings.unset("ui.sidebar")
        self.assertEqual(self.read(), {})
        self.assertEqual(self.marks_lines(), ["loaded", "loaded"])

    def test_project_and_local_scopes_write_their_own_files_and_the_higher_layer_wins(self):
        self.write_settings({"ui": {"sidebar": "left"}})
        app = self.app()
        settings = app.context.settings
        settings.update({"ui.density": "compact"}, scope="project")
        settings.update({"ui.sidebar": "hidden"}, scope="local")
        self.assertEqual(self.read("project"), {"ui": {"density": "compact"}})
        self.assertEqual(self.read("local"), {"ui": {"sidebar": "hidden"}})
        self.assertEqual(self.read(), {"ui": {"sidebar": "left"}})
        self.assertEqual(settings.get("ui.sidebar"), "hidden")
        self.assertEqual(list(settings.layers("ui.sidebar")), ["global", "local"])

    def test_invalid_values_unknown_keys_and_scopes_are_refused_and_nothing_is_written(self):
        self.write_settings({"theme": "keep"})
        before = config.SETTINGS_FILE.read_text()
        settings = self.app().context.settings
        for changes, message in (
            ({"ui.sidebar": "middle"}, "ui.sidebar: ui.sidebar must be one of right, left, hidden"),
            ({"ui.widgets.w": {"area": "nowhere"}}, "ui.widgets must be an object mapping names"),
            ({"nope": 1}, "unknown setting 'nope'"),
            ({"ui": {"sidebar": "left"}}, "unknown setting 'ui'"),
            ({"permissions.allow": ["bad rule!"]}, "invalid permission rule 'bad rule!'"),
            ({"theme": "ok", "agent.max_turns": 0}, "agent.max_turns must be a positive integer"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                settings.update(changes)
        with self.assertRaisesRegex(ValueError, "scope must be one of global, project, local"):
            settings.update({"theme": "x"}, scope="user")
        self.assertEqual(config.SETTINGS_FILE.read_text(), before)  # the valid half of a refused batch is not written

    def test_global_only_keys_are_refused_at_project_and_local_scope(self):
        settings = self.app().context.settings
        for key, value in (("marketplaces", ["u"]), ("prompt.base", "p.md"), ("ui.css", "a.tcss")):
            for scope in ("project", "local"):
                with self.assertRaisesRegex(ValueError, "only read from ~/.minimal/settings.json"):
                    settings.update({key: value}, scope=scope)
        self.assertFalse(config.project_settings_files(str(self.cwd))["project"].exists())
        settings.update({"prompt.base": "p.md"})  # global is fine
        self.assertEqual(self.read(), {"prompt": {"base": "p.md"}})

    def test_trust_gated_keys_need_a_trusted_project_and_then_ask_for_trust_again(self):
        app = self.app()
        settings = app.context.settings
        self.write_settings({"permissions": {"allow": ["read"]}}, "project")
        app.reload_extensions()  # now the project asks to be trusted for something
        self.assertFalse(app.settings.trusted)
        for key, value in (("permissions.allow", ["bash"]), ("extensions.disabled", ["x"]), ("extensions.settings.x.n", 1)):
            with self.assertRaisesRegex(ValueError, "not trusted yet.*?/trust"):
                settings.update({key: value}, scope="project")
            with self.assertRaisesRegex(ValueError, "not trusted yet"):
                settings.update({key: value}, scope="local")
        self.assertEqual(self.read("project"), {"permissions": {"allow": ["read"]}})
        settings.update({"permissions.allow": ["bash"]})  # global scope is always fine
        trusted = self.app(trust_project=True)
        trusted.context.settings.update({"extensions.disabled": ["zzz"]}, scope="local")
        self.assertEqual(self.read("local"), {"extensions": {"disabled": ["zzz"]}})
        self.assertIn("zzz", trusted.settings.disabled)

    def test_writing_a_trust_gated_key_to_a_project_with_nothing_pending_is_allowed_and_asks_for_trust(self):
        app = self.app()
        self.assertTrue(app.settings.trusted)
        app.context.settings.update({"permissions.allow": ["bash"]}, scope="project")
        self.assertEqual(self.read("project"), {"permissions": {"allow": ["bash"]}})
        self.assertIsNotNone(app.untrusted)  # what /trust never reviewed does not apply yet
        self.assertEqual(app.settings.allow, [])
        self.assertIn("/trust", app.untrusted_notice())

    def test_extension_settings_are_checked_against_the_declared_schema(self):
        self.write_ext(
            "lenses",
            """
            def register(minimal):
                minimal.declare_settings({"head": 5}, {"head": {"min": 1, "max": 10}})
            """,
        )
        app = self.app()
        settings = app.context.settings
        with self.assertRaisesRegex(ValueError, "extensions.settings.lenses.head must be at most 10, got 50"):
            settings.update({"extensions.settings.lenses.head": 50})
        with self.assertRaisesRegex(ValueError, "must be int"):
            settings.update({("extensions", "settings", "lenses", "head"): "x"})
        settings.update({("extensions", "settings", "lenses", "head"): 7})
        self.assertEqual(app.registry.declared_settings["lenses"].values["head"], 7)
        self.assertEqual(settings.get("extensions.settings.lenses.head"), 7)

    def test_a_broken_settings_file_is_left_alone(self):
        config.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.SETTINGS_FILE.write_text("{not json")
        settings = self.app().context.settings
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            settings.update({"theme": "x"})
        self.assertEqual(config.SETTINGS_FILE.read_text(), "{not json")

    def test_ui_command_and_ext_settings_write_through_the_same_validated_writer(self):
        self.write_ext("lenses", 'def register(minimal):\n    minimal.declare_settings({"head": 5}, {"head": {"max": 10}})\n')
        app = self.app()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app.run_command("/ui sidebar left")
            app.run_command("/ext settings lenses head 8")
        self.assertEqual(self.read(), {"ui": {"sidebar": "left"}, "extensions": {"settings": {"lenses": {"head": 8}}}})
        self.assertEqual(app.registry.declared_settings["lenses"].values["head"], 8)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            app.run_command("/ext settings lenses head 11")
        self.assertIn("must be at most 10", err.getvalue())
        self.assertEqual(self.read()["extensions"]["settings"]["lenses"]["head"], 8)

    def test_update_global_still_validates_what_setup_and_marketplaces_write(self):
        config.update_global({"provider": "a", "marketplaces": ["u", {"name": "n", "url": "v"}]})
        with self.assertRaisesRegex(ValueError, "provider must be a string"):
            config.update_global({"provider": 3})
        self.assertEqual(self.read()["provider"], "a")


class SetThemeTest(Api24Test):
    def test_the_plain_terminal_has_no_themes(self):
        self.assertFalse(self.app().context.set_theme("ocean", save=True))
        self.assertFalse(config.SETTINGS_FILE.exists())


class TuiSetThemeTest(base.TuiCustomisationTest):
    OCEAN = """
        import os

        def register(minimal):
            minimal.register_theme("ocean", dark=True, background="#0b1622", foreground="#d0e4f5", accent="#4fc3f7")

            @minimal.on("theme_change")
            def changed(event):
                with open(os.environ["MARKS"], "a") as f:
                    f.write(f"{event['name']}\\n")
        """

    async def test_set_theme_switches_fires_theme_change_and_saves_on_request(self):
        self.write_ext("ocean", self.OCEAN)
        app = self.start()
        async with app.run_test() as pilot:
            ctx = app.host.ctx
            self.assertFalse(await asyncio.to_thread(ctx.set_theme, "nope"))
            self.assertFalse(self.marks.exists())
            self.assertTrue(await asyncio.to_thread(ctx.set_theme, "ocean"))
            await self.wait_until(pilot, lambda: self.marks.exists())
            self.assertEqual(self.marks.read_text().split(), ["ocean"])
            self.assertEqual((app.theme, ctx.theme.name), ("ocean", "ocean"))
            self.assertFalse(config.SETTINGS_FILE.exists())  # not saved without save=True
            self.assertTrue(await asyncio.to_thread(ctx.set_theme, "minimal", save=True))
            self.assertEqual(json.loads(config.SETTINGS_FILE.read_text()), {"theme": "minimal"})
            await pilot.pause()


class DeepMergeTest(Api24Test):
    DECLARE = """
        def register(minimal):
            s = minimal.declare_settings(
                {"tools": {}, "plain": {}, "n": 1},
                {"tools": {"merge": "deep", "description": "tool table"}},
            )
            import os, json
            with open(os.environ["MARKS"], "a") as f:
                f.write(json.dumps([s["tools"], s["plain"]], sort_keys=True) + "\\n")
        """

    def test_deep_merge_combines_layers_key_by_key_and_replace_stays_the_default(self):
        self.write_ext("tbl", self.DECLARE)
        self.write_settings(
            {"extensions": {"settings": {"tbl": {"tools": {"a": {"x": 1, "y": 1}, "b": 1}, "plain": {"p": 1}}}}}
        )
        self.write_settings(
            {"extensions": {"settings": {"tbl": {"tools": {"a": {"y": 2}, "c": 3}, "plain": {"q": 2}, "n": 5}}}},
            "project",
        )
        self.write_settings({"extensions": {"settings": {"tbl": {"tools": {"c": None}}}}}, "local")
        app = self.app(trust_project=True)
        self.assertEqual(
            json.loads(self.marks_lines()[0]), [{"a": {"x": 1, "y": 2}, "b": 1, "c": None}, {"q": 2}]
        )
        self.assertEqual(app.registry.declared_settings["tbl"].values["n"], 5)
        self.assertEqual(app.context.settings.get("extensions.settings.tbl.tools.a"), {"x": 1, "y": 2})
        self.assertEqual(app.context.settings.get("extensions.settings.tbl.plain"), {"q": 2})

    def test_an_untrusted_project_layer_is_not_merged(self):
        self.write_ext("tbl", self.DECLARE)
        self.write_settings({"extensions": {"settings": {"tbl": {"tools": {"a": 1}}}}})
        self.write_settings({"extensions": {"settings": {"tbl": {"tools": {"b": 2}}}}}, "project")
        self.app()
        self.assertEqual(json.loads(self.marks_lines()[0]), [{"a": 1}, {}])

    def test_a_non_dict_value_in_a_layer_replaces_and_a_wrong_type_is_still_an_error(self):
        self.write_ext("tbl", self.DECLARE)
        self.write_settings({"extensions": {"settings": {"tbl": {"tools": {"a": {"b": 1}}}}}})
        self.write_settings({"extensions": {"settings": {"tbl": {"tools": {"a": [1]}}}}}, "local")
        self.app(trust_project=True)
        self.assertEqual(json.loads(self.marks_lines()[0])[0], {"a": [1]})
        self.write_settings({"extensions": {"settings": {"tbl": {"tools": 3}}}}, "local")
        errors = self.app(trust_project=True).extension_errors
        self.assertTrue(any("tools must be dict, got 3" in e for e in errors), errors)

    def test_merge_must_be_replace_or_deep_and_deep_needs_an_object_default(self):
        for default, entry, message in (
            ({"a": {}}, {"merge": "shallow"}, "merge must be one of replace, deep"),
            ({"a": 1}, {"merge": "deep"}, "merge 'deep' needs an object setting"),
        ):
            self.write_ext("bad", f"def register(minimal):\n    minimal.declare_settings({default!r}, {{'a': {entry!r}}})\n")
            _, loaded, errors = self.load()
            self.assertEqual(loaded, [])
            self.assertIn(message, errors[0])
        self.write_ext("ok", 'def register(minimal):\n    minimal.declare_settings({"a": {}}, {"a": {"merge": "replace"}})\n')
        self.assertEqual([p.name for p in self.load()[1]], ["ok.py"])

    def test_settings_spec_keeps_the_merge_field_for_ext_settings(self):
        self.write_ext("tbl", self.DECLARE)
        app = self.app()
        self.assertEqual(app.registry.declared_settings["tbl"].schema["tools"]["merge"], "deep")


class ReportProblemTest(Api24Test):
    CROSSFIELD = """
        def register(minimal):
            s = minimal.declare_settings({"head": 5, "tail": 5})
            if s["head"] > s["tail"]:
                minimal.report_problem("head is larger than tail; using tail")
            minimal.register_tool("probe", lambda input, ctx: "ok", "probe")
        """

    def test_a_problem_is_a_warning_not_a_failure_and_clears_on_reload(self):
        self.write_ext("lenses", self.CROSSFIELD)
        self.write_settings({"extensions": {"settings": {"lenses": {"head": 9}}}})
        app = self.app()
        self.assertEqual(app.extension_problems, ["lenses: head is larger than tail; using tail"])
        self.assertEqual(app.extension_errors, [])
        self.assertIn("probe", app.registry.tools)  # still loaded
        summary = app.reload_summary()
        self.assertIn("Loaded 1 extension(s): lenses.py\nWarning: lenses: head is larger than tail; using tail", summary)
        self.assertNotIn("Failed", summary)
        self.assertEqual(app.extension_problems, ["lenses: head is larger than tail; using tail"])  # not doubled
        self.assertEqual(reload_extensions({}, app.context), summary)  # the model's tool does not raise for it
        self.write_settings({"extensions": {"settings": {"lenses": {"head": 2}}}})
        self.assertNotIn("Warning", app.reload_summary())
        self.assertEqual(app.extension_problems, [])

    def test_ext_list_prints_it(self):
        self.write_ext("lenses", self.CROSSFIELD)
        self.write_settings({"extensions": {"settings": {"lenses": {"head": 9}}}})
        app = self.app()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app.run_command("/ext list")
        self.assertIn("warning  lenses: head is larger than tail; using tail", out.getvalue())

    def test_ext_check_prints_a_warn_line_and_still_passes(self):
        path = self.write_ext("lenses", 'def register(minimal):\n    minimal.report_problem("no  fzf\\n on PATH")\n')
        lines, ok = marketplace.check([path])
        self.assertTrue(ok)
        self.assertEqual(lines[0].split(": ", 1), ["WARN extensions/lenses", "lenses: no fzf on PATH"])

    def test_empty_text_is_refused_and_a_problem_from_one_extension_does_not_leak_to_the_next_check(self):
        path = self.write_ext("bad", 'def register(minimal):\n    minimal.report_problem("  ")\n')
        lines, ok = marketplace.check([path])
        self.assertFalse(ok)
        self.assertIn("report_problem: text must be a non-empty string", lines[0])
        good = self.write_ext("good", "def register(minimal):\n    pass\n")
        lines, ok = marketplace.check([good])
        self.assertTrue(ok)
        self.assertTrue(lines[0].startswith("ok"))


class ApiExportTest(unittest.TestCase):
    def test_normalize_key_is_public(self):
        self.assertIn("normalize_key", api.__all__)
        self.assertEqual(api.normalize_key(" Ctrl+O "), "ctrl+o")
        with self.assertRaisesRegex(ValueError, "^my key:.*would steal typing"):
            api.normalize_key("a", "my key")
        self.assertIn("ContextSettings", api.__all__)
        self.assertEqual(api.SETTING_MERGES, ("replace", "deep"))


if __name__ == "__main__":
    unittest.main()
