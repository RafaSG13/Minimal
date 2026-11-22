"""The marketplace's compact, lenses, retry and claude_subscription items: settings, validation and the 0.9 API they use."""

import contextlib
import io
import json
import sys
import time
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

from minimal import config
from minimal.api import REASONING_EFFORTS, result_text
from minimal.core import session as session_mod
from minimal.core.app import App
from minimal.extensions import loader
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply, Usage

from tests.test_bundled_extensions import (
    MARKETPLACE,
    BundledExtensionTest,
    FailingProvider,
    HTTPError,
    RecordingProvider,
    text_reply,
)


class ContextTest(BundledExtensionTest):
    """One extension of ``extensions/`` loaded into a real ``App`` with settings and a mock frontend."""

    item = ""

    def setUp(self):
        super().setUp()
        for patcher in (
            mock.patch.object(session_mod, "SESSIONS_DIR", self.tmp / "sessions"),
            mock.patch.object(config, "HOME_DIR", self.tmp / "home"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.frontend = mock.Mock()
        self.frontend.theme_name.return_value = None

    def build(self, settings=None, replies=(), top=None):
        """``(app, errors)``: the item loaded with ``extensions.settings.<item>`` set to ``settings`` (and ``top`` over it)."""
        data = {"extensions": {"settings": {self.item: settings or {}}}, **(top or {})}
        (self.tmp / "settings.json").write_text(json.dumps(data))
        app = App(
            registry=Registry(),
            session=session_mod.open_session("old"),
            cwd=str(self.tmp),
            auto_approve=True,
            trust_project=True,
            frontend=self.frontend,
            extra_extensions=[str(MARKETPLACE / "extensions" / f"{self.item}.py")],
        )
        _, errors = app.reload_extensions()
        self.warnings = app.extension_problems  # what the item reported with report_problem: it still loaded
        app.provider, app.model = RecordingProvider(replies), "m"
        return app, errors

    def make(self, settings=None, replies=(), top=None):
        app, errors = self.build(settings, replies, top)
        self.assertEqual((errors, self.warnings), ([], []))
        return app

    @property
    def ext(self):
        return sys.modules[loader.module_name(MARKETPLACE / "extensions" / f"{self.item}.py")]

    def run_turn(self, app, text):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            app.run_turn(text)
        return out.getvalue()

    def run_command(self, app, line):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            app.run_command(line)
        return out.getvalue()


class RecordingOptions(RecordingProvider):
    """Also records the model and the request options of each request."""

    def __init__(self, replies):
        super().__init__(replies)
        self.calls = []

    def stream(self, **kw):
        self.calls.append((kw["model"], kw.get("options")))
        return super().stream(**kw)


def exchange(question, answer):
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": [{"type": "text", "text": answer}]},
    ]


def texts(messages):
    return [m["content"] if isinstance(m["content"], str) else m["content"][0]["text"] for m in messages]


# --- compact -------------------------------------------------------------------------------------------------------------


class CompactSettingsTest(ContextTest):
    item = "compact"

    def full(self, app, tokens, replies=("SUMMARY",)):
        app.provider.replies = [text_reply("answer", usage=Usage(input_tokens=tokens, output_tokens=1))] + [
            text_reply(r) for r in replies
        ]
        return self.run_turn(app, "q")

    def test_defaults_are_what_the_item_always_did(self):
        self.make()
        ext = self.ext
        self.assertEqual(
            dict(ext.DEFAULTS),
            {
                "auto": True,
                "compact_at": 100_000,
                "window_ratio": 0.0,
                "keep_last": 1,
                "summary_prompt": ext.SUMMARY_REQUEST,
                "model": "",
                "reasoning_effort": "",
                "notify": False,
            },
        )
        app = self.make()
        self.full(app, 100_000)
        self.assertEqual(texts(app.messages), [ext.SUMMARY_PREFIX + "SUMMARY", ext.ACK, "q", "answer"])
        self.frontend.notify.assert_not_called()
        self.frontend.progress.assert_not_called()

    def test_window_ratio_follows_the_models_real_window(self):
        app = self.make({"window_ratio": 0.5}, top={"context_window": 1000})
        self.assertEqual(app.context.context_window, 1000)
        self.full(app, 498)
        self.assertEqual(app.session.id, "old")  # 499 tokens of a 500 threshold; the default 100000 would not compact either
        self.full(app, 499)
        self.assertNotEqual(app.session.id, "old")

    def test_window_ratio_falls_back_to_compact_at_when_the_window_is_unknown(self):
        app = self.make({"window_ratio": 0.5, "compact_at": 2000})
        self.assertIsNone(app.context.context_window)
        self.full(app, 1998)
        self.assertEqual(app.session.id, "old")
        self.full(app, 1999)
        self.assertNotEqual(app.session.id, "old")

    def test_window_ratio_also_governs_the_middle_of_a_turn(self):
        (self.tmp / "f.txt").write_text("x")
        app = self.make({"window_ratio": 0.1}, top={"context_window": 1000})
        app.messages[:] = exchange("q0", "a0")
        read = {"type": "tool_use", "id": "r1", "name": "read", "input": {"path": "f.txt"}}
        app.provider.replies = [
            Reply(content=[read], stop_reason="tool_use", usage=Usage(input_tokens=100, output_tokens=1)),
            text_reply("SUMMARY"),
            text_reply("done"),
        ]
        self.run_turn(app, "q")
        self.assertEqual(app.provider.requests[2].messages[0]["content"], self.ext.SUMMARY_PREFIX + "SUMMARY")

    def test_auto_false_leaves_compacting_to_the_command(self):
        app = self.make({"auto": False})
        self.full(app, 10_000_000, replies=())
        self.assertEqual(app.session.id, "old")
        self.assertEqual(len(app.provider.requests), 1)
        app.provider.replies = [text_reply("SUMMARY")]
        self.assertIn("[compact] 2 messages summarised", self.run_command(app, "/compact"))
        self.assertNotEqual(app.session.id, "old")

    def test_keep_last_keeps_that_many_recent_exchanges_word_for_word(self):
        ext = self.ext
        app = self.make({"keep_last": 2})
        app.messages[:] = [
            *exchange("q1", "a1"),
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "file"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "a2"}]},
        ]
        app.provider.replies = [text_reply("SUMMARY")]
        self.run_command(app, "/compact")
        self.assertEqual(texts(app.messages), [ext.SUMMARY_PREFIX + "SUMMARY", ext.ACK, "q1", "a1", "q2", "a2"])
        self.assertEqual([m["role"] for m in app.messages], ["user", "assistant"] * 3)

    def test_keep_last_zero_keeps_only_the_summary_and_a_large_one_does_not_repeat_old_summaries(self):
        ext = self.ext
        app = self.make({"keep_last": 0})
        app.messages[:] = exchange("q", "a")
        app.provider.replies = [text_reply("S1")]
        self.run_command(app, "/compact")
        self.assertEqual(texts(app.messages), [ext.SUMMARY_PREFIX + "S1", ext.ACK])

        app = self.make({"keep_last": 5})
        app.messages[:] = [*exchange(ext.SUMMARY_PREFIX + "S0", ext.ACK), *exchange("q", "a")]
        app.provider.replies = [text_reply("S1")]
        self.run_command(app, "/compact")
        self.assertEqual(texts(app.messages), [ext.SUMMARY_PREFIX + "S1", ext.ACK, "q", "a"])

    def test_summary_prompt_replaces_the_instruction(self):
        app = self.make({"summary_prompt": "Summarise in one line."})
        app.messages[:] = exchange("q", "a")
        app.provider.replies = [text_reply("SUMMARY")]
        self.run_command(app, "/compact")
        self.assertEqual(app.provider.requests[0].messages[-1], {"role": "user", "content": "Summarise in one line."})

    def test_summary_prompt_is_used_in_the_middle_of_a_turn_too(self):
        (self.tmp / "f.txt").write_text("x")
        app = self.make({"summary_prompt": "ONE LINE", "compact_at": 50})
        app.messages[:] = exchange("q0", "a0")
        read = {"type": "tool_use", "id": "r1", "name": "read", "input": {"path": "f.txt"}}
        app.provider.replies = [
            Reply(content=[read], stop_reason="tool_use", usage=Usage(input_tokens=50, output_tokens=1)),
            text_reply("SUMMARY"),
            text_reply("done"),
        ]
        self.run_turn(app, "q")
        self.assertTrue(app.provider.requests[1].messages[-1]["content"].endswith("ONE LINE"))

    def test_model_and_reasoning_effort_go_to_the_summary_request_only(self):
        app = self.make({"model": "small-model", "reasoning_effort": "low"})
        app.provider = RecordingOptions([text_reply("answer", usage=Usage(input_tokens=100_000, output_tokens=1)), text_reply("S")])
        self.run_turn(app, "q")
        self.assertEqual(app.provider.calls[0][0], "m")
        self.assertNotEqual((app.provider.calls[0][1] or {}).get("reasoning_effort"), "low")
        model, options = app.provider.calls[1]
        self.assertEqual((model, options["reasoning_effort"]), ("small-model", "low"))
        self.assertEqual(app.provider.requests[1].tools, [])  # another model: no tools offered to warm a cache it lacks
        self.assertEqual(app.model, "m")

    def test_the_default_summary_request_offers_the_tools_as_before(self):
        app = self.make()
        self.full(app, 100_000)
        self.assertEqual(app.provider.requests[1].tools, app.provider.requests[0].tools)

    def test_a_failing_summary_model_is_reported_with_the_fix_and_the_session_is_kept(self):
        app = self.make({"model": "nope"})
        app.provider = FailingProvider([], [text_reply("answer", usage=Usage(input_tokens=100_000, output_tokens=1))])
        app.provider.errors = []

        stream = app.provider.stream

        def fail_on_summary(**kw):
            if kw["model"] == "nope":
                raise RuntimeError("unknown model")
            return stream(**kw)

        app.provider.stream = fail_on_summary
        out = self.run_turn(app, "q")
        self.assertIn("[compact] skipped: the summary request to model 'nope' failed (unknown model)", out)
        self.assertIn("extensions.settings.compact.model", out)
        self.assertEqual(app.session.id, "old")

    def test_notify_shows_toasts_and_a_progress_note_while_summarising(self):
        app = self.make({"notify": True})
        self.full(app, 100_000)
        kinds = [(call.args[0], call.args[1]) for call in self.frontend.notify.call_args_list]
        self.assertEqual(len(kinds), 1)
        self.assertEqual(kinds[0][1], "info")
        self.assertRegex(kinds[0][0], r"^compact: session compacted into \w+")
        notes = [call.args[0] for call in self.frontend.progress.call_args_list]
        self.assertEqual(notes, ["compact: summarising the conversation", None])

    def test_notify_reports_a_skipped_compaction(self):
        app = self.make({"notify": True})
        app.provider.replies = [
            text_reply("answer", usage=Usage(input_tokens=100_000, output_tokens=1)),
            Reply(content=[], stop_reason="end_turn"),
        ]
        self.run_turn(app, "q")
        self.frontend.notify.assert_called_once_with("compact: skipped: the model did not return a summary", "warning")
        self.assertEqual(app.session.id, "old")

    def test_a_reply_with_thinking_blocks_still_yields_the_summary_text(self):
        ext = self.ext
        app = self.make({"reasoning_effort": "high"})
        app.messages[:] = exchange("q", "a")
        thinking = {"type": "thinking", "thinking": "hmm", "signature": "sig"}
        app.provider.replies = [Reply(content=[thinking, {"type": "text", "text": "SUMMARY"}], stop_reason="end_turn")]
        self.run_command(app, "/compact")
        self.assertEqual(app.messages[0]["content"], ext.SUMMARY_PREFIX + "SUMMARY")

    def test_bad_settings_are_reported_and_the_defaults_stay(self):
        for settings, problem in (
            ({"keep_last": -1}, "extensions.settings.compact.keep_last"),
            ({"window_ratio": 1.5}, "extensions.settings.compact.window_ratio"),
            ({"compact_at": 0}, "extensions.settings.compact.compact_at"),
            ({"reasoning_effort": "extreme"}, "extensions.settings.compact.reasoning_effort"),
            ({"auto": "yes"}, "compact.auto"),
        ):
            with self.subTest(settings=settings):
                app, errors = self.build(settings)
                self.assertTrue(any(problem in e for e in errors), errors)
                self.assertIn("compact", app.registry.commands)  # still loaded, with the default

    def test_an_empty_summary_prompt_is_a_warning_and_the_default_prompt_is_used(self):
        app, errors = self.build({"summary_prompt": "  "})
        self.assertEqual(errors, [])
        (warning,) = self.warnings
        self.assertIn("compact: extensions.settings.compact.summary_prompt must not be empty", warning)
        self.assertIn("compact", app.registry.commands)
        app.messages[:] = exchange("q", "a")
        app.provider.replies = [text_reply("SUMMARY")]
        self.run_command(app, "/compact")
        asked = app.provider.requests[0].messages[-1]["content"]
        self.assertEqual(asked, self.ext.SUMMARY_REQUEST)

    def test_it_declares_the_api_it_uses(self):
        self.assertIn('__api__ = "2.4"', (MARKETPLACE / "extensions" / "compact.py").read_text())
        self.assertIn("high", REASONING_EFFORTS)


# --- lenses --------------------------------------------------------------------------------------------------------------


def bash(command, id="b1"):
    return Reply(content=[{"type": "tool_use", "id": id, "name": "bash", "input": {"command": command}}], stop_reason="tool_use")


class LensesTest(ContextTest):
    item = "lenses"

    def output_of(self, app, command, settings_turn="go"):
        """The text of the bash result the model got for ``command``."""
        app.provider.replies = [bash(command), text_reply("ok")]
        self.run_turn(app, settings_turn)
        result = [
            b
            for m in app.messages
            if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"]
            if b["type"] == "tool_result"
        ][-1]
        return result_text(result["content"])

    REPEAT = "yes line | head -n 1000"
    PASSING = 'for i in $(seq 1 300); do echo "test_$i (m.T.test_$i) ... ok"; done # unittest'

    def test_defaults_compress_as_before(self):
        app = self.make()
        out = self.output_of(app, self.REPEAT)
        self.assertIn("[previous line repeated 999 more times]", out)
        self.assertIn("[full output: .minimal/outputs/0001.txt, 1002 lines; read it with offset and limit]", out)
        self.assertEqual((self.tmp / ".minimal" / "outputs" / "0001.txt").read_text().count("line\n"), 1000)
        self.assertIn("Output lenses: bash output over 2000 characters is compressed", app.context.system_prompt())
        out = self.output_of(self.make(), self.PASSING)
        self.assertIn("[lens python-tests: 300 passing lines hidden]", out)

    def test_defaults_table(self):
        self.make()
        ext = self.ext
        d = ext.DEFAULTS
        self.assertEqual(
            (d["enabled"], d["key"], d["min_chars"], d["head"], d["tail"], d["python_tests"], d["repeat_min"], d["rules"], d["output_dir"]),
            (True, "", 2000, 6000, 12000, True, 3, [], ".minimal/outputs"),
        )
        self.assertEqual(d["passing"], list(ext.PASSING))

    def test_limits_are_settings(self):
        app = self.make({"min_chars": 100, "head": 10, "tail": 10})
        out = self.output_of(app, "seq 1 3000")
        self.assertTrue(out.startswith("1\n2\n3\n4\n5\n"))
        self.assertRegex(out, r"\n\.\.\. \[lens: \d+ characters omitted here\]\n")
        app = self.make({"min_chars": 100_000})
        self.assertNotIn("[full output", self.output_of(app, self.REPEAT))

    def test_repeat_min_changes_when_lines_collapse_and_zero_never_does(self):
        out = self.output_of(self.make({"repeat_min": 1000}), "yes line | head -n 999")
        self.assertNotIn("repeated", out)
        self.assertNotIn("[full output", out)  # nothing was compressed
        out = self.output_of(self.make({"repeat_min": 0}), "yes line | head -n 5000")
        self.assertNotIn("repeated", out)

    def test_python_tests_lens_can_be_turned_off_and_its_patterns_replaced(self):
        out = self.output_of(self.make({"python_tests": False}), self.PASSING)
        self.assertNotIn("passing lines hidden", out)
        app = self.make({"passing": [r"test_1\d* .*"]})
        out = self.output_of(app, self.PASSING)
        self.assertRegex(out, r"\[lens python-tests: 111 passing lines hidden\]")
        self.assertIn("test_2 (m.T.test_2) ... ok", out)

    def test_rules_are_your_own_lenses(self):
        rule = {"name": "nums", "command": r"^seq\b", "drop": [r"\d{1,2}"], "tail": 3}
        app = self.make({"rules": [rule]})
        out = self.output_of(app, "seq 1 600")
        lines = out.splitlines()
        self.assertEqual(lines[0], "[lens nums: 499 lines omitted]")
        self.assertEqual(lines[1:4], ["599", "600", ""])  # bash adds an empty line before its [exit 0] line
        self.assertEqual(lines[4], "[lens nums: 99 lines hidden]")
        self.assertIn("[full output:", out)
        listing = self.run_command(app, "/lenses")
        self.assertIn(r"nums  ^seq\b  (settings)", listing)
        self.assertLess(listing.index("nums"), listing.index("python-tests"))

    def test_a_rule_keeps_the_exit_line_and_a_head(self):
        rule = {"name": "x", "command": "exit_after", "head": 2, "tail": 1}
        app = self.make({"rules": [rule], "min_chars": 100})
        out = self.output_of(app, "seq 1 300; echo done; exit_after_command_not_found")
        lines = out.splitlines()
        self.assertEqual(lines[:2], ["1", "2"])
        self.assertTrue(any(line.startswith("[lens x:") for line in lines))
        self.assertIn("[exit", out)

    def test_messages_are_settings(self):
        messages = {
            "repeated": "<<{count} more>>",
            "saved": "<<saved {path} ({lines})>>",
            "prompt": "Lenses are on, over {min_chars} chars, in {dir}.",
        }
        app = self.make({"messages": messages})
        out = self.output_of(app, self.REPEAT)
        self.assertIn("<<999 more>>", out)
        self.assertIn("<<saved .minimal/outputs/0001.txt (1002)>>", out)
        self.assertIn("Lenses are on, over 2000 chars, in .minimal/outputs.", app.context.system_prompt())
        self.assertNotIn("Output lenses:", app.context.system_prompt())

    def test_an_empty_prompt_message_leaves_the_system_prompt_alone(self):
        app = self.make({"messages": {"prompt": ""}})
        self.assertNotIn("lens", app.context.system_prompt().lower())

    def test_output_dir_is_a_setting(self):
        app = self.make({"output_dir": "scratch/out/"})
        out = self.output_of(app, self.REPEAT)
        self.assertIn("scratch/out/0001.txt", out)
        self.assertTrue((self.tmp / "scratch" / "out" / "0001.txt").is_file())
        self.assertIn("1 saved output(s) in scratch/out", self.run_command(app, "/lenses"))
        self.assertIn("Removed 1 saved output(s).", self.run_command(app, "/lenses clear"))

    def test_off_and_on_by_command_and_the_prompt_follows(self):
        app = self.make()
        self.assertIn("Output lenses are off.", self.run_command(app, "/lenses off"))
        self.assertNotIn("Output lenses:", app.context.system_prompt())
        out = self.output_of(app, self.REPEAT)
        self.assertNotIn("repeated", out)
        self.assertFalse((self.tmp / ".minimal").exists())
        self.assertIn("Output lenses are off: /lenses on", self.run_command(app, "/lenses"))
        self.assertIn("Output lenses are on.", self.run_command(app, "/lenses toggle"))
        self.assertIn("[previous line repeated", self.output_of(app, self.REPEAT, "again"))
        self.assertIn("usage: /lenses [on|off|toggle|clear]", self.run_command(app, "/lenses sideways"))

    def test_enabled_false_starts_off(self):
        app = self.make({"enabled": False})
        self.assertNotIn("[full output", self.output_of(app, self.REPEAT))
        self.run_command(app, "/lenses on")
        self.assertIn("[full output", self.output_of(app, self.REPEAT, "again"))

    def test_the_key_turns_the_lenses_on_and_off(self):
        app = self.make({"key": "f7"})
        binding = app.registry.keybindings["f7"]
        self.assertEqual((binding.name, binding.ext), ("toggle", "lenses"))
        binding.run(app.context)
        self.frontend.notify.assert_called_with("Output lenses are off.", "info")
        self.assertNotIn("[full output", self.output_of(app, self.REPEAT))
        binding.run(app.context)
        self.frontend.notify.assert_called_with("Output lenses are on.", "info")
        self.assertEqual(self.make().registry.keybindings, {})  # no key by default

    def test_a_key_the_ui_needs_is_a_warning_with_the_fix_and_the_lenses_still_load(self):
        for key in ("ctrl+c", "a"):
            with self.subTest(key=key):
                app, errors = self.build({"key": key})
                self.assertEqual(errors, [])
                (warning,) = self.warnings
                self.assertIn("extensions.settings.lenses.key", warning)
                self.assertIn('set it to "" for no key', warning)
                self.assertEqual(app.registry.keybindings, {})
                self.assertIn("lenses", app.registry.commands)

    def test_bad_settings_are_reported_with_the_fix(self):
        for settings, problem in (("min_chars", 0), ("tail", -5)):  # a range the schema checks stays a load error
            with self.subTest(settings=settings):
                app, errors = self.build({settings: problem})
                self.assertTrue(any(f"extensions.settings.lenses.{settings}" in e for e in errors), errors)

    def test_inconsistent_settings_are_warnings_with_the_fix_and_the_lenses_load(self):
        for settings, problem in (
            ({"repeat_min": 1}, "repeat_min must be 0 (never) or 2 or more"),
            ({"passing": ["("]}, "passing[0] '(' is not a valid regular expression"),
            ({"passing": [3]}, "passing[0] must be a string"),
            ({"output_dir": "../out"}, "output_dir must be a folder inside the project"),
            ({"output_dir": "/tmp/out"}, "output_dir must be a folder inside the project"),
            ({"messages": {"nope": "x"}}, "messages: unknown key 'nope' (known: hidden, repeated"),
            ({"messages": {"saved": "{bogus}"}}, "messages.saved is not a valid template"),
            ({"messages": {"hidden": ""}}, "messages.hidden must be a non-empty string"),
            ({"rules": ["make"]}, "rules[0] must be an object"),
            ({"rules": [{"name": "x", "command": "make"}]}, "rules[0] (x) does nothing; give it drop, head or tail"),
            ({"rules": [{"name": "x", "command": "(", "tail": 3}]}, "rules[0].command '(' is not a valid regular expression"),
            ({"rules": [{"name": "x", "command": "m", "tail": -1}]}, "rules[0].tail must be a whole number of lines"),
            ({"rules": [{"name": "x", "command": "m", "tail": 3, "keep": 1}]}, "rules[0]: unknown key 'keep'"),
            ({"rules": [{"command": "m", "tail": 3}]}, "rules[0].name must be a non-empty string"),
        ):
            with self.subTest(settings=settings):
                app, errors = self.build(settings)
                self.assertEqual(errors, [])
                self.assertTrue(any(problem in w and w.startswith("lenses: ") for w in self.warnings), self.warnings)
                self.assertIn("(ignored: the default applies)", self.warnings[0])
                self.assertIn("lenses", app.registry.commands)

    def test_a_repeat_min_of_one_falls_back_to_the_default(self):
        app, _ = self.build({"repeat_min": 1})
        self.assertIn("[previous line repeated", self.output_of(app, self.REPEAT, "again"))

    def test_it_declares_the_api_it_uses(self):
        self.assertIn('__api__ = "2.4"', (MARKETPLACE / "extensions" / "lenses.py").read_text())


# --- retry ---------------------------------------------------------------------------------------------------------------


class RetrySettingsTest(ContextTest):
    item = "retry"

    def run_failing(self, app, errors, replies=None):
        app.provider = FailingProvider(errors, replies if replies is not None else [text_reply("ok")])
        return self.run_turn(app, "q")

    def test_defaults_are_what_the_item_always_did(self):
        ext = self.ext
        self.make()
        d = ext.DEFAULTS
        self.assertEqual(
            (d["max_attempts"], d["base_delay"], d["factor"], d["max_delay"], d["respect_retry_after"], d["fallback"]),
            (5, 2.0, 2.0, 60.0, True, ""),
        )
        self.assertEqual(set(d["statuses"]), {408, 409, 425, 429, 500, 502, 503, 504, 529})
        self.assertEqual(set(d["network_errors"]), ext.NETWORK_ERRORS)
        self.assertEqual((d["match"], d["notify"], d["progress"]), (["overloaded"], True, False))
        self.assertEqual(ext.verdict(HTTPError(429), 3, "anthropic", d), {"retry_after": 8.0})

    def test_the_default_toast_and_no_progress(self):
        app = self.make({"base_delay": 0})
        self.run_failing(app, [HTTPError(429)])
        self.frontend.notify.assert_called_once_with("retry: HTTPError (1/5), again in 0s", "warning")
        self.frontend.progress.assert_not_called()

    def test_statuses_decide_what_is_retried(self):
        app = self.make({"base_delay": 0, "statuses": [418]})
        self.run_failing(app, [HTTPError(418)])
        self.assertEqual(len(app.provider.requests), 2)
        app = self.make({"base_delay": 0, "statuses": [418]})
        self.run_failing(app, [HTTPError(429)])
        self.assertEqual(len(app.provider.requests), 1)  # 429 is no longer on the list

    def test_network_error_names_and_message_texts_decide_too(self):
        class Flaky(Exception):
            pass

        app = self.make({"base_delay": 0, "network_errors": ["Flaky"]})
        self.run_failing(app, [Flaky("boom")])
        self.assertEqual(len(app.provider.requests), 2)
        app = self.make({"base_delay": 0, "match": ["Try Later"]})
        self.run_failing(app, [RuntimeError("please try later")])
        self.assertEqual(len(app.provider.requests), 2)
        app = self.make({"base_delay": 0, "match": []})
        self.run_failing(app, [RuntimeError("Overloaded")])
        self.assertEqual(len(app.provider.requests), 1)  # "overloaded" was only the default text

    def test_factor_and_retry_after_are_settings(self):
        verdict = self.ext.verdict
        settings = {"max_attempts": 5, "base_delay": 2.0, "factor": 3.0, "max_delay": 100.0, "fallback": ""}
        self.assertEqual([verdict(HTTPError(429), n, "p", settings)["retry_after"] for n in (1, 2, 3)], [2.0, 6.0, 18.0])
        header = HTTPError(503, {"retry-after": "7"})
        self.assertEqual(verdict(header, 1, "p", settings), {"retry_after": 7.0})
        self.assertEqual(verdict(header, 1, "p", {**settings, "respect_retry_after": False}), {"retry_after": 2.0})

    def test_max_attempts_zero_goes_straight_to_the_fallback(self):
        verdict = self.ext.verdict
        settings = {"max_attempts": 0, "fallback": "openai"}
        self.assertEqual(verdict(HTTPError(429), 1, "anthropic", settings), {"provider": "openai"})

    def test_messages_and_notify_are_settings(self):
        app = self.make({"base_delay": 0, "retry_message": "wait {delay:.1f}s, try {attempt} of {max_attempts} ({error})"})
        self.run_failing(app, [HTTPError(429)])
        self.frontend.notify.assert_called_once_with("wait 0.0s, try 1 of 5 (HTTPError)", "warning")
        self.frontend.notify.reset_mock()
        app = self.make({"base_delay": 0, "notify": False})
        self.run_failing(app, [HTTPError(429)])
        self.frontend.notify.assert_not_called()
        self.assertEqual(len(app.provider.requests), 2)

    def test_fallback_message_is_a_setting(self):
        app = self.make({"max_attempts": 0, "fallback": "fake", "fallback_message": "now on {fallback}!"})
        # The provider is already "fake": the verdict gives up, so the message is built only for a real switch.
        out = self.run_failing(app, [HTTPError(429)], replies=[])
        self.frontend.notify.assert_not_called()
        self.assertEqual(len(app.provider.requests), 1)
        self.assertNotIn("now on", out)
        ext = self.ext
        self.assertEqual(ext.verdict(HTTPError(429), 1, "anthropic", {"max_attempts": 0, "fallback": "fake"}), {"provider": "fake"})

    def test_progress_counts_down_the_wait_and_clears_it(self):
        app = self.make({"base_delay": 0.3, "progress": True})
        with mock.patch.object(self.ext, "PROGRESS_STEP", 0.02):
            self.run_failing(app, [HTTPError(429)])
        notes = [call.args[0] for call in self.frontend.progress.call_args_list]
        self.assertGreaterEqual(len(notes), 3)
        self.assertTrue(all(n is None or n.startswith("retry: HTTPError (1/5), again in ") for n in notes))
        self.assertIsNone(notes[-1])
        self.assertEqual(len(app.provider.requests), 2)
        self.assertEqual(app.messages[-1]["content"][0]["text"], "ok")

    def test_the_countdown_waits_the_whole_delay(self):
        app = self.make({"base_delay": 0.2, "progress": True})
        started = time.monotonic()
        with mock.patch.object(self.ext, "PROGRESS_STEP", 0.05):
            self.run_failing(app, [HTTPError(429)])
        self.assertGreaterEqual(time.monotonic() - started, 0.19)

    def test_esc_interrupts_the_countdown(self):
        app = self.make({"base_delay": 30, "progress": True})
        self.frontend.progress.side_effect = lambda text: app.cancelled.set() if text else None
        started = time.monotonic()
        self.run_failing(app, [HTTPError(429)])
        self.assertLess(time.monotonic() - started, 5)
        self.frontend.turn_end.assert_called_with("interrupted by the user")
        self.assertEqual(len(app.provider.requests), 1)
        self.assertIsNone(self.frontend.progress.call_args_list[-1].args[0])

    def test_bad_settings_are_reported_with_the_fix(self):
        for settings, problem in (
            ({"max_attempts": -1}, "extensions.settings.retry.max_attempts"),
            ({"factor": 0.5}, "extensions.settings.retry.factor"),
            ({"base_delay": -2}, "extensions.settings.retry.base_delay"),
        ):
            with self.subTest(settings=settings):
                app, errors = self.build(settings)
                self.assertTrue(any(problem in e for e in errors), errors)

    def test_inconsistent_settings_are_warnings_and_the_key_is_back_to_its_default(self):
        for settings, problem in (
            ({"statuses": [99]}, "statuses has 99, which is not an HTTP status (100-599)"),
            ({"statuses": ["429"]}, "statuses must be a list of whole numbers"),
            ({"network_errors": [1]}, "network_errors must be a list of strings"),
            ({"match": [None]}, "match must be a list of strings"),
            ({"fallback": "openai/"}, 'fallback \'openai/\' must be "provider" or "provider/model"'),
            ({"fallback": " openai"}, "must be \"provider\" or \"provider/model\""),
            ({"retry_message": "{nope}"}, "retry_message is not a valid template"),
            ({"fallback_message": "{error}"}, "fallback_message is not a valid template"),
        ):
            with self.subTest(settings=settings):
                app, errors = self.build(settings)
                self.assertEqual(errors, [])
                (warning,) = self.warnings
                self.assertTrue(warning.startswith("retry: extensions.settings.retry."), warning)
                self.assertIn(problem, warning)
                self.assertIn("the default is used until you fix it", warning)
                self.assertTrue(app.registry._handlers.get("provider_error"))

    def test_a_bad_status_list_does_not_stop_the_retry_of_the_default_statuses(self):
        app, errors = self.build({"base_delay": 0, "statuses": ["429"]})
        self.assertEqual(len(self.warnings), 1)
        self.run_failing(app, [HTTPError(503)])
        self.assertEqual(len(app.provider.requests), 2)  # 503 is a default status: retried

    def test_it_declares_the_api_it_uses(self):
        self.assertIn('__api__ = "2.4"', (MARKETPLACE / "extensions" / "retry.py").read_text())


# --- claude_subscription -------------------------------------------------------------------------------------------------

CLAUDE = MARKETPLACE / "extensions" / "claude_subscription.py"


@unittest.skipUnless(CLAUDE.is_file(), "the marketplace checkout has no claude_subscription item yet")
class ClaudeSubscriptionSettingsTest(ContextTest):
    item = "claude_subscription"

    def provider(self, captured):
        """A ``ClaudeProvider`` whose client records the request of ``stream`` in ``captured`` and replies with one text."""

        def stream(**kwargs):
            captured.update(kwargs)
            final = SimpleNamespace(content=[], stop_reason="end_turn", usage=None)
            return nullcontext(SimpleNamespace(text_stream=iter(()), get_final_message=lambda: final))

        return self.ext.ClaudeProvider(client=SimpleNamespace(messages=SimpleNamespace(stream=stream)))

    def test_the_spec_keeps_its_defaults_and_knows_the_context_window(self):
        app = self.make()
        spec = app.registry.providers["claude"]
        self.assertEqual((spec.default_model, dict(spec.context_windows)), ("claude-sonnet-5-5", {"claude-": 200_000}))
        app.provider_name, app.model = "claude", spec.default_model
        self.assertEqual(app.context.context_window, 200_000)  # the built-in table only knows `anthropic`

    def test_settings_change_the_default_model_and_the_windows(self):
        app = self.make({"default_model": "claude-opus-9", "context_windows": {"claude-": 200_000, "claude-opus": 1_000_000}})
        spec = app.registry.providers["claude"]
        self.assertEqual(spec.default_model, "claude-opus-9")
        app.provider_name, app.model = "claude", "claude-opus-9"
        self.assertEqual(app.context.context_window, 1_000_000)
        app.model = "claude-haiku"
        self.assertEqual(app.context.context_window, 200_000)

    def test_the_context_window_setting_still_wins(self):
        app = self.make(top={"context_window": 1234})
        app.provider_name, app.model = "claude", "claude-sonnet-5-5"
        self.assertEqual(app.context.context_window, 1234)

    def test_bad_settings_are_reported_with_the_fix(self):
        for settings, problem in (
            ({"default_model": " "}, "default_model must not be empty; remove it to use claude-sonnet-5-5"),
            ({"context_windows": {"claude-": 0}}, "context_windows['claude-'] must be a positive whole number of tokens"),
            ({"context_windows": {"claude-": "big"}}, "context_windows['claude-'] must be a positive whole number"),
            ({"context_windows": []}, "extensions.settings.claude_subscription.context_windows"),
        ):
            with self.subTest(settings=settings):
                app, errors = self.build(settings)
                self.assertTrue(any(problem in e for e in errors), errors)
                if "extensions.settings" not in problem:  # a wrong type keeps the default; a bad value is not registered
                    self.assertNotIn("claude", app.registry.providers)

    def test_reasoning_effort_turns_on_thinking_and_the_signed_thinking_goes_back(self):
        self.make()
        sent = {}
        provider = self.provider(sent)
        history = [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                    {"type": "thinking", "thinking": "other provider's", "signature": ""},
                    {"type": "text", "text": "a"},
                ],
            },
            {"role": "user", "content": "again"},
        ]
        provider.stream(model="m", system="s", messages=history, tools=[], on_text=None, options={"reasoning_effort": "high"})
        self.assertEqual(sent["thinking"], {"type": "enabled", "budget_tokens": 16000})
        self.assertEqual(sent["system"][0], {"type": "text", "text": self.ext.CLAUDE_CODE_IDENTITY})
        self.assertEqual(
            [b["type"] for b in sent["messages"][1]["content"]], ["thinking", "text"]
        )  # only the signed block goes back
        sent.clear()
        provider.stream(model="m", system="s", messages=history, tools=[], on_text=None)
        self.assertNotIn("thinking", sent)
        self.assertEqual([b["type"] for b in sent["messages"][1]["content"]], ["text"])

    def test_pictures_in_messages_and_tool_results_are_sent_as_the_api_wants(self):
        from minimal.api import image_block

        self.make()
        sent = {}
        provider = self.provider(sent)
        picture = image_block(b"\x89PNG\r\n\x1a\n", "image/png")
        history = [
            {"role": "user", "content": [{"type": "text", "text": "look"}, picture]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": [picture]}]},
        ]
        provider.stream(model="m", system="s", messages=history, tools=[], on_text=None)
        self.assertEqual(sent["messages"][0]["content"][1], picture)
        self.assertEqual(sent["messages"][2]["content"][0]["content"], [picture])

    def test_it_declares_the_api_it_uses(self):
        self.assertIn('__api__ = "2.3"', CLAUDE.read_text())


if __name__ == "__main__":
    unittest.main()
