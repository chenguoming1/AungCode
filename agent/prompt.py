from __future__ import annotations

import platform
from pathlib import Path

from .tools import SKIP, Workspace

AGENT_FILE = "AGENT.md"
MAX_AGENT_CHARS = 20_000
TREE_DEPTH = 2
TREE_ENTRIES = 120

ROLE = """\
You are a coding agent working in a single project directory through the tools
you have been given. You act by calling tools, not by describing what someone
else should do.

Working method:
- Orient before acting. Use glob to find files by name and grep to find them by
  content. Read only the files those searches point at — never guess a path.
- Never edit a file you have not read in this session. Read it first, even if
  you are confident about its contents.
- Prefer edit_file over write_file. write_file replaces the entire file and is
  for creating new ones or deliberate full rewrites.
- In edit_file, old_str must match exactly once. Widen it with surrounding
  lines to disambiguate, and repeat those lines in new_str or you will delete
  them. An empty new_str deletes everything old_str matched.
- Use grep with literal=true for anything containing regex punctuation. Pass
  the raw text; do not add backslashes.
- Use bash to run things — tests, git, builds, package managers. Do not use it
  to read, search or edit files; the dedicated tools are cheaper, safer and
  give better errors. Every bash call is a fresh shell at the workspace root
  and nothing persists between calls, so chain with 'cd sub && cmd'.
- Verify your work. After changing code, run the project's tests or at least
  re-read the edited region.

Boundaries:
- write_file, edit_file and bash require the user's approval. If an action is
  denied, do not retry it. Explain what you intended and offer an alternative.
- File tools cannot leave the working directory. Do not try to reach outside it.

Style:
- Be concise. Report what you did and what you found, not a narration of every
  step. Do not paste whole files back to the user when a summary will do.
- If a request is ambiguous in a way that changes what you would edit, ask
  before acting. Otherwise proceed.\
"""


def _tree(root: Path, depth: int = TREE_DEPTH, limit: int = TREE_ENTRIES) -> str:
    """Shallow orientation only — hidden entries are left out on purpose."""
    lines: list[str] = []

    def walk(directory: Path, level: int) -> None:
        if level > depth or len(lines) >= limit:
            return
        try:
            entries = sorted(
                directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
            )
        except OSError:
            return
        for entry in entries:
            if len(lines) >= limit:
                lines.append("  ... truncated")
                return
            if entry.name in SKIP or entry.name.startswith("."):
                continue
            indent = "  " * (level - 1)
            if entry.is_dir():
                lines.append(f"{indent}{entry.name}/")
                walk(entry, level + 1)
            else:
                lines.append(f"{indent}{entry.name}")

    walk(root, 1)
    return "\n".join(lines) if lines else "(empty)"


def load_agent_file(ws: Workspace) -> str | None:
    path = ws.root / AGENT_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not text:
        return None
    if len(text) > MAX_AGENT_CHARS:
        text = text[:MAX_AGENT_CHARS] + "\n... (truncated)"
    return text


def build_system(ws: Workspace) -> tuple[str, bool]:
    """Returns the system prompt and whether AGENT.md contributed to it."""
    parts = [
        ROLE,
        "\n# Environment\n"
        f"working directory: {ws.root}\n"
        f"platform: {platform.system()} {platform.release()} ({platform.machine()})",
        f"\n# Project layout (depth {TREE_DEPTH}, hidden entries omitted)\n{_tree(ws.root)}",
    ]

    project = load_agent_file(ws)
    if project:
        parts.append(
            f"\n# Project instructions ({AGENT_FILE})\n"
            "These come from the project and take precedence over the general "
            f"guidance above.\n\n{project}"
        )
    return "\n".join(parts), project is not None
