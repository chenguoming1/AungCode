from __future__ import annotations

import difflib
import fnmatch
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_READ_LINES = 300
MAX_LINE_CHARS = 500
MAX_LIST_ENTRIES = 200
MAX_DIFF_LINES = 80
MAX_BASH_LINES = 200
MAX_BASH_CHARS = 12000
BASH_TIMEOUT = 60
MAX_BASH_TIMEOUT = 300
MAX_GLOB_RESULTS = 200
MAX_GREP_MATCHES = 100
MAX_GREP_LINE_CHARS = 200
MAX_GREP_FILE_BYTES = 2_000_000
GREP_TIMEOUT = 30
SKIP = {".git", ".venv", "__pycache__", "node_modules", ".DS_Store"}


class ToolError(Exception):
    """A tool failed in a way the model should see and react to."""


@dataclass(frozen=True)
class ToolOutput:
    """Split what the model reads from what the terminal shows."""

    content: str
    display: str | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: dict
    run: Callable[[dict], str | ToolOutput]
    # Anything that mutates the machine asks the user first.
    requires_approval: bool = False


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


def _truncate_middle(text: str) -> str:
    """Keep the head and the tail — a long run's summary is at the end."""
    lines = text.splitlines()
    if len(lines) > MAX_BASH_LINES:
        half = MAX_BASH_LINES // 2
        omitted = len(lines) - 2 * half
        lines = lines[:half] + [f"... [{omitted} lines omitted] ..."] + lines[-half:]
        text = "\n".join(lines)
    if len(text) > MAX_BASH_CHARS:
        half = MAX_BASH_CHARS // 2
        omitted = len(text) - 2 * half
        text = f"{text[:half]}\n... [{omitted} chars omitted] ...\n{text[-half:]}"
    return text


def _bash(ws: Workspace, args: dict) -> str:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ToolError("command must be a non-empty string")

    requested = args.get("timeout")
    try:
        timeout = min(
            BASH_TIMEOUT if requested is None else int(requested), MAX_BASH_TIMEOUT
        )
    except (TypeError, ValueError):
        raise ToolError("timeout must be an integer number of seconds") from None
    if timeout < 1:
        raise ToolError("timeout must be at least 1 second")

    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=ws.root,
        stdin=subprocess.DEVNULL,  # a command that prompts fails instead of hanging
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # interleaved in the order they were written
        text=True,
        errors="replace",
        start_new_session=True,  # own process group, so the kill below is total
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
        status = f"exit code: {proc.returncode}"
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        output, _ = proc.communicate()
        status = f"timed out after {timeout}s and was killed; partial output follows"

    output = _truncate_middle(output.rstrip())
    return f"{status}\n{output}" if output else f"{status}\n(no output)"


def _inside(ws: Workspace, path: Path) -> bool:
    """A symlinked file can still point outside; check the resolved target."""
    try:
        return path.resolve().is_relative_to(ws.root)
    except OSError:
        return False


def _glob(ws: Workspace, args: dict) -> str:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("pattern must be a non-empty string")
    base = ws.resolve(args.get("path") or ".")
    if not base.is_dir():
        raise ToolError(f"not a directory: {args.get('path')!r}")

    found: list[tuple[float, str]] = []
    try:
        candidates = base.glob(pattern)
    except (NotImplementedError, ValueError) as e:
        raise ToolError(f"bad glob pattern {pattern!r}: {e}") from None

    for path in candidates:
        if not path.is_file() or not _inside(ws, path):
            continue
        rel = ws.rel(path)
        if any(part in SKIP for part in Path(rel).parts):
            continue
        try:
            found.append((path.stat().st_mtime, rel))
        except OSError:
            continue

    if not found:
        return f"no files match {pattern!r}"

    found.sort(reverse=True)  # newest first
    shown = [rel for _, rel in found[:MAX_GLOB_RESULTS]]
    out = "\n".join(shown)
    if len(found) > MAX_GLOB_RESULTS:
        out += f"\n-- {len(found) - MAX_GLOB_RESULTS} more; showing the {MAX_GLOB_RESULTS} newest"
    return out


def _grep_ripgrep(
    rg: str, ws: Workspace, pattern: str, target: str, include: str | None, literal: bool
):
    cmd = [
        rg,
        "--line-number",
        "--no-heading",
        "--color=never",
        "--no-ignore",  # parity with the Python fallback, which cannot read .gitignore
        "--hidden",
        "--max-columns",
        str(MAX_GREP_LINE_CHARS),
    ]
    for name in sorted(SKIP):
        cmd += ["-g", f"!{name}"]
    if include:
        cmd += ["-g", include]
    if literal:
        cmd.append("--fixed-strings")
    cmd += ["-e", pattern, target]

    try:
        proc = subprocess.run(
            cmd, cwd=ws.root, capture_output=True, text=True,
            errors="replace", timeout=GREP_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"grep timed out after {GREP_TIMEOUT}s") from None
    if proc.returncode == 1:
        return []
    if proc.returncode != 0:
        raise ToolError(f"ripgrep failed: {proc.stderr.strip()[:200]}")

    hits = []
    for line in proc.stdout.splitlines():
        path, _, rest = line.partition(":")
        number, _, text = rest.partition(":")
        if not number.isdigit():
            continue
        hits.append((path.removeprefix("./"), int(number), text))
    return hits


def _grep_python(ws: Workspace, regex: re.Pattern, base: Path, include: str | None):
    hits = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for name in sorted(filenames):
            if name in SKIP or (include and not fnmatch.fnmatch(name, include)):
                continue
            path = Path(dirpath) / name
            if not _inside(ws, path):
                continue
            try:
                if path.stat().st_size > MAX_GREP_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # unreadable or binary
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append((ws.rel(path), number, line[:MAX_GREP_LINE_CHARS]))
                    if len(hits) > MAX_GREP_MATCHES:
                        return hits
    return hits


def _grep(ws: Workspace, args: dict) -> str:
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("pattern must be a non-empty string")

    literal = bool(args.get("literal"))
    if literal:
        # Any string is a valid fixed-string search, so there is nothing to reject.
        regex = re.compile(re.escape(pattern))
    else:
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise ToolError(
                f"invalid regex {pattern!r}: {e}. "
                "Pass literal=true to search for this text exactly."
            ) from None

    base = ws.resolve(args.get("path") or ".")
    if not base.is_dir():
        raise ToolError(f"not a directory: {args.get('path')!r}")
    include = args.get("include") or None

    rg = shutil.which("rg")
    if rg:
        target = "." if base == ws.root else ws.rel(base)
        hits = _grep_ripgrep(rg, ws, pattern, target, include, literal)
        backend = "ripgrep"
    else:
        hits = _grep_python(ws, regex, base, include)
        backend = "python re"

    if not hits:
        hint = ""
        if literal and "\\" in pattern:
            hint = (
                " — the pattern contains backslashes, which literal=true "
                "searches for as real characters; retry with the raw text"
            )
        return f"no matches for {pattern!r} ({backend}){hint}"

    capped = hits[:MAX_GREP_MATCHES]
    files = len({rel for rel, _, _ in capped})
    lines = [f"{rel}:{number}: {text.strip()}" for rel, number, text in capped]
    summary = f"-- {len(capped)} matches in {files} files ({backend})"
    if len(hits) > MAX_GREP_MATCHES:
        summary += "; more exist, narrow the pattern"
    return "\n".join(lines) + f"\n{summary}"


def _match_lines(text: str, needle: str) -> list[int]:
    """1-based line number of every non-overlapping occurrence."""
    found, start = [], 0
    while (i := text.find(needle, start)) != -1:
        found.append(text.count("\n", 0, i) + 1)
        start = i + len(needle)
    return found


def _unified_diff(rel: str, before: str, after: str) -> str:
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="",
            n=2,
        )
    )
    if len(lines) > MAX_DIFF_LINES:
        omitted = len(lines) - MAX_DIFF_LINES
        lines = lines[:MAX_DIFF_LINES] + [f"... {omitted} more diff lines"]
    return "\n".join(lines)


def _edit_file(ws: Workspace, args: dict) -> ToolOutput:
    path = ws.resolve(args.get("path"))
    old = args.get("old_str")
    new = args.get("new_str", "")
    if not isinstance(old, str) or not isinstance(new, str):
        raise ToolError("old_str and new_str must be strings")
    if not old:
        raise ToolError("old_str must not be empty; use write_file to create a file")
    if old == new:
        raise ToolError("old_str and new_str are identical, nothing to do")
    if not path.is_file():
        raise ToolError(f"no such file: {args.get('path')!r}")

    try:
        before = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"{ws.rel(path)} is not UTF-8 text") from None

    hits = _match_lines(before, old)
    rel = ws.rel(path)

    if not hits:
        # The overwhelmingly common cause is indentation, so say so explicitly
        # rather than making the model guess.
        hint = ""
        if re.match(r"\s*\d+\|", old):
            hint = (
                " old_str still contains read_file's '   12| ' line-number "
                "prefixes — strip them and pass only the file text."
            )
        elif old.strip() and old.strip() in before:
            hint = (
                " The text exists but the surrounding whitespace or indentation "
                "differs — copy it exactly from read_file output."
            )
        raise ToolError(f"old_str not found in {rel}.{hint}")

    if len(hits) > 1:
        where = ", ".join(str(n) for n in hits[:10])
        more = f" (and {len(hits) - 10} more)" if len(hits) > 10 else ""
        raise ToolError(
            f"old_str matched {len(hits)} times in {rel}, at lines {where}{more}. "
            "It must match exactly once — extend old_str with surrounding lines "
            "to make it unique."
        )

    after = before.replace(old, new, 1)
    path.write_text(after, encoding="utf-8")

    removed = len(old.splitlines())
    added = len(new.splitlines())
    action = "deleted" if not new else "replaced"
    return ToolOutput(
        content=f"{action} 1 occurrence in {rel} at line {hits[0]} (-{removed} +{added} lines)",
        display=_unified_diff(rel, before, after),
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
        "glob": Tool(
            name="glob",
            description=(
                "Find files by name pattern, newest first. Use '**/*.py' to "
                "search every subdirectory. This is how you locate files in a "
                "repo you have not seen — reach for it before read_file. "
                "Results are ordered by modification time, so the most "
                f"recently changed files come first. Returns at most "
                f"{MAX_GLOB_RESULTS} paths."
            ),
            schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, e.g. '**/*.py' or 'src/*.ts'.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search from. Defaults to the workspace root.",
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            run=lambda args: _glob(ws, args),
        ),
        "grep": Tool(
            name="grep",
            description=(
                "Search file contents with a regular expression and get back "
                "'path:line: text' for every match. Use it to find where a "
                "symbol is defined or used before reading whole files. Set "
                "literal=true to search for text exactly, without regex "
                "meaning — always do that for punctuation-heavy strings rather "
                "than escaping by hand or falling back to bash. Keep regex "
                "patterns portable: lookahead and backreferences are not "
                "supported when ripgrep is installed. Narrow with 'include' "
                f"(e.g. '*.py'). Returns at most {MAX_GREP_MATCHES} matches."
            ),
            schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regular expression to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in. Defaults to the workspace root.",
                    },
                    "include": {
                        "type": "string",
                        "description": "Only search files matching this glob, e.g. '*.py'.",
                    },
                    "literal": {
                        "type": "boolean",
                        "description": (
                            "Treat pattern as exact text, not a regex. Use for "
                            "strings containing ( ) [ ] . * + ? etc. Pass the "
                            "raw text unescaped — adding backslashes will "
                            "search for the backslashes themselves."
                        ),
                    },
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            run=lambda args: _grep(ws, args),
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
            requires_approval=True,
        ),
        "edit_file": Tool(
            name="edit_file",
            description=(
                "Replace one exact snippet of text in a file. Prefer this over "
                "write_file for changes to existing files — it is far cheaper "
                "than rewriting the whole file. old_str must appear exactly "
                "once, so include enough surrounding lines to make it unique. "
                "Copy old_str verbatim from read_file output but WITHOUT the "
                "'   12| ' line-number prefix. "
                "Everything old_str matches is DELETED and replaced by new_str: "
                "if you widened old_str to disambiguate, repeat that context in "
                "new_str or you will delete it too. Use an empty new_str only "
                "when you mean to remove the entire matched text."
            ),
            schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File relative to the workspace root.",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "Exact text to find, including indentation.",
                    },
                    "new_str": {
                        "type": "string",
                        "description": (
                            "Text that replaces the whole of old_str. Must repeat "
                            "any context lines you added to old_str. Empty string "
                            "deletes everything old_str matched."
                        ),
                    },
                },
                "required": ["path", "old_str", "new_str"],
                "additionalProperties": False,
            },
            run=lambda args: _edit_file(ws, args),
            requires_approval=True,
        ),
        "bash": Tool(
            name="bash",
            description=(
                "Run a shell command and get back its combined stdout/stderr "
                "and exit code. Use it for running tests, git, build tools, "
                "package managers, and anything the other tools cannot do. "
                "Prefer read_file, edit_file and list_files for reading and "
                "changing files — they are cheaper and give better errors. "
                "Every call starts a fresh shell in the workspace root and "
                "NOTHING persists between calls: no cd, no variables, no "
                "background jobs. Chain within one call instead, e.g. "
                "'cd src && pytest -q'. stdin is empty, so a command that "
                "waits for input fails instead of hanging. Long output is "
                "truncated in the middle, keeping the start and the end."
            ),
            schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to run.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            f"Seconds before the command is killed. "
                            f"Defaults to {BASH_TIMEOUT}, maximum {MAX_BASH_TIMEOUT}."
                        ),
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            run=lambda args: _bash(ws, args),
            requires_approval=True,
        ),
    }
