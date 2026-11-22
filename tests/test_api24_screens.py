"""API 2.4 screens: ``ctx.ui.screen``, ``pick`` and ``overlay``, ``timeout`` on every ``ctx.ui`` question,
``ctx.transcript`` and the ``ui.preview_lines`` setting."""

import asyncio
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static

from minimal import config
from minimal.core import session as session_mod
from minimal.core.app import App, ConsoleFrontend
from minimal.core.context import Transcript
from minimal.core.host import Host
from minimal.core.session import Session
from minimal.extensions import loader
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply
from minimal.ui.commands import register_builtin_commands
from minimal.ui.tui.app import MinimalTui
from minimal.ui.tui.composer import Composer
from minimal.ui.tui.screens import ChecklistScreen, ChoiceScreen, ConfirmScreen, OverlayScreen, PickScreen, TextScreen, fuzzy_match
from minimal.ui.tui.widgets import ToolBlock
from tests.test_agents import AgentTest

MESSAGES = [
    {"role": "user", "content": "first question"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me look. NEEDLE here"},
            {"type": "tool_use", "id": "c1", "name": "read", "input": {"path": "notes.txt"}},
        ],
    },
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "contents", "is_error": False}]},
    {"role": "assistant", "content": [{"type": "text", "text": "answer one"}]},
    {"role": "user", "content": "second question"},
    {"role": "assistant", "content": [{"type": "text", "text": "answer two"}]},
]


class RecordingFrontend:
    """A frontend that records what the ``ctx.ui`` methods pass it; the old ones take no ``timeout``."""

    def __init__(self):
        self.calls = []

    def ask(self, question):
        self.calls.append(("ask", question))
        return True

    def choose(self, title, options, timeout=None):
        self.calls.append(("choose", title, timeout))
        return "v"

    def choose_many(self, title, options, timeout=None):
        self.calls.append(("choose_many", title, timeout))
        return []

    def ask_text(self, title, timeout=None):
        self.calls.append(("ask_text", title, timeout))
        return "t"

    def screen(self, factory, timeout=None):
        self.calls.append(("screen", factory, timeout))
        return "s"

    def pick(self, title, items, *, preview=None, on_highlight=None, filter=True, timeout=None):
        self.calls.append(("pick", title, items, filter, timeout))
        return "p"

    def overlay(self, content, title="", timeout=None):
        self.calls.append(("overlay", content, title, timeout))


class Tty(io.StringIO):
    """A stdout that is a terminal."""

    def isatty(self):
        return True


class UiArgumentsTest(AgentTest):
    def setUp(self):
        super().setUp()
        self.fe = RecordingFrontend()
        self.app.frontend = self.fe
        self.ui = self.app.context.ui

    def test_a_question_without_timeout_passes_none_so_frontends_written_before_2_4_keep_working(self):
        self.assertTrue(self.ui.ask("ok?"))
        self.assertEqual(self.fe.calls, [("ask", "ok?")])  # RecordingFrontend.ask has no timeout parameter

    def test_timeout_reaches_the_frontend_as_seconds(self):
        self.ui.choose("t", [("a", "A")], timeout=2)
        self.ui.choose_many("t", [("a", "A", True)], timeout=1.5)
        self.ui.ask_text("t", timeout=3)
        self.ui.screen(lambda dismiss: None, timeout=4)
        self.assertEqual(
            [c[-1] for c in self.fe.calls], [2.0, 1.5, 3.0, 4.0]
        )

    def test_a_timeout_that_is_not_a_positive_finite_number_is_refused(self):
        for bad in (0, -1, True, "5", float("inf"), float("nan")):
            with self.subTest(timeout=bad), self.assertRaisesRegex(ValueError, "timeout must be a number of seconds > 0"):
                self.ui.ask_text("t", timeout=bad)
        self.assertEqual(self.fe.calls, [])

    def test_a_huge_timeout_is_clamped_to_what_a_thread_can_wait(self):
        self.ui.choose("t", [], timeout=1e30)
        self.assertEqual(self.fe.calls[0][-1], threading.TIMEOUT_MAX)

    def test_screen_needs_a_callable(self):
        with self.assertRaises(TypeError):
            self.ui.screen("not callable")

    def test_pick_normalises_items_and_shows_nothing_when_there_are_none(self):
        self.assertEqual(self.ui.pick("t", [("a", "A"), ("b", "B", "detail")], filter=False, timeout=5), "p")
        self.assertEqual(self.fe.calls, [("pick", "t", [("a", "A", ""), ("b", "B", "detail")], False, 5.0)])
        self.fe.calls.clear()
        self.assertIsNone(self.ui.pick("t", []))
        self.assertEqual(self.fe.calls, [])

    def test_pick_refuses_bad_items_and_repeated_values(self):
        for items in ([("a",)], [("a", "A", "d", "x")], [("a", 1)], [["a", "A"]], ["ab"]):
            with self.subTest(items=items), self.assertRaisesRegex(ValueError, "pick: items must be"):
                self.ui.pick("t", items)
        with self.assertRaisesRegex(ValueError, "'a' is listed twice"):
            self.ui.pick("t", [("a", "A"), ("a", "Again")])

    def test_overlay_takes_text_or_styled_lines_and_checks_the_styles(self):
        self.ui.overlay("plain\ntext", "Help", timeout=9)
        self.ui.overlay([["a ", ("b", "bold $accent")], "c"])
        self.assertEqual(self.fe.calls[0], ("overlay", "plain\ntext", "Help", 9.0))
        self.assertEqual(self.fe.calls[1][1], [[("a ", ""), ("b", "bold $accent")], [("c", "")]])
        with self.assertRaisesRegex(ValueError, "unknown style"):
            self.ui.overlay([("x", "$nonsense")])


class ConsoleTest(unittest.TestCase):
    def test_a_custom_screen_needs_the_terminal_ui(self):
        with self.assertRaisesRegex(RuntimeError, "needs the terminal UI"):
            ConsoleFrontend().screen(lambda dismiss: None)

    def test_pick_is_a_numbered_list_with_the_detail_after_the_label(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("builtins.input", return_value="2"):
            picked = ConsoleFrontend().pick("Which?", [("a", "Alpha", ""), ("b", "Beta", "second")], preview=len)
        self.assertEqual(picked, "b")
        self.assertIn("2. Beta  second", out.getvalue())

    def test_overlay_prints_the_text_and_styled_lines_without_their_styles(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ConsoleFrontend().overlay("hello", "Title")
            ConsoleFrontend().overlay([[("a", "bold"), ("b", "")], [("c", "")]])
        self.assertEqual(out.getvalue(), "\nTitle\nhello\nab\nc\n")

    def test_a_console_has_no_transcript(self):
        console = ConsoleFrontend()
        self.assertFalse(console.transcript_scroll_to(0))
        console.transcript_mark(0, "x")
        console.transcript_unmark(None)
        self.assertEqual(console.transcript_marks(), [])

    def test_the_timeout_ends_a_question_nobody_answers_when_stdin_is_a_terminal(self):
        stdin = SimpleNamespace(isatty=lambda: True)
        with (
            mock.patch("sys.stdin", stdin),
            mock.patch("sys.stdout", Tty()),
            mock.patch("minimal.core.app.select.select", return_value=([], [], [])) as select,
        ):
            console = ConsoleFrontend()
            self.assertFalse(console.ask("sure?", timeout=0.5))
            self.assertIsNone(console.ask_text("name", timeout=0.5))
            self.assertIsNone(console.choose("t", [("a", "A")], timeout=0.5))
            self.assertIsNone(console.choose_many("t", [("a", "A", True)], timeout=0.5))
        self.assertEqual(select.call_args.args[3], 0.5)

    def test_a_line_that_arrives_in_time_is_the_answer(self):
        stdin = SimpleNamespace(isatty=lambda: True, readline=lambda: "yes\n")
        with (
            mock.patch("sys.stdin", stdin),
            mock.patch("sys.stdout", Tty()),
            mock.patch("minimal.core.app.select.select", return_value=([stdin], [], [])),
        ):
            self.assertTrue(ConsoleFrontend().ask("sure?", timeout=5))

    def test_without_a_terminal_the_timeout_is_ignored(self):
        with mock.patch("builtins.input", return_value="x"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ConsoleFrontend().ask_text("name", timeout=0.01), "x")


class FuzzyTest(unittest.TestCase):
    def test_terms_are_case_insensitive_subsequences_and_every_term_must_match(self):
        self.assertIsNotNone(fuzzy_match("FB", "foo_bar"))
        self.assertIsNone(fuzzy_match("bf", "foo_bar"))
        self.assertIsNotNone(fuzzy_match("foo bar", "bar and foo"))
        self.assertIsNone(fuzzy_match("foo baz", "bar and foo"))
        self.assertEqual(fuzzy_match("", "anything"), (1.0, []))
        score, offsets = fuzzy_match("fb", "foo_bar")
        self.assertEqual(offsets, [0, 4])
        self.assertGreater(fuzzy_match("foo", "foo")[0], fuzzy_match("foo", "f_o_o")[0])


class TranscriptSearchTest(AgentTest):
    def setUp(self):
        super().setUp()
        self.app.messages.extend(json.loads(json.dumps(MESSAGES)))
        self.transcript = self.app.context.transcript

    def test_find_searches_text_tool_calls_and_results_case_insensitively(self):
        self.assertIsInstance(self.transcript, Transcript)
        self.assertEqual(self.transcript.find("needle"), [1])
        self.assertEqual(self.transcript.find("NOTES.TXT"), [1])  # the tool call's input
        self.assertEqual(self.transcript.find("contents"), [2])  # a tool result
        self.assertEqual(self.transcript.find("question"), [0, 4])
        self.assertEqual(self.transcript.find("absent"), [])

    def test_find_with_a_regex_is_case_sensitive_unless_it_says_otherwise(self):
        self.assertEqual(self.transcript.find(r"answer (one|two)", regex=True), [3, 5])
        self.assertEqual(self.transcript.find(r"ANSWER", regex=True), [])
        self.assertEqual(self.transcript.find(r"(?i)ANSWER", regex=True), [3, 5])
        self.assertEqual(self.transcript.find("a.swer", regex=False), [])  # not a pattern then

    def test_find_refuses_an_empty_text_and_a_broken_pattern(self):
        with self.assertRaisesRegex(ValueError, "text is empty"):
            self.transcript.find("")
        with self.assertRaisesRegex(ValueError, "invalid regular expression"):
            self.transcript.find("(", regex=True)

    def test_find_ignores_thinking_and_sees_a_message_added_since(self):
        self.app.messages.append(
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "secret"}, {"type": "text", "text": "late"}]}
        )
        self.assertEqual(self.transcript.find("secret"), [])
        self.assertEqual(self.transcript.find("late"), [6])

    def test_indexes_are_checked(self):
        for bad in (-1, 6, True, "1", 1.0):
            with self.subTest(index=bad), self.assertRaisesRegex(ValueError, "message_index"):
                self.transcript.mark(bad)
        self.assertFalse(self.transcript.scroll_to(99))  # out of range: no such block
        self.assertFalse(self.transcript.scroll_to(-1))
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            self.transcript.scroll_to("1")

    def test_marks_and_scrolling_go_to_the_frontend(self):
        fe = mock.Mock()
        fe.transcript_scroll_to.return_value = True
        fe.transcript_marks.return_value = [(1, "x")]
        self.app.frontend = fe
        self.transcript.mark(1, "x")
        self.transcript.mark(2)
        self.transcript.unmark(1)
        self.transcript.unmark()
        self.assertTrue(self.transcript.scroll_to(3))
        self.assertEqual(self.transcript.marks(), [(1, "x")])
        self.assertEqual(
            [c.args for c in fe.method_calls if c[0] != "transcript_marks"],
            [(1, "x"), (2, ""), (1,), (None,), (3,)],
        )


class TuiScreensTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        (self.tmp / "notes.txt").write_text("contents\n")
        for patcher in (
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.tmp / "global"),
            mock.patch.object(config, "SETTINGS_FILE", self.tmp / "settings.json"),
            mock.patch.object(config, "TRUST_FILE", self.tmp / "trusted.json"),
            mock.patch.object(session_mod, "SESSIONS_DIR", self.tmp / "sessions"),
            mock.patch.object(config, "HOME_DIR", self.tmp / "home"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.core = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.tmp),
            auto_approve=False,
            register_commands=register_builtin_commands,
        )
        self.core.reload_extensions()
        self.core.provider_name, self.core.model = "fake", "m"

    def start(self, messages=()) -> MinimalTui:
        self.core.messages.extend(json.loads(json.dumps(list(messages))))
        return MinimalTui(Host(self.core))

    def worker(self, fn, *args, **kwargs):
        """Run ``fn`` (a ``ctx`` call that waits for the UI) in a thread, as a task to await."""
        return asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))

    async def wait_until(self, pilot, condition):
        for _ in range(100):
            await pilot.pause(0.05)
            if condition():
                return
        self.fail("timed out")

    async def result(self, task):
        return await asyncio.wait_for(task, 5)

    def shown(self, app, screen_type):
        return lambda: isinstance(app.screen, screen_type)

    # --- pick -----------------------------------------------------------------

    ITEMS = [("apple", "Apple", "fruit"), ("banana", "Banana", "yellow fruit"), ("apricot", "Apricot"), ("cherry", "Cherry")]

    async def test_typing_filters_and_enter_returns_the_best_match(self):
        app = self.start()
        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.pick, "Fruit", self.ITEMS)
            await self.wait_until(pilot, self.shown(app, PickScreen))
            self.assertEqual(app.screen.listed, ["apple", "banana", "apricot", "cherry"])  # the order given
            await self.wait_until(pilot, lambda: isinstance(app.focused, Input))
            await pilot.press("b", "n", "n")  # fuzzy: B-a-N-a-N-a
            await self.wait_until(pilot, lambda: app.screen.listed == ["banana"])
            await pilot.press("enter")
            self.assertEqual(await self.result(task), "banana")
            self.assertNotIsInstance(app.screen, PickScreen)

    async def test_the_detail_is_matched_too_and_ranking_puts_the_closest_first(self):
        app = self.start()
        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.pick, "Fruit", self.ITEMS)
            await self.wait_until(pilot, self.shown(app, PickScreen))
            await pilot.press(*"yellow")
            await self.wait_until(pilot, lambda: app.screen.listed == ["banana"])
            for _ in range(6):
                await pilot.press("backspace")
            await pilot.press(*"ap")
            await self.wait_until(pilot, lambda: set(app.screen.listed) == {"apple", "apricot"})
            await pilot.press("escape")
            self.assertIsNone(await self.result(task))

    async def test_arrow_keys_move_the_highlight_and_escape_cancels(self):
        app = self.start()
        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.pick, "Fruit", self.ITEMS)
            await self.wait_until(pilot, self.shown(app, PickScreen))
            await pilot.press("down", "down", "up")
            await pilot.press("enter")
            self.assertEqual(await self.result(task), "banana")
            task = self.worker(app.host.ctx.ui.pick, "Fruit", self.ITEMS)
            await self.wait_until(pilot, self.shown(app, PickScreen))
            await pilot.press("escape")
            self.assertIsNone(await self.result(task))

    async def test_enter_with_no_match_keeps_the_dialog_open(self):
        app = self.start()
        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.pick, "Fruit", self.ITEMS)
            await self.wait_until(pilot, self.shown(app, PickScreen))
            await pilot.press(*"zzz")
            await self.wait_until(pilot, lambda: app.screen.listed == [])
            await pilot.press("enter")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, PickScreen)
            self.assertFalse(task.done())
            await pilot.press("escape")
            self.assertIsNone(await self.result(task))

    async def test_on_highlight_runs_on_the_ui_thread_for_each_new_highlighted_row_the_first_included(self):
        app = self.start()
        seen: list[tuple[bool, str]] = []

        def on_highlight(value):
            seen.append((threading.get_ident() == app.ui_thread, value))

        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.pick, "Fruit", self.ITEMS, on_highlight=on_highlight)
            await self.wait_until(pilot, lambda: seen)
            self.assertEqual(seen, [(True, "apple")])
            await pilot.press("down")
            await self.wait_until(pilot, lambda: len(seen) == 2)
            await pilot.press(*"che")  # the filter makes cherry the first row
            await self.wait_until(pilot, lambda: seen[-1][1] == "cherry")
            await pilot.press("escape")
            await self.result(task)
        self.assertEqual([v for _, v in seen[:2]], ["apple", "banana"])
        self.assertTrue(all(on_ui for on_ui, _ in seen))
        self.assertEqual(seen[-1][1], "cherry")
        self.assertEqual(
            [v for _, v in seen].count("cherry"), 1
        )  # typing "c", "h", "e" kept cherry first: it is reported once, not once per keystroke

    async def test_a_failing_on_highlight_is_reported_once_and_does_not_close_the_dialog(self):
        app = self.start()
        calls = []

        def on_highlight(value):
            calls.append(value)
            raise RuntimeError("preview broke")

        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.pick, "Fruit", self.ITEMS, on_highlight=on_highlight)
            await self.wait_until(pilot, lambda: calls)
            await pilot.press("down")
            await self.wait_until(pilot, lambda: len(calls) == 2)
            self.assertIsInstance(app.screen, PickScreen)
            self.assertEqual(len(app._notifications), 1)
            self.assertIn("preview broke", next(iter(app._notifications)).message)
            await pilot.press("enter")
            self.assertEqual(await self.result(task), "banana")

    async def test_the_preview_pane_follows_the_highlight(self):
        app = self.start()
        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.pick, "Fruit", self.ITEMS, preview=lambda v: f"about {v}\n[not markup]")
            await self.wait_until(pilot, self.shown(app, PickScreen))
            pane = lambda: str(app.screen.query_one("#pick-preview", Static).render())  # noqa: E731
            await self.wait_until(pilot, lambda: "about apple" in pane())
            self.assertIn("[not markup]", pane())
            await pilot.press("down")
            await self.wait_until(pilot, lambda: "about banana" in pane())
            await pilot.press("escape")
            await self.result(task)

    async def test_a_failing_preview_shows_in_the_pane(self):
        app = self.start()

        def preview(value):
            raise RuntimeError("no preview")

        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.pick, "Fruit", self.ITEMS, preview=preview)
            await self.wait_until(pilot, self.shown(app, PickScreen))
            await self.wait_until(pilot, lambda: "no preview" in str(app.screen.query_one("#pick-preview", Static).render()))
            await pilot.press("escape")
            await self.result(task)

    async def test_without_the_filter_box_the_list_has_the_focus(self):
        app = self.start()
        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.pick, "Fruit", self.ITEMS, filter=False)
            await self.wait_until(pilot, self.shown(app, PickScreen))
            self.assertEqual(len(app.screen.query(Input)), 0)
            await self.wait_until(pilot, lambda: isinstance(app.focused, OptionList))
            await pilot.press("down", "down", "enter")
            self.assertEqual(await self.result(task), "apricot")

    async def test_a_long_list_shows_the_first_rows_and_says_so(self):
        app = self.start()
        items = [(f"v{i}", f"item {i}") for i in range(1000)]
        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.pick, "Many", items)
            await self.wait_until(pilot, self.shown(app, PickScreen))
            self.assertEqual(len(app.screen.listed), 300)
            self.assertIn("first 300 of 1000", str(app.screen.query_one("#pick-hint", Label).render()))
            await pilot.press(*"item 999")
            await self.wait_until(pilot, lambda: app.screen.listed[:1] == ["v999"])
            await pilot.press("enter")
            self.assertEqual(await self.result(task), "v999")

    async def test_a_dialog_with_a_preview_pane_is_wider(self):
        app = self.start()
        async with app.run_test(size=(200, 40)) as pilot:
            widths = []
            for preview in (None, str):
                task = self.worker(app.host.ctx.ui.pick, "Fruit", self.ITEMS, preview=preview)
                await self.wait_until(pilot, self.shown(app, PickScreen))
                await pilot.pause(0.1)
                widths.append(app.screen.query_one("Vertical").region.width)
                await pilot.press("escape")
                await self.result(task)
            self.assertEqual(widths, [100, 160])

    # --- timeouts -------------------------------------------------------------

    async def test_every_question_returns_what_a_cancel_returns_when_the_timeout_passes(self):
        app = self.start()
        ui = app.host.ctx.ui
        async with app.run_test() as pilot:
            cases = [
                (ConfirmScreen, False, ui.ask, ("q",)),
                (ChoiceScreen, None, ui.choose, ("t", [("a", "A")])),
                (ChecklistScreen, None, ui.choose_many, ("t", [("a", "A", True)])),
                (TextScreen, None, ui.ask_text, ("t",)),
                (PickScreen, None, ui.pick, ("t", [("a", "A")])),
                (OverlayScreen, None, ui.overlay, ("text", "title")),
            ]
            for screen_type, expected, method, args in cases:
                with self.subTest(method=method.__name__):
                    task = self.worker(method, *args, timeout=0.3)
                    await self.wait_until(pilot, self.shown(app, screen_type))
                    self.assertIs(await self.result(task), expected)
                    self.assertNotIsInstance(app.screen, screen_type)
            self.assertEqual(app.pending_modals, set())

    async def test_a_screen_times_out_with_none(self):
        app = self.start()

        class Mine(ModalScreen):
            pass

        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.screen, lambda dismiss: Mine(), timeout=0.3)
            await self.wait_until(pilot, self.shown(app, Mine))
            self.assertIsNone(await self.result(task))
            self.assertNotIsInstance(app.screen, Mine)

    async def test_an_answer_before_the_timeout_wins_and_a_late_timeout_does_nothing(self):
        app = self.start()
        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.choose, "t", [("a", "A"), ("b", "B")], timeout=0.6)
            await self.wait_until(pilot, self.shown(app, ChoiceScreen))
            await pilot.press("down", "enter")
            self.assertEqual(await self.result(task), "b")
            await pilot.pause(0.8)  # the timeout would have fired by now
            self.assertNotIsInstance(app.screen, ChoiceScreen)

    async def test_a_timeout_waits_until_the_screen_is_on_top_before_closing_it(self):
        app = self.start()

        class Cover(ModalScreen):
            pass

        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.ask_text, "t", timeout=0.2)
            await self.wait_until(pilot, self.shown(app, TextScreen))
            await app.push_screen(Cover())
            await pilot.pause(0.6)
            self.assertFalse(task.done())  # the covered dialog was not torn down under the other screen
            self.assertIsInstance(app.screen, Cover)
            app.pop_screen()
            self.assertIsNone(await self.result(task))
            self.assertNotIsInstance(app.screen, (Cover, TextScreen))

    async def test_quitting_wakes_a_worker_waiting_for_an_answer(self):
        app = self.start()
        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.pick, "Fruit", self.ITEMS)
            await self.wait_until(pilot, self.shown(app, PickScreen))
            self.assertEqual(len(app.pending_modals), 1)
            app.exit()  # the test harness unmounts the app when the block ends
        self.assertIsNone(await self.result(task))

    async def test_quitting_wakes_every_waiting_worker_and_a_timeout_during_quit_is_harmless(self):
        app = self.start()
        async with app.run_test() as pilot:
            first = self.worker(app.host.ctx.ui.ask, "one?")
            await self.wait_until(pilot, self.shown(app, ConfirmScreen))
            second = self.worker(app.host.ctx.ui.choose, "two", [("a", "A")], timeout=30)
            await self.wait_until(pilot, lambda: len(app.pending_modals) == 2)
            app.exit()
        self.assertFalse(await self.result(first))
        self.assertIsNone(await self.result(second))

    # --- screen ---------------------------------------------------------------

    async def test_a_custom_screen_returns_what_it_dismisses_with(self):
        app = self.start()
        built_on = []

        class Mine(ModalScreen):
            BINDINGS = [Binding("y", "yes", "Yes")]

            def compose(self) -> ComposeResult:
                yield Label("mine")

            def action_yes(self):
                self.dismiss({"answer": 42})

        def factory(dismiss):
            built_on.append(threading.get_ident())
            return Mine()

        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.screen, factory)
            await self.wait_until(pilot, self.shown(app, Mine))
            self.assertEqual(built_on, [app.ui_thread])
            await pilot.press("y")
            self.assertEqual(await self.result(task), {"answer": 42})

    async def test_dismiss_works_from_the_screen_and_from_another_thread_and_only_once(self):
        app = self.start()
        dismissers = []

        class Mine(ModalScreen):
            def key_x(self):
                dismissers[0]("from the screen")
                dismissers[0]("twice")  # the second one is ignored

        def factory(dismiss):
            dismissers.append(dismiss)
            return Mine()

        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.screen, factory)
            await self.wait_until(pilot, self.shown(app, Mine))
            await pilot.press("x")
            self.assertEqual(await self.result(task), "from the screen")
            task = self.worker(app.host.ctx.ui.screen, factory)
            await self.wait_until(pilot, lambda: len(dismissers) == 2 and isinstance(app.screen, Mine))
            await asyncio.to_thread(dismissers[1], "from a thread")
            self.assertEqual(await self.result(task), "from a thread")
            await asyncio.to_thread(dismissers[1], "after it closed")  # nothing to close: no error
            self.assertNotIsInstance(app.screen, Mine)

    async def test_dismiss_without_a_value_gives_none(self):
        app = self.start()
        holder = []

        class Mine(ModalScreen):
            pass

        def factory(dismiss):
            holder.append(dismiss)
            return Mine()

        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.screen, factory)
            await self.wait_until(pilot, self.shown(app, Mine))
            await asyncio.to_thread(holder[0])
            self.assertIsNone(await self.result(task))

    async def test_a_factory_that_raises_reaches_the_caller_and_leaves_nothing_waiting(self):
        app = self.start()

        def factory(dismiss):
            raise KeyError("broken factory")

        async with app.run_test():
            task = self.worker(app.host.ctx.ui.screen, factory)
            with self.assertRaises(KeyError):
                await self.result(task)
            self.assertEqual(app.pending_modals, set())

    async def test_from_the_ui_thread_a_screen_cannot_be_shown(self):
        app = self.start()
        async with app.run_test():
            with self.assertRaisesRegex(RuntimeError, "needs the terminal UI"):
                app.host.ctx.ui.screen(lambda dismiss: None)  # a terminal command's thread: it would deadlock

    # --- overlay --------------------------------------------------------------

    async def test_an_overlay_scrolls_and_escape_closes_it(self):
        app = self.start()
        text = "\n".join(f"line {i}" for i in range(100))
        async with app.run_test(size=(100, 30)) as pilot:
            task = self.worker(app.host.ctx.ui.overlay, text, "Help")
            await self.wait_until(pilot, self.shown(app, OverlayScreen))
            body = app.screen.query_one("#overlay-body")
            self.assertEqual(body.scroll_y, 0)
            await self.wait_until(pilot, lambda: app.focused is body)
            await pilot.press("pagedown")
            await self.wait_until(pilot, lambda: body.scroll_y > 0)
            moved = body.scroll_y
            await pilot.press("down", "j")
            await self.wait_until(pilot, lambda: body.scroll_y > moved)
            await pilot.press("end")
            await self.wait_until(pilot, lambda: body.scroll_y == body.max_scroll_y)
            await pilot.press("home")
            await self.wait_until(pilot, lambda: body.scroll_y == 0)
            await pilot.press("escape")
            self.assertIsNone(await self.result(task))
            self.assertNotIsInstance(app.screen, OverlayScreen)

    async def test_q_also_closes_an_overlay_and_square_brackets_are_not_markup(self):
        app = self.start()
        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.overlay, "see [bold]this[/bold]")
            await self.wait_until(pilot, self.shown(app, OverlayScreen))
            self.assertIn("[bold]this[/bold]", str(app.screen.query_one("#overlay-body").query_one(Static).render()))
            await pilot.press("q")
            self.assertIsNone(await self.result(task))

    async def test_an_overlay_shows_styled_lines_with_theme_styles(self):
        app = self.start()
        lines = [[("added ", "$diff-added"), ("name", "bold $accent")], "plain"]
        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.overlay, lines, "Diff")
            await self.wait_until(pilot, self.shown(app, OverlayScreen))
            shown = app.screen.query_one("#overlay-body").query_one(Static).render()
            self.assertEqual(str(shown), "added name\nplain")
            self.assertTrue(shown.spans)  # styled, not plain text
            self.assertIn("Diff", "".join(str(label.render()) for label in app.screen.query(Label)))
            await pilot.press("escape")
            await self.result(task)

    async def test_an_overlay_takes_textual_content_but_not_other_things(self):
        from textual.content import Content

        app = self.start()
        async with app.run_test() as pilot:
            task = self.worker(app.host.ctx.ui.overlay, Content.styled("rich", "bold"))
            await self.wait_until(pilot, self.shown(app, OverlayScreen))
            await pilot.press("escape")
            await self.result(task)
            with self.assertRaisesRegex(TypeError, "expected text"):
                await self.result(self.worker(app.host.ctx.ui.overlay, 42))

    # --- transcript -----------------------------------------------------------

    def marked(self, app):
        return [w for w in app.query(".-marked")]

    async def test_marking_a_message_marks_its_block_and_a_tool_message_marks_its_tool_block(self):
        app = self.start(MESSAGES)
        transcript = app.host.ctx.transcript
        async with app.run_test() as pilot:
            await asyncio.to_thread(transcript.mark, 0, "the question")
            await asyncio.to_thread(transcript.mark, 2, "the result")  # a tool_result message: the call's block
            await pilot.pause()
            blocks = self.marked(app)
            self.assertEqual(len(blocks), 2)
            self.assertEqual(blocks[0].classes - {"-marked"}, {"user"})
            self.assertIsInstance(blocks[1], ToolBlock)
            self.assertEqual(await asyncio.to_thread(transcript.marks), [(0, "the question"), (2, "the result")])
            self.assertEqual(str(blocks[0].tooltip), "the question")

    async def test_a_message_with_text_and_a_call_marks_its_text_and_marking_again_replaces_the_label(self):
        app = self.start(MESSAGES)
        transcript = app.host.ctx.transcript
        async with app.run_test() as pilot:
            await asyncio.to_thread(transcript.mark, 1, "a")
            await asyncio.to_thread(transcript.mark, 1, "b")
            await pilot.pause()
            self.assertEqual([b.__class__.__name__ for b in self.marked(app)], ["TranscriptMarkdown"])
            self.assertEqual(await asyncio.to_thread(transcript.marks), [(1, "b")])

    async def test_unmark_removes_one_or_all(self):
        app = self.start(MESSAGES)
        transcript = app.host.ctx.transcript
        async with app.run_test() as pilot:
            for index in (0, 1, 2):  # the question, the assistant's text, the tool call's block
                await asyncio.to_thread(transcript.mark, index)
            await pilot.pause()
            self.assertEqual(len(self.marked(app)), 3)
            await asyncio.to_thread(transcript.unmark, 1)
            await asyncio.to_thread(transcript.unmark, 1)  # nothing happens
            await pilot.pause()
            self.assertEqual(len(self.marked(app)), 2)
            await asyncio.to_thread(transcript.unmark)
            await pilot.pause()
            self.assertEqual(self.marked(app), [])
            self.assertEqual(await asyncio.to_thread(transcript.marks), [])

    async def test_a_tool_call_and_its_result_share_one_block_which_stays_marked_until_both_marks_go(self):
        app = self.start(self.TOOL_MESSAGES)  # 1: the call alone, 2: its result
        transcript = app.host.ctx.transcript
        async with app.run_test() as pilot:
            await asyncio.to_thread(transcript.mark, 1)
            await asyncio.to_thread(transcript.mark, 2)
            await pilot.pause()
            self.assertEqual(len(self.marked(app)), 1)
            await asyncio.to_thread(transcript.unmark, 1)
            await pilot.pause()
            self.assertEqual(len(self.marked(app)), 1)
            await asyncio.to_thread(transcript.unmark, 2)
            await pilot.pause()
            self.assertEqual(self.marked(app), [])

    async def test_scroll_to_brings_a_block_into_view_and_reports_whether_it_has_one(self):
        many = [m for i in range(40) for m in (
            {"role": "user", "content": f"question {i}"},
            {"role": "assistant", "content": [{"type": "text", "text": f"answer {i}"}]},
        )]
        app = self.start(many)
        transcript = app.host.ctx.transcript
        async with app.run_test(size=(100, 30)) as pilot:
            body = app.query_one("#transcript")
            await self.wait_until(pilot, lambda: body.max_scroll_y > 0)
            body.scroll_end(animate=False)
            await pilot.pause()
            self.assertGreater(body.scroll_y, 0)
            self.assertTrue(await asyncio.to_thread(transcript.scroll_to, 0))
            await self.wait_until(pilot, lambda: abs(app.widget_for_message(0).region.y - body.region.y) <= 1)
            self.assertTrue(await asyncio.to_thread(transcript.scroll_to, 50))
            widget = app.widget_for_message(50)
            await self.wait_until(pilot, lambda: abs(widget.region.y - body.region.y) <= 1)  # at the top of the view
            self.assertFalse(await asyncio.to_thread(transcript.scroll_to, 80))  # past the end

    async def test_messages_before_the_replay_window_have_no_block(self):
        app = self.start(MESSAGES)
        transcript = app.host.ctx.transcript
        with mock.patch("minimal.ui.tui.app.REPLAY_LIMIT", 3):
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertFalse(await asyncio.to_thread(transcript.scroll_to, 0))
                self.assertTrue(await asyncio.to_thread(transcript.scroll_to, 5))
                await asyncio.to_thread(transcript.mark, 0, "hidden")  # kept, nothing to style yet
                self.assertEqual(self.marked(app), [])
                self.assertEqual(await asyncio.to_thread(transcript.marks), [(0, "hidden")])

    def use_replies(self, *replies):
        class Provider:
            name = "fake"

            def __init__(self, replies):
                self.replies = list(replies)

            def stream(self, **kw):
                reply = self.replies.pop(0)
                for block in reply.content:
                    if block["type"] == "text":
                        kw["on_text"](block["text"])
                return reply

        self.core.provider = Provider(replies)

    async def submit(self, pilot, line):
        pilot.app.query_one("#prompt", Composer).set_text(line)
        await pilot.press("enter")
        await self.wait_until(pilot, lambda: not pilot.app.busy)
        await pilot.pause()

    async def test_blocks_drawn_during_a_live_turn_map_to_their_messages_too(self):
        self.use_replies(
            Reply(
                content=[
                    {"type": "text", "text": "looking"},
                    {"type": "tool_use", "id": "c1", "name": "read", "input": {"path": "notes.txt"}},
                ],
                stop_reason="tool_use",
            ),
            Reply(content=[{"type": "text", "text": "done"}], stop_reason="end_turn"),
        )
        app = self.start()
        transcript = app.host.ctx.transcript
        async with app.run_test() as pilot:
            await self.submit(pilot, "hello")
            self.assertEqual([m["role"] for m in self.core.messages], ["user", "assistant", "user", "assistant"])
            names = {}
            for index in range(4):
                await asyncio.to_thread(transcript.mark, index)
                widget = app.widget_for_message(index)
                self.assertIsNotNone(widget, index)
                names[index] = widget
            await pilot.pause()
            self.assertEqual(names[0].classes & {"user"}, {"user"})
            self.assertIn("looking", names[1].source)
            self.assertIsInstance(names[2], ToolBlock)
            self.assertIn("done", names[3].source)
            self.assertIsNot(names[1], names[3])
            self.assertEqual(len(self.marked(app)), 4)  # the question, the first reply's text, the tool block, the answer
            self.assertTrue(await asyncio.to_thread(transcript.scroll_to, 3))

    async def test_truncating_drops_marks_it_cut_and_a_new_session_clears_them_all(self):
        app = self.start(MESSAGES)
        transcript = app.host.ctx.transcript
        async with app.run_test() as pilot:
            for index in (0, 3, 5):
                await asyncio.to_thread(transcript.mark, index, f"m{index}")
            await pilot.pause()
            await self.submit_command(pilot, "/rewind 1")  # cuts before "second question" (message 4)
            self.assertEqual(len(self.core.messages), 4)
            self.assertEqual(await asyncio.to_thread(transcript.marks), [(0, "m0"), (3, "m3")])
            self.assertEqual(len(self.marked(app)), 2)
            await self.submit_command(pilot, "/new")
            self.assertEqual(await asyncio.to_thread(transcript.marks), [])
            self.assertEqual(self.marked(app), [])

    async def submit_command(self, pilot, line):
        pilot.app.submit(line)
        await self.wait_until(pilot, lambda: not pilot.app.busy)
        await pilot.pause()

    # --- ui.preview_lines -----------------------------------------------------

    OUTPUT = "\n".join(f"row {i}" for i in range(12))
    TOOL_MESSAGES = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "grep", "input": {"pattern": "x"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": OUTPUT, "is_error": False}]},
    ]

    def set_preview(self, value):
        (self.tmp / "settings.json").write_text(json.dumps({"ui": {"preview_lines": value}}))
        self.core.reload_extensions()

    async def test_the_default_shows_eight_lines_before_the_fold(self):
        app = self.start(self.TOOL_MESSAGES)
        async with app.run_test() as pilot:
            await pilot.pause()
            shown = str(app.query_one(ToolBlock).render())
            self.assertIn("row 7", shown)
            self.assertNotIn("row 8", shown)
            self.assertIn("… 4 more lines · click to expand", shown)

    async def test_preview_lines_sets_how_many_lines_show_and_a_reload_applies_it_to_blocks_already_drawn(self):
        self.set_preview(3)
        app = self.start(self.TOOL_MESSAGES)
        async with app.run_test() as pilot:
            await pilot.pause()
            block = app.query_one(ToolBlock)
            self.assertIn("… 9 more lines", str(block.render()))
            self.assertNotIn("row 3", str(block.render()))
            (self.tmp / "settings.json").write_text(json.dumps({"ui": {"preview_lines": 20}}))
            await asyncio.to_thread(self.core.reload_extensions)
            await self.wait_until(pilot, lambda: "row 11" in str(block.render()))
            self.assertNotIn("more lines", str(block.render()))  # it all fits: nothing to fold

    async def test_a_tool_result_of_a_live_turn_uses_the_setting_too(self):
        self.set_preview(2)
        self.use_replies(
            Reply(
                content=[{"type": "tool_use", "id": "c1", "name": "read", "input": {"path": "many.txt"}}],
                stop_reason="tool_use",
            ),
            Reply(content=[{"type": "text", "text": "ok"}], stop_reason="end_turn"),
        )
        (self.tmp / "many.txt").write_text("\n".join(f"l{i}" for i in range(10)))
        app = self.start()
        async with app.run_test() as pilot:
            await self.submit(pilot, "read it")
            self.assertIn("… 8 more lines", str(app.query_one(ToolBlock).render()))

    def test_the_setting_must_be_a_positive_integer(self):
        for bad in (0, -3, 2.5, "8", True):
            with self.subTest(value=bad):
                (self.tmp / "settings.json").write_text(json.dumps({"ui": {"preview_lines": bad}}))
                layers, errors = config.read_layers(str(self.tmp))
                self.assertTrue(any("ui.preview_lines" in e for e in errors), errors)
                self.assertNotIn("ui.preview_lines", layers[0].data if layers else {})
        (self.tmp / "settings.json").write_text(json.dumps({"ui": {"preview_lines": 12}}))
        layers, errors = config.read_layers(str(self.tmp))
        self.assertEqual((errors, layers[0].data["ui.preview_lines"]), ([], 12))


if __name__ == "__main__":
    unittest.main()
