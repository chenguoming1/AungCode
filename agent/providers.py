from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import anthropic
import openai

from .config import ProviderConfig

# Every network/API failure either SDK raises descends from one of these.
STREAM_ERRORS = (anthropic.AnthropicError, openai.OpenAIError)

Message = dict[str, str]
Emit = Callable[[str], None]


@dataclass(frozen=True)
class Usage:
    """Per-turn token counts, normalized across SDKs."""

    input_tokens: int
    output_tokens: int
    cache_read: int = 0
    cache_write: int = 0


class Provider(Protocol):
    def run(self, messages: list[Message], emit: Emit) -> Usage | None:
        """Send the full history, push text deltas to `emit`, return usage."""


class AnthropicProvider:
    def __init__(self, cfg: ProviderConfig) -> None:
        self._cfg = cfg
        self._client = anthropic.Anthropic(api_key=cfg.api_key, base_url=cfg.base_url)

    def run(self, messages: list[Message], emit: Emit) -> Usage | None:
        with self._client.messages.stream(
            model=self._cfg.model,
            max_tokens=self._cfg.max_tokens,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                emit(text)
            usage = stream.get_final_message().usage

        return Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read=usage.cache_read_input_tokens or 0,
            cache_write=usage.cache_creation_input_tokens or 0,
        )


class OpenAIProvider:
    """Also serves DeepSeek and anything else on the OpenAI wire format."""

    def __init__(self, cfg: ProviderConfig) -> None:
        self._cfg = cfg
        self._client = openai.OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)

    def run(self, messages: list[Message], emit: Emit) -> Usage | None:
        raw = None
        with self._client.chat.completions.create(
            model=self._cfg.model,
            messages=messages,
            stream=True,
            # Without this, a streamed response carries no usage at all.
            stream_options={"include_usage": True},
            **{self._cfg.token_param: self._cfg.max_tokens},
        ) as stream:
            for chunk in stream:
                if chunk.usage:
                    raw = chunk.usage
                if not chunk.choices:
                    continue
                text = chunk.choices[0].delta.content
                if text:
                    emit(text)

        if raw is None:
            return None

        details = getattr(raw, "prompt_tokens_details", None)
        return Usage(
            input_tokens=raw.prompt_tokens,
            output_tokens=raw.completion_tokens,
            cache_read=getattr(details, "cached_tokens", 0) or 0,
        )


def build(cfg: ProviderConfig) -> Provider:
    if cfg.kind == "anthropic":
        return AnthropicProvider(cfg)
    return OpenAIProvider(cfg)
