"""The marketplace's logo, git panel, status line kit, context gauge and token sparkline: settings, validation, caching,
threads and the real terminal UI."""

import importlib.util
import json
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from textual.widgets import Static

from minimal import config
from minimal.api import GitInfo
from minimal.core.app import App
from minimal.core.host import Host
from minimal.core.session import Session
from minimal.extensions.loader import load_extensions
from minimal.extensions.registry import Registry
from minimal.providers.base import Usage
from minimal.ui.tui.app import MinimalTui
from minimal.ui.tui.composer import Composer
from minimal.ui.tui.widgets import PanelView
from tests.test_bundled_extensions import MARKETPLACE, BundledExtensionTest, RecordingProvider, text_reply

WIDGETS = MARKETPLACE / "widgets"


def load_module(name):
    path = WIDGETS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"kit_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def user_settings(ext_id, values):
    """Settings whose ``extensions.settings.<ext_id>`` is ``values``."""
    layer = config.Layer("global", Path("settings.json"), {"extensions.settings": {ext_id: values}})
    return config.Settings(layers=(layer,))


def fake_extension(module, **overrides):
    """A stand-in for ``minimal``: ``declare_settings`` merges ``overrides`` over the defaults; calls are recorded."""
    calls = SimpleNamespace(status=[], panels=[], widgets=[], logos=[], disposers=[], handlers={}, schema=None)

    def declare(defaults, schema=None):
        calls.schema = schema
        return {**defaults, **overrides}

    fake = SimpleNamespace(
        declare_settings=declare,
        on_dispose=calls.disposers.append,
        on=lambda event, handler=None: calls.handlers.setdefault(event, []).append(handler),
        register_status=lambda name, render, side="right", order=0: calls.status.append((name, render, side, order)),
        register_panel=lambda name, render, area="sidebar", live=None, interval=0, **kw: calls.panels.append(
            SimpleNamespace(name=name, render=render, area=area, interval=interval, **kw)
        ),
        register_widget=lambda name, create, area="sidebar", **kw: calls.widgets.append((name, create, area, kw)),
        register_logo=lambda art, colors, align="center": calls.logos.append((art, colors, align)),
    )
    module.register(fake)
    return calls


def rows(lines):
    """Each line of styled pieces as its text."""
    return ["".join(p if isinstance(p, str) else p[0] for p in line) for line in lines]


class KitTest(BundledExtensionTest):
    def load(self, name, settings=None):
        registry = Registry()
        _, errors = load_extensions(
            registry, str(self.tmp), settings or config.Settings(), extra=[WIDGETS / f"{name}.py"]
        )
        return registry, errors

    def write_settings(self, ext_id, values):
        (self.tmp / "settings.json").write_text(json.dumps({"extensions": {"settings": {ext_id: values}}}))

    def init_repo(self, files=()):
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.tmp)], check=True)
        for name, text in files:
            (self.tmp / name).write_text(text)


class LogoTest(KitTest):
    def setUp(self):
        super().setUp()
        self.module = load_module("logo")

    def legacy_art(self):
        """The art the two old items built: the 1.3 algorithm with its constants."""
        m = self.module.M
        font = {
            "m": ["#####", "#.#.#", "#.#.#", "#.#.#", "#.#.#"],
            "i": ["#", ".", "#", "#", "#"],
            "n": ["####", "#..#", "#..#", "#..#", "#..#"],
            "a": ["####", "...#", "####", "#..#", "####"],
            "l": ["#.", "#.", "#.", "#.", "##"],
        }
        word = [".".join(font[c][r] for c in "minimal") for r in range(5)]
        height, width = len(m), len(m[0])
        canvas = [["."] * (width + 1 + 3 + len(word[0])) for _ in range(height + 1)]
        for y in range(height):
            for x in range(width):
                if m[y][x] == "#":
                    canvas[y + 1][x + 1] = "s"
        for y in range(height):
            for x in range(width):
                if m[y][x] == "#":
                    canvas[y][x] = "0123456789"[(x + y) * 7 // (width + height - 1)]
        top = (height + 1 - 5) // 2 + 1
        for r, line in enumerate(word):
            for x, pixel in enumerate(line):
                if pixel == "#":
                    canvas[top + r][width + 4 + x] = "w"
        return ["".join(r) for r in canvas]

    def test_defaults_draw_exactly_the_old_logo(self):
        registry, errors = self.load("logo")
        self.assertEqual(errors, [])
        self.assertEqual(registry.logo.art, self.legacy_art())
        self.assertEqual(registry.logo.align, "center")
        self.assertEqual(registry.logo.colors["s"], "#3a2a5e")
        self.assertEqual(registry.logo.colors["w"], "$foreground")
        self.assertEqual(registry.logo.colors["0"], "#f25fc4")
        self.assertEqual(registry.logo.colors["6"], "#3bb7ec")
        self.assertEqual(registry.widgets, {})  # not animated: no widget

    def test_every_glyph_is_five_rows_of_one_width(self):
        for ch, glyph in self.module.FONT.items():
            with self.subTest(glyph=ch):
                self.assertEqual(len(glyph), 5)
                self.assertEqual(len({len(row) for row in glyph}), 1)
        self.assertTrue(set("abcdefghijklmnopqrstuvwxyz0123456789-_ ") <= set(self.module.FONT))

    def test_settings_choose_gradient_shadow_text_colour_and_align(self):
        self.write_settings(
            "widgets/logo",
            {
                "gradient": ["$accent", "#112233"],
                "shadow": "",
                "text": "Hi 5",
                "text_color": "#ffffff",
                "align": "left",
            },
        )
        registry, errors = self.load(
            "logo",
            user_settings(
                "widgets/logo",
                {
                    "gradient": ["$accent", "#112233"],
                    "shadow": "",
                    "text": "Hi 5",
                    "text_color": "#ffffff",
                    "align": "left",
                },
            ),
        )
        self.assertEqual(errors, [])
        logo = registry.logo
        self.assertEqual(logo.align, "left")
        self.assertEqual(logo.colors, {"0": "$accent", "1": "#112233", "w": "#ffffff"})  # no shadow key
        self.assertNotIn("s", "".join(logo.art))
        self.assertEqual(len(logo.art[0]), len(self.module.M[0]) + 1 + 3 + len(self.module.wordmark("hi 5")[0]))

    def test_empty_text_draws_only_the_m_and_disabled_registers_nothing(self):
        registry, errors = self.load("logo", user_settings("widgets/logo", {"text": ""}))
        self.assertEqual(errors, [])
        self.assertEqual(len(registry.logo.art[0]), len(self.module.M[0]) + 1)
        self.assertNotIn("w", "".join(registry.logo.art))
        registry, errors = self.load("logo", user_settings("widgets/logo", {"enabled": False, "text": "é"}))
        self.assertEqual((errors, registry.logo), ([], None))

    def test_bad_settings_are_load_errors_that_name_the_fix(self):
        cases = {
            "align": ({"align": "middle"}, "extensions.settings.widgets/logo.align"),
            "fps": ({"fps": 0}, "extensions.settings.widgets/logo.fps"),
            "glyph": ({"text": "héllo"}, "pixel font"),
            "length": ({"text": "x" * 17}, "at most 16"),
            "colour": ({"gradient": ["red"]}, "not a #rrggbb colour"),
            "shadow": ({"shadow": "blue"}, "'shadow'"),
            "none": ({"gradient": []}, "1 to 30"),
            "many": ({"gradient": ["#000000"] * 31}, "1 to 30"),
        }
        for label, (values, expected) in cases.items():
            with self.subTest(label):
                registry, errors = self.load("logo", user_settings("widgets/logo", values))
                self.assertTrue(any(expected in e for e in errors), errors)
        registry, errors = self.load("logo", user_settings("widgets/logo", {"align": "middle"}))
        self.assertEqual(registry.declared_settings["widgets/logo"].values["align"], "center")  # the default stays

    def test_every_setting_has_a_description(self):
        calls = fake_extension(self.module)
        self.assertEqual(set(calls.schema), set(self.module.DEFAULTS))
        self.assertTrue(all(entry["description"] for entry in calls.schema.values()))

    def test_animation_registers_a_widget_and_a_cycle_that_loops(self):
        registry, errors = self.load(
            "logo", user_settings("widgets/logo", {"animate": True, "gradient": ["#000001", "#000002", "#000003"]})
        )
        self.assertEqual(errors, [])
        self.assertEqual(list(registry.widgets), ["logo"])
        self.assertEqual(registry.widgets["logo"].area, "top")
        self.assertEqual(len(registry.logo.colors) - 2, 4)  # there and back: 1 2 3 2

    def test_player_frames_are_drawn_once_and_pause_while_a_turn_runs(self):
        settings = {**self.module.DEFAULTS, "animate": True}
        gradient = self.module.cycle_of(settings["gradient"])
        art, _ = self.module.build_art(settings, gradient)
        player = self.module.Player(settings, art, gradient)
        first = player.step()
        again = [player.step() for _ in range(len(gradient) - 1)]
        self.assertIs(again[-1], player.frame(0))  # back at the start: the frame is the one drawn before
        self.assertIsNot(first, again[-1])
        self.assertEqual(len(player.frames), len(gradient))
        player.busy = True
        phase = player.phase
        self.assertIsNone(player.step())
        self.assertEqual(player.phase, phase)
        player.pause = False
        self.assertIsNotNone(player.step())
        player.stopped = True
        self.assertIsNone(player.step())

    def test_main_turns_pause_the_animation_and_dispose_stops_it(self):
        calls = fake_extension(self.module, animate=True)
        self.assertEqual(len(calls.disposers), 1)
        calls.handlers["agent_start"][0]({"agent_id": "sub", "parent_id": "main"})
        calls.handlers["agent_start"][0]({"agent_id": "main"})
        # the widget factory closes over the player: build it without a UI to read its state
        ((name, create, area, _),) = calls.widgets
        self.assertEqual((name, area), ("logo", "top"))
        calls.disposers[0]()

    async def run_ui(self, settings, body):
        (self.tmp / "settings.json").write_text(json.dumps({"extensions": {"settings": {"widgets/logo": settings}}}))
        app = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            trust_project=True,
            extra_extensions=[str(WIDGETS / "logo.py")],
        )
        _, errors = app.reload_extensions()
        self.assertEqual(errors, [])
        app.provider, app.model = RecordingProvider([text_reply("ok")]), "m"
        tui = MinimalTui(Host(app))
        async with tui.run_test() as pilot:
            await body(tui, pilot, app)

    async def test_ui_animation_recolours_the_header_logo_and_stops_on_dispose(self):
        async def body(tui, pilot, app):
            await pilot.pause(0.1)
            logo = tui.query_one("Header .logo", Static)
            seen = {repr(logo.render().spans)}
            for _ in range(20):
                await pilot.pause(0.05)
                seen.add(repr(logo.render().spans))
            self.assertGreater(len(seen), 2)
            app.registry.dispose()
            await pilot.pause(0.2)
            frozen = repr(logo.render().spans)
            await pilot.pause(0.3)
            self.assertEqual(repr(logo.render().spans), frozen)

        await self.run_ui({"animate": True, "fps": 30}, body)

    async def test_ui_static_logo_never_changes(self):
        async def body(tui, pilot, app):
            await pilot.pause(0.1)
            logo = tui.query_one("Header .logo", Static)
            before = repr(logo.render().spans)
            await pilot.pause(0.4)
            self.assertEqual(repr(logo.render().spans), before)
            self.assertEqual(len(tui.query("Shimmer")), 0)

        await self.run_ui({}, body)


class GitPanelTest(KitTest):
    def setUp(self):
        super().setUp()
        self.module = load_module("git_panel")
        self.settings = dict(self.module.DEFAULTS)
        self.glyphs, self.styles = dict(self.module.GLYPHS), dict(self.module.STYLES)

    def lines(self, info, stats=None, cwd="/work/shop", **overrides):
        settings = {**self.settings, **overrides}
        problems, glyphs, styles = self.module.check_settings(settings)
        self.assertEqual(problems, [])
        return self.module.build_lines(info, cwd, stats or {}, settings, glyphs, styles)

    def info(self, **kw):
        base = {"branch": "main", "dirty": True, "ahead": 2, "behind": 0, "staged": 1, "unstaged": 2, "untracked": 1}
        base["files"] = (("M ", "a.py"), (" M", "src/very/long/path/to/some/module_name.py"), ("??", "new.txt"))
        return GitInfo(**{**base, **kw})

    def test_default_panel_is_the_old_git_panel_with_the_branch_row_first(self):
        text = rows(self.lines(self.info(), {"a.py": (3, 1), "src/very/long/path/to/some/module_name.py": None}))
        self.assertEqual(text[0], f"{self.glyphs['branch']} main * ↑2 ↓0")
        self.assertEqual(text[1], "staged 1 · unstaged 2 · untracked 1")
        self.assertEqual(text[2], "a.py".ljust(18) + " +3 −1 " + "████" + "█")  # 3:1 of 5 cells rounds to 4+1
        self.assertEqual(text[3], "…" + "some/module_name.py"[-17:] + " bin")
        self.assertEqual(len(text), 4)  # untracked files are not listed by default

    def test_title_collapsible_area_order_and_no_interval_are_registered(self):
        calls = fake_extension(self.module, area="left", order=5, title="Changes")
        (panel,) = calls.panels
        self.assertEqual(
            (panel.name, panel.area, panel.order, panel.title, panel.collapsible, panel.interval),
            ("git_panel", "left", 5, "Changes", True, 0),
        )
        self.assertIsNone(fake_extension(self.module, title="").panels[0].title)
        self.assertEqual(fake_extension(self.module, enabled=False).panels, [])

    def test_each_part_can_be_hidden_or_changed(self):
        info = self.info()
        self.assertEqual(rows(self.lines(info, show_project=True))[0], "shop")
        no_branch = rows(self.lines(info, show_branch=False, show_files=False))
        self.assertEqual(no_branch, ["staged 1 · unstaged 2 · untracked 1"])
        self.assertTrue(rows(self.lines(info, show_dirty=False))[0].endswith("main ↑2 ↓0"))
        self.assertTrue(rows(self.lines(info, arrows="never"))[0].endswith("main *"))
        quiet = self.info(ahead=0, behind=3)
        self.assertTrue(rows(self.lines(quiet, arrows="changes"))[0].endswith("↓3"))
        self.assertNotIn("↑", rows(self.lines(quiet, arrows="changes"))[0])
        self.assertEqual(len(self.lines(info, rows=1)), 3)
        self.assertEqual(len(self.lines(info, show_counts=False)), 3)

    def test_status_letters_untracked_files_and_widths(self):
        text = rows(
            self.lines(
                self.info(),
                show_status=True,
                list_untracked=True,
                name_width=10,
                bar_width=0,
                show_branch=False,
                show_counts=False,
            )
        )
        self.assertEqual([t[:2] for t in text], ["M ", "M ", "? "])
        self.assertTrue(text[1].startswith("M …"))
        self.assertEqual(len(text[1].rstrip()), 2 + 10)
        deleted = self.info(files=(("D ", "gone"), ("UU", "clash"), ("A ", "new")))
        styles = [line[0][1] for line in self.lines(deleted, show_status=True, show_branch=False, show_counts=False)]
        self.assertEqual(styles, ["error", "bold error", "success"])

    def test_glyphs_and_styles_override_and_unknown_ones_are_refused(self):
        lines = self.lines(
            self.info(),
            {"a.py": (1, 0)},
            glyphs={"branch": "@", "ahead": "+", "dirty": "!", "added": "A"},
            styles={"branch": "bold $accent", "dirty": "#ff0000"},
            show_counts=False,
        )
        self.assertEqual(rows(lines)[0], "@ main ! +2 ↓0")
        self.assertEqual((lines[0][0][1], lines[0][1][1]), ("bold $accent", "#ff0000"))
        self.assertIn("A1", rows(lines)[1])
        for bad, expected in (
            ({"glyphs": {"nope": "x"}}, "no entry 'nope'"),
            ({"glyphs": {"ahead": 1}}, "must be text"),
            ({"styles": {"branch": "sparkly"}}, "styles.branch"),
            ({"show_project": False, "show_branch": False, "show_counts": False, "show_files": False}, "empty"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, f"git_panel settings:.*{expected}"):
                    fake_extension(self.module, **bad)

    def test_text_from_git_is_cleaned_and_clipped(self):
        info = self.info(branch="fe\x1b[31mat/" + "b" * 80, files=(("M ", "we\x07ird\nname.py"),))
        text = rows(self.lines(info, {"we ird name.py": (1, 1)}, cwd="/w/pro\x1bj", show_project=True, width=20))
        self.assertTrue(all("\x1b" not in t and "\x07" not in t for t in text), text)
        self.assertEqual(text[0], "pro j")
        self.assertLessEqual(len(text[1]), 20 + 4)
        self.assertIn("we ird name.py", text[3])

    def test_detached_head_and_hidden_outside_a_repository(self):
        self.assertIn("detached", rows(self.lines(self.info(branch=None)))[0])
        calls = fake_extension(self.module)
        ctx = SimpleNamespace(git=lambda: None, cwd="/x")
        self.assertIsNone(calls.panels[0].render(ctx))

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_read_numstat_reads_a_real_repository_and_tolerates_absence(self):
        self.assertEqual(self.module.read_numstat(str(self.tmp / "missing")), {})
        self.assertEqual(self.module.read_numstat(str(self.tmp)), {})  # not a repository
        self.init_repo([("a.txt", "1\n2\n")])
        self.assertEqual(self.module.read_numstat(str(self.tmp)), {})  # no commit yet
        env = ["git", "-C", str(self.tmp), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*env, "add", "."], check=True)
        subprocess.run([*env, "commit", "-qm", "x"], check=True)
        (self.tmp / "a.txt").write_text("1\n2\n3\n")
        (self.tmp / "b.bin").write_bytes(b"\0\1\2")
        subprocess.run([*env, "add", "b.bin"], check=True)
        self.assertEqual(self.module.read_numstat(str(self.tmp)), {"a.txt": (1, 0), "b.bin": None})

    def test_stats_thread_invalidates_only_on_change_and_stops(self):
        reads = []
        values = [{}, {"a": (1, 1)}, {"a": (1, 1)}, {"a": (2, 1)}]

        def read(cwd):
            reads.append(cwd)
            return values[min(len(reads) - 1, 3)]

        invalidated = []
        ctx = SimpleNamespace(
            git=lambda: GitInfo("main", dirty=True), cwd="/x", invalidate=lambda: invalidated.append(1)
        )
        stats = self.module.Stats(0.01, read)
        stats.start(ctx)
        stats.start(ctx)  # one thread only
        deadline = time.monotonic() + 3
        while len(reads) < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(stats.value, {"a": (2, 1)})
        self.assertEqual(len(invalidated), 2)  # {} -> {} is no change; two real changes
        stats.stop()
        stats.thread.join(1)
        self.assertFalse(stats.thread.is_alive())
        seen = len(reads)
        time.sleep(0.05)
        self.assertEqual(len(reads), seen)

    def test_clean_repository_is_not_asked_for_stats_and_dispose_stops_the_thread(self):
        calls = fake_extension(self.module)
        reads = []
        self.module.read_numstat = lambda cwd: reads.append(cwd) or {}
        clean_ctx = SimpleNamespace(git=lambda: GitInfo("main"), cwd="/x", invalidate=lambda: None)
        calls.panels[0].render(clean_ctx)
        time.sleep(0.1)
        self.assertEqual(reads, [])
        before = threading.active_count()
        calls.disposers[0]()
        time.sleep(0.1)
        self.assertLessEqual(threading.active_count(), before)

    def test_stats_are_not_read_when_files_or_stats_are_hidden(self):
        for hidden in ({"show_files": False}, {"show_stats": False}):
            with self.subTest(hidden):
                calls = fake_extension(self.module, **hidden)
                ctx = SimpleNamespace(git=lambda: GitInfo("main", dirty=True), cwd="/x", invalidate=lambda: None)
                self.assertIsNotNone(calls.panels[0].render(ctx))
                self.assertEqual([t.name for t in threading.enumerate() if t.name == "git-panel-stats"], [])
                calls.disposers[0]()

    @unittest.skipUnless(shutil.which("git"), "needs git")
    async def test_ui_panel_shows_the_repository_and_its_stats(self):
        self.init_repo([("a.txt", "one\n")])
        env = ["git", "-C", str(self.tmp), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*env, "add", "."], check=True)
        subprocess.run([*env, "commit", "-qm", "x"], check=True)
        (self.tmp / "a.txt").write_text("one\ntwo\nthree\n")
        self.write_settings("widgets/git_panel", {"show_project": True, "show_status": True, "refresh": 0.5})
        core = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            trust_project=True,
            extra_extensions=[str(WIDGETS / "git_panel.py")],
        )
        _, errors = core.reload_extensions()
        self.assertEqual(errors, [])
        core.provider, core.model = RecordingProvider([]), "m"
        tui = MinimalTui(Host(core))
        async with tui.run_test() as pilot:
            panel = None
            for _ in range(100):
                await pilot.pause(0.05)
                panel = next((p for p in tui.query(PanelView) if p.spec.name == "git_panel"), None)
                if panel is not None and "+2" in str(panel.render()):
                    break
            text = str(panel.render())
            self.assertIn("GIT", text.splitlines()[0])
            self.assertIn(self.tmp.name, text)
            self.assertIn("main *", text)
            self.assertIn("M a.txt", text)
            self.assertIn("+2 −0", text)
            core.registry.dispose()


class StatuslineKitTest(KitTest):
    def setUp(self):
        super().setUp()
        self.module = load_module("statusline_kit")
        self.settings = dict(self.module.DEFAULTS)
        self.config = self.module.configure(self.settings)

    def facts(self, messages=(), **kw):
        digest = self.module.Digest().update(list(messages))
        base = {
            "model": "m1",
            "cwd": "/home/me/proj",
            "home": "/home/me",
            "git": None,
            "session": "abc",
            "context_tokens": None,
            "context_window": None,
            "digest": digest,
        }
        return {**base, **kw}

    def configure(self, **overrides):
        return self.module.configure({**self.settings, **overrides})

    def test_select_keeps_order_drops_duplicates_and_lists_unknown(self):
        known, unknown = self.module.select(["tokens", "model", "tokens", "nope", "nope", "Model"])
        self.assertEqual(known, ["tokens", "model"])
        self.assertEqual(unknown, ["nope", "Model"])

    def test_settings_problems_name_the_fix(self):
        with self.assertRaisesRegex(ValueError, r"unknown segment 'tittle' in 'segments_left'.*known: model.*title"):
            self.configure(segments_left=["model", "tittle"])
        with self.assertRaisesRegex(ValueError, "both empty"):
            self.configure(segments_left=[], segments_right=[])
        with self.assertRaisesRegex(ValueError, "'segments_right' lists 'cost', which widgets/token_sparkline owns"):
            self.configure(segments_right=["cost"])
        with self.assertRaisesRegex(ValueError, "'labels' has no entry 'x'"):
            self.configure(labels={"x": "y"})
        with self.assertRaisesRegex(ValueError, r"'styles.model'"):
            self.configure(styles={"model": "glittery"})
        self.assertEqual(self.configure(segments_left=[], segments_right=["model"])["right"], ["model"])

    def test_both_sides_register_one_segment_each_with_the_order(self):
        calls = fake_extension(self.module, segments_left=["model"], segments_right=["turns", "usage"], order=7)
        self.assertEqual(
            [(n, s, o) for n, _, s, o in calls.status],
            [("statusline_kit", "left", 7), ("statusline_kit_right", "right", 7)],
        )
        only = fake_extension(self.module, segments_left=[], segments_right=["model"])
        self.assertEqual([(n, s) for n, _, s, _ in only.status], [("statusline_kit_right", "right")])
        self.assertEqual(fake_extension(self.module, enabled=False).status, [])

    def test_the_cost_estimate_belongs_to_token_sparkline(self):
        self.assertNotIn("cost", self.module.SEGMENTS)
        self.assertNotIn("price_in", self.module.DEFAULTS)

    def test_format_tokens_rounds_across_unit_boundaries(self):
        fmt = self.module.format_tokens
        self.assertEqual([fmt(n) for n in (0, 999, 1000, 12_345)], ["0", "999", "1.0k", "12.3k"])
        self.assertEqual([fmt(n) for n in (999_949, 999_950, 1_250_000)], ["999.9k", "1.0M", "1.2M"])

    def test_short_path_uses_home_and_keeps_the_last_folders(self):
        short = self.module.short_path
        self.assertEqual(short("/home/me", "/home/me"), "~")
        self.assertEqual(short("/home/me/proj", "/home/me"), "~/proj")
        self.assertEqual(short("/home/me/a/b/c", "/home/me"), "…/b/c")
        self.assertEqual(short("/home/me/a/b/c", "/home/me", folders=3), "~/a/b/c")
        self.assertEqual(short("/home/me/a/b/c/d", "/home/me", folders=3), "…/b/c/d")
        self.assertEqual(short("/home/meow", "/home/me"), "/home/meow")
        self.assertEqual(short("/srv", "/home/me"), "/srv")

    def test_title_turns_and_usage_come_from_the_messages_once(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "old"}]},
            {"role": "user", "content": "  \n"},
            {"role": "user", "content": [{"type": "text", "text": "fix \x1b]0;pwn\x07 it\nsecond line"}]},
            {"role": "assistant", "content": [], "usage": {"input_tokens": 100, "output_tokens": 10}},
            {"role": "assistant", "content": []},
            {"role": "assistant", "content": [], "usage": {"input_tokens": None, "output_tokens": 5}},
            {"role": "user", "content": "later"},
        ]
        digest = self.module.Digest().update(messages)
        self.assertEqual((digest.title, digest.turns, digest.input, digest.output), ("fix ]0;pwn it", 2, 100, 15))
        self.assertEqual(self.module.Digest().update([{"role": "assistant", "content": "hi"}]).title, "")

    def test_digest_reads_only_new_messages_and_starts_over_when_the_history_changes(self):
        first = {"role": "user", "content": "one"}
        reply = {"role": "assistant", "content": [], "usage": {"input_tokens": 10, "output_tokens": 1}}
        messages = [first, reply]
        digest = self.module.Digest()
        digest.update(messages)
        reply["usage"]["input_tokens"] = 999  # an old message is not read again
        messages.append({"role": "user", "content": "two"})
        digest.update(messages)
        self.assertEqual((digest.turns, digest.input), (2, 10))
        digest.update(
            [{"role": "user", "content": "other"}, {"role": "user", "content": "x"}, {"role": "user", "content": "y"}]
        )
        self.assertEqual((digest.turns, digest.title), (3, "other"))  # same length or longer, but not the same history
        digest.update([])
        self.assertEqual((digest.turns, digest.title), (0, ""))

    def test_segments_use_labels_glyphs_and_styles(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [], "usage": {"input_tokens": 2000, "output_tokens": 300}},
        ]
        facts = self.facts(messages, context_tokens=12_345, context_window=100_000, session="s1")
        cfg = self.configure(
            labels={"tokens": "context", "session": "id", "turn": "msg", "turns": "msgs"},
            glyphs={"up": "in ", "down": "out "},
            styles={"model": "bold $secondary", "usage": "dim"},
            tokens_percent=True,
        )
        seg = lambda name: self.module.segment(name, facts, cfg)  # noqa: E731
        self.assertEqual(seg("tokens"), [("context 12.3k 12%", "muted")])
        self.assertEqual(seg("session"), [("id s1", "dim")])
        self.assertEqual(seg("turns"), [("1 msg", "muted")])
        self.assertEqual(seg("usage"), [("in 2.0k out 300", "dim")])
        self.assertEqual(seg("model"), [("m1", "bold $secondary")])
        self.assertEqual(
            self.module.segment("turns", self.facts([{"role": "user", "content": "a"}] * 2), cfg), [("2 msgs", "muted")]
        )
        self.assertIsNone(self.module.segment("usage", self.facts(), cfg))
        self.assertIsNone(self.module.segment("tokens", self.facts(), cfg))

    def test_title_segment_is_clipped_to_the_configured_width(self):
        facts = self.facts([{"role": "user", "content": "x" * 50}])
        (piece,) = self.module.segment("title", facts, self.configure(title_width=10))
        self.assertEqual(piece[0], "x" * 9 + "…")

    def test_branch_segment_reads_the_shared_git_info(self):
        clean = self.module.segment("branch", self.facts(git=GitInfo("main")), self.config)
        dirty = self.module.segment("branch", self.facts(git=GitInfo("fe\x1bat", dirty=True)), self.config)
        detached = self.module.segment("branch", self.facts(git=GitInfo(None)), self.config)
        self.assertTrue(clean[0][0].endswith("main"))
        self.assertTrue(dirty[0][0].endswith("fe at *"))
        self.assertTrue(detached[0][0].endswith("detached"))
        self.assertEqual((clean[0][1], dirty[0][1]), ("success", "warning"))
        self.assertIsNone(self.module.segment("branch", self.facts(), self.config))

    def test_git_is_only_asked_for_when_a_branch_segment_is_shown(self):
        asked = []
        ctx = SimpleNamespace(
            model="m",
            cwd="/w",
            session_id="s",
            context_tokens=None,
            context_window=None,
            messages=[],
            git=lambda: asked.append(1),
        )
        digest = self.module.Digest()
        self.module.facts_of(ctx, digest, ["model"])
        self.assertEqual(asked, [])
        self.module.facts_of(ctx, digest, ["branch"])
        self.assertEqual(asked, [1])

    def test_line_joins_present_segments_and_is_none_when_all_are_empty(self):
        cfg = self.configure(separator="|")
        facts = self.facts([{"role": "user", "content": "hello"}], context_tokens=12_345)
        line = self.module.build_line(["model", "title", "session", "branch", "tokens"], facts, cfg)
        self.assertEqual("".join(text for text, _ in line), "m1 | hello | session abc | ctx 12.3k")
        self.assertEqual(line[1][1], "dim")
        self.assertIsNone(self.module.build_line(["branch", "turns", "usage"], self.facts(), cfg))

    @unittest.skipUnless(shutil.which("git"), "needs git")
    async def test_ui_shows_both_sides_and_follows_the_repository(self):
        self.init_repo([("f.txt", "x")])
        self.write_settings(
            "widgets/statusline_kit",
            {"segments_left": ["model", "branch"], "segments_right": ["turns", "usage"], "labels": {"turn": "msg"}},
        )
        core = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            trust_project=True,
            extra_extensions=[str(WIDGETS / "statusline_kit.py")],
        )
        _, errors = core.reload_extensions()
        self.assertEqual(errors, [])
        core.provider, core.model = RecordingProvider([text_reply("done", usage=Usage(1500, 250))]), "m"
        tui = MinimalTui(Host(core))
        async with tui.run_test() as pilot:
            tui.query_one("#prompt", Composer).set_text("hi")
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause(0.05)
                right = str(tui.query_one("#status-right", Static).render())
                if not tui.busy and "↑1.5k ↓250" in right:
                    break
            left = str(tui.query_one("#status-left", Static).render())
            self.assertIn("main *", left)
            self.assertIn("1 msg", right)
            self.assertIn("↑1.5k ↓250", right)
            core.registry.dispose()


class ContextGaugeTest(KitTest):
    def setUp(self):
        super().setUp()
        self.module = load_module("context_gauge")
        self.settings = dict(self.module.DEFAULTS)

    def ctx(self, model="claude-x", window=None, tokens=20_000, messages=()):
        return SimpleNamespace(model=model, context_window=window, context_tokens=tokens, messages=list(messages))

    def line(self, tokens=100_000, window=200_000, usage=None, **overrides):
        settings = {**self.settings, **overrides}
        problems, glyphs, styles = self.module.check_settings(settings)
        self.assertEqual(problems, [])
        return self.module.build_line(tokens, window, usage, settings, glyphs, styles)

    def test_default_line_is_the_old_gauge(self):
        text = rows([self.line(usage={"input_tokens": 1000, "cache_read_tokens": 250})])[0]
        self.assertEqual(text, "ctx ▕" + "█" * 12 + "░" * 12 + "▏ 100.0k/200k 50% · cache 25%")

    def test_window_comes_from_limits_then_limit_then_the_model_then_the_fallback(self):
        window = self.module.window_of
        self.assertEqual(window(self.ctx(window=1_000_000), self.settings), 1_000_000)
        self.assertEqual(window(self.ctx(window=None), self.settings), 200_000)
        self.assertEqual(window(self.ctx(window=1_000_000), {**self.settings, "limit": 50_000}), 50_000)
        limits = {**self.settings, "limit": 50_000, "limits": {"claude": 111, "claude-x": 222, "gpt": 333}}
        self.assertEqual(window(self.ctx(model="claude-xl", window=999), limits), 222)  # the longest prefix wins
        self.assertEqual(window(self.ctx(model="other", window=999), limits), 50_000)
        self.assertEqual(window(self.ctx(model=None, window=None), {**self.settings, "fallback_limit": 7}), 7)

    def test_label_width_glyphs_styles_and_parts_are_settings(self):
        text = self.line(
            width=10,
            label="",
            glyphs={"left": "[", "right": "]", "fill": "#", "empty": "-"},
            show_percent=False,
            show_cache=False,
        )
        self.assertEqual(rows([text])[0], "[#####-----] 100.0k/200k")
        full = rows([self.line(tokens=300_000, width=4, show_numbers=False, label="ctx:")])[0]
        self.assertEqual(full, "ctx: ▕████▏ 100%")
        self.assertEqual(rows([self.line(tokens=1, window=1_000_000)])[0].split("▏ ")[1], "0.0k/1M 0%")
        styles = {"ok": "$accent", "warn": "bold", "crit": "underline", "empty": "dim", "label": "dim"}
        for tokens, ok in ((10_000, "$accent"), (120_000, "bold"), (190_000, "underline")):
            self.assertEqual(self.line(tokens=tokens, styles=styles)[2][1], ok)

    def test_partial_cells_use_the_eighths_glyphs(self):
        text = rows([self.line(tokens=500, window=24_000, glyphs={"eighths": "1234567"})])[0]  # 4 eighths: one cell
        self.assertIn("▕4" + "░" * 23 + "▏", text)
        text = rows([self.line(tokens=1_500, window=24_000, glyphs={"eighths": "1234567"})])[0]  # 12 eighths
        self.assertIn("▕█4" + "░" * 22 + "▏", text)

    def test_bad_settings_are_refused_with_the_fix(self):
        for bad, expected in (
            ({"glyphs": {"eighths": "abc"}}, "needs 7 characters"),
            ({"warn": 0.9, "crit": 0.5}, "'warn' must not exceed 'crit'"),
            ({"limits": {"x": 0}}, "limits.x"),
            ({"limits": {"x": "big"}}, "limits.x"),
            ({"styles": {"ok": "neon"}}, "styles.ok"),
            ({"glyphs": {"nope": "x"}}, "no entry 'nope'"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, f"context_gauge settings:.*{expected}"):
                    fake_extension(self.module, **bad)

    def test_schema_validates_the_user_values_when_loaded(self):
        registry, errors = self.load(
            "context_gauge", user_settings("widgets/context_gauge", {"width": 1, "area": "nowhere", "warn": 2.0})
        )
        for key in ("width", "area", "warn"):
            self.assertTrue(any(f"widgets/context_gauge.{key}" in e for e in errors), errors)
        self.assertEqual(registry.panels["context_gauge"].area, "bottom")

    def test_panel_placement_title_and_disable_come_from_the_settings(self):
        calls = fake_extension(self.module, area="footer", order=3, title="CONTEXT")
        panel = calls.panels[0]
        self.assertEqual((panel.area, panel.order, panel.title, panel.collapsible), ("footer", 3, "CONTEXT", True))
        self.assertIsNone(fake_extension(self.module).panels[0].title)
        self.assertEqual(fake_extension(self.module, enabled=False).panels, [])

    def test_render_hides_without_context_tokens_and_reads_the_real_window(self):
        render = fake_extension(self.module).panels[0].render
        self.assertIsNone(render(self.ctx(tokens=None)))
        text = rows(render(self.ctx(tokens=50_000, window=1_000_000)))[0]
        self.assertTrue(text.endswith("50.0k/1M 5%"), text)

    async def test_ui_gauge_follows_the_models_window(self):
        self.write_settings("widgets/context_gauge", {"limits": {"m": 400_000}, "title": "CTX"})
        core = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            trust_project=True,
            extra_extensions=[str(WIDGETS / "context_gauge.py")],
        )
        _, errors = core.reload_extensions()
        self.assertEqual(errors, [])
        core.provider, core.model = RecordingProvider([text_reply("done", usage=Usage(100_000, 10))]), "m"
        tui = MinimalTui(Host(core))
        async with tui.run_test() as pilot:
            tui.query_one("#prompt", Composer).set_text("hi")
            await pilot.press("enter")
            panel = None
            for _ in range(100):
                await pilot.pause(0.05)
                panel = next((p for p in tui.query(PanelView) if p.spec.name == "context_gauge"), None)
                if panel is not None and panel.display and not tui.busy:
                    break
            text = str(panel.render())
            self.assertIn("CTX", text.splitlines()[0])
            self.assertIn("100.0k/400k 25%", text)


class TokenSparklineTest(KitTest):
    def setUp(self):
        super().setUp()
        self.module = load_module("token_sparkline")
        self.settings = dict(self.module.DEFAULTS)

    def reply(self, inp, out=10, read=0, write=0):
        return {
            "role": "assistant",
            "content": [],
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_tokens": read,
                "cache_write_tokens": write,
            },
        }

    def lines(self, digest, cost=None, **overrides):
        settings = {**self.settings, **overrides}
        problems, glyphs, styles = self.module.check_settings(settings)
        self.assertEqual(problems, [])
        return self.module.build_lines(digest, cost, settings, glyphs, styles)

    def test_default_panel_lines_and_hidden_before_the_first_reply(self):
        digest = self.module.Digest().update(
            [self.reply(100), self.reply(800, 20, read=400), {"role": "user", "content": "x"}]
        )
        text = rows(self.lines(digest, 1.234))
        self.assertEqual(text, ["2 requests", "▂█", "in 900 · out 30 · cache 44%", "≈ $1.23"])
        self.assertIsNone(self.lines(self.module.Digest()))

    def test_parts_glyphs_styles_and_formats_are_settings(self):
        digest = self.module.Digest().update([self.reply(10), self.reply(5), self.reply(30)])
        lines = self.lines(
            digest,
            2.0,
            show_totals=False,
            show_cost=False,
            spark_len=2,
            glyphs={"spark": "_-"},
            styles={"spark": "bold $secondary", "requests": "muted"},
        )
        self.assertEqual(rows(lines), ["3 requests", "_-"])  # last two requests, scaled to the larger
        self.assertEqual((lines[0][0][1], lines[1][0][1]), ("muted", "bold $secondary"))
        text = rows(self.lines(digest, 2.5, show_spark=False, show_cache=False, cost_format="{cost:.1f} EUR"))
        self.assertEqual(text, ["3 requests", "in 45 · out 30", "2.5 EUR"])
        for bad in (
            {"cost_format": "{price}"},
            {"status_format": "{"},
            {"glyphs": {"spark": "x"}},
            {"glyphs": {"a": "b"}},
        ):
            with self.assertRaises(ValueError):
                fake_extension(self.module, **bad)

    def test_digest_reads_only_new_messages_and_keeps_the_totals(self):
        messages = [self.reply(10), {"role": "user", "content": "x"}]
        digest = self.module.Digest()
        digest.update(messages)
        messages[0]["usage"]["input_tokens"] = 5_000  # old messages are not read again
        messages.append(self.reply(30, read=10, write=5))
        digest.update(messages)
        self.assertEqual((digest.requests, digest.input, digest.cache_read, digest.cache_write), (2, 40, 10, 5))
        self.assertEqual(list(digest.recent), [10, 30])
        digest.update([self.reply(1)])  # a different history starts over
        self.assertEqual((digest.requests, digest.input), (1, 1))

    def test_cost_uses_the_longest_prefix_and_the_cache_rates(self):
        digest = self.module.Digest().update([self.reply(1_000_000, 100_000, read=400_000, write=100_000)])
        prices = {"claude": (1.0, 1.0, 1.0, 1.0), "claude-x": (10.0, 20.0, 1.0, 2.0)}
        self.assertAlmostEqual(digest.cost("claude-x-1", prices), 500_000 * 10 / 1e6 + 0.4 + 0.2 + 2.0)
        self.assertIsNone(digest.cost("other", prices))
        self.assertIsNone(digest.cost(None, prices))
        shipped = self.module.PRICES["claude-sonnet-5-5"]
        self.assertAlmostEqual(
            digest.cost("claude-sonnet-5-5-x", self.module.PRICES),
            (500_000 * shipped[0] + 400_000 * shipped[2] + 100_000 * shipped[3] + 100_000 * shipped[1]) / 1e6,
        )

    def test_user_prices_are_parsed_and_malformed_ones_are_reported_not_raised(self):
        table, problems = self.module.parse_prices(
            {
                "a": [1, 2],
                "b": [1, 2, 3, 4],
                "c": [1],
                "d": "free",
                "e": [1, -2],
                "f": [True, 1],
                "": [1, 2],
                "g": [1, "x"],
            }
        )
        self.assertEqual(table, {"a": (1.0, 2.0, 1.0, 1.0), "b": (1.0, 2.0, 3.0, 4.0)})
        self.assertEqual(len(problems), 6)
        self.assertIn("prices['d']", problems[1])
        notices = []
        ctx = SimpleNamespace(model="a-1", messages=[self.reply(1_000_000, 0)], notify=lambda *a: notices.append(a))
        calls = fake_extension(self.module, prices={"a": [4, 8], "bad": "x", "worse": [1]})
        text = rows(calls.panels[0].render(ctx))
        self.assertEqual(text[-1], "≈ $4.00")  # the good entry works
        calls.status[0][1](ctx)
        calls.panels[0].render(ctx)
        self.assertEqual(len(notices), 1)  # said once
        self.assertEqual(notices[0][1], "warning")
        self.assertIn("prices['bad']", notices[0][0])
        self.assertIn("prices['worse']", notices[0][0])

    def test_a_user_price_overrides_the_shipped_one(self):
        ctx = SimpleNamespace(model="claude-sonnet-5-5", messages=[self.reply(1_000_000, 0)], notify=lambda *a: None)
        calls = fake_extension(self.module, prices={"claude-sonnet-5-5": [100, 100]})
        self.assertEqual(rows(calls.panels[0].render(ctx))[-1], "≈ $100.00")

    def test_status_segment_side_order_format_and_switch(self):
        calls = fake_extension(
            self.module, cost_side="left", cost_order=-5, status_format="${cost:.3f}", styles={"status": "bold"}
        )
        ((name, render, side, order),) = calls.status
        self.assertEqual((name, side, order), ("cost", "left", -5))
        ctx = SimpleNamespace(model="claude-sonnet-5-5", messages=[self.reply(1_000_000, 0)], notify=lambda *a: None)
        self.assertEqual(render(ctx), ("$2.000", "bold"))
        self.assertIsNone(render(SimpleNamespace(model="unpriced", messages=[], notify=lambda *a: None)))
        self.assertEqual(fake_extension(self.module, cost_status=False).status, [])
        self.assertEqual(fake_extension(self.module, enabled=False).panels, [])

    def test_panel_registration_uses_title_area_order(self):
        panel = fake_extension(self.module, area="left", order=2, title="").panels[0]
        self.assertEqual((panel.area, panel.order, panel.title, panel.collapsible), ("left", 2, None, True))
        self.assertEqual(fake_extension(self.module).panels[0].title, "TOKENS")

    def test_schema_is_complete_and_validates_user_values(self):
        calls = fake_extension(self.module)
        self.assertEqual(set(calls.schema), set(self.module.DEFAULTS))
        self.assertTrue(all(entry["description"] for entry in calls.schema.values()))
        registry, errors = self.load(
            "token_sparkline", user_settings("widgets/token_sparkline", {"spark_len": 1, "cost_side": "up"})
        )
        self.assertTrue(any("spark_len" in e for e in errors) and any("cost_side" in e for e in errors), errors)

    def test_render_cost_stays_cheap_as_the_session_grows(self):
        messages = [self.reply(10) for _ in range(5_000)]
        ctx = SimpleNamespace(model="claude-sonnet-5-5", messages=messages, notify=lambda *a: None)
        render = fake_extension(self.module).panels[0].render
        render(ctx)
        for _ in range(3):  # a redraw with no new message reads nothing: poison the old ones to prove it
            for message in messages:
                message["usage"] = None
            self.assertEqual(rows(render(ctx))[0], "5000 requests")
        messages.append(self.reply(10))
        self.assertEqual(rows(render(ctx))[0], "5001 requests")

    async def test_ui_panel_and_cost_segment_follow_the_session(self):
        self.write_settings("widgets/token_sparkline", {"cost_side": "left", "title": "SPEND"})
        core = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=True,
            trust_project=True,
            extra_extensions=[str(WIDGETS / "token_sparkline.py")],
        )
        _, errors = core.reload_extensions()
        self.assertEqual(errors, [])
        core.provider, core.model = (
            RecordingProvider([text_reply("done", usage=Usage(1_000_000, 0))]),
            "claude-sonnet-5-5",
        )
        tui = MinimalTui(Host(core))
        async with tui.run_test() as pilot:
            tui.query_one("#prompt", Composer).set_text("hi")
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause(0.05)
                left = str(tui.query_one("#status-left", Static).render())
                if not tui.busy and "≈$2.00" in left:
                    break
            self.assertIn("≈$2.00", left)
            panel = next(p for p in tui.query(PanelView) if p.spec.name == "token_sparkline")
            text = str(panel.render())
            self.assertIn("SPEND", text.splitlines()[0])
            self.assertIn("1 requests", text)
            self.assertIn("≈ $2.00", text)


if __name__ == "__main__":
    unittest.main()
