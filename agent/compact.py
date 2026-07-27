from __future__ import annotations

import json
from dataclasses import dataclass

from .providers import Message, Provider

KEEP_TURNS = 4
MAX_RENDER_CHARS = 120_000
MAX_RESULT_CHARS = 400

# Marks the synthetic message compaction inserts. Load-bearing: find_cut uses
# it to tell a real user turn from one we wrote, so repeated compaction does
# not summarize its own summary. Kept in the content rather than an extra dict
# key, because unknown keys can be rejected on the wire.
SUMMARY_PREFIX = "Summary of the earlier conversation:"

SUMMARY_INSTRUCTION = """\
You are compressing the earlier part of a coding session so it can be dropped
from context. Write a dense factual summary, no preamble, no closing remarks.

Preserve, in this order:
1. What the user asked for, including any constraints, preferences or
   corrections they gave. Quote exact wording for anything that reads like a
   rule.
2. Decisions taken and the reasoning behind them.
3. Every file created, modified or deleted, by exact path, and what changed.
4. Facts discovered about the project that were expensive to learn: layout,
   commands that work, versions, conventions, gotchas.
5. Anything still unfinished, failing, or explicitly deferred.

Omit: full file contents, long tool output, exploratory steps that led
nowhere, and conversational filler. Write it for a colleague taking over
with no access to the transcript."""


@dataclass(frozen=True)
class Compaction:
    messages: list[Message]
    removed: int
    summary: str


def _block_text(block) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        kind = block.get("type")
        if kind == "text":
            return block.get("text", "")
        if kind == "tool_use":
            return f"[calls {block.get('name')} {json.dumps(block.get('input', {}))[:200]}]"
        if kind == "tool_result":
            body = block.get("content")
            body = body if isinstance(body, str) else str(body)
            return f"[tool result] {body[:MAX_RESULT_CHARS]}"
        return f"[{kind}]"
    # Anthropic SDK content blocks come back as objects, not dicts.
    kind = getattr(block, "type", None)
    if kind == "text":
        return getattr(block, "text", "")
    if kind == "tool_use":
        return f"[calls {getattr(block, 'name', '?')}]"
    return f"[{kind}]"


def _render_message(message: Message) -> str:
    role = message.get("role", "?")
    content = message.get("content")

    if isinstance(content, list):
        body = "\n".join(t for t in (_block_text(b) for b in content) if t)
    elif isinstance(content, str):
        body = content
    else:
        body = ""

    # OpenAI puts tool calls beside the content rather than inside it.
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        body += f"\n[calls {function.get('name')} {function.get('arguments', '')[:200]}]"

    if role == "tool":
        body = f"[tool result] {body[:MAX_RESULT_CHARS]}"
    return f"{role}: {body}".strip()


def transcript(messages: list[Message]) -> str:
    text = "\n\n".join(_render_message(m) for m in messages)
    if len(text) > MAX_RENDER_CHARS:
        half = MAX_RENDER_CHARS // 2
        omitted = len(text) - 2 * half
        text = f"{text[:half]}\n\n... [{omitted} chars omitted] ...\n\n{text[-half:]}"
    return text


def find_cut(messages: list[Message], keep_turns: int = KEEP_TURNS) -> int | None:
    """Index of the first message to keep — always a real user turn.

    Cutting anywhere else can orphan a tool_use from its tool_result, which
    the API rejects outright.
    """
    boundaries = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "user"
        and isinstance(m.get("content"), str)
        and not m["content"].startswith(SUMMARY_PREFIX)
    ]
    if len(boundaries) <= keep_turns:
        return None
    cut = boundaries[-keep_turns]
    return cut or None


def compact(
    provider: Provider, messages: list[Message], keep_turns: int = KEEP_TURNS
) -> Compaction | None:
    cut = find_cut(messages, keep_turns)
    if cut is None:
        return None

    summary = provider.summarize(transcript(messages[:cut]), SUMMARY_INSTRUCTION)
    if not summary:
        return None

    replacement: list[Message] = [
        {"role": "user", "content": f"{SUMMARY_PREFIX}\n\n{summary}"},
        {"role": "assistant", "content": "Understood. Continuing from that context."},
        *messages[cut:],
    ]
    return Compaction(replacement, cut, summary)
