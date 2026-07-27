from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .providers import RETRYABLE, Emit, Message, Provider, ToolCall, ToolResult, Usage
from .hooks import NoHooks
from .tools import Tool, ToolError, ToolOutput

log = logging.getLogger(__name__)

MAX_ITERATIONS = 100
STREAM_RETRIES = 2

OnTool = Callable[[ToolCall, ToolResult], None]
# Returns True to let the call proceed. Asked once per call, before execution.
Approve = Callable[[ToolCall], bool]


class MaxIterations(RuntimeError):
    """The model kept calling tools past the allowed number of round trips."""


@dataclass(frozen=True)
class TurnResult:
    usages: list[Usage] = field(default_factory=list)
    stop_reason: str = "end_turn"


def _execute(
    call: ToolCall, registry: dict[str, Tool], approve: Approve, hooks
) -> ToolResult:
    if call.error:
        return ToolResult(call.id, call.error, is_error=True)

    tool = registry.get(call.name)
    if tool is None:
        return ToolResult(call.id, f"error: no tool named {call.name!r}", is_error=True)

    if tool.requires_approval and not approve(call):
        return ToolResult(
            call.id,
            "the user denied this action. Do not retry it; explain what you "
            "wanted to do, or propose a different approach.",
            is_error=True,
        )

    # After approval, so a hook's side effects never fire for a call the user
    # was about to refuse.
    blocked = hooks.before(call)
    if blocked:
        return ToolResult(call.id, blocked, is_error=True)

    try:
        out = tool.run(call.args)
        result = (
            ToolResult(call.id, out.content, display=out.display)
            if isinstance(out, ToolOutput)
            else ToolResult(call.id, out)
        )
        hooks.after(call, result)
        return result
    except ToolError as e:
        # Expected failure — the model reads this and adjusts.
        return ToolResult(call.id, f"error: {e}", is_error=True)
    except Exception as e:
        # Unexpected: this is a bug in the tool, not a usage error. Hand a
        # message back so the session continues, but log the traceback so the
        # failure is never silently swallowed.
        log.exception("tool %r raised an unexpected exception", call.name)
        return ToolResult(call.id, f"error: {type(e).__name__}: {e}", is_error=True)


def _step_with_retry(
    provider: Provider, messages: list[Message], tools: list[Tool], emit: Emit, system: str
):
    """Retry transient stream failures, undoing anything a failed call appended."""
    for attempt in range(STREAM_RETRIES + 1):
        mark = len(messages)
        try:
            return provider.step(messages, tools, emit, system)
        except RETRYABLE as e:
            del messages[mark:]  # a half-written assistant turn must not survive
            if attempt == STREAM_RETRIES:
                raise
            delay = 2**attempt
            log.warning(
                "%s: %s — retrying in %ss (attempt %d of %d). "
                "Any text already printed above was discarded.",
                type(e).__name__,
                e,
                delay,
                attempt + 2,
                STREAM_RETRIES + 1,
            )
            time.sleep(delay)


def run_turn(
    provider: Provider,
    messages: list[Message],
    tools: list[Tool],
    emit: Emit,
    on_tool: OnTool,
    approve: Approve,
    system: str,
    hooks=None,
    max_iterations: int = MAX_ITERATIONS,
) -> TurnResult:
    """Drive one user turn to completion. Returns usage for each API call made."""
    registry = {t.name: t for t in tools}
    hooks = hooks or NoHooks()
    usages: list[Usage] = []

    for _ in range(max_iterations):
        step = _step_with_retry(provider, messages, tools, emit, system)
        if step.usage:
            usages.append(step.usage)

        if step.stop_reason != "tool_use" or not step.tool_calls:
            return TurnResult(usages, step.stop_reason)

        results = []
        for call in step.tool_calls:
            result = _execute(call, registry, approve, hooks)
            on_tool(call, result)
            results.append(result)
        provider.append_results(messages, results)

    raise MaxIterations(f"gave up after {max_iterations} tool round trips")
