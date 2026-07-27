from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DIR_NAME = "commands"
ARGS_TOKEN = "$ARGUMENTS"
NAME_OK = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    body: str
    path: Path

    def render(self, args: str) -> str:
        """A command is a prompt template, nothing more."""
        args = args.strip()
        if ARGS_TOKEN in self.body:
            return self.body.replace(ARGS_TOKEN, args)
        return f"{self.body}\n\n{args}" if args else self.body


def commands_dir(workspace_root: Path) -> Path:
    override = os.environ.get("AGENT_COMMANDS_DIR")
    return Path(override) if override else workspace_root / DIR_NAME


def _parse(path: Path) -> Command | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    description = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                key, sep, value = line.partition(":")
                if sep and key.strip() == "description":
                    description = value.strip()
            text = text[end + 4 :].lstrip("\n")

    body = text.strip()
    if not body:
        return None
    return Command(path.stem, description or body.splitlines()[0][:70], body, path)


def load(workspace_root: Path) -> dict[str, Command]:
    root = commands_dir(workspace_root)
    found: dict[str, Command] = {}
    if not root.is_dir():
        return found
    for path in sorted(root.glob("*.md")):
        if not NAME_OK.match(path.stem):
            continue
        command = _parse(path)
        if command:
            found[command.name] = command
    return found
