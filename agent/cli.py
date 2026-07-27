from __future__ import annotations

import argparse
import atexit
import logging
import os
import sys
from pathlib import Path

from . import commands, compact, hooks as hooks_mod, loop, mcp, session, subagent
from .config import ConfigError, load
from .prompt import AGENT_FILE, build_system
from .providers import STREAM_ERRORS, Message, ToolCall, ToolResult, Usage, build
from .render import Renderer, fmt_args, preview
from .tools import ToolError, Workspace, build_registry

APPROVE_ALL = os.environ.get("AGENT_APPROVE_ALL") == "1"

# Compact once the prompt passes this share of the model's context window.
COMPACT_AT = 0.75


def _block(prefix: str, text: str, limit: int = 20) -> str:
    if not text:
        return f"  {prefix} (empty)"
    lines = text.splitlines()
    out = [f"  {prefix} {line}" for line in lines[:limit]]
    if len(lines) > limit:
        out.append(f"  {prefix} ... +{len(lines) - limit} more lines")
    return "\n".join(out)


def _action_text(call: ToolCall, workspace: Workspace) -> str:
    """The exact thing about to happen — never abbreviated. Colouring is the
    renderer's job; this only decides what the words are."""
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

    return f"  {call.name}({fmt_args(call.args)})"


def _approver(r: Renderer, workspace: Workspace, always: set[str]):
    def approve(call: ToolCall) -> bool:
        action = _action_text(call, workspace)
        if APPROVE_ALL:
            r.note("auto-approved")
            r.action(action)
            return True
        if call.name in always:
            r.note(f"approved: always ({call.name})")
            r.action(action)
            return True

        r.action(action)
        while True:
            try:
                reply = _ask(r, f"approve {call.name}? [y/N/a=always] ").strip().lower()
            except EOFError:
                r.note("(no input — denied)")
                return False
            if reply in ("y", "yes"):
                return True
            if reply in ("a", "always"):
                always.add(call.name)
                return True
            if reply in ("", "n", "no"):
                return False

    return approve


def _ask(r: Renderer, text: str) -> str:
    """Prompt and read a line. Piped stdin has no terminal echo, so the
    prompt line is closed by hand to keep transcripts readable."""
    r.prompt(text)
    reply = input()
    if not sys.stdin.isatty():
        r.plain("")
    return reply


def _parse_args(argv: list[str] | None):
    p = argparse.ArgumentParser(prog="agent", description="A small coding agent.")
    p.add_argument(
        "--resume",
        nargs="?",
        const="",
        metavar="ID",
        help="resume a session by id; give no id to choose from a list",
    )
    p.add_argument(
        "--continue",
        dest="continue_",
        action="store_true",
        help="resume the most recent session for this workspace",
    )
    return p.parse_args(argv)


def _pick(r: Renderer, sessions: list[session.Session]) -> session.Session | None:
    if not sessions:
        r.note("no saved sessions for this workspace")
        return None
    shown = sessions[:20]
    for i, s in enumerate(shown, 1):
        r.plain(f"  {r.e.cyan(str(i).rjust(2))}. {session.describe(s)}")
    try:
        reply = _ask(r, "session number (blank to cancel): ").strip()
    except EOFError:
        return None
    if reply.isdigit() and 1 <= int(reply) <= len(shown):
        return shown[int(reply) - 1]
    return None


def _adopt(r: Renderer, cfg, workspace: Workspace, found: session.Session):
    """Load a session's messages, warning about anything that changed."""
    sess, messages = session.read(found.path)
    if sess.meta.get("workspace") != str(workspace.root):
        r.warn(
            f"session was recorded in {sess.meta.get('workspace')} — "
            f"tools now target {workspace.root}"
        )
    if sess.meta.get("model") != cfg.model:
        r.warn(
            f"session used {sess.meta.get('provider')}:{sess.meta.get('model')} — "
            f"now {cfg.profile}:{cfg.model}"
        )
    r.note(
        f"resumed {sess.id} — {len(messages)} msgs, "
        f"{_fmt_tokens(int(sess.meta.get('session_tokens', 0)))} tokens previously"
    )
    return (
        sess,
        messages,
        int(sess.meta.get("session_tokens", 0)),
        float(sess.meta.get("session_cost", 0.0)),
    )


def _persist(
    r: Renderer, sess: session.Session, history: list[Message], tokens: int, cost: float = 0.0
) -> None:
    try:
        session.save(sess, history, tokens, cost)
    except OSError as e:
        r.warn(f"could not save session: {e}")


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _fmt_cost(amount: float) -> str:
    # Cheap models produce sub-cent turns; four decimals would render them all
    # as $0.0000, which reads as "free" rather than "small".
    if amount < 0.01:
        return f"${amount:.6f}"
    return f"${amount:.4f}" if amount < 1 else f"${amount:.2f}"


def turn_cost(usages: list[Usage], cfg) -> float:
    """Derived from configured prices — zero when the profile has none."""
    if not cfg.priced:
        return 0.0
    total = 0.0
    for u in usages:
        total += (
            u.input_tokens * cfg.price_in
            + u.output_tokens * cfg.price_out
            + u.cache_read * cfg.price_cache_read
            + u.cache_write * cfg.price_cache_write
        )
    return total / 1_000_000


def context_size(usages: list[Usage]) -> int:
    """What the next request will carry, measured rather than estimated."""
    if not usages:
        return 0
    last = usages[-1]
    return last.input_tokens + last.cache_read + last.cache_write + last.output_tokens


def _usage_line(
    usages: list[Usage],
    msgs: int,
    ctx: int,
    window: int,
    session: int,
    cost: float,
    session_cost: float,
) -> str:
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
    if session_cost:
        parts.append(f"{_fmt_cost(cost)} · total {_fmt_cost(session_cost)}")
    return " · ".join(parts)


def _compact_now(r: Renderer, provider, history: list[Message]) -> bool:
    r.thinking("compacting…")
    try:
        result = compact.compact(provider, history)
    except STREAM_ERRORS as e:
        r.error(f"compaction failed: {type(e).__name__}: {e}")
        return False
    finally:
        r.done()
    if result is None:
        r.note(f"nothing to compact — fewer than {compact.KEEP_TURNS} earlier turns")
        return False
    history[:] = result.messages
    r.note(
        f"compacted {result.removed} messages into a summary; "
        f"{len(history)} msgs now, last {compact.KEEP_TURNS} turns kept verbatim"
    )
    return True


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    r = Renderer()

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
        r.error(f"config error: {e}")
        return 2

    provider = build(cfg)
    workspace = Workspace(Path(os.environ.get("AGENT_WORKSPACE", ".")).resolve())
    if not workspace.root.is_dir():
        r.error(f"config error: workspace {workspace.root} is not a directory")
        return 2
    toolset = list(build_registry(workspace).values())

    servers = mcp.connect(workspace.root, r.warn)
    if servers:
        atexit.register(lambda: [s.close() for s in servers])
        mcp_tools = mcp.tools_for(servers, r.warn)
        toolset.extend(mcp_tools)
        r.note(
            f"mcp: {len(mcp_tools)} tools from "
            + ", ".join(f"{s.name} ({s.info.get('name', '?')})" for s in servers)
        )

    hooks = hooks_mod.load(workspace.root, r.warn)
    if isinstance(hooks, hooks_mod.Hooks):
        r.note(f"hooks: {len(hooks.pre)} pre, {len(hooks.post)} post")

    user_commands = commands.load(workspace.root)
    if user_commands:
        r.note("commands: " + ", ".join("/" + n for n in sorted(user_commands)))

    budget = subagent.Budget()

    def on_sub_tool(call: ToolCall, result: ToolResult) -> None:
        r.tool(f"↳ {call.name}", call.args, preview(result.content), result.is_error)

    toolset.append(subagent.build(provider, workspace, on_sub_tool, budget))
    system, has_agent_file = build_system(workspace)

    r.plain(
        f"{r.e.bold(cfg.profile + ':' + cfg.model)} · "
        f"{len(toolset)} tool{'s' * (len(toolset) != 1)}"
    )
    r.note(
        "/clear resets history · /compact summarizes it · /system shows the prompt · "
        "/sessions switches session · /exit quits · Ctrl-C cancels a turn"
    )
    r.note(
        f"workspace: {workspace.root}"
        + (f" · {AGENT_FILE} loaded" if has_agent_file else "")
    )
    if APPROVE_ALL:
        r.warn("AGENT_APPROVE_ALL=1 — every action runs unattended")

    history: list[Message] = []
    session_tokens = 0
    session_cost = 0.0
    sess = session.new(cfg, workspace.root)

    found = None
    if args.continue_:
        found = session.most_recent(workspace.root)
        if found is None:
            r.note("no previous session for this workspace — starting a new one")
    elif args.resume is not None:
        if args.resume:
            found = next(
                (s for s in session.listing() if s.id == args.resume), None
            )
            if found is None:
                r.error(f"no session with id {args.resume!r}")
                return 2
        else:
            found = _pick(r, session.listing(workspace.root))
    if found is not None:
        sess, history, session_tokens, session_cost = _adopt(r, cfg, workspace, found)

    lock = session.Lock(sess.path)
    try:
        lock.acquire()
    except session.SessionBusy as holder:
        r.error(
            f"session {sess.id} is already open in another process ({holder}). "
            "Close it first, or resume a different session."
        )
        return 2

    r.note(f"session: {sess.id}")

    # "always" decisions last for this process only, never written to disk —
    # resuming must not silently re-grant approval you gave yesterday.
    always: set[str] = set()
    approve = _approver(r, workspace, always)
    ctx = 0  # 0 means "unknown until the next reply measures it"

    while True:
        try:
            prompt = _ask(r, r.e.bold("› "))
        except EOFError:
            r.plain("")
            return 0
        except KeyboardInterrupt:
            r.plain("")
            continue

        prompt = prompt.strip()
        if not prompt:
            continue
        if prompt in ("/exit", "/quit"):
            return 0
        if prompt == "/clear":
            history.clear()
            _persist(r, sess, history, session_tokens, session_cost)
            r.note("history cleared")
            continue
        if prompt == "/system":
            r.plain(system)
            continue
        if prompt == "/sessions":
            chosen = _pick(r, session.listing(workspace.root))
            if chosen is None or chosen.id == sess.id:
                continue
            # Take the new lock before dropping the old one, so a refused
            # switch leaves this tab exactly where it was.
            new_lock = session.Lock(chosen.path)
            try:
                new_lock.acquire()
            except session.SessionBusy as holder:
                r.error(f"cannot switch: {chosen.id} is open elsewhere ({holder})")
                continue
            lock.release()
            lock = new_lock
            sess, history, session_tokens, session_cost = _adopt(r, cfg, workspace, chosen)
            ctx = 0
            continue
        if prompt == "/compact":
            if _compact_now(r, provider, history):
                ctx = 0
                _persist(r, sess, history, session_tokens, session_cost)
            continue

        if prompt.startswith("/"):
            name, _, rest = prompt[1:].partition(" ")
            command = user_commands.get(name)
            if command is None:
                known = ", ".join("/" + n for n in sorted(user_commands))
                r.warn(f"unknown command /{name}" + (f" — have {known}" if known else ""))
                continue
            r.note(f"/{name} ← {command.path.name}")
            prompt = command.render(rest)

        limit = int(cfg.context_window * COMPACT_AT)
        if ctx > limit:
            r.warn(f"context {_fmt_tokens(ctx)} over the {_fmt_tokens(limit)} threshold")
            if _compact_now(r, provider, history):
                ctx = 0

        # A tool turn appends several messages; rollback truncates to here.
        mark = len(history)
        history.append({"role": "user", "content": prompt})

        def on_tool(call: ToolCall, result: ToolResult) -> None:
            r.tool(call.name, call.args, preview(result.content), result.is_error)
            if result.display:
                r.diff(result.display)
            # Another API call always follows a tool result.
            r.waiting("working…")

        try:
            r.thinking()
            try:
                turn = loop.run_turn(
                    provider, history, toolset, r.emit, on_tool, approve, system, hooks
                )
            finally:
                r.done()
            usages = turn.usages
        except KeyboardInterrupt:
            del history[mark:]
            r.flush()
            r.note("cancelled — turn discarded")
            continue
        except loop.MaxIterations as e:
            del history[mark:]
            r.flush()
            r.warn(f"{e} — turn discarded")
            continue
        except STREAM_ERRORS as e:
            del history[mark:]
            r.flush()
            r.error(f"{type(e).__name__}: {e}")
            logging.getLogger("agent").debug("stream error detail", exc_info=True)
            continue
        except Exception:
            # Never lose the session to an unforeseen bug — but never hide it
            # either. The full traceback is printed before we carry on.
            del history[mark:]
            r.flush()
            logging.getLogger("agent").exception("unexpected error during turn")
            r.error("unexpected error above — turn discarded")
            continue

        r.flush()
        if turn.stop_reason == "max_tokens":
            r.warn(
                f"response hit max_tokens ({cfg.max_tokens}); it was cut off and any "
                "half-written tool call was dropped. Raise max_tokens in "
                "agent/config.toml or ask for smaller steps"
            )
        if usages:
            ctx = context_size(usages)
            session_tokens += sum(u.input_tokens + u.output_tokens for u in usages)
            # Nested loops bill the same account; fold their spend in and reset.
            session_tokens += budget.tokens
            budget.tokens = 0
            cost = turn_cost(usages, cfg)
            session_cost += cost
            r.usage(
                _usage_line(
                    usages, len(history), ctx, cfg.context_window,
                    session_tokens, cost, session_cost,
                )
            )
        _persist(r, sess, history, session_tokens, session_cost)
