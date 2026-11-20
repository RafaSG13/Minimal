"""The composer: multi-line prompt, history, queued messages, @file mentions, command completion, end-of-turn
notification, the command palette, and the /copy and /export commands."""

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from textual import events
from textual.command import CommandList, CommandPalette
from textual.widgets import Static

from minimal import config
from minimal.core import mentions
from minimal.core.app import App, ConsoleFrontend
from minimal.core.history import PromptHistory, history_path
from minimal.core.host import Host
from minimal.core.session import Session
from minimal.extensions import loader
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply
from minimal.ui import commands
from minimal.ui.commands import register_builtin_commands
from minimal.ui.export import session_markdown
from minimal.ui.tui.app import MinimalTui
from minimal.ui.tui.composer import Composer, CompletionPopup


def text_reply(text):
    return Reply(content=[{"type": "text", "text": text}], stop_reason="end_turn")


class ScriptedProvider:
    """Replays ``replies``, recording each request's messages; the first request waits for ``release`` if ``hold``."""

    name = "fake"

    def __init__(self, *replies, hold=False):
        self.replies = list(replies)
        self.requests = []
        self.hold = hold
        self.started = threading.Event()
        self.release = threading.Event()

    def stream(self, **kw):
        self.requests.append(list(kw["messages"]))
        if self.hold and len(self.requests) == 1:
            self.started.set()
            self.release.wait(5)
        reply = self.replies.pop(0)
        for block in reply.content:
            if block["type"] == "text":
                kw["on_text"](block["text"])
        return reply


class ComposerTestBase(unittest.IsolatedAsyncioTestCase):
    """A real App in a temp working directory, with minimal's files redirected into the temp folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.cwd = self.tmp / "work"
        self.cwd.mkdir()
        for patcher in (
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.tmp / "global"),
            mock.patch.object(config, "SETTINGS_FILE", self.tmp / "settings.json"),
            mock.patch.object(config, "HOME_DIR", self.tmp / "home"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, *replies, settings=None, hold=False):
        """The core App (extensions loaded) with a scripted provider; ``settings`` is the global settings file."""
        if settings is not None:
            (self.tmp / "settings.json").write_text(json.dumps(settings))
        self.core = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.cwd),
            auto_approve=False,
            register_commands=register_builtin_commands,
        )
        self.core.reload_extensions()
        self.core.provider_name, self.core.model = "fake", "m"
        self.provider = self.core.provider = ScriptedProvider(*replies, hold=hold)
        return self.core

    async def wait_until(self, pilot, condition):
        for _ in range(100):
            await pilot.pause(0.05)
            if condition():
                return
        self.fail("timed out")

    @staticmethod
    async def type(pilot, text):
        await pilot.press(*("space" if c == " " else c for c in text))

    @staticmethod
    def composer(app):
        return app.query_one("#prompt", Composer)

    @staticmethod
    def users(app):
        return [str(s.render()) for s in app.query(".user").results(Static)]

    @staticmethod
    def outputs(app):
        return [str(s.render()) for s in app.query(".output").results(Static)]


class MultilineTest(ComposerTestBase):
    async def test_enter_sends_and_the_newline_keys_insert_a_newline(self):
        self.build(text_reply("ok"))
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            await pilot.press("a", "shift+enter", "b", "ctrl+j", "c", "alt+enter", "d")
            self.assertEqual(self.composer(app).text, "a\nb\nc\nd")
            self.assertEqual(self.core.messages, [])  # nothing was sent
            await pilot.press("enter")
            await self.wait_until(pilot, lambda: not app.busy)
            self.assertEqual(self.core.messages[0]["content"], "a\nb\nc\nd")
            self.assertEqual(self.users(app), ["a\nb\nc\nd"])
            self.assertEqual(self.composer(app).text, "")

    async def test_pasted_lines_keep_their_newlines_and_the_composer_stops_growing_at_the_setting(self):
        self.build(settings={"ui": {"composer_max_lines": 3}})
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            composer = self.composer(app)
            self.assertEqual(composer.size.height, 1)
            await pilot.press("a", "shift+enter", "b")
            await pilot.pause(0.1)
            self.assertEqual(composer.size.height, 2)
            app.post_message(events.Paste("1\n2\n3\n4\n5\n6"))
            await pilot.pause(0.1)
            self.assertEqual(composer.text.count("\n"), 6)
            self.assertEqual(composer.size.height, 3)
            composer.set_text("")
            await pilot.pause(0.1)
            self.assertEqual(composer.size.height, 1)

    async def test_command_names_are_suggested_as_ghost_text_and_right_accepts_them(self):
        self.build()
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            await self.type(pilot, "/rel")
            self.assertEqual(self.composer(app).suggestion, "oad")
            await pilot.press("right")
            self.assertEqual(self.composer(app).text, "/reload")
            self.composer(app).set_text("")
            await self.type(pilot, "/mo")
            await pilot.press("tab")  # Tab accepts it too, when no popup is open
            self.assertEqual(self.composer(app).text, "/mode")


class HistoryTest(ComposerTestBase):
    async def send(self, app, pilot, text):
        self.composer(app).set_text(text)
        await pilot.press("enter")
        await self.wait_until(pilot, lambda: not app.busy)

    async def test_up_and_down_walk_the_history_and_keep_the_draft(self):
        self.build(text_reply("1"), text_reply("2"), text_reply("3"))
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            for text in ("one", "two", "two"):  # a repeated entry is kept once
                await self.send(app, pilot, text)
            composer = self.composer(app)
            await self.type(pilot, "dra")
            await pilot.press("up")
            self.assertEqual(composer.text, "two")
            await pilot.press("up")
            self.assertEqual(composer.text, "one")
            await pilot.press("up")  # the oldest stays
            self.assertEqual(composer.text, "one")
            await pilot.press("down")
            self.assertEqual(composer.text, "two")
            await pilot.press("down")
            self.assertEqual(composer.text, "dra")  # the draft is back

    async def test_multi_line_entries_are_recalled_whole_and_up_moves_inside_them_first(self):
        self.build(text_reply("1"), text_reply("2"))
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            await self.send(app, pilot, "older")
            await self.send(app, pilot, "a\nb")
            composer = self.composer(app)
            await pilot.press("up")
            self.assertEqual(composer.text, "a\nb")
            self.assertEqual(composer.cursor_location, (1, 1))
            await pilot.press("up")  # the cursor goes to the first line of the entry
            self.assertEqual(composer.text, "a\nb")
            await pilot.press("up")
            self.assertEqual(composer.text, "older")

    async def test_history_is_saved_per_project_privately_capped_and_loaded_by_the_next_run(self):
        self.build(text_reply("1"), text_reply("2"), text_reply("3"), settings={"ui": {"history_size": 2}})
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            for text in ("one", "two", "three"):
                await self.send(app, pilot, text)
        path = history_path(str(self.cwd))
        self.assertEqual(path.parent.parent, self.tmp / "home" / "data" / "projects")
        self.assertEqual(json.loads(path.read_text()), {"entries": ["two", "three"]})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.build(settings={"ui": {"history_size": 2}})
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            await pilot.press("up")
            self.assertEqual(self.composer(app).text, "three")

    def test_history_keeps_entries_other_processes_saved_and_ignores_a_corrupt_file(self):
        path = self.tmp / "history.json"
        path.write_text("not json")
        first = PromptHistory(path)
        self.assertEqual(first.entries, [])
        other = PromptHistory(path)
        first.add("a")
        other.add("b")
        self.assertEqual(PromptHistory(path).entries, ["a", "b"])
        self.assertEqual(PromptHistory(path, 0).entries, [])


class QueueTest(ComposerTestBase):
    async def test_a_message_sent_during_a_turn_is_queued_and_a_command_is_refused(self):
        self.build(text_reply("first"), text_reply("second"), hold=True)
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            composer = self.composer(app)
            composer.set_text("one")
            await pilot.press("enter")
            await self.wait_until(pilot, self.provider.started.is_set)
            composer.set_text("two\nlines")
            await pilot.press("enter")
            self.assertEqual(composer.text, "")  # usable while busy
            self.assertEqual(list(self.core.queued), ["two\nlines"])
            self.assertEqual(str(app.query_one("#queued").render()), "queued: two …")
            composer.set_text("/help")
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("enter")
            self.assertIn("commands wait", notify.call_args.args[0])
            self.assertEqual(composer.text, "/help")  # refused, and kept to edit
            self.assertEqual(list(self.core.queued), ["two\nlines"])
            composer.set_text("")
            self.provider.release.set()
            await self.wait_until(pilot, lambda: len(self.provider.requests) == 2 and not app.busy)
            self.assertEqual([m["content"] for m in self.core.messages if m["role"] == "user"], ["one", "two\nlines"])
            self.assertEqual(self.users(app), ["one", "two\nlines"])
            self.assertFalse(app.query_one("#queued").has_class("-open"))

    async def test_escape_interrupts_and_the_queued_message_still_goes_out(self):
        self.build(text_reply("late"), text_reply("second"), hold=True)
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            self.composer(app).set_text("one")
            await pilot.press("enter")
            await self.wait_until(pilot, self.provider.started.is_set)
            self.composer(app).set_text("two")
            await pilot.press("enter")
            await pilot.press("escape")
            self.provider.release.set()
            await self.wait_until(pilot, lambda: len(self.provider.requests) == 2 and not app.busy)
            self.assertEqual(self.users(app), ["one", "two"])


class MentionTest(ComposerTestBase):
    def make_files(self):
        (self.cwd / "src").mkdir()
        (self.cwd / "README.md").write_text("hello\n")
        (self.cwd / "src" / "app.py").write_text("print(1)\n")
        (self.cwd / "blob.bin").write_bytes(b"\x00\x01\x02")
        (self.tmp / "secret.txt").write_text("top secret")
        (self.cwd / "link.txt").symlink_to(self.tmp / "secret.txt")

    async def test_at_offers_project_files_and_tab_then_enter_accept_one_without_sending(self):
        self.make_files()
        self.build(text_reply("ok"))
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            composer = self.composer(app)
            popup = app.query_one(CompletionPopup)
            await self.type(pilot, "look at @rdm")  # a subsequence of README.md
            self.assertEqual(popup.items, ["README.md"])
            await pilot.press("tab")
            self.assertEqual(composer.text, "look at @README.md")
            await pilot.press("enter")  # accepts, does not send
            self.assertEqual(self.core.messages, [])
            self.assertEqual(popup.items, [])
            await pilot.press("enter")
            await self.wait_until(pilot, lambda: not app.busy)
            self.assertEqual(
                self.core.messages[0]["content"], 'look at @README.md\n\n<file path="README.md">\nhello\n\n</file>'
            )
            self.assertEqual(self.users(app), ["look at @README.md"])  # the transcript shows what was typed
            self.assertTrue(any("attached README.md" in o for o in self.outputs(app)), self.outputs(app))

    async def test_tab_cycles_through_the_matches_in_the_popup(self):
        self.make_files()
        self.build()
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            await self.type(pilot, "@a")
            popup = app.query_one(CompletionPopup)
            self.assertEqual(popup.items[0], "src/app.py")  # a file name starting with it comes first
            self.assertIn("README.md", popup.items)  # a subsequence
            await pilot.press("down", "tab")
            self.assertEqual(self.composer(app).text, "@" + popup.items[1])

    async def test_files_outside_the_working_directory_binary_files_and_links_out_are_never_attached(self):
        self.make_files()
        self.build(text_reply("ok"))
        app = MinimalTui(Host(self.core))
        line = "see @../secret.txt @link.txt @blob.bin @/etc/hosts @nothing"
        async with app.run_test() as pilot:
            self.composer(app).set_text(line)
            await pilot.press("enter")
            await self.wait_until(pilot, lambda: not app.busy)
            self.assertEqual(self.core.messages[0]["content"], line)  # nothing appended
            notes = "\n".join(self.outputs(app))
            self.assertIn("@../secret.txt: outside the working directory", notes)
            self.assertIn("@link.txt: outside the working directory", notes)
            self.assertIn("@blob.bin: binary file", notes)
            self.assertIn("@/etc/hosts: outside the working directory", notes)

    async def test_a_long_file_is_cut_at_the_setting(self):
        (self.cwd / "big.txt").write_text("x" * 100)
        self.build(text_reply("ok"), settings={"ui": {"mention_max_chars": 10}})
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            self.composer(app).set_text("@big.txt")
            await pilot.press("enter")
            await self.wait_until(pilot, lambda: not app.busy)
        self.assertEqual(
            self.core.messages[0]["content"],
            '@big.txt\n\n<file path="big.txt">\n' + "x" * 10 + "\n[truncated to the first 10 characters]\n</file>",
        )

    async def test_a_resumed_session_draws_the_message_without_the_attached_content(self):
        (self.cwd / "a.txt").write_text("secret contents")
        self.build(text_reply("ok"))
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            self.composer(app).set_text("read @a.txt")
            await pilot.press("enter")
            await self.wait_until(pilot, lambda: not app.busy)
        replay = MinimalTui(Host(self.core))
        async with replay.run_test() as pilot:
            await pilot.pause(0.1)
            self.assertEqual(self.users(replay), ["read @a.txt"])
            self.assertIn("attached a.txt", "\n".join(self.outputs(replay)))

    def test_files_respect_gitignore_in_a_repository_and_a_walk_skips_dependency_folders_elsewhere(self):
        mentions._cache.clear()
        (self.cwd / "keep.py").write_text("")
        (self.cwd / "skip.log").write_text("")
        (self.cwd / ".gitignore").write_text("*.log\n")
        (self.cwd / "node_modules").mkdir()
        (self.cwd / "node_modules" / "dep.js").write_text("")
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q"], cwd=self.cwd, check=True)
            files = mentions.project_files(str(self.cwd))
            self.assertIn("keep.py", files)
            self.assertNotIn("skip.log", files)
        mentions._cache.clear()
        with mock.patch.object(mentions, "_git_files", return_value=None):
            walked = mentions.project_files(str(self.cwd))
        self.assertIn("keep.py", walked)
        self.assertIn("skip.log", walked)
        self.assertNotIn("node_modules/dep.js", walked)
        mentions._cache.clear()

    def test_matching_ranks_prefixes_before_substrings_before_subsequences_and_caps_results(self):
        files = ["docs/readme.md", "README.md", "src/rd_me.py", "src/app.py", "notes.txt"]
        self.assertEqual(mentions.match_files("read", files), ["README.md", "docs/readme.md"])
        self.assertEqual(mentions.match_files("rdm", files), ["README.md", "src/rd_me.py", "docs/readme.md"])
        self.assertEqual(len(mentions.match_files("", [f"f{i}" for i in range(100)], limit=5)), 5)

    def test_mentions_ignore_trailing_punctuation_duplicates_and_prose_at_signs(self):
        (self.cwd / "a.txt").write_text("A")
        result = mentions.expand_mentions("see @a.txt, then @a.txt. mail me@a.txt or @nobody", str(self.cwd))
        self.assertEqual([a[0] for a in result.attached], ["a.txt"])
        self.assertEqual(result.notices, [])


class CommandCompleteTest(ComposerTestBase):
    def register(self):
        names = ("alice", "alan", "bob")
        self.core.registry.register_command(
            "greet", lambda args, ctx: print("hi", args), complete=lambda args, ctx: [n for n in names if n.startswith(args)]
        )

        def boom(args, ctx):
            raise RuntimeError("broken")

        self.core.registry.register_command("boom", lambda args, ctx: None, complete=boom)

    async def test_the_popup_offers_a_commands_completions_and_tab_cycles_through_them(self):
        self.build()
        self.register()
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            composer = self.composer(app)
            popup = app.query_one(CompletionPopup)
            await self.type(pilot, "/greet ")
            self.assertEqual(popup.items, ["alice", "alan", "bob"])
            await self.type(pilot, "a")
            self.assertEqual(popup.items, ["alice", "alan"])
            await pilot.press("tab")
            self.assertEqual(composer.text, "/greet alice")
            await pilot.press("tab")
            self.assertEqual(composer.text, "/greet alan")
            await pilot.press("tab")
            self.assertEqual(composer.text, "/greet alice")
            await pilot.press("escape")
            self.assertEqual(popup.items, [])
            await pilot.press("enter")  # sends the completed command
            await self.wait_until(pilot, lambda: not app.busy)
            self.assertTrue(any("hi alice" in o for o in self.outputs(app)), self.outputs(app))

    async def test_enter_with_the_popup_open_sends_the_text_as_typed(self):
        self.build()
        self.register()
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            await self.type(pilot, "/greet b")
            await pilot.press("enter")
            await self.wait_until(pilot, lambda: not app.busy)
            self.assertTrue(any("hi b" in o for o in self.outputs(app)), self.outputs(app))

    async def test_a_failing_completion_is_reported_once_and_typing_goes_on(self):
        self.build()
        self.register()
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            await self.type(pilot, "/boom xyz")
            await pilot.pause(0.1)
            errors = [str(s.render()) for s in app.query(".error").results(Static)]
            self.assertEqual(len([e for e in errors if "/boom completion: broken" in e]), 1, errors)
            self.assertEqual(self.composer(app).text, "/boom xyz")

    async def test_built_in_commands_complete_modes_sessions_and_subcommands(self):
        self.build()
        self.core.registry.register_mode("plan", description="read only")
        self.core.registry.register_mode("build", description="all")
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            popup = app.query_one(CompletionPopup)
            await self.type(pilot, "/mode p")
            self.assertEqual(popup.items, ["plan"])
            self.composer(app).set_text("")
            await self.type(pilot, "/ext s")
            self.assertEqual(popup.items, ["search", "settings"])
            self.composer(app).set_text("")
            await self.type(pilot, "/theme m")
            self.assertEqual(popup.items, ["minimal"])

    def test_host_completes_arguments_and_rejects_a_bad_return(self):
        self.build()
        host = Host(self.core)
        self.core.registry.register_command("one", lambda a, c: None, complete=lambda a, c: ["x"])
        self.core.registry.register_command("bad", lambda a, c: None, complete=lambda a, c: "x")
        self.assertEqual(host.complete_args("/one "), ["x"])
        self.assertEqual(host.complete_args("/one"), [])  # still typing the name
        self.assertEqual(host.complete_args("/help x"), [])  # no completion function
        with self.assertRaisesRegex(ValueError, "list of strings"):
            host.complete_args("/bad ")
        with self.assertRaises(ValueError):
            self.core.registry.register_command("worse", lambda a, c: None, complete="nope")

    def test_extensions_register_completion_through_the_api(self):
        ext = self.tmp / "global"
        ext.mkdir()
        (ext / "demo.py").write_text(
            '__version__ = "1.0"\n__api__ = "2"\n'
            "def register(minimal):\n"
            "    @minimal.command(complete=lambda args, ctx: ['a1'])\n"
            "    def thing(args, ctx):\n"
            "        'Do a thing.'\n"
            "    minimal.register_command('other', lambda a, c: None, complete=lambda a, c: ['b1'])\n"
            "    minimal.register_command('where', lambda a, c: print(minimal.data_dir()))\n"
        )
        self.build()
        host = Host(self.core)
        self.assertEqual(host.complete_args("/thing "), ["a1"])
        self.assertEqual(host.complete_args("/other "), ["b1"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.core.run_command("/where")
        # the prompt history sits next to the extensions' project data folders
        self.assertEqual(Path(out.getvalue().strip()).parent, history_path(str(self.cwd)).parent)


class NotifyTest(ComposerTestBase):
    async def turn(self, settings, *, focus=None):
        """The notifications sent while one turn runs; ``focus`` is the focus event the terminal reports meanwhile."""
        sent = []
        self.build(text_reply("done"), settings=settings, hold=True)
        app = MinimalTui(Host(self.core))
        with mock.patch("textual.drivers.headless_driver.HeadlessDriver.write", lambda driver, data: sent.append(data)):
            async with app.run_test() as pilot:
                self.composer(app).set_text("hi")
                await pilot.press("enter")
                await self.wait_until(pilot, self.provider.started.is_set)
                if focus is not None:  # a key press would count as focus, so the event comes after Enter
                    app.post_message(events.AppFocus() if focus else events.AppBlur())
                    await pilot.pause(0.1)
                self.provider.release.set()
                await self.wait_until(pilot, lambda: not app.busy)
        return [s for s in sent if s == "\a" or s.startswith("\x1b]9;")]

    async def test_osc9_and_bell_are_sent_when_a_long_turn_ends_and_focus_is_unknown_or_lost(self):
        osc = await self.turn({"ui": {"notify": "osc9", "notify_after": 0}})
        self.assertEqual(len(osc), 1)
        self.assertRegex(osc[0], r"^\x1b\]9;minimal: finished after \d+s\x07$")
        self.assertEqual(await self.turn({"ui": {"notify": "bell", "notify_after": 0}}), ["\a"])
        self.assertEqual(len(await self.turn({"ui": {"notify": "bell", "notify_after": 0}}, focus=False)), 1)

    async def test_nothing_is_sent_when_off_when_the_turn_was_short_or_the_terminal_is_focused(self):
        self.assertEqual(await self.turn({}), [])  # the default is off
        self.assertEqual(await self.turn({"ui": {"notify": "bell"}}), [])  # shorter than the default 10 s
        self.assertEqual(await self.turn({"ui": {"notify": "bell", "notify_after": 0}}, focus=True), [])

    def test_invalid_ui_settings_are_dropped_with_an_error(self):
        path = self.tmp / "settings.json"
        path.write_text(json.dumps({"ui": {"notify": "loud", "history_size": -1, "composer_max_lines": 4}}))
        layers, errors = config.read_layers(str(self.cwd))
        self.assertEqual(layers[0].data, {"ui.composer_max_lines": 4})
        self.assertEqual(len(errors), 2)


class PaletteTest(ComposerTestBase):
    async def test_the_palette_lists_every_command_and_runs_or_prefills_the_chosen_one(self):
        self.build()
        self.core.registry.register_command("greet", lambda args, ctx: print("hello there"), "say hi")
        self.core.registry.register_command("pick", lambda args, ctx: None, "pick one: /pick <name>")
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            await pilot.press("ctrl+p")
            await pilot.pause(0.3)
            self.assertIsInstance(app.screen, CommandPalette)
            options = app.screen.query_one(CommandList)
            shown = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
            for name in ("/help", "/greet", "/pick", "/copy", "/export", "/mode"):
                self.assertTrue(any(s.startswith(name + "\n") for s in shown), (name, shown))
            self.assertTrue(any("/copy [n]" in s for s in shown), shown)  # a usage in brackets is not markup
            await self.type(pilot, "greet")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await self.wait_until(pilot, lambda: not isinstance(app.screen, CommandPalette) and not app.busy)
            self.assertTrue(any("hello there" in o for o in self.outputs(app)), self.outputs(app))
            await pilot.press("ctrl+p")
            await pilot.pause(0.3)
            await self.type(pilot, "pick")
            await pilot.pause(0.3)
            await pilot.press("enter")
            await self.wait_until(pilot, lambda: not isinstance(app.screen, CommandPalette))
            self.assertEqual(self.composer(app).text, "/pick ")  # it needs an argument: typed, not run
            self.assertIs(app.focused, self.composer(app))


class FakeFrontend:
    """A console that also copies to a clipboard, like the terminal UI's frontend."""

    def __init__(self):
        self.copied = []

    def copy_text(self, text):
        self.copied.append(text)
        return True


class CopyExportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.cwd = self.tmp / "work"
        self.cwd.mkdir()
        self.core = App(
            registry=Registry(),
            session=Session("sess", self.tmp / "sess.jsonl"),
            cwd=str(self.cwd),
            auto_approve=False,
            register_commands=register_builtin_commands,
        )
        self.core.frontend = FakeFrontend()
        self.core.messages.extend(
            [
                {"role": "user", "content": "first question"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "first answer"},
                        {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "x" * 500}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "y" * 2000, "is_error": True},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                    ],
                },
                {"role": "assistant", "content": [{"type": "text", "text": "second answer"}]},
                {"role": "user", "content": "follow-up\n\n<file path=\"a.py\">\nprint(1)\n</file>"},
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "last"}]},
            ]
        )

    def run_command(self, line):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.core.run_command(line)
        return out.getvalue(), err.getvalue()

    def test_copy_sends_the_last_assistant_text_or_the_nth_from_the_end(self):
        with mock.patch.object(commands, "native_copy", return_value="pbcopy"):
            out, _ = self.run_command("/copy")
            self.assertIn("Copied 4 characters of the last reply (OSC 52 + pbcopy).", out)
            out, _ = self.run_command("/copy 3")
            self.assertIn("reply 3 from the end", out)
        self.assertEqual(self.core.frontend.copied, ["last", "first answer"])

    def test_copy_reports_what_it_could_not_do(self):
        self.core.frontend = FakeFrontend()
        with mock.patch.object(commands, "native_copy", return_value=None):
            self.assertIn("(OSC 52)", self.run_command("/copy")[0])
            self.assertIn("usage: /copy [n]", self.run_command("/copy x")[1])
            self.assertIn("there are 3", self.run_command("/copy 9")[1])
        self.core.frontend = ConsoleFrontend()  # no clipboard access of its own
        with mock.patch.object(commands, "native_copy", return_value=None):
            self.assertIn("no clipboard available", self.run_command("/copy")[1])
        self.core.messages.clear()
        self.assertIn("no assistant message to copy", self.run_command("/copy")[1])

    def test_export_writes_markdown_and_refuses_to_overwrite_without_force(self):
        out, _ = self.run_command("/export")
        path = self.cwd / "minimal-session-sess.md"
        self.assertIn(f"Exported 6 messages to {path}", out)
        text = path.read_text()
        self.assertIn("# minimal session", text)
        self.assertIn("- Session: `sess`", text)
        self.assertIn("## You\n\nfirst question", text)
        self.assertIn("**Tool call** `bash`", text)
        self.assertIn("**Tool error** `bash`:", text)
        self.assertIn("… [1200 more characters]", text)  # the 2000-character result is cut to 800
        self.assertIn("… [215 more characters]", text)  # and the 515-character input to 300
        self.assertIn("*[image]*", text)
        self.assertIn("*[thinking omitted]*", text)
        self.assertIn("follow-up\n\n*Attached: `a.py`*", text)
        self.assertNotIn("print(1)", text)
        self.assertEqual(text.count("## Assistant"), 2)  # a tool round trip stays under one heading
        _, err = self.run_command("/export")
        self.assertIn("already exists", err)
        path.write_text("keep")
        self.run_command("/export")
        self.assertEqual(path.read_text(), "keep")
        self.run_command("/export --force")
        self.assertIn("# minimal session", path.read_text())

    def test_export_takes_a_path_and_names_a_title(self):
        self.core.session.title = "Refactor"
        self.run_command("/export notes.md")
        self.assertTrue((self.cwd / "notes.md").read_text().startswith("# Refactor\n"))
        _, err = self.run_command("/export a b")
        self.assertIn("usage: /export [path] [--force]", err)
        _, err = self.run_command("/export missing/dir/x.md")
        self.assertIn("cannot write", err)

    def test_markdown_fences_grow_around_backticks_in_results(self):
        messages = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "```py\nx\n```"}]},
        ]
        self.assertIn("````text\n```py\nx\n```\n````", session_markdown(messages, "s"))
