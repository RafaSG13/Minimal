"""API 2.3 for customisable extensions: ``ctx.invalidate``, ``ctx.theme`` and ``theme_change``, ``$var``/hex styles,
titled and collapsible panels, ``register_css``, settings schemas and ``/ext settings``, ``ctx.context_window``,
``ctx.git`` and ``ctx.keybindings``."""

import json
import subprocess
import threading
import unittest
from unittest import mock

from textual.widgets import Static

from minimal.core import git as git_mod
from minimal.extensions import loader
from minimal.extensions.registry import Registry, styled_line
from minimal.providers import windows
from minimal.ui.tui.widgets import PanelView, Section, SectionHeading
from tests.test_customisation import CustomisationTest, TuiCustomisationTest


def text_of(widget: Static) -> str:
    return str(widget.render())


class StyleTest(unittest.TestCase):
    def test_theme_variables_and_hex_combine_with_modifiers_and_unknown_names_are_refused(self):
        self.assertEqual(styled_line([("a", "bold $code"), ("b", "#ff8800 underline"), "c"])[1], ("b", "#ff8800 underline"))
        for bad in ("$nonsense", "#ff88", "#gggggg", "sparkly"):
            with self.assertRaisesRegex(ValueError, "unknown style"):
                styled_line(("x", bad))


class ContextWindowTest(unittest.TestCase):
    def test_provider_table_matches_by_longest_prefix_and_unknown_models_have_none(self):
        self.assertIsNone(windows.context_window("anthropic", "no-such-model-family", None))
        self.assertEqual(windows.context_window("p", "big-1", {"big": 1000, "big-1": 2000}), 2000)
        self.assertEqual(windows.context_window("p", "big-2", {"big": 1000, "big-1": 2000}), 1000)


class GitTest(unittest.TestCase):
    def repo(self):
        import tempfile
        from pathlib import Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()

        def git(*args):
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
                cwd=root, check=True, capture_output=True,
            )

        git("init", "-q", "-b", "main")
        (root / "a.txt").write_text("a\n")
        (root / "b.txt").write_text("b\n")
        git("add", ".")
        git("commit", "-q", "-m", "init")
        return root, git

    @unittest.skipUnless(subprocess.run(["git", "--version"], capture_output=True).returncode == 0, "git missing")
    def test_read_git_counts_staged_unstaged_and_untracked_and_is_none_outside_a_repository(self):
        root, git = self.repo()
        self.assertEqual(
            (git_mod.read_git(str(root)).branch, git_mod.read_git(str(root)).dirty), ("main", False)
        )
        (root / "a.txt").write_text("changed\n")
        (root / "b.txt").write_text("changed\n")
        git("add", "b.txt")
        (root / "new.txt").write_text("n\n")
        info = git_mod.read_git(str(root))
        self.assertEqual((info.branch, info.dirty, info.staged, info.unstaged, info.untracked), ("main", True, 1, 1, 1))
        self.assertEqual({path for _, path in info.files}, {"a.txt", "b.txt", "new.txt"})
        import tempfile

        with tempfile.TemporaryDirectory() as outside:
            self.assertIsNone(git_mod.read_git(outside))

    def test_the_poller_reports_changes_only_and_pokes_poll_again(self):
        results = [git_mod.GitInfo(branch="a"), git_mod.GitInfo(branch="a"), git_mod.GitInfo(branch="b")]
        seen: list = []
        polled = threading.Semaphore(0)
        changed = threading.Semaphore(0)

        def read(cwd):
            polled.release()
            return results.pop(0) if results else git_mod.GitInfo(branch="b")

        def on_change(info):
            seen.append(info.branch)
            changed.release()

        poller = git_mod.GitPoller(".", 3600, on_change, read)
        self.addCleanup(poller.stop)
        poller.start()
        self.assertTrue(changed.acquire(timeout=5))
        self.assertTrue(polled.acquire(timeout=5))
        poller.poke()
        self.assertTrue(polled.acquire(timeout=5))  # the second "a": no change reported
        poller.poke()
        self.assertTrue(changed.acquire(timeout=5))
        self.assertEqual(seen, ["a", "b"])  # the second "a" was no change
        self.assertEqual(poller.info.branch, "b")


class SettingsSchemaTest(CustomisationTest):
    EXT = """
        def register(minimal):
            minimal.declare_settings(
                {"mode": "fast", "limit": 5, "ratio": 0.5, "on": True},
                {
                    "mode": {"description": "How fast", "enum": ["fast", "slow"]},
                    "limit": {"description": "How many", "min": 1, "max": 10},
                },
            )
        """

    def test_values_outside_the_schema_are_load_errors_that_keep_the_default(self):
        self.write_ext("tuned", self.EXT)
        self.write_settings({"extensions": {"settings": {"tuned": {"mode": "medium", "limit": 50, "ratio": 2}}}})
        registry = Registry()
        _, errors = loader.load_extensions(registry, str(self.cwd), self.settings())
        joined = "\n".join(errors)
        self.assertIn("extensions.settings.tuned.mode", joined)
        self.assertIn("extensions.settings.tuned.limit", joined)
        spec = registry.declared_settings["tuned"]
        self.assertEqual((spec.values["mode"], spec.values["limit"], spec.values["ratio"]), ("fast", 5, 2))

    def test_a_malformed_schema_is_refused(self):
        for schema in ({"nope": {}}, {"mode": {"enum": ["a"]}}, {"limit": {"min": "x"}}, {"on": {"min": 1}}):
            with self.subTest(schema=schema):
                self.write_ext(
                    "bad", f'def register(minimal):\n    minimal.declare_settings({{"mode": "fast", "limit": 5, "on": True}}, {schema!r})\n'
                )
                _, _, errors = self.load()
                self.assertTrue(any("bad" in e for e in errors), errors)


class ApiTuiTest(TuiCustomisationTest):
    async def test_invalidate_from_a_thread_redraws_a_panel_without_an_interval(self):
        self.write_ext(
            "ticker",
            """
            import os

            def register(minimal):
                def render(ctx):
                    path = os.environ["MARKS"]
                    return "n=" + (open(path).read().strip() if os.path.exists(path) else "0")

                minimal.register_panel("tick", render)
            """,
        )
        app = self.start()
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            panel = app.query_one(PanelView)
            self.assertEqual(text_of(panel), "n=0")
            self.marks.write_text("7")
            threading.Thread(target=lambda: [app.host.ctx.invalidate() for _ in range(5)]).start()
            await self.wait_until(pilot, lambda: text_of(panel) == "n=7")

    async def test_theme_info_follows_the_theme_and_theme_change_fires(self):
        self.write_ext(
            "ocean",
            """
            import os

            def register(minimal):
                minimal.register_theme("ocean", dark=True, background="#0b1622", foreground="#d0e4f5", accent="#4fc3f7")

                @minimal.on("theme_change")
                def changed(event):
                    with open(os.environ["MARKS"], "a") as f:
                        f.write(f"{event['name']} {event['dark']} {event['ctx'].theme.colors['accent']}\\n")
            """,
        )
        app = self.start()
        async with app.run_test() as pilot:
            info = app.host.ctx.theme
            self.assertEqual(info.name, app.theme)
            self.assertRegex(info.colors["accent"], r"^#[0-9a-f]{6}$")
            for key in config_theme_colors():
                self.assertIn(key, info.colors)
            with self.assertRaises(TypeError):
                info.colors["accent"] = "#000000"  # type: ignore[index]
            await self.run_line(pilot, "/theme ocean")
            await self.wait_until(pilot, lambda: self.marks.exists())
            self.assertEqual(self.marks.read_text().splitlines()[-1], "ocean True #4fc3f7")
            self.assertEqual(app.host.ctx.theme.colors["background"], "#0b1622")

    async def test_a_titled_collapsible_panel_collapses_and_the_user_can_override_title_and_state(self):
        self.write_ext(
            "boxed",
            """
            def register(minimal):
                minimal.register_panel("box", lambda ctx: ["one", "two"], title="BOX", collapsible=True)
                minimal.register_panel("plain", lambda ctx: "x")
            """,
        )
        app = self.start()
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            box = next(p for p in app.query(PanelView) if p.spec.name == "box")
            self.assertEqual(text_of(box).splitlines()[0].strip().lstrip("▾▸ ").strip(), "BOX")
            self.assertIn("two", text_of(box))
            box.action_toggle_collapse()
            await pilot.pause()
            self.assertNotIn("two", text_of(box))
            self.assertIn("BOX", text_of(box))
            await self.run_line(pilot, "/ui title box Heading")
            await self.run_line(pilot, "/ui collapse box")
            box = next(p for p in app.query(PanelView) if p.spec.name == "box")
            self.assertEqual((box.heading, box.collapsed), ("Heading", True))
            saved = json.loads((self.home / "settings.json").read_text())
            self.assertEqual(saved["ui"]["widgets"]["box"], {"title": "Heading", "collapsed": True})
            await self.run_line(pilot, "/ui expand box")
            await self.run_line(pilot, "/ui title box ''")
            box = next(p for p in app.query(PanelView) if p.spec.name == "box")
            self.assertEqual((box.heading, box.collapsed), ("", False))
            await self.run_line(pilot, "/ui title nothing_here Heading")
            self.assertIn("nothing_here", self.errors(app))

    async def test_a_titled_widget_sits_under_a_heading_that_collapses_it(self):
        self.write_ext(
            "wid",
            """
            from textual.widgets import Static

            def register(minimal):
                minimal.register_widget("w", lambda ctx: Static("inner", id="inner"), title="W", collapsible=True)
            """,
        )
        app = self.start()
        async with app.run_test() as pilot:
            section = app.query_one(Section)
            self.assertTrue(app.query_one("#inner").display)
            app.query_one(SectionHeading).action_toggle_section()
            await pilot.pause()
            self.assertFalse(section.query_one("#inner").display)

    async def test_register_css_applies_replaces_leaves_on_reload_and_reports_bad_css_once(self):
        self.write_ext(
            "skin",
            """
            def register(minimal):
                minimal.register_css("a", "#transcript { background: #123456; }")
                minimal.register_css("b", "#statusline { background: #654321 }")
                minimal.register_css("a", "#transcript { background: #abcdef; }")
                minimal.register_css("broken", "#statusline { colour: ; ;; }}{")
            """,
        )
        app = self.start()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one("#transcript").styles.background.hex, "#ABCDEF")
            self.assertEqual(app.query_one("#statusline").styles.background.hex, "#654321")
            self.assertEqual(self.errors(app).count("skin:broken: stylesheet was not applied"), 1)
            (self.global_ext / "skin.py").unlink()
            await self.reload(pilot)
            await pilot.pause()
            self.assertNotEqual(app.query_one("#transcript").styles.background.hex, "#ABCDEF")
            self.assertNotEqual(app.query_one("#statusline").styles.background.hex, "#654321")

    async def test_the_documented_selectors_exist(self):
        app = self.start()
        async with app.run_test() as pilot:
            for selector in (
                "#transcript", "#top", "#bottom", "#left", "#sidebar", "#footer", "#composer", "#prompt",
                "#prompt-glyph", "#statusline", "#working", "Header", "Header .logo, Header .wordmark",
            ):
                self.assertTrue(app.query(selector), selector)
            app._add(Static("u", classes="user"))
            app._add(Static("o", classes="output"))
            app.show_error("e")
            await pilot.pause()
            for selector in (".user", ".output", ".error"):
                self.assertTrue(app.query(selector), selector)
            self.assertEqual(config_css_selector_doc_missing(), [])

    async def test_ctx_keybindings_lists_builtin_and_extension_keys_as_remapped(self):
        self.write_ext("marker", self.MARKER)
        self.write_settings({"ui": {"keys": {"toggle_sidebar": "ctrl+t"}}})
        app = self.start()
        async with app.run_test():
            keys = {k.action: k for k in app.host.ctx.keybindings()}
            self.assertEqual(keys["toggle_sidebar"].key, "ctrl+t")
            self.assertEqual(keys["toggle_sidebar"].source, "built-in")
            ext_key = next(k for k in keys.values() if k.key == "ctrl+o")
            self.assertEqual((ext_key.description, ext_key.source), ("Mark the spot", "marker"))

    async def test_ctx_git_returns_the_cached_state_shares_one_poller_and_invalidates_on_change(self):
        branch = ["a"]
        threads: set[str] = set()

        def read(cwd):
            threads.add(threading.current_thread().name)
            return git_mod.GitInfo(branch=branch[0])

        app = self.start()
        with mock.patch.object(git_mod, "read_git", read), mock.patch.object(self.core, "invalidate") as invalidate:
            async with app.run_test() as pilot:
                ctx = app.host.ctx
                ctx.git()
                await self.wait_until(pilot, lambda: ctx.git() is not None and ctx.git().branch == "a")
                self.assertTrue(invalidate.called)
                branch[0] = "b"
                self.core.poke_git()
                await self.wait_until(pilot, lambda: ctx.git().branch == "b")
                self.assertEqual(threads, {"minimal-git"})  # the status line and ctx.git() share one poller
                self.core.stop_git()


class ExtSettingsCommandTest(TuiCustomisationTest):
    EXT = SettingsSchemaTest.EXT

    async def test_list_show_set_validate_and_reset_round_trip(self):
        self.write_ext("tuned", self.EXT)
        app = self.start()
        async with app.run_test() as pilot:
            await self.run_line(pilot, "/ext settings")
            listing = self.output(app)
            self.assertIn("tuned", listing)
            self.assertIn("How fast", listing)
            await self.run_line(pilot, "/ext settings tuned limit 8")
            saved = json.loads((self.home / "settings.json").read_text())
            self.assertEqual(saved["extensions"]["settings"]["tuned"], {"limit": 8})
            await self.wait_until(pilot, lambda: self.core.registry.declared_settings["tuned"].values["limit"] == 8)
            await self.run_line(pilot, "/ext settings tuned limit 99")
            self.assertIn("limit", self.errors(app))
            await self.run_line(pilot, "/ext settings tuned mode medium")
            await self.run_line(pilot, "/ext settings tuned nothing 1")
            self.assertEqual(
                json.loads((self.home / "settings.json").read_text())["extensions"]["settings"]["tuned"], {"limit": 8}
            )
            await self.run_line(pilot, "/ext settings tuned mode slow")
            await self.run_line(pilot, "/ext settings tuned limit reset")
            saved = json.loads((self.home / "settings.json").read_text())
            self.assertEqual(saved["extensions"]["settings"]["tuned"], {"mode": "slow"})
            self.assertEqual(self.core.registry.declared_settings["tuned"].values["limit"], 5)


def config_theme_colors():
    from minimal.extensions.registry import THEME_COLORS

    return THEME_COLORS


def config_css_selector_doc_missing() -> list[str]:
    """Selectors the docs promise that the extensions guide does not mention."""
    from pathlib import Path

    doc = (Path(__file__).resolve().parent.parent / "docs" / "extensions.md").read_text()
    return [s for s in ("#transcript", "#sidebar", "#composer", "#statusline", ".user", "Markdown.assistant") if s not in doc]


if __name__ == "__main__":
    unittest.main()
