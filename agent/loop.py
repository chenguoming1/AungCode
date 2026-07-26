from __future__ import annotations

from collections.abc import Callable

from .providers import Emit, Message, Provider, ToolCall, ToolResult, Usage
from .tools import Tool, ToolError, ToolOutput

MAX_ITERATIONS = 8

OnTool = Callable[[ToolCall, ToolResult], None]
# Returns True to let the call proceed. Asked once per call, before execution.
Approve = Callable[[ToolCall], bool]


class MaxIterations(RuntimeError):
    """The model kept calling tools past the allowed number of round trips."""


def _execute(call: ToolCall, registry: dict[str, Tool], approve: Approve) -> ToolResult:
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

    try:
        out = tool.run(call.args)
        if isinstance(out, ToolOutput):
            return ToolResult(call.id, out.content, display=out.display)
        return ToolResult(call.id, out)
    except ToolError as e:
        # Expected failure — the model reads this and adjusts.
        return ToolResult(call.id, f"error: {e}", is_error=True)
    except Exception as e:
        # A broken tool must not end the session: hand the failure back and
        # let the model decide what to do about it.
        return ToolResult(call.id, f"error: {type(e).__name__}: {e}", is_error=True)


def run_turn(
    provider: Provider,
    messages: list[Message],
    tools: list[Tool],
    emit: Emit,
    on_tool: OnTool,
    approve: Approve,
    system: str,
    max_iterations: int = MAX_ITERATIONS,
) -> list[Usage]:
    """Drive one user turn to completion. Returns usage for each API call made."""
    registry = {t.name: t for t in tools}
    usages: list[Usage] = []

    for _ in range(max_iterations):
        step = provider.step(messages, tools, emit, system)
        if step.usage:
            usages.append(step.usage)

        if step.stop_reason != "tool_use" or not step.tool_calls:
            return usages

        results = []
        for call in step.tool_calls:
            result = _execute(call, registry, approve)
            on_tool(call, result)
            results.append(result)
        provider.append_results(messages, results)

    raise MaxIterations(f"gave up after {max_iterations} tool round trips")
