import importlib.util
import io
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from minimal.core.host import Host
from minimal.core.session import Session
from minimal.ui.tui.app import MinimalTui
from minimal.ui.tui.composer import Composer
from tests.test_bundled_extensions import MARKETPLACE, BundledExtensionTest, text_reply


def load_module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_extension(module, **overrides):
    """A stand-in for the ``minimal`` object: ``declare_settings`` merges ``overrides``; registrations are recorded."""
    calls = SimpleNamespace(status=[], panels=[], disposers=[])
    fake = SimpleNamespace(
        declare_settings=lambda defaults: {**defaults, **overrides},
        on_dispose=calls.disposers.append,
        register_status=lambda name, render, side="right": calls.status.append((name, render, side)),
        register_panel=lambda name, render, area="sidebar", live=None, interval=0: calls.panels.append(
            (name, render, area, interval)
        ),
    )
    module.register(fake)
    return calls


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)


class StatuslineKitTest(BundledExtensionTest):
    def setUp(self):
        super().setUp()
        self.module = load_module(MARKETPLACE / "widgets" / "statusline_kit.py")
        self.settings = dict(self.module.DEFAULTS)

    def facts(self, **kw):
        base = {
            "model": "m1",
            "cwd": "/home/me/proj",
            "home": "/home/me",
            "branch": None,
            "session": "abc",
            "context_tokens": None,
            "messages": [],
        }
        return {**base, **kw}

    def test_select_keeps_order_drops_duplicates_and_lists_unknown(self):
        known, unknown = self.module.select(["tokens", "model", "tokens", "nope", "nope", "Model"])
        self.assertEqual(known, ["tokens", "model"])
        self.assertEqual(unknown, ["nope", "Model"])

    def test_settings_problems_name_the_fix(self):
        check = self.module.check_settings
        self.assertEqual(check(self.settings), [])
        (problem,) = check({**self.settings, "segments": ["model", "tittle"]})
        self.assertIn("'tittle'", problem)
        self.assertIn("title, session", problem)  # the known names are listed
        self.assertIn("at least one", check({**self.settings, "segments": []})[0])
        self.assertIn("one of left, right", check({**self.settings, "side": "middle"})[0])
        self.assertIn("price_in", check({**self.settings, "segments": ["cost"]})[0])
        self.assertEqual(check({**self.settings, "segments": ["cost"], "price_out": 15.0}), [])

    def test_register_refuses_bad_settings_with_the_problem(self):
        with self.assertRaisesRegex(ValueError, r"statusline_kit settings: unknown segment 'foo'.*known: model"):
            fake_extension(self.module, segments=["foo"])
        calls = fake_extension(self.module, segments=["turns"], side="right")
        self.assertEqual([(name, side) for name, _, side in calls.status], [("statusline_kit", "right")])

    def test_format_tokens_rounds_across_unit_boundaries(self):
        fmt = self.module.format_tokens
        self.assertEqual([fmt(n) for n in (0, 999, 1000, 12_345)], ["0", "999", "1.0k", "12.3k"])
        self.assertEqual([fmt(n) for n in (999_949, 999_950, 1_250_000)], ["999.9k", "1.0M", "1.2M"])

    def test_short_path_uses_home_and_keeps_the_last_folders(self):
        short = self.module.short_path
        self.assertEqual(short("/home/me", "/home/me"), "~")
        self.assertEqual(short("/home/me/proj", "/home/me"), "~/proj")
        self.assertEqual(short("/home/me/a/b/c", "/home/me"), "…/b/c")
        self.assertEqual(short("/home/meow", "/home/me"), "/home/meow")  # a sibling folder is not under home
        self.assertEqual(short("/srv", "/home/me"), "/srv")

    def test_title_is_the_first_line_of_the_first_user_text_and_cleaned(self):
        messages = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "old"}]},
            {"role": "user", "content": "  \n"},
            {"role": "user", "content": [{"type": "text", "text": "fix \x1b]0;pwn\x07 it\nsecond line"}]},
            {"role": "user", "content": "later"},
        ]
        title = self.module.session_title(messages)
        self.assertEqual(title, "fix ]0;pwn it")
        self.assertEqual(self.module.session_title([{"role": "assistant", "content": "hi"}]), "")

    def test_turns_count_typed_messages_not_tool_results(self):
        messages = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
            {"role": "user", "content": [{"type": "text", "text": "two"}]},
        ]
        self.assertEqual(self.module.turn_count(messages), 2)

    def test_usage_totals_skip_replies_without_usage(self):
        messages = [
            {"role": "assistant", "content": [], "usage": {"input_tokens": 100, "output_tokens": 10}},
            {"role": "assistant", "content": []},
            {"role": "assistant", "content": [], "usage": {"input_tokens": None, "output_tokens": 5}},
        ]
        self.assertEqual(self.module.usage_totals(messages), (100, 15))

    def test_parse_branch_covers_tracking_new_and_detached_repositories(self):
        parse = self.module.parse_branch
        self.assertEqual(parse("## main...origin/main [ahead 2]\n M a.py\n"), ("main", True))
        self.assertEqual(parse("## main...origin/main\n"), ("main", False))
        self.assertEqual(parse("## No commits yet on dev\n?? f\n"), ("dev", True))
        self.assertEqual(parse("## HEAD (no branch)\n"), ("detached", False))
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("fatal: not a git repository"))

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_read_branch_reports_dirtiness_and_tolerates_absence(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        self.assertIsNone(self.module.read_branch(str(repo)))  # not a repository
        self.assertIsNone(self.module.read_branch(str(self.tmp / "missing")))  # cwd does not exist
        init_repo(repo)
        self.assertEqual(self.module.read_branch(str(repo)), ("main", False))
        (repo / "f.txt").write_text("x")
        self.assertEqual(self.module.read_branch(str(repo)), ("main", True))
        with mock.patch.object(self.module.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertIsNone(self.module.read_branch(str(repo)))  # no git installed

    def test_cost_segment_uses_the_prices_and_keeps_small_amounts_visible(self):
        usage = {"role": "assistant", "content": [], "usage": {"input_tokens": 200_000, "output_tokens": 50_000}}
        settings = {**self.settings, "price_in": 3.0, "price_out": 15.0}
        facts = self.facts(messages=[usage])
        self.assertEqual(self.module.segment("cost", facts, settings), [("~$1.35", "muted")])
        self.assertEqual(self.module.segment("usage", facts, settings), [("↑200.0k ↓50.0k", "muted")])
        tiny = self.facts(messages=[{"usage": {"input_tokens": 100, "output_tokens": 0}}])
        self.assertEqual(self.module.segment("cost", tiny, settings), [("~$0.0003", "muted")])
        self.assertIsNone(self.module.segment("cost", self.facts(), settings))  # no usage reported yet

    def test_title_segment_is_clipped_to_the_configured_width(self):
        facts = self.facts(messages=[{"role": "user", "content": "x" * 50}])
        (piece,) = self.module.segment("title", facts, {**self.settings, "title_width": 10})
        self.assertEqual(piece[0], "x" * 9 + "…")

    def test_branch_segment_marks_dirty_and_strips_control_characters(self):
        (clean_piece,) = self.module.segment("branch", self.facts(branch=("main", False)), self.settings)
        (dirty_piece,) = self.module.segment("branch", self.facts(branch=("fe\x1bat", True)), self.settings)
        self.assertTrue(clean_piece[0].endswith("main"))
        self.assertTrue(dirty_piece[0].endswith("fe at *"))
        self.assertNotEqual(clean_piece[1], dirty_piece[1])
        self.assertIsNone(self.module.segment("branch", self.facts(), self.settings))  # not a repository

    def test_line_joins_present_segments_and_is_none_when_all_are_empty(self):
        settings = {**self.settings, "separator": "|"}
        facts = self.facts(messages=[{"role": "user", "content": "hello"}], context_tokens=12_345)
        line = self.module.build_line(["model", "title", "session", "branch", "tokens"], facts, settings)
        self.assertEqual("".join(text for text, _ in line), "m1 | hello | session abc | ctx 12.3k")
        self.assertIsNone(self.module.build_line(["branch", "turns", "usage"], self.facts(), settings))


class TermTitleTest(BundledExtensionTest, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.module = load_module(MARKETPLACE / "extensions" / "term_title.py")

    def test_title_names_the_project_folder_and_state(self):
        self.assertEqual(self.module.build_title("minimal", "/work/shop", "idle"), "minimal: shop [idle]")
        self.assertEqual(self.module.build_title("minimal", "/", "working"), "minimal: / [working]")

    def test_control_characters_cannot_end_or_inject_a_sequence(self):
        title = self.module.build_title("m\x07", "/work/a\x1b]0;evil\x07b\nc\x9b", "idle")
        self.assertEqual(title, "m: a ]0;evil b c [idle]")
        sequence = self.module.osc_title(title + "\x07\x1b\\x")
        self.assertEqual(sequence.count("\x1b"), 1)
        self.assertEqual(sequence.count("\x07"), 1)
        self.assertTrue(sequence.startswith("\x1b]0;") and sequence.endswith("\x07"))

    def test_long_project_names_are_cut_but_keep_the_state(self):
        title = self.module.build_title("minimal", "/work/" + "p" * 300, "working")
        self.assertEqual(len(title), self.module.MAX_LENGTH)
        self.assertTrue(title.endswith("…" + " [working]"))
        self.assertTrue(title.startswith("minimal: ppp"))

    def test_title_writes_only_changes_and_restores_once(self):
        writes = []
        terminal = SimpleNamespace(write=lambda data: writes.append(data) or True)
        title = self.module.Title({"prefix": "minimal", "restore": ""}, terminal)
        title.show("/w/p", "idle")
        title.show("/w/p", "idle")
        title.show("/w/p", "working")
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

    def test_restore_setting_names_the_title_to_leave_behind(self):
        writes = []
        terminal = SimpleNamespace(write=lambda data: writes.append(data) or True)
        title = self.module.Title({"prefix": "minimal", "restore": "my\x1b shell"}, terminal)
        title.show("/w/p", "idle")
        title.restore()
        self.assertEqual(writes[-1], "\x1b]0;my shell\x07")

    def test_unwritable_terminal_is_retried_and_nothing_is_popped(self):
        writes, available = [], [False]
        terminal = SimpleNamespace(write=lambda data: writes.append(data) if available[0] else False)
        title = self.module.Title({"prefix": "minimal", "restore": ""}, terminal)
        title.show("/w/p", "idle")
        title.restore()
        self.assertEqual(writes, [])
        available[0] = True
        title.show("/w/p", "idle")  # the same title is sent now: the first attempt never reached a terminal
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

    async def test_ui_sends_working_while_a_turn_runs_idle_after_and_restores_on_exit(self):
        sent = []
        core = self.make_app("extensions/term_title.py", Session("t", self.tmp / "t.jsonl"), [text_reply("done")])
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
        self.assertIn(f"minimal: {project} [working]", titles)
        self.assertEqual(titles[-1], f"minimal: {project} [idle]")
        self.assertLess(titles.index(f"minimal: {project} [working]"), len(titles) - 1)
        with mock.patch.object(sys, "__stdout__", real), mock.patch.object(sys, "stdout", real):
            core.shutdown()  # the UI has exited: the saved title goes back through the real terminal
        real.write.assert_called_once_with(self.module.POP)


class ProjectBadgeTest(BundledExtensionTest):
    def setUp(self):
        super().setUp()
        self.module = load_module(MARKETPLACE / "widgets" / "project_badge.py")
        self.settings = dict(self.module.DEFAULTS)

    def text(self, lines):
        return ["".join(piece[0] for piece in line) for line in lines]

    def test_parse_status_reads_branch_tracking_and_dirtiness(self):
        parse = self.module.parse_status
        self.assertEqual(
            parse("## main...origin/main [ahead 2, behind 1]\n M a.py\n"),
            {"branch": "main", "dirty": True, "ahead": 2, "behind": 1},
        )
        self.assertEqual(
            parse("## feat/x...origin/feat/x [behind 3]\n"),
            {"branch": "feat/x", "dirty": False, "ahead": 0, "behind": 3},
        )
        self.assertEqual(parse("## No commits yet on dev\n")["branch"], "dev")
        self.assertEqual(parse("## HEAD (no branch)\n")["branch"], "detached")
        self.assertIsNone(parse("fatal: not a git repository"))

    def test_badge_shows_project_branch_marker_and_counts(self):
        git = {"branch": "main", "dirty": True, "ahead": 2, "behind": 0}
        lines = self.module.format_badge("/work/shop", git, self.settings)
        rows = self.text(lines)
        self.assertEqual(rows[0], "shop")
        self.assertTrue(rows[1].endswith("main * ↑2"), rows)
        clean = {"branch": "main", "dirty": False, "ahead": 0, "behind": 1}
        self.assertTrue(self.text(self.module.format_badge("/work/shop", clean, self.settings))[1].endswith("main ↓1"))

    def test_each_part_can_be_hidden(self):
        git = {"branch": "main", "dirty": True, "ahead": 2, "behind": 0}
        only_branch = self.module.format_badge("/w/shop", git, {**self.settings, "show_project": False})
        self.assertEqual(len(only_branch), 1)
        self.assertNotIn("shop", self.text(only_branch)[0])
        plain = {**self.settings, "show_dirty": False, "show_ahead_behind": False}
        self.assertTrue(self.text(self.module.format_badge("/w/shop", git, plain))[1].endswith("main"))
        no_branch = self.module.format_badge("/w/shop", git, {**self.settings, "show_branch": False})
        self.assertEqual(self.text(no_branch), ["shop"])

    def test_outside_a_repository_only_the_project_shows_and_nothing_shows_if_hidden(self):
        self.assertEqual(self.text(self.module.format_badge("/w/shop", None, self.settings)), ["shop"])
        self.assertIsNone(self.module.format_badge("/w/shop", None, {**self.settings, "show_project": False}))

    def test_rows_fit_the_width_and_control_characters_are_removed(self):
        git = {"branch": "feature/" + "b" * 80 + "\x1b[31m", "dirty": True, "ahead": 12, "behind": 3}
        settings = {**self.settings, "width": 20}
        rows = self.text(self.module.format_badge("/w/" + "p" * 60 + "\x07", git, settings))
        self.assertTrue(all(len(row) <= 20 for row in rows), rows)
        self.assertTrue(rows[0].endswith("…"))
        self.assertIn("…", rows[1])
        self.assertTrue(rows[1].endswith("* ↑12 ↓3"), rows)
        self.assertFalse(any("\x1b" in row or "\x07" in row for row in rows))

    def test_settings_problems_name_the_fix(self):
        check = self.module.check_settings
        self.assertEqual(check(self.settings), [])
        self.assertIn("one of top, bottom, sidebar", check({**self.settings, "area": "left"})[0])
        self.assertIn("at least 8", check({**self.settings, "width": 3})[0])
        both = {**self.settings, "show_project": False, "show_branch": False}
        self.assertIn("empty", check(both)[0])
        with self.assertRaisesRegex(ValueError, "project_badge settings: 'area' must be one of"):
            fake_extension(self.module, area="nowhere")
        (panel,) = fake_extension(self.module, area="bottom").panels
        self.assertEqual((panel[0], panel[2]), ("project_badge", "bottom"))

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_read_git_sees_a_real_repository_and_tolerates_absence(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        self.assertIsNone(self.module.read_git(str(repo)))
        self.assertIsNone(self.module.read_git(str(self.tmp / "missing")))
        init_repo(repo)
        self.assertEqual(self.module.read_git(str(repo)), {"branch": "main", "dirty": False, "ahead": 0, "behind": 0})
        (repo / "f.txt").write_text("x")
        self.assertTrue(self.module.read_git(str(repo))["dirty"])
        with mock.patch.object(self.module.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 2)):
            self.assertIsNone(self.module.read_git(str(repo)))
