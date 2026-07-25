from __future__ import annotations

import sys

from .config import ConfigError, load
from .providers import STREAM_ERRORS, build


def main() -> int:
    try:
        cfg = load()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    provider = build(cfg)
    print(
        f"{cfg.profile}:{cfg.model} — /exit or Ctrl-D to quit, Ctrl-C cancels a turn",
        file=sys.stderr,
    )

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

        try:
            for text in provider.stream(prompt):
                sys.stdout.write(text)
                sys.stdout.flush()
            print()
        except KeyboardInterrupt:
            print("\n[cancelled]", file=sys.stderr)
        except STREAM_ERRORS as e:
            sys.stdout.flush()
            print(f"\n[{type(e).__name__}] {e}", file=sys.stderr)
