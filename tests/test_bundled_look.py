"""The marketplace items that change how minimal looks and reads: style_pack, callouts, help_overlay, term_title,
mermaid and model_picker (modes has its own file)."""

import contextlib
import importlib.util
import io
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from minimal.core.app import App
from minimal.core.host import Host
from minimal.core.session import Session
from minimal.extensions.registry import Registry
from minimal.ui.commands import register_builtin_commands
from minimal.ui.tui.app import MinimalTui
from minimal.ui.tui.composer import Composer
from tests.test_bundled_extensions import MARKETPLACE, BundledExtensionTest, RecordingProvider, text_reply


def load_module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LookTest(BundledExtensionTest):
    """Loads one marketplace item into a real ``App`` with ``settings`` under ``extensions.settings.<id>``."""

    def load(self, extension, settings=None, replies=()):
        """``(app, errors)``; ``extension`` is the path inside the checkout, its id is the file's stem."""
        if settings is not None:
            path = Path(extension)
            ext_id = f"{path.parent.name}/{path.stem}" if path.parent.name == "widgets" else path.stem
            (self.tmp / "settings.json").write_text(json.dumps({"extensions": {"settings": {ext_id: settings}}}))
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            trust_project=True,
            register_commands=register_builtin_commands,
            extra_extensions=[str(MARKETPLACE / extension)],
        )
        _, errors = app.reload_extensions()
        app.provider, app.model = RecordingProvider(replies), "m"
        return app, errors

    def ok(self, extension, settings=None, replies=()):
        app, errors = self.load(extension, settings, replies)
        self.assertEqual(errors, [])
        return app


class StylePackTest(LookTest, unittest.IsolatedAsyncioTestCase):
    PATH = "widgets/style_pack.py"

    def css(self, settings=None):
        app = self.ok(self.PATH, settings)
        return next(spec.css for spec in app.registry.css.values() if spec.name == "style_pack")

    def test_defaults_keep_the_old_look_and_use_only_theme_variables(self):
        css = self.css()
        self.assertIn(".user { border-left: thick $accent; }", css)
        self.assertIn(".tool.-pending { border-left: thick $warning; }", css)
        self.assertIn("Markdown.assistant { padding: 0 1 0 2; }", css)
        self.assertIn("#composer { border: round $border-muted; }", css)
        self.assertIn("#composer:focus-within { border: round $border-blue; }", css)
        self.assertNotIn("margin-top", css)
        self.assertNotIn("#", css.replace("#composer", ""))
        self.assertNotIn("-style-pack", css)

    def test_registers_no_widget_and_no_placement(self):
        app = self.ok(self.PATH)
        self.assertEqual(app.registry.widgets, {})

    def test_settings_change_the_rules(self):
        css = self.css(
            {
                "gutter_style": "tall",
                "composer_border": "heavy",
                "assistant_indent": 4,
                "density": "compact",
                "colors": {"user": "secondary", "composer_focus": "$accent", "pending": "git_dirty"},
            }
        )
        self.assertIn(".user { border-left: tall $secondary; }", css)
        self.assertIn(".tool.-pending { border-left: tall $status-git-dirty; }", css)
        self.assertIn("Markdown.assistant { padding: 0 1 0 4; }", css)
        self.assertIn("#composer { border: heavy $border-muted; }", css)
        self.assertIn("#composer:focus-within { border: heavy $accent; }", css)
        self.assertIn(".user, .tool, Markdown.assistant, .output, .error { margin-top: 0; }", css)

    def test_gutters_off_and_composer_untouched_or_borderless(self):
        css = self.css({"gutters": False, "composer_border": "default"})
        self.assertNotIn("border-left", css)
        self.assertNotIn("#composer", css)
        self.assertIn("#composer { border: none; }", self.css({"composer_border": "none"}))

    def test_disabled_registers_nothing(self):
        self.assertEqual(self.ok(self.PATH, {"enabled": False}).registry.css, {})

    def test_bad_values_are_load_errors_naming_the_key(self):
        for settings, needle in (
            ({"density": "airy"}, "density"),
            ({"gutter_style": "wavy"}, "gutter_style"),
            ({"composer_border": "dotted"}, "composer_border"),
            ({"assistant_indent": 40}, "assistant_indent"),
            ({"colors": {"user": "#ff0000"}}, "colors.user"),
            ({"colors": {"gutter": "accent"}}, "no part 'gutter'"),
        ):
            with self.subTest(settings):
                _, errors = self.load(self.PATH, settings)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(needle, errors[0])

    async def test_ui_applies_the_stylesheet_and_follows_the_settings(self):
        app = self.ok(self.PATH, {"composer_border": "heavy", "gutter_style": "tall"})
        app.provider_name = "fake"
        tui = MinimalTui(Host(app))
        async with tui.run_test() as pilot:
            await pilot.pause()
            self.assertEqual([location[0] for location in tui.ext_css], ["extension:widgets/style_pack:style_pack"])
            self.assertEqual(tui.query_one("#composer").styles.border_top[0], "heavy")
            tui.query_one("#prompt", Composer).set_text("hi")
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause(0.05)
                if not tui.busy:
                    break
            self.assertEqual(tui.query(".user").first().styles.border_left[0], "tall")

    async def test_every_gutter_and_border_style_parses(self):
        module = load_module(MARKETPLACE / self.PATH)
        for gutter in module.GUTTER_STYLES:
            for border in module.COMPOSER_BORDERS:
                with self.subTest(gutter=gutter, border=border):
                    app = self.ok(self.PATH, {"gutter_style": gutter, "composer_border": border})
                    app.provider_name = "fake"
                    tui = MinimalTui(Host(app))
                    async with tui.run_test() as pilot:
                        await pilot.pause()
                        self.assertEqual(len(tui.ext_css), 1)


class CalloutsTest(LookTest):
    PATH = "widgets/callouts.py"

    def render(self, app, alert="note", text="hello", width=40):
        return app.registry.renderers["blockquote:alert"].render(None, {"alert": alert, "text": text, "width": width})

    def test_defaults_draw_icon_title_and_bar_as_before(self):
        app = self.ok(self.PATH)
        self.assertEqual(self.render(app), [[("ⓘ Note", "bold accent")], [("▎ ", "accent"), "hello"]])
        self.assertEqual(self.render(app, "caution")[0], [("✘ Caution", "bold error")])
        self.assertIn("alert blockquotes", app.system_prompt())

    def test_icons_titles_styles_and_bar_are_settings(self):
        app = self.ok(
            self.PATH,
            {
                "icons": {"note": "i", "tip": ""},
                "titles": {"note": "FYI", "warning": ""},
                "styles": {"note": "$code", "tip": "#ff8800"},
                "bar": "| ",
            },
        )
        self.assertEqual(self.render(app), [[("i FYI", "bold $code")], [("| ", "$code"), "hello"]])
        self.assertEqual(self.render(app, "tip")[0], [("Tip", "bold #ff8800")])
        self.assertEqual(self.render(app, "warning")[0], [("⚠", "bold warning")])

    def test_no_title_and_no_icon_drops_the_heading_line(self):
        app = self.ok(self.PATH, {"icons": {"note": ""}, "titles": {"note": ""}})
        self.assertEqual(self.render(app), [[("▎ ", "accent"), "hello"]])

    def test_long_lines_wrap_to_the_width_and_wrap_can_be_turned_off(self):
        text = "word " * 20
        app = self.ok(self.PATH)
        rows = self.render(app, text=text, width=30)[1:]
        self.assertGreater(len(rows), 2)
        self.assertTrue(all(len(row[1]) <= 28 for row in rows))
        flat = self.ok(self.PATH, {"wrap": False})
        self.assertEqual(len(self.render(flat, text=text, width=30)), 2)

    def test_model_text_is_cleaned(self):
        app = self.ok(self.PATH)
        rows = self.render(app, text="a\x1b[31mred\x07\nb")
        self.assertEqual([row[1] for row in rows[1:]], ["a [31mred", "b"])

    def test_prompt_hint_off_and_disabled(self):
        self.assertNotIn("alert blockquotes", self.ok(self.PATH, {"prompt_hint": False}).system_prompt())
        app = self.ok(self.PATH, {"enabled": False})
        self.assertNotIn("blockquote:alert", app.registry.renderers)
        self.assertNotIn("alert blockquotes", app.system_prompt())

    def test_bad_settings_are_load_errors(self):
        for settings, needle in (
            ({"icons": {"danger": "!"}}, "no kind 'danger'"),
            ({"icons": {"note": 1}}, "icons.note"),
            ({"styles": {"note": "chartreuse"}}, "unknown style"),
        ):
            with self.subTest(settings):
                _, errors = self.load(self.PATH, settings)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(needle, errors[0])


class HelpOverlayTest(LookTest):
    PATH = "extensions/help_overlay.py"

    def setUp(self):
        super().setUp()
        self.module = load_module(MARKETPLACE / self.PATH)

    def run_help(self, app, line):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            app.run_command(line)
        return out.getvalue()

    def test_commands_sorted_aligned_and_keys_first(self):
        text = self.module.format_help({"zeta": "last", "alpha": "first one", "mid": ""}, [("Enter", "Send")])
        self.assertLess(text.index("Keys"), text.index("Commands"))
        rows = text[text.index("Commands") :].splitlines()[1:]
        self.assertEqual([row.split()[0] for row in rows], ["/alpha", "/mid", "/zeta"])
        self.assertEqual(rows[0].index("first"), rows[2].index("last"))
        self.assertEqual(rows[1], "  /mid")

    def test_control_characters_and_long_descriptions_are_neutralised(self):
        text = self.module.format_help({"x\x1b[31m": "evil\x1b]0;t\x07\nline2", "y": "w" * 500}, [("a\x1b", "b\x07")])
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertIn("evil ]0;t line2", text)
        self.assertTrue(all(len(line) < 120 for line in text.splitlines()))
        self.assertIn("…", text)

    def test_width_cuts_descriptions(self):
        text = self.module.format_help({"y": "w" * 100}, width=30)
        self.assertEqual(len(text.splitlines()[1].split()[1]), 30)

    def test_filter_drops_empty_sections_and_reports_no_match(self):
        commands = {"model": "pick a model", "exit": "quit"}
        text = self.module.format_help(commands, [("Enter", "Send")], query="MODEL")
        self.assertIn("/model", text)
        self.assertNotIn("/exit", text)
        self.assertNotIn("Keys", text)
        self.assertIn("Nothing matches 'zzz'", self.module.format_help(commands, query="zzz"))

    def test_keys_come_from_the_live_bindings_with_the_users_remapping(self):
        (self.tmp / "settings.json").write_text(json.dumps({"ui": {"keys": {"toggle_sidebar": "ctrl+o"}}}))
        app, errors = self.load(self.PATH)
        self.assertEqual(errors, [])
        text = self.run_help(app, "/keys")
        self.assertIn("Shift+Tab", text)
        self.assertIn("Ctrl+O", text)
        self.assertNotIn("Ctrl+B", text)
        self.assertIn("Ctrl+P", text)  # an extra key
        self.assertIn("/keys", text)
        self.assertIn("/exit", text)  # built-in commands are listed too
        self.assertEqual(text, self.run_help(app, "/shortcuts"))

    def test_extension_keybindings_are_listed_with_their_owner(self):
        rows = self.module.binding_rows(
            [
                SimpleNamespace(key="ctrl+o", action="stats:stats", description="show stats", source="stats"),
                SimpleNamespace(key="shift+tab", action="cycle_mode", description="", source="built-in"),
            ]
        )
        self.assertEqual(rows, [("Ctrl+O", "show stats [stats]"), ("Shift+Tab", "cycle_mode")])

    def test_settings_extra_keys_titles_hidden_commands_and_names(self):
        app = self.ok(
            self.PATH,
            {
                "command": "help2",
                "alias": "",
                "extra_keys": [["Ctrl+X", "my own"]],
                "keys_title": "Atajos",
                "commands_title": "Comandos",
                "hide_commands": ["/exit"],
                "width": 30,
            },
        )
        self.assertNotIn("keys", app.registry.commands)
        self.assertNotIn("shortcuts", app.registry.commands)
        text = self.run_help(app, "/help2")
        self.assertIn("Atajos", text)
        self.assertIn("Comandos", text)
        self.assertIn("Ctrl+X", text)
        self.assertNotIn("Ctrl+P", text)
        self.assertNotIn("/exit", text)
        self.assertTrue(all(len(line) < 70 for line in text.splitlines()))

    def test_sections_can_be_turned_off(self):
        app = self.ok(self.PATH, {"show_keys": False})
        self.assertNotIn("Keys", self.run_help(app, "/keys"))
        app = self.ok(self.PATH, {"show_commands": False})
        self.assertNotIn("Commands", self.run_help(app, "/keys"))

    def test_disabled_registers_nothing_and_bad_extra_keys_are_a_warning_with_the_default_keys(self):
        self.assertNotIn("keys", self.ok(self.PATH, {"enabled": False}).registry.commands)
        app, errors = self.load(self.PATH, {"extra_keys": [["only one"]]})
        self.assertEqual(errors, [])
        self.assertIn("extra_keys entries must be [key, text] pairs", app.extension_problems[0])
        self.assertIn("Ctrl+Q", self.run_help(app, "/keys"))
        _, errors = self.load(self.PATH, {"width": 5})
        self.assertIn("width", errors[0])


class TermTitleTest(LookTest, unittest.IsolatedAsyncioTestCase):
    PATH = "extensions/term_title.py"

    def setUp(self):
        super().setUp()
        self.module = load_module(MARKETPLACE / self.PATH)
        self.settings = {
            "template": "{prefix}: {project} [{state}]",
            "prefix": "minimal",
            "idle": "idle",
            "working": "working",
            "max_length": 100,
            "restore": "",
            "push_pop": True,
        }

    def build(self, cwd="/work/shop", state="idle", **values):
        values = {"prefix": "minimal", "project": Path(cwd).name or cwd, "state": state, **values}
        return self.module.build_title(self.settings["template"], values, self.settings["max_length"])

    def ctx(self, cwd="/w/p", branch="main", model="m1", mode=None):
        git = SimpleNamespace(branch=branch) if branch is not None else None
        return SimpleNamespace(cwd=cwd, model=model, provider_name="fake", mode=mode, git=lambda: git)

    def test_title_names_the_project_folder_and_state(self):
        self.assertEqual(self.build(), "minimal: shop [idle]")
        self.assertEqual(self.build("/", "working"), "minimal: / [working]")

    def test_control_characters_cannot_end_or_inject_a_sequence(self):
        title = self.build("/work/a\x1b]0;evil\x07b\nc\x9b", prefix="m\x07")
        self.assertEqual(title, "m: a ]0;evil b c [idle]")
        sequence = self.module.osc_title(title + "\x07\x1b\\x")
        self.assertEqual(sequence.count("\x1b"), 1)
        self.assertEqual(sequence.count("\x07"), 1)
        self.assertTrue(sequence.startswith("\x1b]0;") and sequence.endswith("\x07"))

    def test_long_project_names_are_cut_but_keep_the_state(self):
        title = self.build("/work/" + "p" * 300, "working")
        self.assertEqual(len(title), 100)
        self.assertTrue(title.endswith("…" + " [working]"))
        self.assertTrue(title.startswith("minimal: ppp"))

    def test_a_template_without_project_is_cut_at_the_end(self):
        title = self.module.build_title("{prefix} " + "x" * 50, {"prefix": "m"}, 20)
        self.assertEqual((len(title), title[-1]), (20, "…"))

    def test_custom_template_with_branch_model_mode_and_state_words(self):
        settings = {**self.settings, "template": "{project}@{branch} {model}/{mode} {state}", "working": "busy"}
        title = self.module.Title(settings, SimpleNamespace(write=lambda data: True))
        writes = []
        title.terminal = SimpleNamespace(write=lambda data: writes.append(data) or True)
        title.show(self.ctx(mode="plan"), "working")
        self.assertEqual(writes[-1], "\x1b]0;p@main m1/plan busy\x07")
        title.show(self.ctx(branch=None), "idle")  # no repository yet
        self.assertEqual(writes[-1], "\x1b]0;p@ m1/ idle\x07")

    def test_git_is_only_asked_when_the_template_uses_it(self):
        ctx = self.ctx()
        ctx.git = mock.Mock(side_effect=AssertionError("git() called"))
        title = self.module.Title(self.settings, SimpleNamespace(write=lambda data: True))
        title.show(ctx, "idle")

    def test_template_is_validated_at_load(self):
        for template in ("{nope}", "{project", "{0}"):
            with self.subTest(template):
                app, errors = self.load(self.PATH, {"template": template})
                self.assertEqual(errors, [])
                self.assertEqual(len(app.extension_problems), 1, app.extension_problems)
                self.assertIn("template", app.extension_problems[0])
        _, errors = self.load(self.PATH, {"max_length": 2})
        self.assertIn("max_length", errors[0])

    def test_title_writes_only_changes_and_restores_once(self):
        writes = []
        terminal = SimpleNamespace(write=lambda data: writes.append(data) or True)
        title = self.module.Title(self.settings, terminal)
        ctx = self.ctx()
        title.show(ctx, "idle")
        title.show(ctx, "idle")
        title.show(ctx, "working")
        title.show(ctx)  # a redraw keeps the state
        title.restore()
        title.restore()
        self.assertEqual(
            writes,
            [
                self.module.PUSH,
                "\x1b]0;minimal: p [idle]\x07",
                "\x1b]0;minimal: p [working]\x07",
                self.module.POP,
            ],
        )

    def test_push_pop_off_never_pushes_or_pops(self):
        writes = []
        terminal = SimpleNamespace(write=lambda data: writes.append(data) or True)
        title = self.module.Title({**self.settings, "push_pop": False}, terminal)
        title.show(self.ctx(), "idle")
        title.restore()
        self.assertEqual(writes, ["\x1b]0;minimal: p [idle]\x07"])
        writes.clear()
        title = self.module.Title({**self.settings, "push_pop": False, "restore": "shell"}, terminal)
        title.show(self.ctx(), "idle")
        title.restore()
        self.assertEqual(writes[-1], "\x1b]0;shell\x07")

    def test_restore_setting_names_the_title_to_leave_behind(self):
        writes = []
        terminal = SimpleNamespace(write=lambda data: writes.append(data) or True)
        title = self.module.Title({**self.settings, "restore": "my\x1b shell"}, terminal)
        title.show(self.ctx(), "idle")
        title.restore()
        self.assertEqual(writes[-1], "\x1b]0;my shell\x07")

    def test_unwritable_terminal_is_retried_and_nothing_is_popped(self):
        writes, available = [], [False]
        terminal = SimpleNamespace(write=lambda data: writes.append(data) if available[0] else False)
        title = self.module.Title(self.settings, terminal)
        title.show(self.ctx(), "idle")
        title.restore()
        self.assertEqual(writes, [])
        available[0] = True
        title.show(self.ctx(), "idle")  # the same title is sent now: the first attempt never reached a terminal
        self.assertEqual(writes[-1], "\x1b]0;minimal: p [idle]\x07")

    def test_terminal_prefers_the_running_ui_driver_over_stdout(self):
        sent = []
        app = SimpleNamespace(is_running=True, _driver=SimpleNamespace(write=sent.append))
        terminal = self.module.Terminal()
        terminal.app = app
        stdout = mock.MagicMock()
        stdout.isatty.return_value = True
        with mock.patch.object(sys, "__stdout__", stdout), mock.patch.object(sys, "stdout", stdout):
            self.assertTrue(terminal.write("a"))
        self.assertEqual(sent, ["a"])
        stdout.write.assert_not_called()

    def test_terminal_never_writes_to_a_captured_or_non_tty_stdout(self):
        real = mock.MagicMock()
        real.isatty.return_value = True
        terminal = self.module.Terminal()
        with mock.patch.object(sys, "__stdout__", real):
            with mock.patch.object(sys, "stdout", io.StringIO()):  # Textual's capture, while its UI runs
                self.assertFalse(terminal.write("x"))
            with mock.patch.object(sys, "stdout", real):
                self.assertTrue(terminal.write("y"))
            real.isatty.return_value = False  # piped output
            with mock.patch.object(sys, "stdout", real):
                self.assertFalse(terminal.write("z"))
        real.write.assert_called_once_with("y")

    def test_disabled_registers_nothing(self):
        app = self.ok(self.PATH, {"enabled": False})
        self.assertEqual(app.registry.widgets, {})

    async def test_ui_sends_working_while_a_turn_runs_idle_after_and_restores_on_exit(self):
        sent = []
        core = self.ok(self.PATH, {"template": "{prefix}: {project} {model} [{state}]"}, [text_reply("done")])
        core.provider_name = "fake"
        tui = MinimalTui(Host(core))
        real = mock.MagicMock()
        real.isatty.return_value = True
        with mock.patch("textual.drivers.headless_driver.HeadlessDriver.write", lambda driver, data: sent.append(data)):
            async with tui.run_test() as pilot:
                tui.query_one("#prompt", Composer).set_text("hi")
                await pilot.press("enter")
                for _ in range(100):
                    await pilot.pause(0.05)
                    if not tui.busy:
                        break
        sent = [data for data in sent if data.startswith("\x1b]0;") or data == self.module.PUSH]
        titles = [data[4:-1] for data in sent if data.startswith("\x1b]0;")]
        project = Path(core.cwd).name
        self.assertEqual(sent[0], self.module.PUSH)
        self.assertIn(f"minimal: {project} m [working]", titles)
        self.assertEqual(titles[-1], f"minimal: {project} m [idle]")
        self.assertLess(titles.index(f"minimal: {project} m [working]"), len(titles) - 1)
        with mock.patch.object(sys, "__stdout__", real), mock.patch.object(sys, "stdout", real):
            core.shutdown()  # the UI has exited: the saved title goes back through the real terminal
        real.write.assert_called_once_with(self.module.POP)


def fake_termaid(calls):
    """A stand-in ``termaid`` whose ``render`` records its calls; a source starting with ``bad`` raises."""

    def render(src, **options):
        calls.append((src, options))
        if src.startswith("bad"):
            raise ValueError("no such diagram")
        if src.startswith("blank"):
            return "  "
        return "\n".join(f"[{line}]" for line in src.splitlines())

    def fit_to_width(draw, result, target_width, **options):
        calls.append(("fit", target_width, options))
        return result

    package = types.ModuleType("termaid")
    package.render = render
    utils = types.ModuleType("termaid.utils")
    utils.fit_to_width = fit_to_width
    package.utils = utils
    return {"termaid": package, "termaid.utils": utils}


class MermaidTest(LookTest):
    PATH = "extensions/mermaid.py"

    def setUp(self):
        super().setUp()
        self.calls = []
        patcher = mock.patch.dict(sys.modules, fake_termaid(self.calls))
        patcher.start()
        self.addCleanup(patcher.stop)

    def draw(self, app, source, closed=True, width=80):
        block = {"kind": "fence:mermaid", "language": "mermaid", "source": source, "closed": closed, "width": width}
        return app.registry.renderers["fence:mermaid"].render(None, block)

    def test_draws_the_source_and_remembers_it_so_a_redraw_costs_nothing(self):
        app = self.ok(self.PATH)
        self.assertEqual(self.draw(app, "a\nb"), ["[a]", "[b]"])
        renders = len([c for c in self.calls if c[0] != "fit"])
        self.assertEqual(self.draw(app, "a\nb"), ["[a]", "[b]"])
        self.assertEqual(len([c for c in self.calls if c[0] != "fit"]), renders)
        self.draw(app, "a\nb", width=60)  # another width is another drawing
        self.assertGreater(len([c for c in self.calls if c[0] != "fit"]), renders)

    def test_the_cache_is_bounded_and_errors_are_cached_too(self):
        app = self.ok(self.PATH)
        for n in range(100):
            self.draw(app, f"n{n}")
        before = len(self.calls)
        self.draw(app, "n99")  # recent: remembered
        self.assertEqual(len(self.calls), before)
        self.draw(app, "n0")  # old: forgotten, drawn again
        self.assertGreater(len(self.calls), before)
        before = len(self.calls)
        self.draw(app, "bad one")
        self.draw(app, "bad one")
        self.assertEqual(len([c for c in self.calls[before:] if c[0] != "fit"]), 1)

    def test_a_diagram_that_fails_shows_its_source_and_the_error_once_closed(self):
        app = self.ok(self.PATH)
        rows = self.draw(app, "bad\nx")
        self.assertEqual(rows[:2], ["bad", "x"])
        self.assertIn("ValueError: no such diagram", rows[-1][0][0])
        self.assertIsNone(self.draw(app, "bad\nx", closed=False))
        self.assertIn("nothing to draw", self.draw(app, "blank")[-1][0][0])

    def test_a_huge_source_is_not_drawn_at_all(self):
        app = self.ok(self.PATH, {"max_source_chars": 200})
        rows = self.draw(app, "a\n" * 150)
        self.assertEqual(self.calls, [])
        self.assertIn("over max_source_chars", rows[-1][0][0])
        self.assertLessEqual(len(rows), 12 + 2)

    def test_a_streaming_diagram_over_the_stream_limit_waits_for_its_fence_to_close(self):
        app = self.ok(self.PATH, {"max_stream_chars": 100})
        source = "x\n" * 80
        self.assertIsNone(self.draw(app, source, closed=False))
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.draw(app, source, closed=True)), 80)

    def test_max_lines_cuts_tall_drawings(self):
        app = self.ok(self.PATH, {"max_lines": 3})
        rows = self.draw(app, "\n".join("abcdef"))
        self.assertEqual(rows[:3], ["[a]", "[b]", "[c]"])
        self.assertIn("3 more lines", rows[3][0][0])

    def test_ascii_and_termaid_options_reach_termaid(self):
        app = self.ok(self.PATH, {"ascii": True, "termaid": {"padding_x": 2, "arrow_style": "triangle"}})
        self.draw(app, "a")
        src, options = self.calls[0]
        self.assertEqual(options, {"padding_x": 2, "arrow_style": "triangle", "use_ascii": True})
        fit = next(c for c in self.calls if c[0] == "fit")
        self.assertEqual(fit[2]["padding_x"], 2)

    def test_bad_termaid_options_are_load_errors(self):
        for options, needle in (
            ({"colour": 1}, "no option 'colour'"),
            ({"padding_x": "wide"}, "padding_x"),
            ({"padding_x": -1}, "padding_x"),
            ({"rounded_edges": 1}, "rounded_edges"),
            ({"arrow_style": "fancy"}, "arrow_style"),
        ):
            with self.subTest(options):
                _, errors = self.load(self.PATH, {"termaid": options})
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(needle, errors[0])

    def test_prompt_hint_and_enabled_switches(self):
        self.assertIn("mermaid", self.ok(self.PATH).system_prompt())
        self.assertNotIn("```mermaid", self.ok(self.PATH, {"prompt_hint": False}).system_prompt())
        app = self.ok(self.PATH, {"enabled": False})
        self.assertNotIn("fence:mermaid", app.registry.renderers)

    def test_missing_termaid_is_the_install_hint_unless_disabled(self):
        with mock.patch.dict(sys.modules, {"termaid": None, "termaid.utils": None}):
            _, errors = self.load(self.PATH)
            self.assertEqual(len(errors), 1)
            self.assertIn("needs termaid", errors[0])
            app, errors = self.load(self.PATH, {"enabled": False})
            self.assertEqual(errors, [])

    def test_source_lines_shown_instead_of_a_drawing_are_cleaned(self):
        app = self.ok(self.PATH)
        rows = self.draw(app, "bad\x1b[31m\nx")
        self.assertEqual(rows[0], "bad [31m")


class ModelPickerTest(LookTest, unittest.IsolatedAsyncioTestCase):
    PATH = "widgets/model_picker.py"
    MODELS = ["claude-a", "gpt-4", "gpt-5", "llama-3", "zeta"]

    def setUp(self):
        super().setUp()
        self.module = load_module(MARKETPLACE / self.PATH)

    def ui(self, settings=None, models=None):
        core = self.ok(self.PATH, settings)
        core.provider_name = "fake"
        core.model = "gpt-4"
        core.available_models = lambda: (list(models or self.MODELS), None)
        return core, MinimalTui(Host(core))

    @staticmethod
    async def settle(pilot, tui):
        for _ in range(100):
            await pilot.pause(0.05)
            if not tui.busy:
                return

    async def open_picker(self, pilot, tui):
        tui.query_one("#prompt", Composer).set_text("/model")
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.05)
            if tui.query_one("#model-picker").has_class("-open"):
                return tui.query_one("#model-picker")
        self.fail("the picker did not open")

    def labels(self, picker):
        return [picker.get_option_at_index(i).prompt.plain for i in range(picker.option_count)]

    def test_arrange_pins_favourites_hides_and_sorts(self):
        hide = self.module.compile_hide(["^gpt"])
        self.assertEqual(
            self.module.arrange(self.MODELS, "gpt-4", ["zeta", "gpt-5", "missing"], hide, "provider"),
            ["zeta", "gpt-5", "claude-a", "gpt-4", "llama-3"],  # favourites stay; the current one is never hidden
        )
        self.assertEqual(self.module.arrange(["b", "A", "c", "b"], "c", [], [], "alpha"), ["A", "b", "c"])
        self.assertEqual(self.module.narrow(["Gpt-4", "x"], "GPT"), ["Gpt-4"])

    def test_settings_are_validated_at_load(self):
        for settings, needle in (
            ({"hide": ["("]}, "not a valid regex"),
            ({"hide": [3]}, "hide entries"),
            ({"area": "sidebar"}, "area"),
            ({"sort": "random"}, "sort"),
            ({"max_height": 1}, "max_height"),
        ):
            with self.subTest(settings):
                _, errors = self.load(self.PATH, settings)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(needle, errors[0])

    def test_area_order_and_disabled(self):
        app = self.ok(self.PATH, {"area": "top", "order": 5})
        spec = app.registry.widgets["model_picker"]
        self.assertEqual((spec.area, spec.order), ("top", 5))
        app = self.ok(self.PATH, {"enabled": False})
        self.assertEqual(app.registry.widgets, {})
        self.assertNotIn("model", app.registry.commands)

    def test_model_completion_offers_favourites_the_current_model_and_the_last_list(self):
        app = self.ok(self.PATH, {"favourites": ["zeta"], "hide": ["^llama"]})
        complete = app.registry.commands["model"].complete
        ctx = SimpleNamespace(model="gpt-4")
        self.assertEqual(complete("", ctx), ["zeta", "gpt-4"])
        self.assertEqual(complete("GPT", ctx), ["gpt-4"])
        self.assertEqual(complete("3", ctx), [])  # a number: the built-in picks by index

    async def test_pick_with_the_keys_marks_current_and_favourite(self):
        core, tui = self.ui({"favourites": ["zeta"]})
        async with tui.run_test() as pilot:
            picker = await self.open_picker(pilot, tui)
            self.assertEqual(
                self.labels(picker),
                ["  ★ zeta", "    claude-a", "●   gpt-4  (current)", "    gpt-5", "    llama-3"],
            )
            self.assertEqual(picker.highlighted, 2)
            await pilot.press("down", "enter")
            await self.settle(pilot, tui)
        self.assertEqual(core.model, "gpt-5")

    async def test_type_to_filter_then_enter_and_escape_steps(self):
        core, tui = self.ui()
        async with tui.run_test() as pilot:
            picker = await self.open_picker(pilot, tui)
            await pilot.press("l", "l")
            await pilot.pause()
            self.assertEqual([label.strip() for label in self.labels(picker)], ["llama-3"])
            self.assertIn("filter: ll", picker.border_title)
            await pilot.press("backspace", "backspace", "z")
            await pilot.pause()
            self.assertEqual([label.strip() for label in self.labels(picker)], ["zeta"])
            await pilot.press("escape")  # clears the filter first
            await pilot.pause()
            self.assertEqual(picker.option_count, 5)
            self.assertTrue(picker.has_class("-open"))
            await pilot.press("q", "q", "q")
            await pilot.pause()
            self.assertEqual(self.labels(picker), ["no model matches"])
            await pilot.press("escape", "escape")
            await self.settle(pilot, tui)
            self.assertFalse(picker.has_class("-open"))
        self.assertEqual(core.model, "gpt-4")

    async def test_filter_off_ignores_typing_and_markers_and_title_are_settings(self):
        core, tui = self.ui(
            {"filter": False, "current_marker": ">", "current_label": "", "title": "Pick {provider}", "hint": "", "max_height": 5}
        )
        async with tui.run_test() as pilot:
            picker = await self.open_picker(pilot, tui)
            await pilot.press("l")
            await pilot.pause()
            self.assertEqual(picker.option_count, 5)
            self.assertEqual(self.labels(picker)[1], "> gpt-4")
            self.assertEqual(picker.border_title, "Pick fake")
            self.assertEqual(picker.styles.max_height.value, 5)
            await pilot.press("escape")
            await self.settle(pilot, tui)

    async def test_hidden_models_are_not_listed_and_direct_set_still_works(self):
        core, tui = self.ui({"hide": ["^claude", "zeta"]})
        async with tui.run_test() as pilot:
            picker = await self.open_picker(pilot, tui)
            self.assertEqual([label.strip("● ").split()[0] for label in self.labels(picker)], ["gpt-4", "gpt-5", "llama-3"])
            await pilot.press("escape")
            await self.settle(pilot, tui)
            tui.query_one("#prompt", Composer).set_text("/model zeta")
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause(0.05)
                if core.model == "zeta":
                    break
        self.assertEqual(core.model, "zeta")


if __name__ == "__main__":
    unittest.main()
