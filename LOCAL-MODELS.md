# Running against a local model

Any OpenAI-compatible server works with no code changes — `kind = "openai"`
plus a `base_url` is the whole integration. Nothing is billed, so no cost is
reported.

Findings below are from an actual run, not theory.

## Config

```toml
[providers.local]
kind = "openai"
model = "mlx-community/Qwen3-Coder-30B-A3B-Instruct-3bit"
api_key = "not-needed"
base_url = "http://127.0.0.1:8080/v1"
max_tokens = 8192
context_window = 32768
```

```bash
AGENT_PROVIDER=local .venv/bin/python -m agent
```

Four things about that block:

- **`api_key` is inline, not `api_key_env`.** A local server has no secret to
  protect, so forcing one through the environment would mean putting a fake
  key in `.env`. Network providers still require `api_key_env`. Most local
  servers ignore the value entirely, but the SDK needs something non-empty.
- **`model` must match the server exactly.** Get it from the server, never
  from memory — see below.
- **`context_window` should match how the server was started.** Too low only
  compacts early; too high fails hard.
- **No prices.** Nothing is billed, so the usage line correctly ends at
  `session N` with no `$`.

## Finding the model id

```bash
curl -s http://127.0.0.1:8080/v1/models | python3 -m json.tool
```

Getting this wrong produces a confusing error. With MLX, passing a model the
server does not have makes it try to *download* that name from Hugging Face:

```
401 Client Error ... Repository Not Found for url:
https://huggingface.co/api/models/local/revision/main
```

That is not an auth problem with your setup. It means the `model` string does
not match what is loaded.

## Common servers

| Server | Typical `base_url` |
|---|---|
| MLX (`mlx_lm.server`) | `http://127.0.0.1:8080/v1` |
| llama.cpp (`llama-server`) | `http://127.0.0.1:8080/v1` |
| Ollama | `http://127.0.0.1:11434/v1` |
| LM Studio | `http://127.0.0.1:1234/v1` |
| vLLM | `http://127.0.0.1:8000/v1` |

## Test

Run **interactively**. Turns take 30–90s on a 30B model, and piping requires
`AGENT_APPROVE_ALL=1`, which removes the approval prompts you would otherwise
use to interrupt a misbehaving model.

```bash
mkdir -p /tmp/local16 && printf 'def add(a, b):\n    return a - b\n' > /tmp/local16/calc.py
cd /Users/aungbonaing/develop/personal/Apps/AungCode
AGENT_PROVIDER=local AGENT_WORKSPACE=/tmp/local16 .venv/bin/python -m agent
```

| # | Do | Expect |
|---|---|---|
| 1 | Read the banner | `local:<model id> · 9 tools` |
| 2 | `what is 2+2? answer with just the number` | `4`, no `●` tool lines. Proves endpoint, config and streaming |
| 3 | `read calc.py and tell me the bug in one sentence` | One `● read_file(path='calc.py')`, then the `a - b` bug identified. Proves tool calling |
| 4 | Watch for repetition | If it repeats a call, it must stop by itself at 3 — see the guards below |
| 5 | Check the usage line | Ends at `session N` with no `$` — nothing is billed |
| 6 | Ctrl-C mid-turn | Cancels and rolls the turn back, as with any provider |

If turn 1 hangs past ~2 minutes the model is probably still loading. Check the
server's own logs rather than assuming the agent is stuck.

## Loop guards matter more here

Weak or heavily quantized models answer correctly and then fail to *stop*.
Observed with Qwen3-Coder-30B at 3-bit: it read the file, identified the bug
correctly, then called `read_file` on the same path **50 more times** saying
"I need to see the current state of the file", never terminating.

Two guards in [`agent/loop.py`](agent/loop.py) catch this:

| Guard | Trips at | `stop_reason` |
|---|---|---|
| Same call, same result, repeatedly | 3 | `repeated_calls` |
| Model re-asks for an action you denied | 2 | `denied_repeat` |
| Nothing else caught it | `MAX_ITERATIONS` (100) | turn discarded |

The repeat guard keys on **call *and* result**, so re-reading a file you just
edited (same arguments, new contents) is never mistaken for a loop.

These exist because of local models but protect every provider — on a paid
API each of those loops would have been 100 billed calls.

Verify the guards without a model or a key:

```bash
.venv/bin/python - <<'PY'
from agent import loop
from agent.providers import Step, ToolCall, Usage
from agent.tools import Tool
tools = [Tool("read_file", "d", {"type": "object"}, lambda a: "same output")]
class Stuck:
    n = 0
    def step(s, m, t, e, sysmsg):
        Stuck.n += 1
        m.append({"role": "assistant", "content": "again"})
        return Step("tool_use", [ToolCall(f"c{Stuck.n}", "read_file", {"path": "calc.py"})], Usage(1, 1))
    def append_results(s, m, r): m.append({"role": "tool", "content": r[0].content})
    def summarize(s, t, i): return ""
res = loop.run_turn(Stuck(), [{"role": "user", "content": "x"}], tools,
                    lambda t: None, lambda c, r: None, lambda c: True, "sys")
print("calls:", Stuck.n, "stop_reason:", res.stop_reason)   # expect 3, repeated_calls
PY
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| `401 ... Repository Not Found for url: huggingface.co/...` | `model` does not match what the server has loaded |
| Connection refused | Server not running, or `base_url` port is wrong |
| `providers.local needs api_key_env or api_key` | Neither key was set; local servers want inline `api_key` |
| Turn 1 hangs for minutes | Model still loading — check the server's logs |
| Answers correctly, then loops | Model too weak or too quantized for agent work — the guards stop it, but see below |
| No `$` on the usage line | Correct — a local model has no prices |

## Observed failure modes (Qwen3-Coder-30B, 3-bit)

Both of these are the model, not the agent. The first was confirmed by reading
the persisted session file, which showed a perfectly well-formed request.

**Answers the previous question.** Asked `what is 2+2?` then
`read calc.py and tell me the bug`, it replied `2+2=4` to the second question
and never called a tool. What was actually sent:

```
[0] user      'what is 2+2? answer with just the number'
[1] assistant '4'
[2] user      'read calc.py and tell me the bug in one sentence'
[3] assistant '2+2=4'          <- restated the old answer
```

**Cannot end a turn.** With no prior turn it identifies the bug correctly, then
keeps calling `glob` and `read_file` until the repeat guard stops it.

**It is non-deterministic.** The identical prompt pair worked once and failed
once. Do not conclude anything from a single run.

Workarounds, most effective first:

1. `/clear` between unrelated questions — the previous turn is what confuses it.
2. Name the tool: `use the read_file tool to read calc.py`, not `read calc.py`.
3. One question per session for anything that matters.
4. A higher-precision quant (4-bit or 6-bit) if the RAM allows. This is the
   actual fix; the rest are mitigations.

Diagnosing "is this the agent or the model" is quick — the session file records
exactly what was sent:

```bash
.venv/bin/python - <<'PY'
from agent import session
# Skip sessions with no messages — /clear empties history but keeps metadata,
# so the newest file is not always the one you want to inspect.
found = [x for x in session.listing()
         if x.meta.get("provider") == "local" and x.meta.get("messages")]
if not found:
    print("no local session with messages"); raise SystemExit
sess, msgs = session.read(found[0].path)
print(sess.id, sess.meta["model"])
for i, m in enumerate(msgs):
    tc = [t["function"]["name"] for t in m.get("tool_calls") or []]
    print(f"[{i}] {m['role']:9} {str(m.get('content'))[:70]!r}" + (f"  +{tc}" if tc else ""))
PY
```

If the roles alternate and the last user message is the question you asked,
the agent did its job.

## Is it good enough?

Single-step questions: yes. The 3-bit 30B answered both test questions
correctly. Multi-step agent work: not yet — it could not reliably end a turn.

If you want local for real agent work, try a higher-precision quant (4-bit or
6-bit) if the RAM allows. Otherwise keep `local` for cheap one-shot questions
and use a hosted profile for anything requiring a tool loop. On DeepSeek's
pricing that choice costs fractions of a cent per session.
