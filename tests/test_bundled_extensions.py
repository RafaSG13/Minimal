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

from minimal.core import session as session_mod
from minimal.core.app import App
from minimal.core.session import Session
from minimal import config
from minimal.extensions import loader
from minimal.extensions.loader import load_extensions
from minimal.extensions.registry import Registry
from minimal.providers.base import Reply, Usage

# Absolute: the tests chdir into temporary directories.
MARKETPLACE = Path(
    os.environ.get("MARKETPLACE_CHECKOUT") or Path(__file__).resolve().parents[2] / "minimal-marketplace"
).resolve()


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


class InterruptingProvider(RecordingProvider):
    """Like RecordingProvider, but ``app.cancelled`` is set while the second request (the summary) is in flight."""

    def __init__(self, app_getter, replies, stream_chunks):
        super().__init__(replies)
        self.app_getter = app_getter
        self.stream_chunks = stream_chunks

    def stream(self, **kw):
        reply = super().stream(**kw)
        if len(self.requests) == 2:
            self.app_getter().cancelled.set()
            if self.stream_chunks and kw["on_text"]:
                kw["on_text"]("partial")  # a real provider streams a chunk; raising here aborts the request
        return reply


@unittest.skipUnless(MARKETPLACE.is_dir(), "no marketplace checkout; set MARKETPLACE_CHECKOUT")
class BundledExtensionTest(unittest.TestCase):
    """Loads one extension of the marketplace checkout with no global or project extensions around."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for patcher in (
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.tmp / "global"),
            mock.patch.object(config, "SETTINGS_FILE", self.tmp / "settings.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_app(self, extension, session, replies):
        """An ``App`` with only ``extension`` loaded, whose provider replays ``replies``."""
        app = App(
            registry=Registry(),
            session=session,
            cwd=str(self.tmp),
            auto_approve=True,
            trust_project=True,
            extra_extensions=[str(MARKETPLACE / extension)],
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
        _, errors = load_extensions(
            self.registry, str(self.tmp), config.Settings(), extra=[MARKETPLACE / "tools" / "web_search.py"]
        )
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

    def test_subagent_sees_tools_reloaded_during_its_run(self):
        ext_dir = self.tmp / ".minimal" / "extensions"
        ext_dir.mkdir(parents=True)
        hello = ext_dir / "hello.py"
        hello.write_text('__api__ = "1"\ndef register(minimal):\n    minimal.register_tool(name="hello", run=lambda i, c: "v1")\n')
        call = {"type": "tool_use", "id": "t1", "name": "subagent", "input": {"prompt": "reload"}}
        reload = {"type": "tool_use", "id": "r1", "name": "reload_extensions", "input": {}}
        use = [
            {"type": "tool_use", "id": "h1", "name": "hello", "input": {}},
            {"type": "tool_use", "id": "b1", "name": "bye", "input": {}},
        ]
        app = self.make_app(
            "tools/subagent.py",
            Session("t", self.tmp / "t.jsonl"),
            [
                Reply(content=[call], stop_reason="tool_use"),
                Reply(content=[reload], stop_reason="tool_use"),
                Reply(content=use, stop_reason="tool_use"),
                text_reply("sub done"),
                text_reply("ok"),
            ],
        )
        hello.write_text(
            '__api__ = "1"\n'
            "def register(minimal):\n"
            '    minimal.register_tool(name="hello", run=lambda i, c: "v2")\n'
            '    minimal.register_tool(name="bye", run=lambda i, c: "bye")\n'
        )
        self.run_turn(app, "go")
        _, _, after_reload, last_inner, _ = app.provider.requests
        self.assertIn("bye", after_reload.tools)
        self.assertNotIn("subagent", after_reload.tools)
        results = {r["tool_use_id"]: r["content"] for r in last_inner.messages[-1]["content"]}
        self.assertEqual(results, {"h1": "v2", "b1": "bye"})


class CompactTest(BundledExtensionTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(session_mod, "SESSIONS_DIR", self.tmp / "sessions")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = self.make_app("extensions/compact.py", session_mod.open_session("old"), [])
        self.ext = sys.modules[loader.module_name(MARKETPLACE / "extensions" / "compact.py")]

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
        self.assertEqual(len(session_mod.open_session("old").messages), 2)  # the full history stays in the old session
        self.assertEqual(len(session_mod.open_session(app.session.id).messages), 4)
        self.assertEqual(app.provider.requests[1].messages[-1], {"role": "user", "content": ext.SUMMARY_REQUEST})

    def test_below_the_threshold_nothing_happens(self):
        ext, app = self.ext, self.app
        app.provider.replies = [text_reply("answer", usage=Usage(input_tokens=ext.COMPACT_AT - 2, output_tokens=1))]
        self.run_turn(app, "q")
        self.assertEqual(app.session.id, "old")
        self.assertEqual(len(app.provider.requests), 1)

    def interrupted_turn(self, stream_chunks):
        """Run a turn that reaches the threshold, with Esc pressed while the summary request is in flight."""
        ext, app = self.ext, self.app
        app.provider = InterruptingProvider(
            lambda: app,
            [text_reply("answer", usage=Usage(input_tokens=ext.COMPACT_AT + 1, output_tokens=1)), text_reply("SUMMARY")],
            stream_chunks,
        )
        out = self.run_turn(app, "q")
        self.assertEqual(app.session.id, "old")
        self.assertEqual([m["role"] for m in app.messages], ["user", "assistant"])  # the answer was complete: kept
        self.assertEqual(session_mod.list_sessions(), ["old"])  # holds the kept answer; no compacted session was created
        self.assertIn("interrupted by the user", out)
        self.assertNotIn("[extension] error", out)

    def test_interrupt_while_summarising_streams(self):
        self.interrupted_turn(stream_chunks=True)

    def test_interrupt_after_summary_reply_is_not_applied(self):
        self.interrupted_turn(stream_chunks=False)

    def test_compact_command_ignores_a_stale_interrupt(self):
        app = self.app
        app.messages[:] = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
        ]
        app.provider.replies = [text_reply("SUMMARY")]
        app.cancelled.set()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            app.run_command("/compact")
        self.assertNotEqual(app.session.id, "old")
        self.assertEqual(app.messages[0]["content"], self.ext.SUMMARY_PREFIX + "SUMMARY")

    def test_long_turn_is_summarised_before_the_next_request(self):
        ext, app = self.ext, self.app
        (self.tmp / "f.txt").write_text("x")
        app.messages[:] = [
            {"role": "user", "content": "q0"},
            {"role": "assistant", "content": [{"type": "text", "text": "a0"}]},
        ]
        app.provider.replies = [
            Reply(
                content=[{"type": "tool_use", "id": "r1", "name": "read", "input": {"path": "f.txt"}}],
                stop_reason="tool_use",
                usage=Usage(input_tokens=ext.COMPACT_AT, output_tokens=1),
            ),
            text_reply("SUMMARY"),
            text_reply("done"),
        ]
        self.run_turn(app, "q")
        requests = app.provider.requests
        self.assertEqual(len(requests), 3)
        self.assertTrue(requests[1].messages[-1]["content"].endswith(ext.SUMMARY_REQUEST))
        self.assertEqual(requests[2].messages[0]["content"], ext.SUMMARY_PREFIX + "SUMMARY")
        self.assertEqual([m["role"] for m in requests[2].messages], ["user", "assistant", "user"])
        self.assertNotEqual(app.session.id, "old")
        self.assertEqual([m["role"] for m in app.messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual(len(session_mod.open_session("old").messages), 6)


class FailingProvider(RecordingProvider):
    """Like RecordingProvider, but each request raises the next of ``errors`` while any are left."""

    def __init__(self, errors, replies):
        super().__init__(replies)
        self.errors = list(errors)

    def stream(self, **kw):
        if self.errors:
            self.requests.append(SimpleNamespace(messages=list(kw["messages"]), tools=[]))
            raise self.errors.pop(0)
        return super().stream(**kw)


class HTTPError(Exception):
    def __init__(self, status_code, headers=None):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers or {})


class RetryTest(BundledExtensionTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(session_mod, "SESSIONS_DIR", self.tmp / "sessions")
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.tmp / "settings.json").write_text(json.dumps({"extensions": {"settings": {"retry": {"base_delay": 0}}}}))
        self.app = self.make_app("extensions/retry.py", session_mod.open_session("s"), [])
        self.ext = sys.modules[loader.module_name(MARKETPLACE / "extensions" / "retry.py")]

    def test_rate_limit_is_retried(self):
        app = self.app
        app.provider = FailingProvider([HTTPError(429)], [text_reply("ok")])
        self.run_turn(app, "q")
        self.assertEqual(len(app.provider.requests), 2)
        self.assertEqual(app.messages[-1]["content"][0]["text"], "ok")

    def test_client_error_is_not_retried(self):
        app = self.app
        app.provider = FailingProvider([HTTPError(400)], [text_reply("ok")])
        self.run_turn(app, "q")
        self.assertEqual(len(app.provider.requests), 1)
        self.assertEqual(app.messages, [])

    def test_verdict_backoff_cap_and_single_fallback(self):
        verdict = self.ext.verdict
        settings = {"max_attempts": 2, "base_delay": 2.0, "max_delay": 5.0, "fallback": "openai/gpt-x"}
        self.assertEqual(verdict(HTTPError(429), 2, "anthropic", settings), {"retry_after": 4.0})
        self.assertEqual(verdict(HTTPError(503, {"retry-after": "30"}), 1, "anthropic", settings), {"retry_after": 5.0})
        self.assertEqual(verdict(HTTPError(529), 3, "anthropic", settings), {"provider": "openai", "model": "gpt-x"})
        self.assertIsNone(verdict(HTTPError(529), 4, "openai", settings))


class ScopeFenceTest(BundledExtensionTest):
    def test_fence_lives_outside_the_repository(self):
        for patcher in (
            mock.patch.object(config, "HOME_DIR", self.tmp / "home"),
            mock.patch.object(session_mod, "SESSIONS_DIR", self.tmp / "sessions"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        planted = self.tmp / ".minimal" / "state" / "fence.json"
        planted.parent.mkdir(parents=True)
        planted.write_text(json.dumps({"globs": ["docs/**"], "lines": None, "blocked": []}))
        before = planted.read_text()
        write = {"type": "tool_use", "id": "w1", "name": "write", "input": {"path": "src/x.py", "content": "x"}}
        app = self.make_app(
            "extensions/scope_fence.py",
            session_mod.open_session("s"),
            [Reply(content=[write], stop_reason="tool_use"), text_reply("ok")],
        )
        self.run_turn(app, "go")
        self.assertTrue((self.tmp / "src" / "x.py").exists())  # the planted fence is ignored
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            app.run_command("/fence docs")
        self.assertEqual(len(list((self.tmp / "home" / "data" / "projects").rglob("state.json"))), 1)
        self.assertEqual(planted.read_text(), before)


if __name__ == "__main__":
    unittest.main()
