import base64
import contextlib
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from textual.widgets import Static

from minimal import config
from minimal.core import session as session_mod
from minimal.core.agent import execute_tool_call
from minimal.core.app import App
from minimal.core.host import Host
from minimal.core.session import Session
from minimal.extensions import loader
from minimal.extensions.registry import Registry
from minimal.providers.anthropic import AnthropicProvider, thinking_request, to_request_messages
from minimal.providers.base import IMAGE_TYPES, Reply, image_block, image_summary, result_text
from minimal.providers.gemini import GeminiProvider, read_stream, to_interaction_input, to_reply
from minimal.providers.openai import OpenAIProvider, read_chunks, to_chat_messages
from minimal.tools import files
from minimal.tools.builtin import register_builtin_tools
from minimal.ui.commands import register_builtin_commands
from minimal.ui.tui.app import MinimalTui
from minimal.ui.tui.composer import Composer
from minimal.ui.tui.widgets import ToolBlock
from tests.test_security import TempDirTest

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAHnOcQAAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG).decode()
IMAGE = image_block(PNG, "image/png")

HISTORY = [
    {"role": "user", "content": [{"type": "text", "text": "what is this?"}, IMAGE]},
    {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "a pixel", "signature": "sig-1"},
            {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "p.png"}},
        ],
        "provider_data": {"provider": "anthropic", "data": "x"},
        "usage": {"input_tokens": 1, "output_tokens": 1},
    },
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "t1",
                "content": [{"type": "text", "text": "p.png (image/png, 70 bytes)"}, IMAGE],
                "is_error": False,
            }
        ],
    },
    {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "no signature", "signature": None},
            {"type": "text", "text": "a red pixel"},
        ],
    },
]


class ImageBlockTest(unittest.TestCase):
    def test_image_block_encodes_bytes_and_takes_base64_text(self):
        self.assertEqual(
            IMAGE, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64}}
        )
        self.assertEqual(image_block(PNG_B64, "image/png"), IMAGE)

    def test_media_type_must_be_one_providers_send(self):
        with self.assertRaisesRegex(ValueError, "media_type must be one of"):
            image_block(b"x", "image/bmp")
        self.assertEqual(IMAGE_TYPES, ("image/png", "image/jpeg", "image/gif", "image/webp"))

    def test_text_forms_never_contain_the_data(self):
        self.assertEqual(image_summary(IMAGE), f"[image: image/png, {len(PNG)} bytes]")
        self.assertEqual(result_text([{"type": "text", "text": "a "}, IMAGE]), f"a [image: image/png, {len(PNG)} bytes]")
        self.assertEqual(result_text([{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "ok"}]), "ok")
        self.assertEqual(image_summary({"type": "image"}), "[image: unknown, 0 bytes]")


class ReadImageTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.registry = Registry()
        register_builtin_tools(self.registry)
        self.ctx = NS(cwd=str(self.cwd), cancelled=False, session_id="s")

    def read(self, name):
        return self.registry.tools["read"].run({"path": name}, self.ctx)

    def test_pictures_come_back_as_a_text_block_and_an_image_block(self):
        samples = {
            "a.png": (PNG, "image/png"),
            "b.jpg": (b"\xff\xd8\xff\xe0" + b"\0" * 20, "image/jpeg"),
            "c.gif": (b"GIF89a" + b"\0" * 20, "image/gif"),
            "d.webp": (b"RIFF\x1a\0\0\0WEBPVP8 " + b"\0" * 20, "image/webp"),
            "no-extension": (PNG, "image/png"),  # the first bytes decide, not the name
        }
        for name, (data, media_type) in samples.items():
            with self.subTest(name=name):
                (self.cwd / name).write_bytes(data)
                note, picture = self.read(name)
                self.assertEqual(note["type"], "text")
                self.assertIn(f"{media_type}, {len(data)} bytes", note["text"])
                self.assertEqual(picture, image_block(data, media_type))

    def test_a_picture_over_the_limit_is_reported_not_sent(self):
        (self.cwd / "big.png").write_bytes(PNG + b"\0" * files.IMAGE_MAX_BYTES)
        out = self.read("big.png")
        self.assertIsInstance(out, str)
        self.assertIn("is a binary file", out)
        self.assertIn("images over", out)

    def test_other_files_stay_text(self):
        (self.cwd / "t.txt").write_text("hello\n")
        (self.cwd / "fake.png").write_text("this is not a picture\n")
        self.assertEqual(self.read("t.txt"), "1\thello\n2\t")
        self.assertEqual(self.read("fake.png"), "1\tthis is not a picture\n2\t")

    def test_the_tool_result_carries_the_picture_and_its_handlers_only_its_text(self):
        (self.cwd / "p.png").write_bytes(PNG)
        seen = []
        self.registry.on("tool_result", lambda e: seen.append(e["output"]))
        result = execute_tool_call({"id": "t1", "name": "read", "input": {"path": "p.png"}}, self.registry, self.ctx)
        self.assertFalse(result["is_error"])
        self.assertEqual(result["content"][1], IMAGE)
        self.assertEqual(len(seen), 1)
        self.assertIn(f"[image: image/png, {len(PNG)} bytes]", seen[0])
        self.assertNotIn(PNG_B64, seen[0])

    def test_a_tool_result_handler_rewrite_replaces_the_picture_with_text(self):
        (self.cwd / "p.png").write_bytes(PNG)
        self.registry.on("tool_result", lambda e: {"output": "redacted"})
        result = execute_tool_call({"id": "t1", "name": "read", "input": {"path": "p.png"}}, self.registry, self.ctx)
        self.assertEqual(result["content"], "redacted")

    def test_an_extension_tool_may_return_blocks_and_other_values_stay_text(self):
        self.registry.register_tool("shot", lambda i, c: [{"type": "text", "text": "hi"}, IMAGE])
        self.registry.register_tool("junk", lambda i, c: [1, 2])
        self.registry.register_tool("empty", lambda i, c: [])
        run = lambda name: execute_tool_call({"id": "t", "name": name, "input": {}}, self.registry, self.ctx)  # noqa: E731
        self.assertEqual(run("shot")["content"], [{"type": "text", "text": "hi"}, IMAGE])
        self.assertEqual(run("junk")["content"], "[1, 2]")
        self.assertEqual(run("empty")["content"], "[]")


class SessionBlocksTest(TempDirTest):
    def test_image_and_thinking_blocks_survive_save_and_load(self):
        session = session_mod.open_session("blocks")
        session.messages += copy.deepcopy(HISTORY)
        session.save()
        again = session_mod.open_session("blocks")
        self.assertEqual(again.messages, HISTORY)
        self.assertIn(PNG_B64, session.file.read_text())  # on disk as base64 in the JSON line


class AnthropicBlocksTest(unittest.TestCase):
    def provider(self, reply_content=None, sent=None):
        reply_content = reply_content or [{"type": "text", "text": "ok"}]

        def stream(**kwargs):
            if sent is not None:
                sent.update(kwargs)
            message = NS(
                content=[NS(model_dump=lambda exclude_none, b=b: dict(b)) for b in reply_content],
                stop_reason="end_turn",
                usage=None,
            )
            return contextlib.nullcontext(NS(text_stream=iter(["ok"]), get_final_message=lambda: message))

        return AnthropicProvider(client=NS(messages=NS(stream=stream)))

    def test_images_go_as_they_are_and_unsigned_thinking_is_dropped(self):
        sent = {}
        before = copy.deepcopy(HISTORY)
        self.provider(sent=sent).stream(
            model="m", system="s", messages=HISTORY, tools=[], on_text=None, options={"reasoning_effort": "low"}
        )
        self.assertEqual(HISTORY, before)  # not modified
        request = sent["messages"]
        self.assertEqual(request[0]["content"][1], IMAGE)
        self.assertEqual(request[1], {"role": "assistant", "content": [HISTORY[1]["content"][0], HISTORY[1]["content"][1]]})
        self.assertEqual(request[2]["content"][0]["content"][1], IMAGE)  # a tool result's picture
        self.assertEqual(request[3], {"role": "assistant", "content": [{"type": "text", "text": "a red pixel"}]})
        self.assertEqual(sent["thinking"], {"type": "enabled", "budget_tokens": 1024})

    def test_without_reasoning_effort_no_thinking_is_requested_or_sent(self):
        sent = {}
        self.provider(sent=sent).stream(
            model="m", system="s", messages=HISTORY, tools=[], on_text=None, options={"temperature": 0.3}
        )
        self.assertNotIn("thinking", sent)
        self.assertEqual(sent["temperature"], 0.3)
        self.assertEqual(sent["messages"][1]["content"], [HISTORY[1]["content"][1]])
        self.assertEqual(sent["max_tokens"], 8192)

    def test_extended_thinking_takes_no_temperature_and_adds_its_budget_to_the_limit(self):
        sent = {}
        self.provider(sent=sent).stream(
            model="m", system="s", messages=[], tools=[], on_text=None,
            options={"reasoning_effort": "high", "temperature": 0.3},
        )
        self.assertEqual(sent["thinking"], {"type": "enabled", "budget_tokens": 16000})
        self.assertEqual(sent["max_tokens"], 8192 + 16000)
        self.assertNotIn("temperature", sent)

    def test_a_small_limit_shrinks_the_budget_or_turns_thinking_off(self):
        self.assertEqual(thinking_request("high", 6000), ({"type": "enabled", "budget_tokens": 3000}, 6000))
        self.assertEqual(thinking_request("medium", 1500), (None, 1500))
        self.assertEqual(thinking_request(None, None), (None, 8192))
        self.assertEqual(thinking_request("extreme", 100), (None, 100))

    def test_the_reply_keeps_thinking_blocks_with_their_signature_and_they_go_back(self):
        replied = [
            {"type": "thinking", "thinking": "hmm", "signature": "abc"},
            {"type": "redacted_thinking", "data": "zzz"},
            {"type": "text", "text": "ok"},
        ]
        reply = self.provider(replied).stream(model="m", system="s", messages=[], tools=[], on_text=None)
        self.assertEqual(reply.content, replied)
        history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": reply.content}]
        sent = {}
        self.provider(sent=sent).stream(
            model="m", system="s", messages=history, tools=[], on_text=None, options={"reasoning_effort": "low"}
        )
        self.assertEqual(sent["messages"][1]["content"], replied)

    def test_a_message_with_nothing_left_is_left_out(self):
        history = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": "s"}]},
        ]
        self.assertEqual(to_request_messages(history, False), [{"role": "user", "content": "q"}])
        self.assertEqual(len(to_request_messages(history, True)), 2)


def chunk(**fields):
    delta = {"content": None, "tool_calls": None, **fields}
    return NS(usage=None, choices=[NS(delta=NS(**delta), finish_reason=None)])


class OpenAIBlocksTest(unittest.TestCase):
    def test_images_become_data_urls_and_thinking_is_dropped(self):
        before = copy.deepcopy(HISTORY)
        chat = to_chat_messages("sys", HISTORY)
        self.assertEqual(HISTORY, before)
        url = f"data:image/png;base64,{PNG_B64}"
        self.assertEqual(
            chat[1],
            {"role": "user", "content": [{"type": "text", "text": "what is this?"}, {"type": "image_url", "image_url": {"url": url}}]},
        )
        self.assertEqual(
            chat[2],
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "read", "arguments": '{"path": "p.png"}'}}
            ]},
        )
        # a tool message holds text only: the picture follows in a user message
        self.assertEqual(chat[3]["role"], "tool")
        self.assertIn("p.png (image/png, 70 bytes)", chat[3]["content"])
        self.assertNotIn(PNG_B64, str(chat[3]))
        self.assertEqual(chat[4]["role"], "user")
        self.assertEqual(chat[4]["content"][1], {"type": "image_url", "image_url": {"url": url}})
        self.assertEqual(chat[5], {"role": "assistant", "content": "a red pixel"})
        self.assertEqual(len(chat), 6)

    def test_text_only_tool_results_and_user_blocks_convert_as_before(self):
        history = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "c", "name": "n", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c", "content": [{"type": "text", "text": "ab"}]},
                {"type": "text", "text": "and"},
            ]},
        ]
        chat = to_chat_messages("s", history)
        self.assertEqual(chat[2], {"role": "tool", "tool_call_id": "c", "content": "ab"})
        self.assertEqual(chat[3], {"role": "user", "content": "and"})

    def test_reasoning_streamed_by_a_server_becomes_a_signatureless_thinking_block(self):
        for field in ("reasoning_content", "reasoning"):
            with self.subTest(field=field):
                chunks = [chunk(**{field: "let me "}), chunk(**{field: "think"}), chunk(content="Hi")]
                reply = read_chunks(chunks, None)
                self.assertEqual(
                    reply.content,
                    [{"type": "thinking", "thinking": "let me think", "signature": None}, {"type": "text", "text": "Hi"}],
                )

    def request(self, model, options, base_url=None):
        sent = {}

        def create(**kwargs):
            sent.update(kwargs)
            return contextlib.nullcontext([])

        OpenAIProvider(client=NS(chat=NS(completions=NS(create=create))), base_url=base_url).stream(
            model=model, system="s", messages=[], tools=[], on_text=None, options=options
        )
        return sent

    def test_reasoning_effort_goes_only_to_models_that_take_it(self):
        for model in ("o3-mini", "gpt-5.5", "openai/gpt-5", "o4"):
            with self.subTest(model=model):
                self.assertEqual(self.request(model, {"reasoning_effort": "high"})["reasoning_effort"], "high")
        for model in ("gpt-4o", "llama3", "deepseek-chat", "foo/oxygen"):
            with self.subTest(model=model):
                self.assertNotIn("reasoning_effort", self.request(model, {"reasoning_effort": "high"}))
        self.assertNotIn("reasoning_effort", self.request("gpt-5.5", {}))


class GeminiBlocksTest(unittest.TestCase):
    def test_images_become_image_contents_in_user_input_and_function_result(self):
        before = copy.deepcopy(HISTORY)
        steps = to_interaction_input(HISTORY)
        self.assertEqual(HISTORY, before)
        image = {"type": "image", "data": PNG_B64, "mime_type": "image/png"}
        self.assertEqual(steps[0], {"type": "user_input", "content": [{"type": "text", "text": "what is this?"}]})
        self.assertEqual(steps[1], {"type": "user_input", "content": [image]})
        self.assertEqual(steps[2], {"type": "function_call", "id": "t1", "name": "read", "arguments": {"path": "p.png"}})
        self.assertEqual(
            steps[3],
            {
                "type": "function_result",
                "call_id": "t1",
                "name": "read",
                "result": [{"type": "text", "text": "p.png (image/png, 70 bytes)"}, image],
                "is_error": False,
            },
        )
        self.assertEqual(steps[4], {"type": "model_output", "content": [{"type": "text", "text": "a red pixel"}]})
        self.assertEqual(len(steps), 5)  # thinking blocks are not sent

    def test_a_thought_summary_becomes_a_thinking_block_and_the_step_is_kept_for_replay(self):
        steps = [
            {"type": "thought", "signature": "sig", "summary": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]},
            {"type": "thought", "signature": "sig2"},
            {"type": "model_output", "content": [{"type": "text", "text": "answer"}]},
        ]
        reply = to_reply("completed", steps)
        self.assertEqual(
            reply.content,
            [{"type": "thinking", "thinking": "first\nsecond", "signature": None}, {"type": "text", "text": "answer"}],
        )
        self.assertEqual(reply.provider_data, steps)
        self.assertEqual(reply.stop_reason, "end_turn")

    def test_streamed_summaries_reach_the_reply(self):
        events = [
            NS(event_type="interaction.created"),
            NS(event_type="step.start", index=0, step=NS(model_dump=lambda mode=None: {"type": "thought"})),
            NS(event_type="step.delta", index=0, delta=NS(type="thought_summary", content=NS(is_unknown=False, model_dump=lambda mode=None: {"type": "text", "text": "why"}))),
            NS(event_type="interaction.completed", interaction=NS(status="completed")),
        ]
        reply = to_reply(*read_stream(events, None))
        self.assertEqual(reply.content, [{"type": "thinking", "thinking": "why", "signature": None}])

    def test_reasoning_effort_sets_the_thinking_level_and_summaries(self):
        sent = {}

        class Events(list):
            __enter__ = lambda self: self  # noqa: E731
            __exit__ = lambda self, *exc: None  # noqa: E731

        def create(**kwargs):
            sent.update(kwargs)
            return Events()

        provider = GeminiProvider(client=NS(interactions=NS(create=create)))
        provider.stream(model="m", system="s", messages=[], tools=[], on_text=None, options={"reasoning_effort": "medium"})
        self.assertEqual(sent["generation_config"]["thinking_level"], "medium")
        self.assertEqual(sent["generation_config"]["thinking_summaries"], "auto")
        provider.stream(model="m", system="s", messages=[], tools=[], on_text=None)
        self.assertNotIn("thinking_level", sent["generation_config"])


class TuiBlocksTest(unittest.IsolatedAsyncioTestCase):
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
        self.core = App(
            registry=Registry(), session=Session("t", self.tmp / "t.jsonl"), cwd=str(self.tmp), auto_approve=False,
            register_commands=register_builtin_commands,
        )
        self.core.reload_extensions()
        self.core.provider_name, self.core.model = "fake", "m"

    def body(self, block):
        return "\n".join(str(line) for line in block.lines)

    async def test_a_resumed_session_with_pictures_and_thinking_is_replayed_without_the_data(self):
        self.core.session.messages += copy.deepcopy(HISTORY)
        app = MinimalTui(Host(self.core))
        async with app.run_test():
            block = app.query_one(ToolBlock)
            self.assertTrue(block.has_class("-success"))
            self.assertIn(f"[image: image/png, {len(PNG)} bytes]", self.body(block))
            self.assertNotIn(PNG_B64, self.body(block))

    async def test_a_turn_that_reads_a_picture_shows_it_as_text(self):
        (self.tmp / "p.png").write_bytes(PNG)
        replies = [
            Reply(content=[{"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "p.png"}}], stop_reason="tool_use"),
            Reply(content=[{"type": "text", "text": "a pixel"}], stop_reason="end_turn"),
        ]
        self.core.provider = NS(name="fake", stream=lambda **kw: replies.pop(0))
        app = MinimalTui(Host(self.core))
        async with app.run_test() as pilot:
            app.query_one("#prompt", Composer).set_text("look")
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause(0.05)
                if not app.busy and not replies:
                    break
            block = app.query_one(ToolBlock)
            self.assertIn("[image: image/png,", self.body(block))
            self.assertNotIn(PNG_B64, self.body(block))
            self.assertEqual(self.core.messages[2]["content"][0]["content"][1], IMAGE)
            self.assertTrue(all(PNG_B64 not in str(s.render()) for s in app.query(Static)))


if __name__ == "__main__":
    unittest.main()
