from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic
import httpx
import openai

from .config import ProviderConfig
from .tools import Tool

# Both SDKs wrap transport failures when opening a request, but a connection
# dropped *mid-stream* surfaces as a raw httpx error while iterating chunks —
# neither SDK base class covers that, and uncaught it kills the REPL.
STREAM_ERRORS = (anthropic.AnthropicError, openai.OpenAIError, httpx.HTTPError)

# Worth retrying: the request is idempotent and the failure is transient.
# Rate limits are deliberately excluded — they do not clear in seconds, so
# retrying would just look like a hang. Fail fast and say so instead.
RETRYABLE = (
    httpx.TransportError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
    openai.APIConnectionError,
    openai.InternalServerError,
)

Message = dict[str, Any]
Emit = Callable[[str], None]
SUMMARY_MAX_TOKENS = 2048


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
    # Set when the user refused the call, so the loop can spot a model that
    # keeps re-asking for the same thing.
    denied: bool = False


@dataclass(frozen=True)
class Step:
    """The outcome of exactly one API call."""

    stop_reason: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage | None = None


class Provider(Protocol):
    def step(
        self, messages: list[Message], tools: list[Tool], emit: Emit, system: str
    ) -> Step:
        """One API call. Appends the assistant turn to `messages` in place."""

    def append_results(self, messages: list[Message], results: list[ToolResult]) -> None:
        """Append tool results in this provider's wire format."""

    def summarize(self, text: str, instruction: str) -> str:
        """One-shot completion used for compaction. No tools, no streaming."""


class AnthropicProvider:
    def __init__(self, cfg: ProviderConfig) -> None:
        self._cfg = cfg
        self._client = anthropic.Anthropic(api_key=cfg.api_key, base_url=cfg.base_url)

    def step(
        self, messages: list[Message], tools: list[Tool], emit: Emit, system: str
    ) -> Step:
        with self._client.messages.stream(
            model=self._cfg.model,
            max_tokens=self._cfg.max_tokens,
            # Stable across the session, so it belongs in the cached prefix.
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
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

        # A turn cut off at max_tokens may hold a half-written tool_use. Its
        # arguments are unusable, and leaving it unanswered makes every later
        # request invalid, so drop it rather than orphan it.
        truncated = final.stop_reason == "max_tokens"
        blocks = [b for b in final.content if not (truncated and b.type == "tool_use")]
        if blocks:
            # Echoed back verbatim — block ids and any signatures must survive.
            messages.append({"role": "assistant", "content": blocks})

        usage = final.usage
        return Step(
            stop_reason=final.stop_reason or "end_turn",
            tool_calls=[]
            if truncated
            else [
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

    def summarize(self, text: str, instruction: str) -> str:
        message = self._client.messages.create(
            model=self._cfg.model,
            max_tokens=SUMMARY_MAX_TOKENS,
            system=instruction,
            messages=[{"role": "user", "content": text}],
        )
        return "".join(b.text for b in message.content if b.type == "text").strip()

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

    def step(
        self, messages: list[Message], tools: list[Tool], emit: Emit, system: str
    ) -> Step:
        raw = None
        finish = "stop"
        # Omitted entirely unless configured, so servers keep their own default.
        sampling = {} if self._cfg.temperature is None else {"temperature": self._cfg.temperature}
        text: list[str] = []
        # Tool calls stream in fragments keyed by index; accumulate then parse.
        acc: dict[int, dict[str, str]] = {}

        with self._client.chat.completions.create(
            model=self._cfg.model,
            # Prepend to a copy — the stored history holds no system message.
            messages=[{"role": "system", "content": system}, *messages],
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
            **sampling,
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

        # See the note in AnthropicProvider.step: a tool call truncated at
        # max_tokens has incomplete arguments and no result can be produced
        # for it, so it must not reach the history.
        truncated = finish == "length"
        assistant: Message = {"role": "assistant", "content": "".join(text) or None}
        if acc and not truncated:
            assistant["tool_calls"] = [
                {
                    "id": s["id"],
                    "type": "function",
                    "function": {"name": s["name"], "arguments": s["args"] or "{}"},
                }
                for _, s in sorted(acc.items())
            ]
        if assistant["content"] or assistant.get("tool_calls"):
            messages.append(assistant)

        calls = []
        for _, s in sorted(acc.items()) if not truncated else []:
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
            cached = getattr(details, "cached_tokens", 0) or 0
            usage = Usage(
                # OpenAI's prompt_tokens includes the cached part; Anthropic's
                # input_tokens excludes it. Normalise to Anthropic's meaning so
                # "input" is always the uncached remainder — otherwise cached
                # tokens get counted twice in both cost and context size.
                input_tokens=max(raw.prompt_tokens - cached, 0),
                output_tokens=raw.completion_tokens,
                cache_read=cached,
            )

        return Step(
            stop_reason=self._FINISH.get(finish, finish),
            tool_calls=calls,
            usage=usage,
        )

    def summarize(self, text: str, instruction: str) -> str:
        response = self._client.chat.completions.create(
            model=self._cfg.model,
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": text},
            ],
            **{self._cfg.token_param: SUMMARY_MAX_TOKENS},
        )
        return (response.choices[0].message.content or "").strip()

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
