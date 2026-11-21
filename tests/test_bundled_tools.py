import io
import json
import os
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

from minimal import config
from minimal.extensions.loader import load_extensions
from minimal.extensions.registry import Registry

from tests.test_bundled_extensions import MARKETPLACE, BundledExtensionTest


class ToolTest(BundledExtensionTest):
    NAME = ""

    def load(self, **settings):
        """Load the tool with ``settings``; returns ``(registry, errors)``."""
        if settings:
            (self.tmp / "settings.json").write_text(
                json.dumps({"extensions": {"settings": {f"tools/{self.NAME}": settings}}})
            )
        registry = Registry()
        layers, _ = config.read_layers(str(self.tmp))
        _, errors = load_extensions(
            registry, str(self.tmp), config.Settings(layers=tuple(layers), trusted=True), extra=[MARKETPLACE / "tools" / f"{self.NAME}.py"]
        )
        return registry, errors


class WebSearchSettingsTest(ToolTest):
    NAME = "web_search"
    PAYLOAD = {"web": {"results": [{"title": "T", "url": "https://a.test", "description": "word " * 30}]}}

    def search(self, payload=PAYLOAD, **inp):
        """``(output, request, progress notes)`` of a search with the key set and the API answering ``payload``."""
        sent, notes = [], []
        registry = self.registry

        def urlopen(request, timeout):
            sent.append((request, timeout))
            return nullcontext(io.BytesIO(json.dumps(payload).encode()))

        ctx = SimpleNamespace(cwd=str(self.tmp), progress=notes.append)
        env = {"BRAVE_API_KEY": "k", "ALT_KEY": "alt"}
        with mock.patch.dict(os.environ, env), mock.patch("urllib.request.urlopen", urlopen):
            out = registry.tools["web_search"].run(inp, ctx)
        return out, sent[0], notes

    def use(self, **settings):
        self.registry, errors = self.load(**settings)
        self.assertEqual(errors, [])

    def test_defaults_are_unchanged_and_the_tool_may_run_in_parallel(self):
        self.use()
        self.assertTrue(self.registry.tools["web_search"].read_only)
        out, (request, timeout), notes = self.search(query="docs")
        self.assertEqual(request.full_url, "https://api.search.brave.com/res/v1/web/search?q=docs&count=5")
        self.assertEqual(timeout, 20)
        self.assertEqual(request.get_header("User-agent"), None)
        self.assertIn("word " * 30, out.replace("  ", " "))
        self.assertEqual(notes, ["searching: docs", None])

    def test_count_max_count_and_the_schema_follow_the_settings(self):
        self.use(count=3, max_count=7)
        self.assertIn("default 3, max 7", self.registry.tools["web_search"].input_schema["properties"]["count"]["description"])
        self.assertTrue(self.search(query="x")[1][0].full_url.endswith("count=3"))
        self.assertTrue(self.search(query="x", count=50)[1][0].full_url.endswith("count=7"))

    def test_timeout_user_agent_and_endpoint_are_sent(self):
        self.use(timeout=5, user_agent="me/1", endpoint="https://search.test/api")
        _, (request, timeout), _ = self.search(query="x")
        self.assertEqual(timeout, 5)
        self.assertEqual(request.get_header("User-agent"), "me/1")
        self.assertTrue(request.full_url.startswith("https://search.test/api?q=x"))

    def test_country_language_and_safesearch_are_added_when_set(self):
        self.use(country="DE", language="de", safesearch="strict")
        url = self.search(query="x")[1][0].full_url
        self.assertTrue(url.endswith("&country=DE&search_lang=de&safesearch=strict"), url)

    def test_clip_cuts_snippets_with_an_ellipsis(self):
        self.use(clip=12)
        out = self.search(query="x")[0]
        self.assertEqual(len(out.splitlines()[2]), 12)
        self.assertTrue(out.splitlines()[2].endswith("…"))

    def test_the_key_variable_is_a_setting_and_the_error_names_it(self):
        self.use(key_env="ALT_KEY")
        self.assertEqual(self.search(query="x")[1][0].get_header("X-subscription-token"), "alt")
        self.use(key_env="NOPE_KEY")
        with mock.patch.dict(os.environ):
            os.environ.pop("NOPE_KEY", None)
            with self.assertRaisesRegex(ValueError, "NOPE_KEY is not set"):
                self.registry.tools["web_search"].run({"query": "x"}, SimpleNamespace(progress=print))

    def test_progress_can_be_turned_off(self):
        self.use(progress=False)
        self.assertEqual(self.search(query="x")[2], [])

    def test_bad_values_are_load_errors_that_keep_the_default(self):
        for settings, needle in (
            ({"count": 0}, "web_search.count"),
            ({"timeout": 0}, "web_search.timeout"),
            ({"safesearch": "max"}, "web_search.safesearch"),
            ({"clip": "x"}, "web_search.clip"),
        ):
            with self.subTest(settings):
                registry, errors = self.load(**settings)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(needle, errors[0])
                self.assertIn("web_search", registry.tools)

    def test_a_count_above_max_count_is_reported_with_the_fix(self):
        _, errors = self.load(count=9, max_count=3)
        self.assertEqual(len(errors), 1)
        self.assertIn("lower count or raise max_count", errors[0])


class AskUserSettingsTest(ToolTest):
    NAME = "ask_user"

    def use(self, **settings):
        registry, errors = self.load(**settings)
        self.assertEqual(errors, [])
        self.calls, self.reply = [], None
        ui = SimpleNamespace(
            ask_text=lambda question: self.calls.append(("ask_text", question)) or self.reply,
            choose=lambda title, options: self.calls.append(("choose", title, options)) or self.reply,
        )
        self.tool, self.ctx = registry.tools["ask_user"], SimpleNamespace(ui=ui)

    def test_defaults_are_unchanged_and_the_tool_is_not_read_only(self):
        self.use()
        self.assertFalse(self.tool.read_only)  # it asks the user: never concurrently
        self.assertIn("at most 9", self.tool.input_schema["properties"]["options"]["description"])
        self.reply = "0"
        self.tool.run({"question": "q", "options": [str(i) for i in range(12)]}, self.ctx)
        self.assertEqual(len(self.calls[0][2]), 9)
        self.reply = None
        with self.assertRaisesRegex(ValueError, "The user gave no answer .cancelled, empty reply"):
            self.tool.run({"question": "q"}, self.ctx)

    def test_max_options_changes_the_cap_and_the_schema(self):
        self.use(max_options=3)
        self.assertIn("at most 3", self.tool.input_schema["properties"]["options"]["description"])
        self.reply = "a"
        self.tool.run({"question": "q", "options": list("abcde")}, self.ctx)
        self.assertEqual([value for value, _ in self.calls[0][2]], ["a", "b", "c"])

    def test_no_answer_text_is_a_setting(self):
        self.use(no_answer="Stop and summarise what you need.")
        with self.assertRaisesRegex(ValueError, "^Stop and summarise what you need.$"):
            self.tool.run({"question": "q"}, self.ctx)

    def test_bad_values_are_load_errors_naming_the_setting(self):
        for settings, needle in (({"max_options": 0}, "ask_user.max_options"), ({"no_answer": 3}, "ask_user.no_answer")):
            with self.subTest(settings):
                _, errors = self.load(**settings)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(needle, errors[0])


if __name__ == "__main__":
    unittest.main()
