from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

import anthropic
import openai

from .config import ProviderConfig

# Every network/API failure either SDK raises descends from one of these.
STREAM_ERRORS = (anthropic.AnthropicError, openai.OpenAIError)


class Provider(Protocol):
    def stream(self, prompt: str) -> Iterator[str]:
        """Yield response text deltas as they arrive."""


class AnthropicProvider:
    def __init__(self, cfg: ProviderConfig) -> None:
        self._cfg = cfg
        self._client = anthropic.Anthropic(api_key=cfg.api_key, base_url=cfg.base_url)

    def stream(self, prompt: str) -> Iterator[str]:
        with self._client.messages.stream(
            model=self._cfg.model,
            max_tokens=self._cfg.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            yield from stream.text_stream


class OpenAIProvider:
    """Also serves DeepSeek and anything else on the OpenAI wire format."""

    def __init__(self, cfg: ProviderConfig) -> None:
        self._cfg = cfg
        self._client = openai.OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)

    def stream(self, prompt: str) -> Iterator[str]:
        with self._client.chat.completions.create(
            model=self._cfg.model,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
            **{self._cfg.token_param: self._cfg.max_tokens},
        ) as stream:
            for chunk in stream:
                if not chunk.choices:
                    continue
                text = chunk.choices[0].delta.content
                if text:
                    yield text


def build(cfg: ProviderConfig) -> Provider:
    if cfg.kind == "anthropic":
        return AnthropicProvider(cfg)
    return OpenAIProvider(cfg)
