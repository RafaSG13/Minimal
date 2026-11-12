"""Google Gemini provider, using the ``google-genai`` SDK and the Interactions API.

Requests are stateless (``store=False``): the whole history is sent each time.
Mapping between the canonical format and Interactions steps:

==============================  =============================================
canonical                       Interactions step
==============================  =============================================
user text                       ``user_input`` with a text content block
assistant ``text`` blocks       ``model_output`` with text content blocks
assistant ``tool_use``          ``function_call`` (``id``, ``name``, ``arguments``)
user ``tool_result``            ``function_result`` (``call_id``, ``result``, ``is_error``)
==============================  =============================================

Gemini requires its own reply steps (including ``thought`` steps carrying
signatures) to be sent back exactly as received, so they are kept in the
message's ``provider_data`` and replayed verbatim. Turns produced by another
provider are rebuilt from the canonical blocks instead.

Credentials: ``GOOGLE_API_KEY`` or ``GEMINI_API_KEY`` (read by the SDK).
``GOOGLE_GEMINI_BASE_URL`` overrides the endpoint.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Iterable

from minimal.providers.base import ProviderSpec, Reply, TextCallback, Usage, result_text

MAX_OUTPUT_TOKENS = 8192

# Interaction statuses that mean the request did not produce a usable reply.
_FAILED = {"failed", "cancelled", "budget_exceeded"}


class GeminiProvider:
    """Streams replies from the Gemini Interactions API."""

    name = "gemini"

    def __init__(self, client: Any = None) -> None:
        if client is None:
            from google import genai

            client = genai.Client()
        self.client = client

    def list_models(self) -> list[str]:
        """Gemini model ids the key can use, as reported by the API."""
        names = (m.name or "" for m in self.client.models.list())
        return sorted(n.removeprefix("models/") for n in names if n.startswith("models/gemini"))

    def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[dict],
        tools: list[dict],
        on_text: TextCallback | None,
        options: Mapping[str, Any] | None = None,
    ) -> Reply:
        options = options or {}
        generation_config: dict[str, Any] = {"max_output_tokens": options.get("max_tokens", MAX_OUTPUT_TOKENS)}
        if options.get("temperature") is not None:
            generation_config["temperature"] = options["temperature"]
        stream = self.client.interactions.create(
            model=model,
            input=to_interaction_input(messages),
            system_instruction=system,
            tools=[
                {"type": "function", "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}
                for t in tools
            ],
            generation_config=generation_config,
            store=False,
            stream=True,
        )
        with stream:
            status, steps, usage = read_stream(stream, on_text)
        return to_reply(status, steps, usage)


# --- canonical history -> Interactions input -----------------------------------


def to_interaction_input(messages: list[dict]) -> list[dict]:
    """Convert the canonical history to a list of Interactions steps."""
    steps: list[dict] = []
    tool_names: dict[str, str] = {}  # tool_use id -> tool name, for function_result steps
    for message in messages:
        content = message["content"]
        if message["role"] == "assistant":
            for block in content:
                if block["type"] == "tool_use":
                    tool_names[block["id"]] = block["name"]
            data = message.get("provider_data") or {}
            if data.get("provider") == GeminiProvider.name:
                steps.extend(data["data"])
            else:
                steps.extend(_assistant_steps(content))
        elif isinstance(content, str):
            steps.append({"type": "user_input", "content": [{"type": "text", "text": content}]})
        else:
            steps.extend(_user_block_steps(content, tool_names))
    return steps


def _assistant_steps(blocks: list[dict]) -> list[dict]:
    """Rebuild Interactions steps for an assistant turn produced by another provider."""
    steps: list[dict] = []
    for block in blocks:
        if block["type"] == "text":
            if steps and steps[-1]["type"] == "model_output":
                steps[-1]["content"].append({"type": "text", "text": block["text"]})
            else:
                steps.append({"type": "model_output", "content": [{"type": "text", "text": block["text"]}]})
        elif block["type"] == "tool_use":
            steps.append(
                {"type": "function_call", "id": block["id"], "name": block["name"], "arguments": block["input"]}
            )
    return steps


def _user_block_steps(blocks: list[dict], tool_names: dict[str, str]) -> list[dict]:
    """Convert a user message made of blocks (tool results and/or text)."""
    steps: list[dict] = []
    for block in blocks:
        if block["type"] == "tool_result":
            step = {
                "type": "function_result",
                "call_id": block["tool_use_id"],
                "result": [{"type": "text", "text": result_text(block.get("content", ""))}],
                "is_error": bool(block.get("is_error")),
            }
            if block["tool_use_id"] in tool_names:
                step["name"] = tool_names[block["tool_use_id"]]
            steps.append(step)
        elif block["type"] == "text":
            steps.append({"type": "user_input", "content": [{"type": "text", "text": block["text"]}]})
    return steps


# --- streamed events -> reply ---------------------------------------------------


def read_stream(events: Iterable[Any], on_text: TextCallback | None) -> tuple[str | None, list[dict], Any]:
    """Consume a streamed interaction; return its final status, the reply steps as dicts and its usage.

    The usage is the SDK's object, or ``None`` if the stream carried none.
    The stream never carries the finished steps, so they are rebuilt from
    ``step.start`` / ``step.delta`` events. Steps are keyed by
    ``(hop, index)`` because the SDK may chain continuation requests into
    one stream, each starting again at index 0.

    A tool call whose arguments are not valid JSON keeps ``{}`` and makes the status ``"incomplete"``.

    Raises:
        RuntimeError: on an ``error`` event.
    """
    steps: dict[tuple[int, int], dict] = {}
    arguments: dict[tuple[int, int], str] = {}  # partial JSON of function_call arguments
    hop, status, usage = -1, None, None
    for event in events:
        kind = event.event_type
        if kind == "interaction.created":
            hop += 1
        elif kind == "step.start":
            step = _step_dict(event.step)
            steps[(hop, event.index)] = step
            if on_text and step.get("type") == "model_output":
                for block in step.get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        on_text(block["text"])
        elif kind == "step.delta":
            key = (hop, event.index)
            _apply_delta(steps.setdefault(key, {}), event.delta, key, arguments, on_text)
        elif kind == "interaction.status_update":
            status = event.status
        elif kind == "interaction.completed":
            status = event.interaction.status
            usage = getattr(event.interaction, "usage", None)
        elif kind == "error":
            error = event.error
            raise RuntimeError(f"Gemini error: {getattr(error, 'message', None) or getattr(error, 'code', None) or error}")

    for key, partial in arguments.items():
        try:
            steps[key]["arguments"] = json.loads(partial) if partial.strip() else {}
        except ValueError:
            steps[key]["arguments"] = {}  # cut off in the middle of the arguments: the call cannot run
            if status not in _FAILED:
                status = "incomplete"
    return status, [steps[key] for key in sorted(steps)], usage


def _apply_delta(
    step: dict, delta: Any, key: tuple[int, int], arguments: dict, on_text: TextCallback | None
) -> None:
    """Merge one ``step.delta`` into the step being built."""
    if delta.type == "text":
        content = step.setdefault("content", [])
        if content and content[-1].get("type") == "text":
            content[-1]["text"] += delta.text
        else:
            content.append({"type": "text", "text": delta.text})
        if on_text:
            on_text(delta.text)
    elif delta.type == "thought_signature":
        step["signature"] = delta.signature
    elif delta.type == "thought_summary" and delta.content is not None:
        step.setdefault("summary", []).append(_step_dict(delta.content))
    elif delta.type == "arguments_delta":
        arguments[key] = arguments.get(key, "") + (delta.arguments or "")


def _step_dict(model: Any) -> dict:
    """SDK model -> JSON-safe dict, keeping unknown step types exactly as received."""
    if getattr(model, "is_unknown", False):
        return model.raw
    return model.model_dump(mode="json")


def to_reply(status: str | None, steps: list[dict], usage: Any = None) -> Reply:
    """Turn the reply steps, and the SDK's usage object if any, into a canonical :class:`Reply`.

    Raises:
        RuntimeError: if the interaction failed, was cancelled or exceeded its budget.
    """
    if status in _FAILED:
        raise RuntimeError(f"Gemini interaction {status}")
    steps = [s for s in steps if s.get("type") not in ("user_input", "function_result")]

    content: list[dict] = []
    for step in steps:
        if step.get("type") == "model_output":
            for block in step.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    content.append({"type": "text", "text": block["text"]})
        elif step.get("type") == "function_call":
            content.append(
                {"type": "tool_use", "id": step["id"], "name": step["name"], "input": step.get("arguments") or {}}
            )

    if status == "incomplete":
        stop_reason = "max_tokens"  # also with tool calls: the last one may be cut off, so none are run
    elif any(block["type"] == "tool_use" for block in content):
        stop_reason = "tool_use"
    else:
        stop_reason = "end_turn"
    counts = None
    if usage is not None:
        counts = Usage(
            input_tokens=usage.total_input_tokens or 0,
            output_tokens=(usage.total_output_tokens or 0) + (usage.total_thought_tokens or 0),
            cache_read_tokens=usage.total_cached_tokens or 0,
        )
    return Reply(content=content, stop_reason=stop_reason, provider_data=steps, usage=counts)


SPEC = ProviderSpec(
    name="gemini",
    create=GeminiProvider,
    default_model="gemini-3.8-flash",
    env_keys=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    description="Google Gemini models via the Interactions API (free tier available)",
)
