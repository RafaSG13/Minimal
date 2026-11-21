"""API 2.4, terminal UI slice: ``terminal`` keybindings, the composer draft, ``ctx.show``, ``builtin_commands()``,
``ctx.git_refresh()``, focus control and the ``height`` of a widget in ``ui.widgets``."""

import asyncio
import contextlib
import io
import json
import stat
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import Static

from minimal import config
from minimal.core.app import App, ConsoleFrontend
from minimal.core.host import Host
from minimal.core.layout import resolve
from minimal.core.session import Session
from minimal.extensions import loader
from minimal.extensions.registry import Registry
from minimal.ui.commands import register_builtin_commands
from minimal.ui.tui.app import MinimalTui
from minimal.ui.tui.composer import Composer

HEADER = '"""{name}."""\n__version__ = "1.0"\n__api__ = "2"\n'


class CoreTest(unittest.TestCase):
    """A temp ``~/.minimal`` and working folder and a real ``App`` with the built-in commands."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.cwd = self.tmp / "work"
        self.cwd.mkdir()
        for patcher in (
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.home / "extensions"),
            mock.patch.object(config, "HOME_DIR", self.home),
            mock.patch.object(config, "SETTINGS_FILE", self.home / "settings.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.core = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.cwd),
            auto_approve=False,
            register_commands=register_builtin_commands,
        )
        self.core.provider_name, self.core.model = "fake", "m"

    def write_settings(self, data: dict) -> None:
        path = config.settings_files(str(self.cwd))["global"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def write_ext(self, name: str, body: str) -> None:
        folder = self.home / "extensions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.py").write_text(HEADER.format(name=name) + textwrap.dedent(body))

    def run_command(self, line: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.core.run_command(line)
        return out.getvalue()


class KeybindingFlagTest(CoreTest):
    def test_terminal_defaults_to_false_and_is_set_by_the_registry_the_facade_and_the_decorator(self):
        self.write_ext(
            "keys",
            """
            def register(minimal):
                @minimal.keybinding("ctrl+o", name="a", terminal=True)
                def a(ctx):
                    "Open the picker"

                minimal.register_keybinding("f6", lambda ctx: None, "plain")
                minimal.register_keybinding("f7", lambda ctx: None, "tty", terminal=True)
            """,
        )
        self.core.reload_extensions()
        keys = Host(self.core).keybindings
        self.assertEqual({k: s.terminal for k, s in keys.items()}, {"ctrl+o": True, "f6": False, "f7": True})
        self.assertEqual(keys["ctrl+o"].description, "Open the picker")
        bound = {b.key: b for b in Host(self.core).key_map()[0] if b.spec is not None}
        self.assertTrue(bound["ctrl+o"].spec.terminal)
        self.assertFalse(bound["f6"].spec.terminal)
        self.assertEqual(self.core.context.keybindings()[-1].source, "keys")  # listings are unchanged

    def test_registry_register_keybinding_takes_terminal_after_the_existing_arguments(self):
        registry = Registry()
        spec = registry.register_keybinding("ctrl+o", lambda ctx: None, "x", show=True, name="n", ext="e", terminal=True)
        self.assertIsNone(spec)
        stored = registry.keybindings["ctrl+o"]
        self.assertEqual((stored.show, stored.name, stored.ext, stored.terminal), (True, "n", "e", True))
        registry.register_keybinding("ctrl+t", lambda ctx: None, "y")
        self.assertFalse(registry.keybindings["ctrl+t"].terminal)


class BuiltinCommandsTest(CoreTest):
    def test_builtin_names_are_known_at_register_time_and_exclude_extension_commands(self):
        self.write_ext(
            "names",
            """
            def register(minimal):
                names = minimal.builtin_commands()
                assert isinstance(names, frozenset), type(names)

                @minimal.command()
                def seen(args, ctx):
                    print(" ".join(sorted(names)))

                minimal.register_command("mine", lambda args, ctx: None)
            """,
        )
        self.core.reload_extensions()
        shown = set(self.run_command("/seen").split())
        self.assertEqual(shown, set(self.core.registry.builtin_commands))
        self.assertTrue({"help", "model", "reload"} <= shown)
        self.assertNotIn("mine", shown)
        self.assertNotIn("seen", shown)

    def test_a_command_that_replaces_a_builtin_does_not_hide_it_from_the_list(self):
        self.write_ext(
            "shadow",
            """
            def register(minimal):
                before = minimal.builtin_commands()
                minimal.register_command("help", lambda args, ctx: print("mine", "help" in before))
            """,
        )
        self.core.reload_extensions()
        self.assertEqual(self.run_command("/help").strip(), "mine True")


class ConsoleContextTest(CoreTest):
    """With no terminal UI the new ``ctx`` members have sane defaults."""

    def test_show_prints_and_validates_and_ignores_blank_text(self):
        ctx = self.core.context
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            ctx.show("one\ntwo")
            ctx.show("bad news", "error")
            ctx.show("  \n")
            ctx.show("esc\x1b[31m")
        self.assertEqual(out.getvalue(), "one\ntwo\nesc\\x1b[31m\n")
        self.assertEqual(err.getvalue(), "bad news\n")
        for bad in (("x", "warning"), ("x", "")):
            with self.assertRaisesRegex(ValueError, "show: kind must be one of output, error"):
                ctx.show(*bad)
        with self.assertRaisesRegex(ValueError, "text must be a str"):
            ctx.show(None)  # type: ignore[arg-type]

    def test_draft_and_focus_do_nothing_in_a_console(self):
        ctx = self.core.context
        self.assertIsInstance(self.core.frontend, ConsoleFrontend)
        ctx.ui.set_draft("hello")
        ctx.ui.set_draft("more", append=True)
        self.assertEqual(ctx.ui.get_draft(), "")
        with self.assertRaisesRegex(ValueError, "text must be a str"):
            ctx.ui.set_draft(5)  # type: ignore[arg-type]
        self.assertFalse(ctx.focus_widget("anything"))
        self.assertFalse(ctx.focus_widget("anything", hold=True))
        ctx.focus_composer()

    def test_git_refresh_pokes_the_poller_only_when_it_runs(self):
        class Poller:
            pokes = 0

            def poke(self):
                self.pokes += 1

        self.core.context.git_refresh()  # no poller yet: nothing to poke, nothing raised
        poller = Poller()
        self.core._git = poller  # type: ignore[assignment]
        self.core.context.git_refresh()
        self.core.context.git_refresh()
        self.assertEqual(poller.pokes, 2)

    def test_a_terminal_command_that_returns_text_has_it_shown_and_a_plain_one_does_not(self):
        shown = []
        self.core.frontend.show = lambda text, kind: shown.append((text, kind))  # type: ignore[method-assign]
        self.core.registry.register_command("tty", lambda args, ctx: f"got {args}", "", True)
        self.core.registry.register_command("plain", lambda args, ctx: "ignored", "", False)
        self.core.registry.register_command("blank", lambda args, ctx: "  ", "", True)
        self.core.registry.register_command("none", lambda args, ctx: 5, "", True)
        for line in ("/tty a b", "/plain", "/blank", "/none"):
            self.run_command(line)
        self.assertEqual(shown, [("got a b", "output")])


class TuiTest(CoreTest, unittest.IsolatedAsyncioTestCase):
    """The real terminal UI under Textual's pilot; the suspend is faked as in the tui_tools tests."""

    def setUp(self):
        super().setUp()
        self.events: list[str] = []
        events = self.events

        @contextlib.contextmanager
        def suspend(self_):
            events.append("suspend")
            try:
                yield
            finally:
                events.append("resume")

        patcher = mock.patch.object(MinimalTui, "suspend", suspend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, setup=None) -> MinimalTui:
        self.core.reload_extensions()
        self.core.registry.register_command("noop", lambda args, ctx: None, "does nothing")
        if setup is not None:
            setup(self.core.registry)
        return MinimalTui(Host(self.core))

    async def wait_until(self, pilot, condition):
        for _ in range(100):
            await pilot.pause(0.05)
            if condition():
                return
        self.fail("timed out")

    async def idle(self, pilot, tui):
        await self.wait_until(pilot, lambda: not tui.busy)
        await pilot.pause()

    async def run_line(self, pilot, line):
        pilot.app.submit(line)
        await self.idle(pilot, pilot.app)

    async def reload(self, pilot):
        await asyncio.to_thread(self.core.reload_extensions)
        await pilot.pause()

    @staticmethod
    def blocks(tui, selector=".output") -> list[str]:
        return [str(s.render()) for s in tui.query(selector).results(Static)]

    @staticmethod
    def composer(tui) -> Composer:
        return tui.query_one("#prompt", Composer)

    # --- terminal keybindings ---------------------------------------------------

    async def test_a_terminal_key_runs_on_the_ui_thread_suspended_and_a_plain_key_on_a_worker(self):
        seen = {}

        def terminal(ctx):
            seen["terminal"] = (threading.get_ident() == tui.ui_thread, list(self.events), tui.busy)

        def plain(ctx):
            seen["plain"] = (threading.get_ident() == tui.ui_thread, list(self.events), tui.busy)

        def setup(registry):
            registry.register_keybinding("f5", terminal, "tty", terminal=True)
            registry.register_keybinding("f6", plain, "plain")

        tui = self.start(setup)
        async with tui.run_test() as pilot:
            await pilot.press("f5")
            await self.idle(pilot, tui)
            await pilot.press("f6")
            await self.idle(pilot, tui)
            self.assertEqual(seen["terminal"], (True, ["suspend"], True))
            self.assertEqual(seen["plain"], (False, ["suspend", "resume"], True))  # the first key's suspend only
            self.assertEqual(self.events, ["suspend", "resume"])
            self.assertIs(tui.focused, self.composer(tui))  # after_action ran

    async def test_a_terminal_key_is_ignored_while_a_turn_or_command_runs(self):
        ran = []
        tui = self.start(lambda r: r.register_keybinding("f5", lambda ctx: ran.append(1), "tty", terminal=True))
        async with tui.run_test() as pilot:
            tui.set_busy(True)
            await pilot.press("f5")
            await pilot.pause(0.2)
            self.assertEqual((ran, self.events), ([], []))
            tui.set_busy(False)

    async def test_what_a_terminal_key_shows_returns_or_raises_is_drawn_after_the_resume_in_order(self):
        inside = {}

        def key(ctx):
            ctx.show("first")
            ctx.show("problem", "error")
            late = threading.Thread(target=lambda: ctx.show("from a thread"))
            late.start()
            late.join(2)  # a worker must not wait for the suspended UI loop
            inside["thread_returned"] = not late.is_alive()
            inside["drawn"] = self.blocks(tui) + self.blocks(tui, ".error")
            return "last"

        tui = self.start(lambda r: r.register_keybinding("f5", key, "tty", terminal=True))
        async with tui.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("f5")
            await self.idle(pilot, tui)
            self.assertTrue(inside["thread_returned"])
            self.assertEqual(inside["drawn"], [])  # nothing drawn while suspended
            transcript = tui.query_one("#transcript")
            drawn = [(str(w.render()), w.has_class("error")) for w in transcript.children if isinstance(w, Static) and (w.has_class("output") or w.has_class("error"))]
            self.assertEqual(
                [d for d in drawn if d[0] in ("first", "problem", "from a thread", "last")],
                [("first", False), ("problem", True), ("from a thread", False), ("last", False)],
            )

    async def test_an_error_in_a_terminal_key_is_shown_once_after_the_resume(self):
        def key(ctx):
            raise RuntimeError("boom")

        tui = self.start(lambda r: r.register_keybinding("f5", key, "Breaks", terminal=True))
        async with tui.run_test() as pilot:
            await pilot.press("f5")
            await self.idle(pilot, tui)
            errors = self.blocks(tui, ".error")
            self.assertEqual(sum("boom" in e for e in errors), 1)
            self.assertIn("[extension] key f5 (Breaks): boom", " ".join(errors))
            self.assertEqual(self.events, ["suspend", "resume"])
            self.assertFalse(tui.busy)

    async def test_a_terminal_key_may_run_an_external_program_and_put_its_choice_in_the_prompt(self):
        script = self.tmp / "fakefzf"
        script.write_text('#!/bin/sh\nprintf "src/main.py\\n"\n')
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        import subprocess

        def pick(ctx):
            chosen = subprocess.run([str(script)], capture_output=True, text=True, check=True).stdout.strip()
            ctx.ui.set_draft("@" + chosen, append=True)

        tui = self.start(lambda r: r.register_keybinding("ctrl+o", pick, "pick", terminal=True))
        async with tui.run_test() as pilot:
            composer = self.composer(tui)
            composer.set_text("look at ")
            await pilot.press("ctrl+o")
            await self.idle(pilot, tui)
            self.assertEqual(composer.text, "look at @src/main.py")
            self.assertEqual(composer.cursor_location, (0, len("look at @src/main.py")))
            self.assertIs(tui.focused, composer)
            self.assertEqual(self.events, ["suspend", "resume"])

    async def test_a_terminal_key_asking_to_exit_closes_the_ui(self):
        from minimal.core.app import ExitRepl

        def key(ctx):
            raise ExitRepl

        tui = self.start(lambda r: r.register_keybinding("f5", key, "bye", terminal=True))
        async with tui.run_test() as pilot:
            await pilot.press("f5")
            await pilot.pause(0.2)
        self.assertEqual(self.events, ["suspend", "resume"])

    # --- terminal commands and ctx.show -------------------------------------------

    async def test_a_terminal_command_returning_text_is_shown_after_the_resume(self):
        def run(args, ctx):
            print("lost in the real terminal")
            return f"result: {args}"

        tui = self.start(lambda r: r.register_command("tty", run, "tty", terminal=True))
        async with tui.run_test() as pilot:
            await self.run_line(pilot, "/tty abc")
            self.assertIn("result: abc", self.blocks(tui))
            self.assertEqual(self.events, ["suspend", "resume"])

    async def test_show_from_a_worker_thread_between_turns_draws_a_block_and_ends_the_model_block(self):
        tui = self.start()
        async with tui.run_test() as pilot:
            ctx = self.core.context
            await asyncio.to_thread(ctx.show, "note\n")
            await asyncio.to_thread(ctx.show, "bad", "error")
            await pilot.pause()
            self.assertIn("note", self.blocks(tui))  # trailing newline trimmed
            self.assertIn("bad", self.blocks(tui, ".error"))

    async def test_show_inside_a_plain_command_keeps_its_place_between_printed_output(self):
        def run(args, ctx):
            print("before")
            ctx.show("shown")
            print("after")

        tui = self.start(lambda r: r.register_command("both", run, "both"))
        async with tui.run_test() as pilot:
            await self.run_line(pilot, "/both")
            texts = [t for t in self.blocks(tui) if t in ("before", "shown", "after")]
            self.assertEqual(texts, ["before", "shown", "after"])

    # --- the draft -------------------------------------------------------------

    async def test_draft_is_read_and_set_from_a_worker_thread_with_the_cursor_at_the_end(self):
        tui = self.start()
        async with tui.run_test() as pilot:
            ui = self.core.context.ui
            composer = self.composer(tui)
            self.assertEqual(await asyncio.to_thread(ui.get_draft), "")
            await asyncio.to_thread(ui.set_draft, "hello")
            await pilot.pause()
            self.assertEqual((await asyncio.to_thread(ui.get_draft), composer.cursor_location), ("hello", (0, 5)))
            await asyncio.to_thread(lambda: ui.set_draft(" world\nline 2", append=True))
            await pilot.pause()
            self.assertEqual(composer.text, "hello world\nline 2")
            self.assertEqual(composer.cursor_location, (1, 6))
            await asyncio.to_thread(ui.set_draft, "")
            await pilot.pause()
            self.assertEqual(composer.text, "")

    async def test_set_draft_is_undoable_and_opens_no_completion_popup(self):
        tui = self.start()
        async with tui.run_test() as pilot:
            composer = self.composer(tui)
            await asyncio.to_thread(self.core.context.ui.set_draft, "/he")
            await pilot.pause(0.2)
            self.assertEqual(composer.text, "/he")
            self.assertEqual(composer.popup.items, [])
            composer.undo()
            await pilot.pause()
            self.assertEqual(composer.text, "")

    async def test_set_draft_ends_a_walk_through_the_history_so_arrows_do_not_replace_it(self):
        tui = self.start()
        async with tui.run_test() as pilot:
            composer = self.composer(tui)
            composer.focus()
            composer.prompts.add("old message")
            composer.set_text("typed")
            await pilot.press("up")
            self.assertEqual(composer.text, "old message")
            await asyncio.to_thread(self.core.context.ui.set_draft, "from an extension")
            await pilot.pause()
            await pilot.press("down")
            self.assertEqual(composer.text, "from an extension")
            await pilot.press("up")  # a new walk starts from the extension's text
            self.assertEqual(composer.text, "old message")
            await pilot.press("down")
            self.assertEqual(composer.text, "from an extension")

    async def test_a_terminal_key_reads_and_writes_the_draft_on_the_ui_thread(self):
        def key(ctx):
            ctx.ui.set_draft(ctx.ui.get_draft().upper() + "!")
            seen.append(ctx.ui.get_draft())

        seen: list[str] = []
        tui = self.start(lambda r: r.register_keybinding("f5", key, "shout", terminal=True))
        async with tui.run_test() as pilot:
            self.composer(tui).set_text("hey")
            await pilot.press("f5")
            await self.idle(pilot, tui)
            self.assertEqual((seen, self.composer(tui).text), (["HEY!"], "HEY!"))

    # --- builtin names with the UI up ---------------------------------------------

    async def test_the_uis_own_commands_are_known_to_extensions_from_the_first_reload_on(self):
        self.write_ext(
            "names",
            """
            def register(minimal):
                names = minimal.builtin_commands()
                minimal.register_command("seen", lambda args, ctx: print("theme" in names))
            """,
        )
        tui = self.start()
        async with tui.run_test() as pilot:
            self.assertEqual(self.run_command("/seen").strip(), "False")  # loaded before the UI added /theme
            await self.reload(pilot)
            self.assertEqual(self.run_command("/seen").strip(), "True")

    # --- focus -----------------------------------------------------------------

    WIDGETS = """
        from textual.containers import Vertical
        from textual.widgets import Input, Static

        class Term(Static):
            can_focus = True

        def register(minimal):
            minimal.register_widget("term", lambda ctx: Term("t"), "bottom")
            minimal.register_widget("box", lambda ctx: Vertical(Input(id="inner")), "bottom")
            minimal.register_widget("plain", lambda ctx: Static("p"), "bottom")
        """

    def widget(self, tui, name):
        return dict(tui.ext_widgets)[name]

    async def test_focus_widget_focuses_the_widget_or_its_first_focusable_part_and_says_whether_it_could(self):
        self.write_ext("focus", self.WIDGETS)
        tui = self.start()
        async with tui.run_test() as pilot:
            ctx = self.core.context
            self.assertTrue(await asyncio.to_thread(ctx.focus_widget, "term"))
            await pilot.pause()
            self.assertIs(tui.focused, self.widget(tui, "term"))
            self.assertTrue(ctx.focus_widget("box"))
            await pilot.pause()
            self.assertIs(tui.focused, tui.query_one("#inner"))
            self.assertFalse(ctx.focus_widget("plain"))  # nothing in it takes focus
            self.assertFalse(ctx.focus_widget("ghost"))
            self.assertIs(tui.focused, tui.query_one("#inner"))  # a refusal moves nothing
            await asyncio.to_thread(ctx.focus_composer)
            await pilot.pause()
            self.assertIs(tui.focused, self.composer(tui))

    async def test_after_a_command_the_prompt_gets_the_focus_back_unless_a_widget_holds_it(self):
        self.write_ext("focus", self.WIDGETS)
        tui = self.start()
        async with tui.run_test() as pilot:
            ctx = self.core.context
            ctx.focus_widget("term")
            await pilot.pause()
            await self.run_line(pilot, "/noop")
            self.assertIs(tui.focused, self.composer(tui))
            ctx.focus_widget("term", hold=True)
            await pilot.pause()
            await self.run_line(pilot, "/noop")
            self.assertIs(tui.focused, self.widget(tui, "term"))
            await self.run_line(pilot, "/noop")  # and again
            self.assertIs(tui.focused, self.widget(tui, "term"))

    async def test_the_hold_ends_on_escape_on_focus_composer_when_the_focus_leaves_and_on_reload(self):
        self.write_ext("focus", self.WIDGETS)
        tui = self.start()
        async with tui.run_test() as pilot:
            ctx = self.core.context
            term = self.widget(tui, "term")

            ctx.focus_widget("term", hold=True)
            await pilot.pause()
            await pilot.press("escape")  # the widget does not use it: it reaches the app, which takes the focus back
            self.assertIs(tui.focused, self.composer(tui))
            self.assertIsNone(tui.held_focus)

            ctx.focus_widget("term", hold=True)
            await pilot.pause()
            ctx.focus_composer()
            await pilot.pause()
            self.assertIsNone(tui.held_focus)
            await self.run_line(pilot, "/noop")
            self.assertIs(tui.focused, self.composer(tui))

            ctx.focus_widget("term", hold=True)
            await pilot.pause()
            tui.query_one("#inner").focus()  # the user clicked another widget
            await pilot.pause()
            await self.run_line(pilot, "/noop")
            self.assertIs(tui.focused, self.composer(tui))

            ctx.focus_widget("term", hold=True)
            await pilot.pause()
            await self.reload(pilot)
            self.core.registry.register_command("noop", lambda args, ctx: None, "does nothing")
            self.assertIsNone(tui.held_focus)
            self.assertIsNot(self.widget(tui, "term"), term)
            await self.run_line(pilot, "/noop")
            self.assertIs(tui.focused, self.composer(tui))

    async def test_a_widget_that_handles_escape_keeps_the_focus(self):
        self.write_ext(
            "esc",
            """
            from textual.widgets import Static

            class Term(Static):
                can_focus = True
                keys = []

                def on_key(self, event):
                    self.keys.append(event.key)
                    event.stop()

            def register(minimal):
                minimal.register_widget("term", lambda ctx: Term("t"), "bottom")
            """,
        )
        tui = self.start()
        async with tui.run_test() as pilot:
            self.core.context.focus_widget("term", hold=True)
            await pilot.pause()
            await pilot.press("escape", "x")
            term = self.widget(tui, "term")
            self.assertEqual(term.keys, ["escape", "x"])
            self.assertIs(tui.focused, term)
            self.assertIs(tui.held_focus, term)

    async def test_a_terminal_key_may_take_the_focus_for_a_widget_and_keep_it(self):
        self.write_ext("focus", self.WIDGETS)
        tui = self.start(lambda r: r.register_keybinding("f5", lambda ctx: ctx.focus_widget("term", hold=True), "go", terminal=True))
        async with tui.run_test() as pilot:
            await pilot.press("f5")
            await self.idle(pilot, tui)
            self.assertIs(tui.focused, self.widget(tui, "term"))

    # --- widget height ---------------------------------------------------------

    HEIGHTS = """
        from textual.widgets import Static

        def register(minimal):
            minimal.register_widget("tall", lambda ctx: Static("a\\nb"), "bottom")
            minimal.register_widget("side", lambda ctx: Static("s"), "left")
            minimal.register_panel("pan", lambda ctx: "p", "footer")
            minimal.register_panel("free", lambda ctx: "p", "footer")
        """

    async def test_ui_widgets_height_sizes_a_widget_or_panel_in_any_area(self):
        self.write_ext("heights", self.HEIGHTS)
        self.write_settings({"ui": {"widgets": {"tall": {"height": 7}, "side": {"height": 3}, "pan": {"height": 4}}}})
        tui = self.start()
        async with tui.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(
                {n: w.region.height for n, w in tui.ext_widgets if n != "free"}, {"tall": 7, "side": 3, "pan": 4}
            )
            self.assertEqual(self.widget(tui, "free").region.height, 1)  # without a setting: its own
            self.write_settings({"ui": {"widgets": {"tall": {"height": 2}}}})
            await self.reload(pilot)
            self.assertEqual(self.widget(tui, "tall").region.height, 2)
            self.assertEqual(self.widget(tui, "pan").region.height, 1)  # dropped with the setting
            report = self.run_command("/ui")
            self.assertIn("height 2", report)

    def test_the_layout_reports_the_height_and_that_the_user_set_it(self):
        self.write_ext("heights", self.HEIGHTS)
        self.write_settings({"ui": {"widgets": {"tall": {"height": 7}}}})
        self.core.reload_extensions()
        placed = {p.name: p for p in resolve(self.core.registry, self.core.settings).placements if p.kind != "status"}
        self.assertEqual((placed["tall"].height, placed["tall"].overridden), (7, ("height",)))
        self.assertEqual((placed["side"].height, placed["side"].overridden), (None, ()))

    def test_a_bad_height_is_dropped_with_the_setting_and_reported(self):
        for bad in (0, 101, -1, True, 2.5, "3", None):
            self.write_settings({"ui": {"widgets": {"w": {"height": bad}}}})
            layers, errors = config.read_layers(str(self.cwd))
            self.assertEqual(len(errors), 1, bad)
            self.assertIn("ui.widgets must be an object mapping names to", errors[0])
            self.assertIn('"height": integer from 1 to 100', errors[0])
        self.write_settings({"ui": {"widgets": {"w": {"height": 100}, "v": {"height": 1}}}})
        self.assertEqual(config.read_layers(str(self.cwd))[1], [])
        self.write_settings({"ui": {"status": {"s": {"height": 3}}}})  # status segments have no height
        self.assertEqual(len(config.read_layers(str(self.cwd))[1]), 1)


if __name__ == "__main__":
    unittest.main()
