from __future__ import annotations

import sys

import os
from pathlib import Path

from . import loop
from .config import ConfigError, load
from .providers import STREAM_ERRORS, Message, ToolCall, ToolResult, Usage, build
from .tools import Workspace, build_registry


def _preview(text: str, width: int = 96) -> str:
    """Tool results can be whole files; keep the transcript readable."""
    first, _, rest = text.partition("\n")
    if len(first) > width:
        first = first[:width] + "…"
    extra = text.count("\n")
    return f"{first} (+{extra} lines)" if rest else first


def _fmt_args(args: dict, width: int = 44) -> str:
    """Edit and write calls carry whole file bodies — never print them raw."""
    bits = []
    for key, value in args.items():
        if not isinstance(value, str):
            bits.append(f"{key}={value!r}")
            continue
        text = value.replace("\n", "\\n")
        if len(text) > width:
            text = text[:width] + "…"
        bits.append(f"{key}={text!r}")
    return ", ".join(bits)


_DIFF_COLORS = {"+": "\033[32m", "-": "\033[31m", "@": "\033[36m"}


def _colorize(diff: str) -> str:
    if not sys.stderr.isatty():
        return diff
    out = []
    for line in diff.splitlines():
        color = None if line.startswith(("+++", "---")) else _DIFF_COLORS.get(line[:1])
        out.append(f"{color}{line}\033[0m" if color else line)
    return "\n".join(out)


def _usage_line(usages: list[Usage], msgs: int) -> str:
    parts = [
        f"in {sum(u.input_tokens for u in usages)}",
        f"out {sum(u.output_tokens for u in usages)}",
    ]
    cached = sum(u.cache_read for u in usages)
    written = sum(u.cache_write for u in usages)
    if cached:
        parts.append(f"cached {cached}")
    if written:
        parts.append(f"cache-write {written}")
    if len(usages) > 1:
        parts.append(f"{len(usages)} api calls")
    parts.append(f"{msgs} msgs in context")
    return " · ".join(parts)


def main() -> int:
    try:
        cfg = load()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    provider = build(cfg)
    workspace = Workspace(Path(os.environ.get("AGENT_WORKSPACE", ".")).resolve())
    if not workspace.root.is_dir():
        print(f"config error: workspace {workspace.root} is not a directory", file=sys.stderr)
        return 2
    toolset = list(build_registry(workspace).values())

    print(
        f"{cfg.profile}:{cfg.model} · {len(toolset)} tool{'s' * (len(toolset) != 1)} — "
        "/clear resets history, "
        "/exit or Ctrl-D quits, Ctrl-C cancels a turn",
        file=sys.stderr,
    )
    print(f"workspace: {workspace.root}", file=sys.stderr)

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

        # A tool turn appends several messages; rollback truncates to here.
        mark = len(history)
        history.append({"role": "user", "content": prompt})

        def emit(text: str) -> None:
            sys.stdout.write(text)
            sys.stdout.flush()

        def on_tool(call: ToolCall, result: ToolResult) -> None:
            arrow = "!!" if result.is_error else "->"
            print(
                f"\n[tool] {call.name}({_fmt_args(call.args)}) "
                f"{arrow} {_preview(result.content)}",
                file=sys.stderr,
            )
            if result.display:
                print(_colorize(result.display), file=sys.stderr)

        try:
            usages = loop.run_turn(provider, history, toolset, emit, on_tool)
        except KeyboardInterrupt:
            del history[mark:]
            print("\n[cancelled — turn discarded]", file=sys.stderr)
            continue
        except loop.MaxIterations as e:
            del history[mark:]
            print(f"\n[{e} — turn discarded]", file=sys.stderr)
            continue
        except STREAM_ERRORS as e:
            del history[mark:]
            sys.stdout.flush()
            print(f"\n[{type(e).__name__}] {e}", file=sys.stderr)
            continue

        print()
        if usages:
            print(f"[{_usage_line(usages, len(history))}]", file=sys.stderr)
