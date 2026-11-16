import os
import tempfile
import unittest
from contextlib import nullcontext
from types import SimpleNamespace as NS
from pathlib import Path
from unittest import mock

from minimal import cli
from minimal.core.app import App
from minimal.core.session import Session
from minimal.extensions.registry import Registry
from minimal.providers import login_provider
from minimal.providers.anthropic import AnthropicProvider
from minimal.providers.base import ProviderSpec, Usage
from minimal.providers.claude import CLAUDE_CODE_IDENTITY, ClaudeProvider
from minimal.providers.gemini import read_stream, to_interaction_input, to_reply
from minimal.providers.openai import OpenAIProvider, openai_compatible, read_chunks, to_chat_messages

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

    def test_truncated_tool_call_is_max_tokens_not_a_crash(self):
        for status in ("incomplete", "requires_action"):
            with self.subTest(status=status):
                events = [
                    event("interaction.created"),
                    event("step.start", index=0, step=step({"type": "function_call", "id": "c1", "name": "write", "arguments": {}})),
                    event("step.delta", index=0, delta=NS(type="arguments_delta", arguments='{"path": "a", "con')),
                    event("interaction.completed", interaction=NS(status=status)),
                ]
                reply = to_reply(*read_stream(events, None))
                self.assertEqual(reply.stop_reason, "max_tokens")
                self.assertEqual(reply.content, [{"type": "tool_use", "id": "c1", "name": "write", "input": {}}])

    def test_complete_tool_call_cut_by_the_limit_is_still_max_tokens(self):
        call = {"type": "function_call", "id": "c1", "name": "read", "arguments": {"path": "x"}}
        self.assertEqual(to_reply("incomplete", [call]).stop_reason, "max_tokens")


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

    def test_options_set_max_tokens_and_temperature(self):
        sent, client = self.capture()
        provider = AnthropicProvider(client=client)
        provider.stream(model="m", system="s", messages=[], tools=[], on_text=None)
        self.assertEqual(sent["max_tokens"], 8192)
        self.assertNotIn("temperature", sent)
        provider.stream(
            model="m", system="s", messages=[], tools=[], on_text=None, options={"max_tokens": 1000, "temperature": 0.2}
        )
        self.assertEqual((sent["max_tokens"], sent["temperature"]), (1000, 0.2))


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

    def test_truncated_tool_call_is_max_tokens_not_a_crash(self):
        for finish_reason in ("length", "tool_calls"):
            with self.subTest(finish_reason=finish_reason):
                chunks = [
                    chunk(delta(tool_calls=[NS(index=0, id="c1", function=NS(name="write", arguments='{"path": "a", "con'))])),
                    chunk(delta(content=None), finish_reason=finish_reason),
                ]
                reply = read_chunks(chunks, None)
                self.assertEqual(reply.stop_reason, "max_tokens")
                self.assertEqual(reply.content, [{"type": "tool_use", "id": "c1", "name": "write", "input": {}}])

    def test_complete_tool_call_cut_by_length_is_still_max_tokens(self):
        call = NS(index=0, id="c1", function=NS(name="read", arguments='{"path": "x"}'))
        reply = read_chunks([chunk(delta(tool_calls=[call]), finish_reason="length")], None)
        self.assertEqual(reply.stop_reason, "max_tokens")

    def test_options_name_the_limit_per_server(self):
        sent = {}

        def create(**kwargs):
            sent.clear()
            sent.update(kwargs)
            return nullcontext([])

        provider = OpenAIProvider(client=NS(chat=NS(completions=NS(create=create))))
        with mock.patch.dict(os.environ):
            os.environ.pop("OPENAI_BASE_URL", None)
            provider.stream(model="m", system="s", messages=[], tools=[], on_text=None)
            self.assertFalse({"max_tokens", "max_completion_tokens", "temperature"} & set(sent))
            provider.stream(model="m", system="s", messages=[], tools=[], on_text=None, options={"max_tokens": 50})
            self.assertEqual(sent["max_completion_tokens"], 50)
            self.assertNotIn("max_tokens", sent)
            os.environ["OPENAI_BASE_URL"] = "http://localhost:11434/v1"
            provider.stream(
                model="m", system="s", messages=[], tools=[], on_text=None, options={"max_tokens": 50, "temperature": 0}
            )
            self.assertEqual((sent["max_tokens"], sent["temperature"]), (50, 0))
            self.assertNotIn("max_completion_tokens", sent)


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


class OptionalSdkTest(unittest.TestCase):
    def spec(self, **kwargs):
        return ProviderSpec(name="p", create=lambda: None, default_model="m", **kwargs)

    def test_missing_sdk_names_the_package_and_how_to_install_it(self):
        message = self.spec(sdk="no_such_sdk_for_minimal", install="mnml-agent[p]").missing_sdk()
        self.assertIn("no_such_sdk_for_minimal", message)
        self.assertIn("'mnml-agent[p]'", message)

    def test_installed_or_undeclared_sdk_is_not_missing(self):
        self.assertIsNone(self.spec(sdk="json", install="x").missing_sdk())
        self.assertIsNone(self.spec().missing_sdk())

    def test_dotted_sdk_with_a_missing_parent_is_missing(self):
        self.assertIsNotNone(self.spec(sdk="no_such_parent.child").missing_sdk())

    def test_use_provider_refuses_a_provider_whose_sdk_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = App(registry=Registry(), session=Session("u", Path(tmp) / "u.jsonl"), cwd=tmp, auto_approve=True)
            app.registry.add_provider(self.spec(sdk="no_such_sdk_for_minimal", install="mnml-agent[p]"))
            with self.assertRaisesRegex(ValueError, "mnml-agent\\[p\\]"):
                app.use_provider("p")


class OpenAICompatibleTest(unittest.TestCase):
    """``openai_compatible``: one more server with its own URL and key, built through a fake ``openai`` module."""

    def fake_sdk(self):
        built = []

        class Client:
            def __init__(self, **kwargs):
                built.append(kwargs)
                self.sent = []
                self.chat = NS(completions=NS(create=self.create))

            def create(self, **kwargs):
                self.sent.append(kwargs)
                return nullcontext([chunk(delta(content="ok"), finish_reason="stop")])

        patcher = mock.patch.dict("sys.modules", {"openai": NS(OpenAI=Client)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return built

    def test_each_server_keeps_its_own_url_and_key(self):
        built = self.fake_sdk()
        with mock.patch.dict(os.environ, {"OPENAI_BASE_URL": "http://elsewhere", "OPENROUTER_KEY": "sk-or"}):
            router = openai_compatible("router", "https://router.example/v1", "m1", key_env="OPENROUTER_KEY")
            ollama = openai_compatible("ollama", "http://localhost:11434/v1", "llama")
            self.assertEqual(router.env_keys, ("OPENROUTER_KEY",))
            self.assertEqual(ollama.env_keys, ())
            self.assertEqual((router.create().name, ollama.create().name), ("router", "ollama"))
        self.assertEqual(
            built,
            [
                {"base_url": "https://router.example/v1", "api_key": "sk-or"},
                {"base_url": "http://localhost:11434/v1", "api_key": "not-needed"},
            ],
        )

    def test_output_limit_uses_max_tokens_for_a_custom_server(self):
        self.fake_sdk()
        provider = openai_compatible("local", "http://localhost:1/v1", "m").create()
        provider.stream(model="m", system="s", messages=[], tools=[], on_text=None, options={"max_tokens": 99})
        sent = provider.client.sent[0]
        self.assertEqual(sent["max_tokens"], 99)
        self.assertNotIn("max_completion_tokens", sent)

    def test_requires_name_url_and_model(self):
        for args in (("", "http://x", "m"), ("n", "", "m"), ("n", "http://x", "")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                openai_compatible(*args)


class LoginProviderTest(unittest.TestCase):
    def specs(self, **logins):
        specs = {"key": ProviderSpec(name="key", create=lambda: None, default_model="m", env_keys=("K",))}
        for name, login in logins.items():
            specs[name] = ProviderSpec(name=name, create=lambda: None, default_model="m", login=login)
        return specs

    def test_the_named_provider_wins(self):
        specs = self.specs(one=print, two=print)
        self.assertEqual(login_provider(specs, "two", current="one").name, "two")

    def test_the_current_provider_is_next_then_the_only_one(self):
        self.assertEqual(login_provider(self.specs(one=print, two=print), current="two").name, "two")
        self.assertEqual(login_provider(self.specs(one=print), current="key").name, "one")

    def test_ambiguous_missing_or_wrong_providers_say_what_to_do(self):
        with self.assertRaisesRegex(ValueError, "Name one: one, two"):
            login_provider(self.specs(one=print, two=print))
        with self.assertRaisesRegex(ValueError, "no installed provider offers a login"):
            login_provider(self.specs())
        with self.assertRaisesRegex(ValueError, "key signs in with a key"):
            login_provider(self.specs(one=print), "key")
        with self.assertRaisesRegex(ValueError, "unknown provider: nope"):
            login_provider(self.specs(one=print), "nope")


class SignInOrOutTest(unittest.TestCase):
    def run_cli(self, login, logout, calls):
        sub = ProviderSpec(
            name="sub", create=lambda: None, default_model="m",
            login=lambda: calls.append("login"), logout=lambda: calls.append("logout"),
        )
        with (
            mock.patch.object(cli, "register_builtin_providers", lambda registry: registry.add_provider(sub)),
            mock.patch.object(cli, "load_extensions"),
        ):
            cli.sign_in_or_out(NS(login=login, logout=logout, ext=[]))

    def test_logout_runs_before_login_on_the_only_provider_with_a_login(self):
        calls = []
        self.run_cli(login="", logout="sub", calls=calls)
        self.assertEqual(calls, ["logout", "login"])

    def test_only_the_requested_action_runs(self):
        calls = []
        self.run_cli(login=None, logout="", calls=calls)
        self.assertEqual(calls, ["logout"])
