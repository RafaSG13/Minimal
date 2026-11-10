import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from minimal import extensions
from minimal import session as session_mod
from minimal.app import App
from minimal.extensions import load_extensions
from minimal.providers.base import Reply, Usage
from minimal.registry import Registry
from minimal.session import Session

BUNDLED = Path(__file__).resolve().parent.parent / "extensions"


class RecordingProvider:
    """Replays ``replies`` in order, recording each request's messages and tool names."""

    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def stream(self, **kw):
        # Copy the messages: the agent loop appends to the same list after the request.
        self.requests.append(SimpleNamespace(messages=list(kw["messages"]), tools=[t["name"] for t in kw["tools"]]))
        return self.replies.pop(0)


def text_reply(text, **kw):
    return Reply(content=[{"type": "text", "text": text}], stop_reason="end_turn", **kw)


class BundledExtensionTest(unittest.TestCase):
    """Loads one extension of this repo's ``extensions/`` folder with no global or project extensions around."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.object(extensions, "GLOBAL_EXTENSIONS_DIR", self.tmp / "global")
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_app(self, extension, session, replies):
        """An ``App`` with only ``extension`` loaded, whose provider replays ``replies``."""
        app = App(
            registry=Registry(),
            session=session,
            cwd=str(self.tmp),
            auto_approve=True,
            extra_extensions=[str(BUNDLED / extension)],
        )
        _, errors = app.reload_extensions()
        self.assertEqual(errors, [])
        app.provider, app.model = RecordingProvider(replies), "m"
        return app

    def run_turn(self, app, text):
        """Run one turn with the console output captured; returns it (stdout and stderr together)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            app.run_turn(text)
        return out.getvalue()


class WebSearchTest(BundledExtensionTest):
    def setUp(self):
        super().setUp()
        self.registry = Registry()
        _, errors = load_extensions(self.registry, str(self.tmp), extra=[BUNDLED / "tools" / "web_search.py"])
        self.assertEqual(errors, [])
        self.ctx = SimpleNamespace(cwd=str(self.tmp))

    def search(self, payload, **inp):
        """Run the tool with ``BRAVE_API_KEY`` set and the API answering ``payload``; return ``(output, request)``."""
        sent = []

        def urlopen(request, timeout):
            sent.append(request)
            return nullcontext(io.BytesIO(json.dumps(payload).encode()))

        with mock.patch.dict(os.environ, {"BRAVE_API_KEY": "k"}), mock.patch("urllib.request.urlopen", urlopen):
            return self.registry.tools["web_search"].run(inp, self.ctx), sent[0]

    def test_results_are_numbered_and_cleaned(self):
        payload = {
            "web": {
                "results": [
                    {
                        "title": "<strong>Textual</strong> &amp; Rich",
                        "url": "https://textual.textualize.io",
                        "description": "A <strong>TUI</strong> framework &amp; more",
                    },
                    {"title": "Second", "url": "https://example.com", "description": "Plain"},
                ]
            }
        }
        output, request = self.search(payload, query=" textual docs ", count=50)
        self.assertEqual(
            output,
            "1. Textual & Rich\nhttps://textual.textualize.io\nA TUI framework & more\n\n"
            "2. Second\nhttps://example.com\nPlain",
        )
        self.assertEqual(
            request.full_url, "https://api.search.brave.com/res/v1/web/search?q=textual+docs&count=20"
        )
        self.assertEqual(request.get_header("X-subscription-token"), "k")

    def test_no_results(self):
        self.assertEqual(self.search({"web": {"results": []}}, query="x")[0], "No results")

    def test_missing_key(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("BRAVE_API_KEY", None)
            with self.assertRaisesRegex(ValueError, "BRAVE_API_KEY"):
                self.registry.tools["web_search"].run({"query": "x"}, self.ctx)


class SubagentTest(BundledExtensionTest):
    def test_subagent_has_its_own_history_and_cannot_nest(self):
        call = {"type": "tool_use", "id": "t1", "name": "subagent", "input": {"prompt": "find x"}}
        app = self.make_app(
            "tools/subagent.py",
            Session("t", self.tmp / "t.jsonl"),
            [Reply(content=[call], stop_reason="tool_use"), text_reply("sub done"), text_reply("ok")],
        )
        self.run_turn(app, "go")
        outer, inner, final = app.provider.requests
        self.assertEqual(app.messages[2]["content"][0]["content"], "sub done")
        self.assertEqual(inner.messages, [{"role": "user", "content": "find x"}])
        self.assertNotIn("subagent", inner.tools)
        self.assertIn("subagent", final.tools)


class CompactTest(BundledExtensionTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(session_mod, "SESSIONS_DIR", self.tmp / "sessions")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = self.make_app("compact.py", session_mod.open_session("old"), [])
        self.ext = sys.modules["minimal_ext_compact"]

    def test_compacts_into_a_new_session_at_the_threshold(self):
        ext, app = self.ext, self.app
        app.provider.replies = [
            text_reply("answer", usage=Usage(input_tokens=ext.COMPACT_AT, output_tokens=1)),
            text_reply("SUMMARY"),
        ]
        self.run_turn(app, "q")
        self.assertNotEqual(app.session.id, "old")
        self.assertEqual(
            [m["content"] if isinstance(m["content"], str) else m["content"][0]["text"] for m in app.messages],
            [ext.SUMMARY_PREFIX + "SUMMARY", ext.ACK, "q", "answer"],
        )
        self.assertEqual(len((self.tmp / "sessions" / "old.jsonl").read_text().splitlines()), 2)
        self.assertEqual(len(app.session.file.read_text().splitlines()), 4)
        self.assertEqual(app.provider.requests[1].messages[-1], {"role": "user", "content": ext.SUMMARY_REQUEST})

    def test_below_the_threshold_nothing_happens(self):
        ext, app = self.ext, self.app
        app.provider.replies = [text_reply("answer", usage=Usage(input_tokens=ext.COMPACT_AT - 2, output_tokens=1))]
        self.run_turn(app, "q")
        self.assertEqual(app.session.id, "old")
        self.assertEqual(len(app.provider.requests), 1)


if __name__ == "__main__":
    unittest.main()
