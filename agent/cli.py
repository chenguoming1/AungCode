from __future__ import annotations

import sys

import logging
import os
from pathlib import Path

from . import compact, loop
from .config import ConfigError, load
from .prompt import AGENT_FILE, build_system
from .providers import STREAM_ERRORS, Message, ToolCall, ToolResult, Usage, build
from .tools import ToolError, Workspace, build_registry

APPROVE_ALL = os.environ.get("AGENT_APPROVE_ALL") == "1"


def _block(prefix: str, text: str, limit: int = 20) -> str:
    if not text:
        return f"  {prefix} (empty)"
    lines = text.splitlines()
    out = [f"  {prefix} {line}" for line in lines[:limit]]
    if len(lines) > limit:
        out.append(f"  {prefix} ... +{len(lines) - limit} more lines")
    return "\n".join(out)


def _render_action(call: ToolCall, workspace: Workspace) -> str:
    """The exact thing about to happen — never abbreviated."""
    args = call.args
    if call.name == "bash":
        return f"  $ {args.get('command', '')}"

    if call.name == "write_file":
        path = args.get("path", "?")
        content = args.get("content", "")
        note = ""
        try:
            target = workspace.resolve(path)
            if target.is_file():
                existing = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
                note = f"  [OVERWRITES existing file, {existing} lines]"
        except (ToolError, OSError):
            pass
        return f"  write {path}{note}\n{_block('+', content)}"

    if call.name == "edit_file":
        return (
            f"  edit {args.get('path', '?')}\n"
            f"{_block('-', args.get('old_str', ''))}\n"
            f"{_block('+', args.get('new_str', ''))}"
        )

    return f"  {call.name}({_fmt_args(call.args)})"


def _approver(workspace: Workspace, always: set[str]):
    def approve(call: ToolCall) -> bool:
        action = _render_action(call, workspace)
        if APPROVE_ALL:
            print(f"\n[auto-approved]\n{action}", file=sys.stderr)
            return True
        if call.name in always:
            print(f"\n[approved: always]\n{action}", file=sys.stderr)
            return True

        print(f"\n{action}", file=sys.stderr)
        while True:
            print(f"approve {call.name}? [y/N/a=always] ", end="", file=sys.stderr, flush=True)
            try:
                reply = input().strip().lower()
            except EOFError:
                print("(no input — denied)", file=sys.stderr)
                return False
            if reply in ("y", "yes"):
                return True
            if reply in ("a", "always"):
                always.add(call.name)
                return True
            if reply in ("", "n", "no"):
                return False

    return approve


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


# Compact once the prompt passes this share of the model's context window.
COMPACT_AT = 0.75


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def context_size(usages: list[Usage]) -> int:
    """What the next request will carry, measured rather than estimated."""
    if not usages:
        return 0
    last = usages[-1]
    return last.input_tokens + last.cache_read + last.cache_write + last.output_tokens


def _usage_line(usages: list[Usage], msgs: int, ctx: int, window: int, session: int) -> str:
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
    parts.append(f"{msgs} msgs")
    if ctx:
        parts.append(f"ctx {_fmt_tokens(ctx)}/{_fmt_tokens(window)} ({ctx * 100 // window}%)")
    parts.append(f"session {_fmt_tokens(session)}")
    return " · ".join(parts)


def _compact_now(provider, history: list[Message]) -> bool:
    try:
        result = compact.compact(provider, history)
    except STREAM_ERRORS as e:
        print(f"[compaction failed: {type(e).__name__}: {e}]", file=sys.stderr)
        return False
    if result is None:
        print(
            f"[nothing to compact — fewer than {compact.KEEP_TURNS} earlier turns]",
            file=sys.stderr,
        )
        return False
    history[:] = result.messages
    print(
        f"[compacted {result.removed} messages into a summary; "
        f"{len(history)} msgs now, last {compact.KEEP_TURNS} turns kept verbatim]",
        file=sys.stderr,
    )
    return True


def main() -> int:
    # Warnings and tracebacks go to stderr so stdout stays pipeable.
    # AGENT_DEBUG=1 adds the SDKs' own debug chatter.
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("AGENT_DEBUG") == "1" else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

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
    system, has_agent_file = build_system(workspace)

    print(
        f"{cfg.profile}:{cfg.model} · {len(toolset)} tool{'s' * (len(toolset) != 1)} — "
        "/clear resets history, /compact summarizes it, /system shows the prompt, "
        "/exit or Ctrl-D quits, Ctrl-C cancels a turn",
        file=sys.stderr,
    )
    print(
        f"workspace: {workspace.root}"
        + (f" · {AGENT_FILE} loaded" if has_agent_file else ""),
        file=sys.stderr,
    )
    if APPROVE_ALL:
        print("AGENT_APPROVE_ALL=1 — every action runs unattended", file=sys.stderr)

    history: list[Message] = []
    # "always" decisions last for this process only, never written to disk.
    always: set[str] = set()
    approve = _approver(workspace, always)
    ctx = 0  # 0 means "unknown until the next reply measures it"
    session_tokens = 0

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
        if prompt == "/system":
            print(system, file=sys.stderr)
            continue
        if prompt == "/compact":
            if _compact_now(provider, history):
                ctx = 0
            continue

        limit = int(cfg.context_window * COMPACT_AT)
        if ctx > limit:
            print(
                f"[context {_fmt_tokens(ctx)} over the "
                f"{_fmt_tokens(limit)} threshold]",
                file=sys.stderr,
            )
            if _compact_now(provider, history):
                ctx = 0

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
            turn = loop.run_turn(
                provider, history, toolset, emit, on_tool, approve, system
            )
            usages = turn.usages
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
            logging.getLogger("agent").debug("stream error detail", exc_info=True)
            continue
        except Exception:
            # Never lose the session to an unforeseen bug — but never hide it
            # either. The full traceback is printed before we carry on.
            del history[mark:]
            sys.stdout.flush()
            logging.getLogger("agent").exception("unexpected error during turn")
            print("\n[unexpected error above — turn discarded]", file=sys.stderr)
            continue

        print()
        if turn.stop_reason == "max_tokens":
            print(
                f"[response hit max_tokens ({cfg.max_tokens}); it was cut off and any "
                "half-written tool call was dropped. Raise max_tokens in "
                "agent/config.toml or ask for smaller steps]",
                file=sys.stderr,
            )
        if usages:
            ctx = context_size(usages)
            session_tokens += sum(u.input_tokens + u.output_tokens for u in usages)
            print(
                f"[{_usage_line(usages, len(history), ctx, cfg.context_window, session_tokens)}]",
                file=sys.stderr,
            )
