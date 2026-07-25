from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_READ_LINES = 300
MAX_LINE_CHARS = 500
MAX_LIST_ENTRIES = 200
SKIP = {".git", ".venv", "__pycache__", "node_modules", ".DS_Store"}


class ToolError(Exception):
    """A tool failed in a way the model should see and react to."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    run: Callable[[dict], str]


@dataclass(frozen=True)
class Workspace:
    """The single gate between model-supplied text and the filesystem."""

    root: Path

    def resolve(self, raw: str) -> Path:
        if not raw or not isinstance(raw, str):
            raise ToolError("path must be a non-empty string")
        # resolve() collapses '..' and follows symlinks, so the containment
        # check below cannot be fooled by either. An absolute path simply
        # replaces the root and then fails the check.
        target = (self.root / raw).resolve()
        if target != self.root and not target.is_relative_to(self.root):
            raise ToolError(f"path {raw!r} escapes the workspace root")
        return target

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.root)) if path != self.root else "."


def _get_current_time(args: dict) -> str:
    name = args.get("timezone") or "UTC"
    try:
        tz = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return f"error: unknown timezone {name!r}; use an IANA name like 'Asia/Yangon'"
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def _read_file(ws: Workspace, args: dict) -> str:
    path = ws.resolve(args.get("path"))
    if not path.is_file():
        raise ToolError(f"no such file: {args.get('path')!r}")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{ws.rel(path)} is not UTF-8 text") from None

    lines = text.splitlines()
    if not lines:
        return f"{ws.rel(path)} is empty"

    offset = max(1, int(args.get("offset") or 1))
    limit = min(int(args.get("limit") or MAX_READ_LINES), MAX_READ_LINES)
    window = lines[offset - 1 : offset - 1 + limit]
    if not window:
        raise ToolError(f"offset {offset} is past the end ({len(lines)} lines)")

    out = []
    for number, line in enumerate(window, start=offset):
        if len(line) > MAX_LINE_CHARS:
            line = f"{line[:MAX_LINE_CHARS]}… [+{len(line) - MAX_LINE_CHARS} chars]"
        out.append(f"{number:>6}| {line}")

    last = offset + len(window) - 1
    if last < len(lines):
        out.append(
            f"... truncated at line {last} of {len(lines)}; "
            f"call again with offset={last + 1} to continue"
        )
    return "\n".join(out)


def _write_file(ws: Workspace, args: dict) -> str:
    path = ws.resolve(args.get("path"))
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    if path.is_dir():
        raise ToolError(f"{ws.rel(path)} is a directory")

    existed = path.is_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    verb = "overwrote" if existed else "created"
    return (
        f"{verb} {ws.rel(path)} "
        f"({len(content.splitlines())} lines, {len(content.encode())} bytes)"
    )


def _list_files(ws: Workspace, args: dict) -> str:
    path = ws.resolve(args.get("path") or ".")
    if not path.is_dir():
        raise ToolError(f"not a directory: {args.get('path')!r}")

    rows = []
    entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    for entry in entries:
        if entry.name in SKIP:
            continue
        if len(rows) >= MAX_LIST_ENTRIES:
            rows.append(f"... {len(entries) - len(rows)} more entries omitted")
            break
        if entry.is_dir():
            rows.append(f"{entry.name}/")
        else:
            rows.append(f"{entry.name}  ({entry.stat().st_size} bytes)")

    if not rows:
        return f"{ws.rel(path)}/ is empty"
    return f"{ws.rel(path)}/\n" + "\n".join(rows)


def build_registry(ws: Workspace) -> dict[str, Tool]:
    return {
        "get_current_time": Tool(
            name="get_current_time",
            description=(
                "Get the current date and time. Call this whenever the user asks "
                "what the time or date is — you have no other way to know it."
            ),
            schema={
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": (
                            "IANA timezone name, e.g. 'Asia/Yangon' or "
                            "'America/New_York'. Defaults to UTC."
                        ),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            run=_get_current_time,
        ),
        "list_files": Tool(
            name="list_files",
            description=(
                "List the contents of a directory in the workspace. "
                "Use this to discover what exists before reading files."
            ),
            schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory relative to the workspace root. Defaults to '.'.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            run=lambda args: _list_files(ws, args),
        ),
        "read_file": Tool(
            name="read_file",
            description=(
                "Read a UTF-8 text file from the workspace. Output is prefixed "
                f"with line numbers. Reads at most {MAX_READ_LINES} lines per call; "
                "if the file is longer, use 'offset' to page through it."
            ),
            schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File relative to the workspace root.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "1-based line number to start from. Defaults to 1.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Lines to read. Capped at {MAX_READ_LINES}.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            run=lambda args: _read_file(ws, args),
        ),
        "write_file": Tool(
            name="write_file",
            description=(
                "Write text to a file in the workspace, creating parent "
                "directories as needed. Overwrites the whole file, so read it "
                "first unless you are creating it."
            ),
            schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File relative to the workspace root.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file contents to write.",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            run=lambda args: _write_file(ws, args),
        ),
    }
