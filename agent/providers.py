from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
import openai

from .config import ProviderConfig
from .tools import Tool

# Every network/API failure either SDK raises descends from one of these.
STREAM_ERRORS = (anthropic.AnthropicError, openai.OpenAIError)

Message = dict[str, Any]
Emit = Callable[[str], None]


@dataclass(frozen=True)
class Usage:
    """Per-turn token counts, normalized across SDKs."""

    input_tokens: int
    output_tokens: int
    cache_read: int = 0
    cache_write: int = 0


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict
    # Set when the model emitted arguments we could not parse. The loop still
    # owes the API a result for this id, so it reports the error instead.
    error: str | None = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False
    # Terminal-only. Never sent to the API — see append_results below.
    display: str | None = None


@dataclass(frozen=True)
class Step:
    """The outcome of exactly one API call."""

    stop_reason: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None


class Provider(Protocol):
    def step(self, messages: list[Message], tools: list[Tool], emit: Emit) -> Step:
        """One API call. Appends the assistant turn to `messages` in place."""

    def append_results(self, messages: list[Message], results: list[ToolResult]) -> None:
        """Append tool results in this provider's wire format."""


class AnthropicProvider:
    def __init__(self, cfg: ProviderConfig) -> None:
        self._cfg = cfg
        self._client = anthropic.Anthropic(api_key=cfg.api_key, base_url=cfg.base_url)

    def step(self, messages: list[Message], tools: list[Tool], emit: Emit) -> Step:
        with self._client.messages.stream(
            model=self._cfg.model,
            max_tokens=self._cfg.max_tokens,
            messages=messages,
            tools=[
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.schema,
                }
                for t in tools
            ],
        ) as stream:
            for text in stream.text_stream:
                emit(text)
            final = stream.get_final_message()

        # Echoed back verbatim — block ids and any signatures must survive.
        messages.append({"role": "assistant", "content": final.content})

        usage = final.usage
        return Step(
            stop_reason=final.stop_reason or "end_turn",
            tool_calls=[
                ToolCall(id=b.id, name=b.name, args=dict(b.input))
                for b in final.content
                if b.type == "tool_use"
            ],
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read=usage.cache_read_input_tokens or 0,
                cache_write=usage.cache_creation_input_tokens or 0,
            ),
        )

    def append_results(self, messages: list[Message], results: list[ToolResult]) -> None:
        # Every result for one assistant turn goes in a single user message.
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call_id,
                        "content": r.content,
                        "is_error": r.is_error,
                    }
                    for r in results
                ],
            }
        )


class OpenAIProvider:
    """Also serves DeepSeek and anything else on the OpenAI wire format."""

    def __init__(self, cfg: ProviderConfig) -> None:
        self._cfg = cfg
        self._client = openai.OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)

    _FINISH = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}

    def step(self, messages: list[Message], tools: list[Tool], emit: Emit) -> Step:
        raw = None
        finish = "stop"
        text: list[str] = []
        # Tool calls stream in fragments keyed by index; accumulate then parse.
        acc: dict[int, dict[str, str]] = {}

        with self._client.chat.completions.create(
            model=self._cfg.model,
            messages=messages,
            stream=True,
            # Without this, a streamed response carries no usage at all.
            stream_options={"include_usage": True},
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.schema,
                    },
                }
                for t in tools
            ],
            **{self._cfg.token_param: self._cfg.max_tokens},
        ) as stream:
            for chunk in stream:
                if chunk.usage:
                    raw = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish = choice.finish_reason
                if choice.delta.content:
                    text.append(choice.delta.content)
                    emit(choice.delta.content)
                for frag in choice.delta.tool_calls or []:
                    slot = acc.setdefault(frag.index, {"id": "", "name": "", "args": ""})
                    if frag.id:
                        slot["id"] += frag.id
                    if frag.function and frag.function.name:
                        slot["name"] += frag.function.name
                    if frag.function and frag.function.arguments:
                        slot["args"] += frag.function.arguments

        assistant: Message = {"role": "assistant", "content": "".join(text) or None}
        if acc:
            assistant["tool_calls"] = [
                {
                    "id": s["id"],
                    "type": "function",
                    "function": {"name": s["name"], "arguments": s["args"] or "{}"},
                }
                for _, s in sorted(acc.items())
            ]
        messages.append(assistant)

        calls = []
        for _, s in sorted(acc.items()):
            try:
                args = json.loads(s["args"] or "{}")
                calls.append(ToolCall(id=s["id"], name=s["name"], args=args))
            except json.JSONDecodeError as e:
                calls.append(
                    ToolCall(id=s["id"], name=s["name"], args={}, error=f"bad arguments: {e}")
                )

        usage = None
        if raw is not None:
            details = getattr(raw, "prompt_tokens_details", None)
            usage = Usage(
                input_tokens=raw.prompt_tokens,
                output_tokens=raw.completion_tokens,
                cache_read=getattr(details, "cached_tokens", 0) or 0,
            )

        return Step(
            stop_reason=self._FINISH.get(finish, finish),
            tool_calls=calls,
            usage=usage,
        )

    def append_results(self, messages: list[Message], results: list[ToolResult]) -> None:
        # One message per result here — the mirror of Anthropic's single batch.
        for r in results:
            messages.append(
                {"role": "tool", "tool_call_id": r.call_id, "content": r.content}
            )


def build(cfg: ProviderConfig) -> Provider:
    if cfg.kind == "anthropic":
        return AnthropicProvider(cfg)
    return OpenAIProvider(cfg)
