import unittest
from contextlib import nullcontext
from types import SimpleNamespace as NS

from minimal.providers.anthropic import AnthropicProvider
from minimal.providers.base import Usage
from minimal.providers.claude import CLAUDE_CODE_IDENTITY, ClaudeProvider
from minimal.providers.gemini import read_stream, to_interaction_input, to_reply
from minimal.providers.openai import read_chunks, to_chat_messages

GEMINI_STEPS = [
    {"type": "thought", "signature": "sig1"},
    {"type": "function_call", "id": "c1", "name": "read", "arguments": {"path": "a"}},
]

HISTORY = [
    {"role": "user", "content": "hi"},
    {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "c1", "name": "read", "input": {"path": "a"}}],
        "provider_data": {"provider": "gemini", "data": GEMINI_STEPS},
    },
    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "boom", "is_error": True}]},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
            {"type": "tool_use", "id": "x1", "name": "bash", "input": {"command": "ls"}},
        ],
    },
]


class GeminiInputTest(unittest.TestCase):
    def test_history_conversion(self):
        steps = to_interaction_input(HISTORY)
        self.assertEqual(steps[0], {"type": "user_input", "content": [{"type": "text", "text": "hi"}]})
        # Gemini's own turn is replayed verbatim, thought signature included.
        self.assertEqual(steps[1:3], GEMINI_STEPS)
        self.assertEqual(
            steps[3],
            {
                "type": "function_result",
                "call_id": "c1",
                "name": "read",
                "result": [{"type": "text", "text": "boom"}],
                "is_error": True,
            },
        )
        # A turn from another provider is rebuilt from canonical blocks.
        self.assertEqual(
            steps[4:],
            [
                {"type": "model_output", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
                {"type": "function_call", "id": "x1", "name": "bash", "arguments": {"command": "ls"}},
            ],
        )


def event(kind, **fields):
    return NS(event_type=kind, **fields)


def step(data):
    return NS(model_dump=lambda mode=None: dict(data))


class GeminiStreamTest(unittest.TestCase):
    def test_tool_call_stream(self):
        events = [
            event("interaction.created"),
            event("step.start", index=0, step=step({"type": "thought"})),
            event("step.delta", index=0, delta=NS(type="thought_signature", signature="sig")),
            event("step.start", index=1, step=step({"type": "model_output"})),
            event("step.delta", index=1, delta=NS(type="text", text="Hel")),
            event("step.delta", index=1, delta=NS(type="text", text="lo")),
            event("step.start", index=2, step=step({"type": "function_call", "id": "c1", "name": "read", "arguments": {}})),
            event("step.delta", index=2, delta=NS(type="arguments_delta", arguments='{"pa')),
            event("step.delta", index=2, delta=NS(type="arguments_delta", arguments='th": "x"}')),
            event("interaction.completed", interaction=NS(status="requires_action")),
        ]
        chunks = []
        status, steps, usage = read_stream(events, chunks.append)
        self.assertIsNone(usage)
        reply = to_reply(status, steps)

        self.assertEqual(chunks, ["Hel", "lo"])
        self.assertEqual(
            reply.content,
            [
                {"type": "text", "text": "Hello"},
                {"type": "tool_use", "id": "c1", "name": "read", "input": {"path": "x"}},
            ],
        )
        self.assertEqual(reply.stop_reason, "tool_use")
        self.assertEqual(reply.provider_data[0], {"type": "thought", "signature": "sig"})

    def test_status_mapping(self):
        text = [{"type": "model_output", "content": [{"type": "text", "text": "x"}]}]
        self.assertEqual(to_reply("completed", text).stop_reason, "end_turn")
        self.assertEqual(to_reply("incomplete", text).stop_reason, "max_tokens")
        with self.assertRaisesRegex(RuntimeError, "failed"):
            to_reply("failed", text)

    def test_error_event(self):
        with self.assertRaisesRegex(RuntimeError, "quota"):
            read_stream([event("error", error=NS(message="quota exceeded", code="429"))], None)

    def test_usage(self):
        usage = NS(total_input_tokens=7, total_output_tokens=2, total_thought_tokens=3, total_cached_tokens=None)
        events = [event("interaction.completed", interaction=NS(status="completed", usage=usage))]
        self.assertEqual(to_reply(*read_stream(events, None)).usage, Usage(7, 5, 0, 0))


class AnthropicRequestTest(unittest.TestCase):
    def capture(self):
        sent = {}

        def stream(**kwargs):
            sent.update(kwargs)
            final = NS(content=[], stop_reason="end_turn")
            return nullcontext(NS(text_stream=iter(()), get_final_message=lambda: final))

        return sent, NS(messages=NS(stream=stream))

    def test_provider_data_is_not_sent(self):
        sent, client = self.capture()
        AnthropicProvider(client=client).stream(model="m", system="s", messages=HISTORY, tools=[], on_text=None)
        self.assertTrue(all(set(m) == {"role", "content"} for m in sent["messages"]))
        self.assertEqual(sent["system"], "s")

    def test_claude_sends_identity_block_first(self):
        sent, client = self.capture()
        ClaudeProvider(client=client).stream(model="m", system="s", messages=HISTORY, tools=[], on_text=None)
        self.assertEqual(
            sent["system"],
            [{"type": "text", "text": CLAUDE_CODE_IDENTITY}, {"type": "text", "text": "s"}],
        )

    def test_usage_counts_cached_tokens_as_input(self):
        usage = NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=100, cache_creation_input_tokens=None)
        final = NS(content=[], stop_reason="end_turn", usage=usage)
        client = NS(
            messages=NS(stream=lambda **kw: nullcontext(NS(text_stream=iter(()), get_final_message=lambda: final)))
        )
        reply = AnthropicProvider(client=client).stream(model="m", system="s", messages=[], tools=[], on_text=None)
        self.assertEqual(reply.usage, Usage(110, 5, 100, 0))


def chunk(delta=None, finish_reason=None, usage=None):
    choices = [] if delta is None else [NS(delta=delta, finish_reason=finish_reason)]
    return NS(choices=choices, usage=usage)


def delta(content=None, tool_calls=None):
    return NS(content=content, tool_calls=tool_calls)


class OpenAITest(unittest.TestCase):
    def test_history_conversion(self):
        self.assertEqual(
            to_chat_messages("s", HISTORY),
            [
                {"role": "system", "content": "s"},
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a"}'}}
                    ],
                },
                {"role": "tool", "tool_call_id": "c1", "content": "boom"},
                {
                    "role": "assistant",
                    "content": "ab",
                    "tool_calls": [
                        {"id": "x1", "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}
                    ],
                },
            ],
        )

    def test_tool_call_stream(self):
        chunks = [
            chunk(delta(content="Hi")),
            chunk(delta(tool_calls=[NS(index=0, id="c1", function=NS(name="read", arguments='{"pa'))])),
            chunk(
                delta(tool_calls=[NS(index=0, id=None, function=NS(name=None, arguments='th": "x"}'))]),
                finish_reason="tool_calls",
            ),
            chunk(usage=NS(prompt_tokens=9, completion_tokens=4, prompt_tokens_details=NS(cached_tokens=6))),
        ]
        texts = []
        reply = read_chunks(chunks, texts.append)
        self.assertEqual(texts, ["Hi"])
        self.assertEqual(
            reply.content,
            [
                {"type": "text", "text": "Hi"},
                {"type": "tool_use", "id": "c1", "name": "read", "input": {"path": "x"}},
            ],
        )
        self.assertEqual(reply.stop_reason, "tool_use")
        self.assertEqual(reply.usage, Usage(9, 4, 6, 0))

    def test_length_means_max_tokens(self):
        reply = read_chunks([chunk(delta(content="cut"), finish_reason="length")], None)
        self.assertEqual(reply.stop_reason, "max_tokens")
        self.assertIsNone(reply.usage)


if __name__ == "__main__":
    unittest.main()


class ListModelsTest(unittest.TestCase):
    def test_anthropic_lists_ids(self):
        from types import SimpleNamespace as NS

        from minimal.providers.anthropic import AnthropicProvider

        client = NS(models=NS(list=lambda limit: [NS(id="claude-a"), NS(id="claude-b")]))
        self.assertEqual(AnthropicProvider(client).list_models(), ["claude-a", "claude-b"])

    def test_gemini_lists_gemini_models_only(self):
        from types import SimpleNamespace as NS

        from minimal.providers.gemini import GeminiProvider

        names = ["models/gemini-x", "models/embedding-001", "models/gemini-a"]
        client = NS(models=NS(list=lambda: [NS(name=n) for n in names]))
        self.assertEqual(GeminiProvider(client).list_models(), ["gemini-a", "gemini-x"])
