from __future__ import annotations

from dataclasses import dataclass

from . import loop
from .prompt import build_task_system
from .providers import Message, Provider, ToolCall
from .tools import Tool, ToolError, ToolOutput, Workspace, build_registry

MAX_TASK_ITERATIONS = 30

DESCRIPTION = (
    "Delegate a self-contained investigation to a subagent that has its own "
    "fresh context and read-only tools (list_files, glob, grep, read_file). "
    "Use it when answering would mean reading many files whose contents you do "
    "not need to keep — the subagent reads them and returns only its "
    "conclusion, so your own context stays small. Write a complete, "
    "self-contained brief: the subagent cannot see this conversation, so state "
    "the question, any paths worth starting from, and what the answer should "
    "contain. It cannot write, edit, run commands, or delegate further, and it "
    "replies exactly once — you get its final message and nothing else."
)


@dataclass
class Budget:
    """Mutable so nested spend is visible in the parent's usage line."""

    tokens: int = 0


def _deny(call: ToolCall) -> bool:
    """Read-only tools never ask for approval; this is the fail-closed default
    if a mutating tool ever reaches a subagent."""
    return False


def _final_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    parts.append(block.get("text", ""))
                else:
                    parts.append(getattr(block, "text", "") or "")
            text = "\n".join(p for p in parts if p)
            if text.strip():
                return text.strip()
    return ""


def build(provider: Provider, ws: Workspace, on_tool, budget: Budget) -> Tool:
    # Read-only is defined by the approval flag, so the two can never drift.
    # This also excludes `task` itself — subagents cannot recurse.
    readonly = [t for t in build_registry(ws).values() if not t.requires_approval]

    def run(args: dict) -> ToolOutput:
        task = args.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ToolError("task must be a non-empty description")

        # A brand-new message list: nothing from the parent conversation.
        messages: list[Message] = [{"role": "user", "content": task.strip()}]
        try:
            result = loop.run_turn(
                provider,
                messages,
                readonly,
                lambda text: None,  # the subagent's prose is not the parent's output
                on_tool,
                _deny,
                build_task_system(ws),
                max_iterations=MAX_TASK_ITERATIONS,
            )
        except loop.MaxIterations:
            raise ToolError(
                f"subagent hit {MAX_TASK_ITERATIONS} tool round trips without "
                "answering; narrow the task and try again"
            ) from None

        budget.tokens += sum(u.input_tokens + u.output_tokens for u in result.usages)
        answer = _final_text(messages)
        if not answer:
            raise ToolError("subagent returned no answer")

        spent = sum(u.input_tokens + u.output_tokens for u in result.usages)
        return ToolOutput(
            content=answer,
            display=(
                f"  subagent finished: {len(result.usages)} api calls, "
                f"{spent} tokens, {len(messages)} msgs discarded"
            ),
        )

    return Tool(
        name="task",
        description=DESCRIPTION,
        schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "Self-contained brief. The subagent sees only this — "
                        "include the question, useful starting paths, and what "
                        "the answer must contain."
                    ),
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
        run=run,
    )
