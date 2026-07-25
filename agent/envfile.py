from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _candidates() -> tuple[list[Path], bool]:
    """Files to try, and whether the location was named explicitly."""
    override = os.environ.get("AGENT_ENV_FILE")
    if override:
        return [Path(override)], True
    return [Path.cwd() / ".env", PROJECT_ROOT / ".env"], False


def load() -> Path | None:
    """Populate os.environ from the first .env found; return which one.

    Values already present in the real environment are never overwritten,
    so an inline `KEY=... python -m agent` still wins for that run.
    Syntax: KEY=value per line, optional `export ` prefix, optional
    surrounding quotes. `#` starts a comment only at the start of a line.
    """
    paths, explicit = _candidates()

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            if explicit:
                raise ValueError(f"cannot read $AGENT_ENV_FILE {path}: {e.strerror}")
            continue

        for lineno, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line.removeprefix("export ").lstrip()

            key, sep, value = line.partition("=")
            if not sep or not key.strip():
                raise ValueError(f"{path}:{lineno}: expected KEY=value, got {raw!r}")

            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)

        return path

    return None
