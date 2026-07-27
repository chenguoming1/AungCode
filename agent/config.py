from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import envfile

DEFAULT_PATH = Path(__file__).with_name("config.toml")


class ConfigError(Exception):
    """Configuration is missing or malformed."""


@dataclass(frozen=True)
class ProviderConfig:
    profile: str
    kind: str
    model: str
    api_key: str
    max_tokens: int
    context_window: int
    base_url: str | None = None
    token_param: str = "max_tokens"
    # USD per million tokens. All zero means "pricing unknown" — no cost shown.
    price_in: float = 0.0
    price_out: float = 0.0
    price_cache_read: float = 0.0
    price_cache_write: float = 0.0

    @property
    def priced(self) -> bool:
        return bool(self.price_in or self.price_out)


def load(path: Path | None = None) -> ProviderConfig:
    try:
        envfile.load()
    except ValueError as e:
        raise ConfigError(str(e)) from None

    path = path or Path(os.environ.get("AGENT_CONFIG", DEFAULT_PATH))

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from None

    profile = os.environ.get("AGENT_PROVIDER") or raw.get("provider")
    if not profile:
        raise ConfigError(f"no `provider` set in {path}")

    section = raw.get("providers", {}).get(profile)
    if section is None:
        known = ", ".join(sorted(raw.get("providers", {}))) or "none"
        raise ConfigError(f"unknown provider {profile!r} (defined: {known})")

    for key in ("kind", "model", "api_key_env"):
        if not section.get(key):
            raise ConfigError(f"providers.{profile} is missing `{key}`")

    kind = section["kind"]
    if kind not in ("anthropic", "openai"):
        raise ConfigError(f"providers.{profile}.kind must be 'anthropic' or 'openai'")

    key_env = section["api_key_env"]
    api_key = os.environ.get(key_env)
    if not api_key:
        raise ConfigError(f"${key_env} is not set (required by providers.{profile})")

    return ProviderConfig(
        profile=profile,
        kind=kind,
        model=section["model"],
        api_key=api_key,
        max_tokens=int(section.get("max_tokens", 8192)),
        context_window=int(section.get("context_window", 200_000)),
        base_url=section.get("base_url"),
        token_param=section.get("token_param", "max_tokens"),
        price_in=float(section.get("price_in", 0)),
        price_out=float(section.get("price_out", 0)),
        price_cache_read=float(section.get("price_cache_read", 0)),
        price_cache_write=float(section.get("price_cache_write", 0)),
    )
