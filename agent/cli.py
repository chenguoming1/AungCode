from __future__ import annotations

import sys

from .config import ConfigError, load
from .providers import STREAM_ERRORS, Message, Usage, build


def _usage_line(usage: Usage, turns: int) -> str:
    parts = [f"in {usage.input_tokens}", f"out {usage.output_tokens}"]
    if usage.cache_read:
        parts.append(f"cached {usage.cache_read}")
    if usage.cache_write:
        parts.append(f"cache-write {usage.cache_write}")
    parts.append(f"{turns} msgs in context")
    return " · ".join(parts)


def main() -> int:
    try:
        cfg = load()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    provider = build(cfg)
    print(
        f"{cfg.profile}:{cfg.model} — /clear resets history, "
        "/exit or Ctrl-D quits, Ctrl-C cancels a turn",
        file=sys.stderr,
    )

    history: list[Message] = []

    while True:
        try:
            prompt = input("> ")
        except EOFError:
            print(file=sys.stderr)
            return 0
        except KeyboardInterrupt:
            print(file=sys.stderr)
            continue

        prompt = prompt.strip()
        if not prompt:
            continue
        if prompt in ("/exit", "/quit"):
            return 0
        if prompt == "/clear":
            history.clear()
            print("[history cleared]", file=sys.stderr)
            continue

        history.append({"role": "user", "content": prompt})
        reply: list[str] = []

        def emit(text: str) -> None:
            reply.append(text)
            sys.stdout.write(text)
            sys.stdout.flush()

        try:
            usage = provider.run(history, emit)
        except KeyboardInterrupt:
            history.pop()
            print("\n[cancelled — turn discarded]", file=sys.stderr)
            continue
        except STREAM_ERRORS as e:
            history.pop()
            sys.stdout.flush()
            print(f"\n[{type(e).__name__}] {e}", file=sys.stderr)
            continue

        print()
        if not reply:
            history.pop()
            print("[empty response — turn discarded]", file=sys.stderr)
            continue

        history.append({"role": "assistant", "content": "".join(reply)})
        if usage:
            print(f"[{_usage_line(usage, len(history))}]", file=sys.stderr)
