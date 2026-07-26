# Context, caps, compaction and cost

Why this agent spends what it spends, which knob controls what, and how each
one fails when set wrong. Numbers here are real traces from running it, not
illustrations.

## The one fact everything follows from

**The API is stateless.** Nothing is stored between calls. Every request
re-sends the system prompt, every tool schema, and the entire conversation so
far. "Memory" is you re-uploading the transcript each time.

This compounds twice over:

- **Within a turn.** One thing you type can trigger many API calls — read a
  file, edit it, run tests. Call 5 re-sends everything calls 1–4 produced,
  including the full arguments of every tool call.
- **Across turns.** Turn 10 re-sends turns 1–9.

A real turn from this agent, reading one 441-line file:

```
[in 61329 · out 9193 · cached 39552 · 3 api calls · 21 msgs · ctx 54.6k/65.5k (83%)]
```

61k input tokens for three calls. Call 1 sent ~2k, call 2 sent that plus the
file, call 3 sent all of it again.

### Tool arguments live in history forever

When the model calls `write_file`, the file body is inside the assistant
message:

```json
{"role": "assistant", "tool_calls": [{"function": {
    "name": "write_file",
    "arguments": "{\"path\":\"x.html\",\"content\":\"<!DOCTYPE html>...1500 lines...\"}"
}}]}
```

That stays in context for the rest of the session. One 1500-line write adds
~20k tokens permanently; thirty small `edit_file` calls covering the same
change add ~9k. This is the strongest argument for incremental edits — beyond
resilience, they leave a smaller permanent footprint.

## The four caps

| Cap | Where | Controls | Wrong-way failure |
|---|---|---|---|
| `max_tokens` | [`agent/config.toml`](agent/config.toml) | Longest single response | Too low: replies truncate mid-tool-call. Too high: a runaway response is slow and expensive |
| `context_window` | [`agent/config.toml`](agent/config.toml) | When compaction triggers | Too low: compacts needlessly. **Too high: overruns the real limit and fails hard** |
| `MAX_ITERATIONS` | [`agent/loop.py`](agent/loop.py) | API calls per turn | Too low: multi-step work is impossible and the whole turn is discarded. Too high: a loop can bill you a long time |
| `COMPACT_AT` | [`agent/cli.py`](agent/cli.py) | Share of the window that triggers compaction | Too high: risk hitting the hard limit. Too low: throws away context you still need |

Set `max_tokens` and `context_window` from the model's published limits, not by
guessing. Both were wrong here at first: `max_tokens = 8192` against a model
allowing 384K, and `context_window = 65536` against a 1M window. The first
caused repeated truncation; the second caused compaction at 8% utilisation.

When unsure about `context_window`, **go low**. Compacting early wastes some
detail; overrunning the real window is an error you cannot retry past.

## Prompt caching

Providers cache a request's prefix, so an unchanged prefix is re-billed at a
fraction of normal input price (roughly a tenth on Anthropic; other providers
differ). This is what makes the constant re-sending affordable.

The cached prefix here is the system prompt plus the tool schemas — identical
on every call, which is why `build_system` runs once at startup and the
Anthropic path marks it `cache_control`.

Caching is a **prefix match**: one changed byte early invalidates everything
after it. So anything that mutates the front of the request throws the whole
cache away. Watch `cached` in the usage line — if it stops growing, something
is invalidating the prefix.

## Compaction

At `COMPACT_AT` (75%) of the window, or on `/compact`, the older part of the
conversation is replaced by a summary. The most recent 4 user turns stay
verbatim. See [`agent/compact.py`](agent/compact.py).

Observed: 106 messages compacted to 22, with planted facts still recalled
correctly afterwards.

**It is not free.** Compaction:

- Costs an API call, sending the whole prefix to be summarized.
- **Destroys the cache.** Every message after the summary is new bytes, so the
  next call pays full input price on the entire history. This is visible as
  `cached` collapsing right after a compaction.
- Permanently discards detail. The original messages are deleted, not
  archived.

What survives is only what `SUMMARY_INSTRUCTION` asks for: goals, decisions,
file paths, discovered facts, unresolved threads. Gone for good: exact file
contents, full tool output, precise wording of earlier turns, and the order
things happened in. **Re-read a file rather than trusting a remembered
version of it.**

Because of the cache cost, compacting when context is not tight is a net loss.
Compacting at `ctx 8.9k/1000.0k (0%)` spends a call and destroys detail to
solve a problem you do not have.

## Reading the usage line

```
[in 2468 · out 54 · cached 2048 · 12 msgs · ctx 4.6k/65.5k (6%) · session 27.2k]
```

| Field | Meaning |
|---|---|
| `in` / `out` | Tokens this turn, summed across every API call it made |
| `cached` | Input tokens served from cache — cheap. Low after a compaction |
| `N api calls` | Iterations in this turn. Shown only when more than one |
| `msgs` | Length of the history list. A tool turn adds 4, a plain turn 2 |
| `ctx` | Measured prompt size vs the window. Drives compaction |
| `session` | Cumulative tokens since launch. This is your bill |

`ctx` is measured, not estimated: it is the last call's
`input + cache_read + cache_write + output`, i.e. what the server actually saw.

A single session in testing reached `session 3270.1k` — 3.27 million tokens —
mostly from re-sending a 106-message history on every call before compaction.

## Symptom → cause

| Symptom | Cause | Fix |
|---|---|---|
| Reply cut off, `[response hit max_tokens]` | `max_tokens` too low for the output | Raise it, or ask for smaller steps |
| `MaxIterations` and the turn is thrown away | Work needs more calls than `MAX_ITERATIONS` | Raise the cap; prefer many small edits |
| `in` huge, `cached` near zero | Cache prefix invalidated, often right after compaction | Expected post-compaction; otherwise check for a changing prefix |
| Compaction firing constantly | `context_window` set below the model's real window | Correct it from the provider's docs |
| `400 ... tool_calls must be followed by tool messages` | History has an unanswered tool call | Fixed in providers; if seen again, `/clear` |

## Rules of thumb

1. **Prefer `edit_file` to `write_file`.** Smaller permanent footprint,
   survives failures, reviewable one diff at a time.
2. **Many small tool calls beat one large call.** A failure costs one step
   instead of everything.
3. **Only compact when context is actually tight.** It trades cache and detail
   for room you may not need.
4. **Set the caps from published limits.** Every cap problem in this project
   came from a guessed number.
5. **Watch `session`.** It is the only field that only ever goes up.
