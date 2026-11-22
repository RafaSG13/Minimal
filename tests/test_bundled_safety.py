"""The marketplace's safety and workflow extensions: scope_fence, tripwire, loose_ends and crystallize."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import unittest
from unittest import mock

from minimal import config
from minimal.core import session as session_mod
from minimal.core.app import App
from minimal.core.session import Session
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply

from tests.test_bundled_extensions import MARKETPLACE, BundledExtensionTest, RecordingProvider, text_reply


def tool_reply(name, input, id="c1"):
    return Reply(content=[{"type": "tool_use", "id": id, "name": name, "input": input}], stop_reason="tool_use")


def load_module(name):
    spec = importlib.util.spec_from_file_location(f"safety_test_{name}", MARKETPLACE / "extensions" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SafetyTest(BundledExtensionTest):
    """One extension loaded into a real ``App`` in a temporary project, with ``extensions.settings`` from ``settings``."""

    EXT = ""

    def setUp(self):
        super().setUp()
        for patcher in (
            mock.patch.object(config, "HOME_DIR", self.tmp / "home"),
            mock.patch.object(session_mod, "SESSIONS_DIR", self.tmp / "sessions"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.project = self.tmp / "project"
        self.project.mkdir()
        old = os.getcwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, old)
        self.frontend = mock.Mock()
        self.frontend.ask.return_value = True

    def git_repo(self, files):
        def git(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=self.project, check=True, capture_output=True)

        git("init", "-q")
        for name, text in files.items():
            (self.project / name).parent.mkdir(parents=True, exist_ok=True)
            (self.project / name).write_text(text)
        git("add", ".")
        git("commit", "-q", "-m", "init")

    def load(self, settings=None, replies=()):
        """``(app, errors)`` with the extension loaded; a load problem is returned, not asserted away. The non-fatal
        problems it reported with ``report_problem`` are in ``self.warnings``."""
        (self.tmp / "settings.json").write_text(
            json.dumps({"extensions": {"settings": {self.EXT: settings or {}}}})
        )
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.project),
            auto_approve=True,
            trust_project=True,
            frontend=self.frontend,
            extra_extensions=[str(MARKETPLACE / "extensions" / f"{self.EXT}.py")],
        )
        _, errors = app.reload_extensions()
        self.warnings = app.extension_problems
        app.provider, app.model = RecordingProvider(replies), "m"
        return app, errors

    def make(self, settings=None, replies=()):
        app, errors = self.load(settings, replies)
        self.assertEqual((errors, self.warnings), ([], []))
        return app

    def command(self, app, line):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            app.run_command(line)
        return out.getvalue()

    @staticmethod
    def results(app, request=-1):
        """The tool_result texts the model saw in its ``request``-th (default: latest) request."""
        texts = []
        for message in app.provider.requests[request].messages:
            if isinstance(message.get("content"), list):
                texts += [str(b.get("content")) for b in message["content"] if b.get("type") == "tool_result"]
        return "\n".join(texts)


class ScopeFenceSettingsTest(SafetyTest):
    EXT = "scope_fence"

    def write_turn(self, app, path="src/x.py", tool="write"):
        input = {"path": path, "content": "x"} if tool == "write" else {"path": path, "old": "a", "new": "b"}
        app.provider.replies[:] = [tool_reply(tool, input), text_reply("ok")]
        self.run_turn(app, "go")
        return self.results(app)

    def test_default_message_and_defaults_unchanged(self):
        app = self.make()
        self.command(app, "/fence docs/**")
        seen = self.write_turn(app)
        self.assertIn("src/x.py is outside the fence (docs/**). Do not work around it:", seen)
        self.assertIn("widen the fence with /fence", seen)
        self.assertFalse((self.project / "src" / "x.py").exists())
        self.assertNotIn("fence", app.registry.status)
        self.assertEqual(app.registry.keybindings, {})
        self.assertEqual(app.registry.panels["fence"].title, "FENCE")

    def test_messages_are_settings(self):
        app = self.make({"outside_message": "no {path} (only {globs})"})
        self.command(app, "/fence docs/**")
        self.assertIn("no src/x.py (only docs/**)", self.write_turn(app))

    def test_bad_template_loads_with_the_default_message_and_a_warning_with_the_fix(self):
        app, errors = self.load({"outside_message": "no {file}"})
        self.assertEqual(errors, [])
        (warning,) = self.warnings
        self.assertIn("scope_fence: extensions.settings.scope_fence.outside_message", warning)
        self.assertIn("{path}", warning)
        self.assertIn("default message is used", warning)
        self.command(app, "/fence docs/**")
        seen = self.write_turn(app)  # the fence still holds, with the default wording
        self.assertIn("src/x.py is outside the fence (docs/**). Do not work around it:", seen)
        self.assertFalse((self.project / "src" / "x.py").exists())

    def test_a_bad_template_only_resets_its_own_key(self):
        app, errors = self.load({"outside_message": "no {file}", "budget_message": "over: {used}/{lines}"})
        self.assertEqual((errors, len(self.warnings)), ([], 1))
        self.command(app, "/fence docs/** --lines 1")
        self.assertIn("src/x.py is outside the fence", self.write_turn(app))

    def test_wrong_type_is_reported_and_default_kept(self):
        app, errors = self.load({"default_lines": "many"})
        self.assertTrue(any("default_lines" in e for e in errors), errors)
        self.assertIn("fence", app.registry.panels)

    def test_unknown_tool_falls_back_to_guarding_write_and_edit_with_a_warning(self):
        app, errors = self.load({"tools": ["write", "bash"]})
        self.assertEqual(errors, [])
        (warning,) = self.warnings
        self.assertTrue("scope_fence.tools" in warning and "write, edit" in warning, warning)
        self.command(app, "/fence docs/**")
        self.write_turn(app, tool="edit", path="src/y.py")
        self.assertIn("src/y.py is outside the fence", self.results(app))  # edit is guarded too: the default list

    def test_bad_exempt_falls_back_to_the_default_prefixes(self):
        app, errors = self.load({"exempt": ["ok/", ""]})
        self.assertEqual(errors, [])
        (warning,) = self.warnings
        self.assertIn("scope_fence.exempt must be a list of non-empty strings", warning)
        self.command(app, "/fence docs/**")
        self.assertIn("src/x.py is outside the fence", self.write_turn(app))

    def test_tools_setting_narrows_the_guard(self):
        app = self.make({"tools": ["edit"]})
        self.command(app, "/fence docs/**")
        self.write_turn(app)
        self.assertTrue((self.project / "src" / "x.py").exists())  # write is no longer guarded

    def test_exempt_prefix(self):
        app = self.make({"exempt": [".minimal/", "scratch/"]})
        self.command(app, "/fence docs/**")
        self.write_turn(app, "scratch/a.txt")
        self.assertTrue((self.project / "scratch" / "a.txt").exists())

    def test_default_lines_budget(self):
        self.git_repo({"a.txt": "1\n2\n3\n"})
        (self.project / "a.txt").write_text("a\nb\nc\n")  # 6 changed lines
        app = self.make({"default_lines": 4, "budget_message": "over {used}/{lines}"})
        self.assertIn("budget 4 lines", self.command(app, "/fence src/**"))
        self.assertIn("over 6/4", self.write_turn(app, "src/x.py"))
        self.assertFalse((self.project / "src" / "x.py").exists())
        self.assertIn("budget 9 lines", self.command(app, "/fence src/** --lines 9"))  # --lines wins

    def test_panel_shows_blocked_count_setting(self):
        app = self.make({"blocked_shown": 1, "title": "SCOPE"})
        self.command(app, "/fence docs/**")
        self.write_turn(app, "one.py")
        self.write_turn(app, "two.py")
        spec = app.registry.panels["fence"]
        self.assertEqual(spec.title, "SCOPE")
        text = spec.render(mock.Mock(cwd=str(self.project)))
        self.assertIn("blocked 2: two.py", text)
        self.assertNotIn("one.py,", text)

    def test_status_segment_and_key(self):
        app = self.make({"status": True, "status_side": "right", "key": "ctrl+shift+f"})
        spec = app.registry.status["fence"]
        self.assertEqual(spec.side, "right")
        self.assertIsNone(spec.render(mock.Mock(cwd=str(self.project))))
        self.command(app, "/fence docs/**")
        self.assertEqual(spec.render(mock.Mock(cwd=str(self.project))), "fence docs/**")
        binding = app.registry.keybindings["ctrl+shift+f"]
        ctx = mock.Mock()
        binding.run(ctx)
        ctx.notify.assert_called_once_with("Fence: docs/**")

    def test_bad_key_is_a_warning_with_the_fix_and_the_fence_still_loads(self):
        app, errors = self.load({"key": "x"})
        self.assertEqual(errors, [])
        (warning,) = self.warnings
        self.assertTrue("scope_fence.key" in warning and "\"\"" in warning, warning)
        self.assertIn("fence", app.registry.panels)
        self.assertIsNotNone(app.find_command("fence"))
        self.assertEqual(app.registry.keybindings, {})

    def test_disabled_registers_nothing(self):
        app = self.make({"enabled": False})
        self.assertNotIn("fence", app.registry.panels)
        self.assertIsNone(app.find_command("fence"))


class TripwireTest(SafetyTest):
    EXT = "tripwire"

    def setUp(self):
        super().setUp()
        self.git_repo({"a.txt": "good\n"})

    def break_turn(self, app, text="bad\n"):
        app.provider.replies[:] = [tool_reply("write", {"path": "a.txt", "content": text}), text_reply("ok")]
        self.run_turn(app, "go")
        return self.results(app)

    def test_default_note_names_the_broken_invariant(self):
        app = self.make()
        self.assertIn("passing now", self.command(app, "/tripwire add grep -q good a.txt"))
        seen = self.break_turn(app)
        self.assertIn('[tripwire] "grep -q good a.txt" was passing before this call and fails now:\n(no output)', seen)
        self.frontend.notify.assert_not_called()  # notify is off by default

    def test_note_templates_are_settings(self):
        app = self.make({"broke_note": "BROKE {cmd}!"})
        self.command(app, "/tripwire add grep -q good a.txt")
        self.assertIn("BROKE grep -q good a.txt!", self.break_turn(app))

    def test_bad_template_loads_with_the_default_note_and_a_warning_with_the_fix(self):
        app, errors = self.load({"still_note": "{nope}", "broke_note": "BROKE {cmd}!"})
        self.assertEqual(errors, [])
        (warning,) = self.warnings
        self.assertTrue("tripwire: extensions.settings.tripwire.still_note" in warning and "{cmd}" in warning, warning)
        self.assertIn("default note is used", warning)
        self.command(app, "/tripwire add grep -q good a.txt")
        self.assertIn("BROKE grep -q good a.txt!", self.break_turn(app))  # the good template still applies; the wire guards
        self.assertIn("tripwire", app.registry.panels)

    def test_bad_glyph_falls_back_to_the_default_with_a_warning(self):
        app, errors = self.load({"ok_glyph": "\n"})
        self.assertEqual(errors, [])
        (warning,) = self.warnings
        self.assertIn("tripwire.ok_glyph must be printable text", warning)
        self.command(app, "/tripwire add true")
        self.assertTrue(app.registry.panels["tripwire"].render(mock.Mock()).startswith("●"))

    def test_progress_and_failure_notice(self):
        app = self.make({"notify": True})
        self.command(app, "/tripwire add grep -q good a.txt")
        self.frontend.progress.reset_mock()
        self.break_turn(app)
        calls = [c.args[0] for c in self.frontend.progress.call_args_list]
        self.assertIn("tripwire 1/1: grep -q good a.txt", calls)
        self.assertIsNone(calls[-1])
        self.frontend.notify.assert_called_once()
        text, kind = self.frontend.notify.call_args.args
        self.assertEqual(kind, "warning")
        self.assertIn("grep -q good a.txt", text)

    def test_progress_can_be_turned_off(self):
        app = self.make({"progress": False})
        self.command(app, "/tripwire add grep -q good a.txt")
        self.frontend.progress.reset_mock()
        self.break_turn(app)
        self.frontend.progress.assert_not_called()

    def test_debounce_skips_the_next_run(self):
        app = self.make({"debounce": 600})
        self.command(app, "/tripwire add grep -q good a.txt")
        self.command(app, "/tripwire run")
        self.assertNotIn("tripwire", self.break_turn(app))  # skipped: it ran a moment ago
        self.assertIn("passing", self.command(app, "/tripwire"))  # state not re-run

    def test_commands_setting_adds_managed_invariants(self):
        app = self.make({"commands": ["grep -q good a.txt"]})
        self.assertIn("1. not run  grep -q good a.txt", self.command(app, "/tripwire"))
        self.assertIn("comes from the commands setting", self.command(app, "/tripwire rm 1"))
        self.assertIn("1. not run", self.command(app, "/tripwire"))
        self.assertIn('[tripwire] "grep -q good a.txt" fails:', self.break_turn(app))  # runs with the rest after a change
        self.assertIn("1. failing  grep -q good a.txt", self.command(app, "/tripwire"))
        app = self.make({"commands": []})  # removed from settings: gone
        self.assertIn("No tripwires", self.command(app, "/tripwire"))

    def test_commands_keep_hand_added_ones(self):
        app = self.make()
        self.command(app, "/tripwire add true")
        app = self.make({"commands": ["false"]})
        listing = self.command(app, "/tripwire")
        self.assertIn("true", listing)
        self.assertIn("false", listing)

    def test_bad_commands_entry_is_dropped_with_a_warning_and_the_others_still_guard(self):
        app, errors = self.load({"commands": ["grep -q good a.txt", ""]})
        self.assertEqual(errors, [])
        (warning,) = self.warnings
        self.assertIn("tripwire.commands must be a list of non-empty command strings", warning)
        self.assertIn("grep -q good a.txt", self.command(app, "/tripwire"))
        self.assertIn('[tripwire] "grep -q good a.txt" fails:', self.break_turn(app))  # the configured wire still guards

    def test_timeout_and_output_limits(self):
        module = load_module("tripwire")
        settings = dict(module.DEFAULTS)
        ok, tail = module._run_wire("sleep 5", self.project, {**settings, "timeout": 1})
        self.assertEqual((ok, tail), (False, "timed out after 1s"))
        ok, tail = module._run_wire("seq 1 40; false", self.project, settings)
        self.assertEqual(tail.splitlines()[0], "26")  # the last 15 lines by default
        ok, tail = module._run_wire("seq 1 40; false", self.project, {**settings, "tail_lines": 2})
        self.assertEqual(tail, "39\n40")
        ok, tail = module._run_wire("echo abcdefghij; false", self.project, {**settings, "clip_chars": 4})
        self.assertEqual(tail, "…ghij")

    def test_panel_glyphs_and_title(self):
        app = self.make({"fail_glyph": "X", "title": "WIRES", "area": "footer"})
        self.command(app, "/tripwire add false")
        spec = app.registry.panels["tripwire"]
        self.assertEqual((spec.area, spec.title), ("footer", "WIRES"))
        self.assertTrue(spec.render(mock.Mock()).startswith("X 0/1 wires"))

    def test_disabled_registers_nothing(self):
        app = self.make({"enabled": False})
        self.assertNotIn("tripwire", app.registry.panels)

    def test_prompt_note_can_be_replaced_or_dropped(self):
        app = self.make({"prompt_note": ""})
        self.assertNotIn("Tripwires:", app.system_prompt())


class LooseEndsTest(SafetyTest):
    EXT = "loose_ends"

    def note(self, app, text, kind="noticed"):
        app.provider.replies[:] = [tool_reply("note", {"kind": kind, "text": text}), text_reply("ok")]
        self.run_turn(app, "go")
        return self.results(app)

    def test_defaults_unchanged(self):
        app = self.make()
        self.assertEqual(self.note(app, "x" * 100), "Noted #1")
        spec = app.registry.panels["loose_ends"]
        lines = spec.render(mock.Mock()).splitlines()
        self.assertEqual(lines[0], "LOOSE ENDS (1)")
        self.assertEqual(lines[1], "! " + "x" * 30)  # 32 columns, glyph included
        self.assertIn("Whenever you tell the user", app.system_prompt())

    def test_panel_settings(self):
        app = self.make({"title": "TODO", "rows": 2, "text_width": 10, "glyphs": {"noticed": "N"}})
        for n in range(3):
            self.note(app, f"item number {n}")
        lines = app.registry.panels["loose_ends"].render(mock.Mock()).splitlines()
        self.assertEqual(lines, ["TODO (3)", "N item num", "N item num"])

    def test_bad_glyph_is_a_warning_with_the_fix_and_the_default_glyph_stays(self):
        app, errors = self.load({"glyphs": {"urgent": "U", "noticed": "", "followup": "F"}})
        self.assertEqual(errors, [])
        self.assertEqual(len(self.warnings), 2, self.warnings)
        self.assertTrue(any("glyphs" in w and "unverified, noticed, followup" in w for w in self.warnings), self.warnings)
        self.assertTrue(any("glyphs.noticed" in w for w in self.warnings), self.warnings)
        self.note(app, "x", kind="noticed")
        self.note(app, "y", kind="followup")
        lines = app.registry.panels["loose_ends"].render(mock.Mock()).splitlines()
        self.assertEqual(lines[1:], ["F y", "! x"])  # the bad override left the default ``!`` and the good one applied

    def test_max_text_refuses_with_a_fix(self):
        app = self.make({"max_text": 10})
        self.assertIn("keep it under 10", self.note(app, "x" * 11))
        self.assertTrue(self.note(app, "x" * 10).endswith("Noted #1"))

    def test_texts_are_settings(self):
        app = self.make({"prompt_note": "", "tool_description": "my note tool", "dialog_title": "Backlog"})
        self.assertNotIn("Whenever you tell the user", app.system_prompt())
        self.assertEqual(app.registry.tools["note"].description, "my note tool")
        self.note(app, "thing")
        self.frontend.choose_many.return_value = []
        self.command(app, "/ends")
        self.assertEqual(self.frontend.choose_many.call_args.args[0], "Backlog")

    def test_disabled_registers_nothing(self):
        app = self.make({"enabled": False})
        self.assertNotIn("note", app.registry.tools)
        self.assertIsNone(app.find_command("ends"))


class CrystallizeTest(SafetyTest):
    EXT = "crystallize"

    def habits_app(self, settings=None, commands=("make lint",) * 3):
        app = self.make(settings)
        messages = []
        for n, command in enumerate(commands):
            messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"b{n}", "name": "bash", "input": {"command": command}}]})
        denied = {"type": "tool_use", "id": "d1", "name": "bash", "input": {"command": "rm -rf build"}}
        messages.append({"role": "assistant", "content": [denied]})
        messages.append(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "d1", "is_error": True, "content": "Error: " + _denied()}]}
        )
        messages.append({"role": "user", "content": "use the clean script instead of that please"})
        return app, messages

    def offered(self, app, messages):
        self.frontend.choose_many.return_value = None
        ctx = mock.Mock(cwd=str(self.project), tools={})
        ctx.read_session.return_value = messages
        ctx.sessions.return_value = ["s1"]
        ctx.ui = self.frontend
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app.find_command("crystallize").run("", ctx)
        return [o[1] for o in self.frontend.choose_many.call_args.args[1]], ctx, out.getvalue()

    def test_defaults_unchanged(self):
        app, messages = self.habits_app()
        labels, _, _ = self.offered(app, messages)
        self.assertEqual(labels[0], "tool  3x in 1 session(s)  make lint")
        self.assertEqual(labels[1], 'rule  Do not run `rm -rf build` without asking first.  (denied 1x) · then: "use the clean script instead of that please"')

    def test_min_runs_and_skip_commands(self):
        app, messages = self.habits_app({"min_runs": 4})
        labels, _, out = self.offered(app, messages)
        self.assertFalse(any(label.startswith("tool") for label in labels))
        app, messages = self.habits_app({"skip_commands": ["make"]})
        labels, _, _ = self.offered(app, messages)
        self.assertFalse(any(label.startswith("tool") for label in labels))

    def test_rule_wording_hint_and_limits(self):
        app, messages = self.habits_app({"bash_rule": "NEVER {detail}", "hint_chars": 3, "max_habits": 0})
        labels, _, _ = self.offered(app, messages)
        self.assertEqual(labels, ['rule  NEVER rm -rf build  (denied 1x) · then: "use"'])

    def test_rules_go_to_the_configured_file_and_heading(self):
        app, messages = self.habits_app({"rules_file": "docs/RULES.md", "rules_heading": "## House rules"})
        self.frontend.choose_many.return_value = ["rule:0"]
        ctx = mock.Mock(cwd=str(self.project), tools={})
        ctx.read_session.return_value = messages
        ctx.sessions.return_value = ["s1"]
        ctx.ui = self.frontend
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app.find_command("crystallize").run("all", ctx)
        text = (self.project / "docs" / "RULES.md").read_text()
        self.assertIn("## House rules\n- Do not run `rm -rf build` without asking first.\n", text)
        self.assertIn("rule(s) to docs/RULES.md", out.getvalue())
        self.assertFalse((self.project / "AGENTS.md").exists())

    def test_tool_name_pattern(self):
        app, messages = self.habits_app({"tool_name_pattern": "[a-z]+"})
        self.frontend.choose_many.return_value = ["tool:0"]
        self.frontend.ask_text.return_value = "lint_all"
        ctx = mock.Mock(cwd=str(self.project), tools={})
        ctx.read_session.return_value = messages
        ctx.sessions.return_value = ["s1"]
        ctx.ui = self.frontend
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app.find_command("crystallize").run("all", ctx)
        self.assertIn("not a valid tool name (use [a-z]+)", out.getvalue())

    def test_bad_settings_load_with_the_default_and_a_warning_with_the_fix(self):
        for settings, key, needle in (
            ({"tool_name_pattern": "["}, "tool_name_pattern", "regular expression"),
            ({"other_rule": "no {tool}"}, "other_rule", "{name}"),
            ({"rules_file": "../x.md"}, "rules_file", "inside the project"),
            ({"rules_heading": "a\nb"}, "rules_heading", "one non-empty line"),
            ({"skip_commands": ["ls", ""]}, "skip_commands", "non-empty command names"),
        ):
            with self.subTest(key=key):
                app, errors = self.load(settings)
                self.assertEqual(errors, [])
                (warning,) = self.warnings
                self.assertTrue(f"crystallize.{key}" in warning and needle in warning, warning)
                self.assertIn("the default is used", warning)
                self.assertIsNotNone(app.find_command("crystallize"))
        _, errors = self.load({"min_runs": 1})  # a range the schema checks stays a load error
        self.assertTrue(any("min_runs" in e for e in errors), errors)

    def test_disabled_registers_nothing(self):
        app = self.make({"enabled": False})
        self.assertIsNone(app.find_command("crystallize"))


def _denied():
    from minimal.api import USER_DENIED

    return USER_DENIED


if __name__ == "__main__":
    unittest.main()
