# Manual test notes

Verification steps for each stage, in the order they were built. Every check
is something you run and eyeball — there is no test runner yet.

## Before you start

```bash
cd /Users/aungbonaing/develop/personal/Apps/AungCode
# .env must hold a key for the provider named in agent/config.toml
.venv/bin/python -m agent
```

Three things that will otherwise confuse you:

- **Since Stage 7, `write_file` / `edit_file` / `bash` prompt for approval.**
  Older stage checks assume they just run. Answer `y`, or prefix the command
  with `AGENT_APPROVE_ALL=1` when replaying a scripted test.
- **Free-tier providers rate-limit.** `[RateLimitError] Too many requests`
  means wait, not broken. The session survives it; the turn rolls back.
- **Always test file/bash behaviour in a sandbox**, never in this repo.
  `AGENT_WORKSPACE=/tmp/...` is how you point it somewhere disposable.

## Fixtures

```bash
# Stage 4-6 sandbox
mkdir -p /tmp/sandbox && cd /tmp/sandbox
printf 'a\nb\nc\n' > notes.txt
seq 1 1000 > big.txt

# Stage 5: duplicate lines, to force an ambiguous match
mkdir -p /tmp/edit5 && printf 'def f():\n    x = 1\n    return x\n\ndef g():\n    x = 1\n    return x\n' > /tmp/edit5/dup.py

# Stage 6: a project with one planted bug (add returns a - b)
mkdir -p /tmp/proj6 && cd /tmp/proj6
cat > calc.py <<'EOF'
def add(a, b):
    return a - b

def mul(a, b):
    return a * b
EOF
cat > test_calc.py <<'EOF'
import unittest
from calc import add, mul

class T(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)
    def test_mul(self):
        self.assertEqual(mul(2, 3), 6)

if __name__ == "__main__":
    unittest.main()
EOF

# Stage 7 sandbox
mkdir -p /tmp/appr7 && printf 'keep me\n' > /tmp/appr7/data.txt
```

---

## Stage 1 — streaming REPL

| # | Do | Expect |
|---|---|---|
| 1 | `write a haiku about tail latency` | Text arrives incrementally, not all at once |
| 2 | `my name is Chen`, then `what is my name?` | It does **not** know — turns are independent by design |
| 3 | Ctrl-C mid-stream | `[cancelled]`, prompt returns, process alive |
| 4 | Ctrl-D on an empty prompt | Exits 0 |
| 5 | `AGENT_PROVIDER=github .venv/bin/python -m agent` | Banner shows the overridden provider |
| 6 | `echo "say hi" \| .venv/bin/python -m agent > out.txt` | `out.txt` holds only the reply; banner went to stderr |

## Stage 2 — history + token usage

| # | Do | Expect |
|---|---|---|
| 1 | `my name is Chen`, then `what is my name?` | Now it answers correctly |
| 2 | Watch the usage line each turn | `in N` climbs, `out N` stays small — the whole history is re-sent |
| 3 | `/clear`, then `what is my name?` | No memory; `in` drops; `2 msgs in context` |
| 4 | `count slowly to 200`, Ctrl-C, then ask what you just requested | `[cancelled — turn discarded]`; it has no idea; msg count unchanged |
| 5 | Run with a bad key and send a message | `[AuthenticationError]`, prompt returns, history rolled back |

## Stage 3 — tool loop

Config check: banner reads `... · N tools`.

| # | Do | Expect |
|---|---|---|
| 1 | `what time is it in Tokyo?` | A `[tool]` line, then prose, and `2 api calls` in the usage line |
| 2 | `what is 2+2?` | No `[tool]` line, no api-call count (a single call isn't shown) |
| 3 | Follow up with `and in UTC?` | Tool fires again; `msgs in context` jumps by 4, not 2 |
| 4 | `what time is it in Mars/Olympus?` | `[tool] ... !!` with an error, and the model recovers |

## Stage 4 — workspace file tools

```bash
AGENT_WORKSPACE=/tmp/sandbox .venv/bin/python -m agent
```

| # | Do | Expect |
|---|---|---|
| 1 | `what files are here?` | `list_files` call |
| 2 | `read notes.txt` | Tool output is line-numbered (`1\| a`). The model's prose reply strips the numbers — that is normal |
| 3 | `create hello.py that prints hello, then read it back` | Two tool calls; `cat /tmp/sandbox/hello.py` confirms |
| 4 | `read ../../etc/passwd` | `!! error: path '../../etc/passwd' escapes the workspace root`. If you get a polite refusal with **no** `[tool]` line, the model declined on its own — ask more directly so the guard is actually exercised |
| 5 | `read big.txt` | Stops at line 300 with `... truncated at line 300 of 1000; call again with offset=301` |
| 6 | `show me the last 10 lines` | It calls `read_file` with a computed `offset` |

## Stage 5 — edit_file

```bash
AGENT_WORKSPACE=/tmp/edit5 .venv/bin/python -m agent
```

| # | Do | Expect |
|---|---|---|
| 1 | `in dup.py rename f to first` | `[tool] edit_file`, then a coloured unified diff |
| 2 | `change x = 1 to x = 2 in g only` | May first get `matched 2 times ... at lines 2, 6`, then retry with more context in the same turn. Multiple `[tool]` lines *is* the recovery |
| 3 | `in dup.py, delete the "return x" line from the first() function only` | Diff shows a single `-    return x`; `python3 -c "import ast; ast.parse(open('/tmp/edit5/dup.py').read())"` still parses |
| 4 | Compare token cost against `rewrite dup.py with both functions renamed` | Edit sends a snippet, write sends the whole file |
| 5 | `edit ../../etc/hosts` | Escapes the workspace root |

Known hazard: if the model widens `old_str` to disambiguate and passes an
empty `new_str`, it deletes the context too. The tool description warns it;
that is mitigation, not enforcement.

## Stage 6 — bash

```bash
AGENT_WORKSPACE=/tmp/proj6 .venv/bin/python -m agent
```

| # | Do | Expect |
|---|---|---|
| 1 | `run: echo hello; echo oops >&2; exit 7` | `exit code: 7`, both streams interleaved |
| 2 | `run pwd`, `run cd /usr && pwd`, `run pwd` | Third is back at the workspace root — no state persists between calls |
| 3 | `run sleep 120 with a 3 second timeout` | Returns in ~3s, `timed out after 3s and was killed`. `pgrep -f "sleep 120"` finds nothing |
| 4 | `run seq 1 5000` | `... [4800 lines omitted] ...` between line 1 and line 5000 |
| 5 | `run the tests with python3 -m unittest, then fix what's broken and re-run` | bash (exit 1) → read → read → edit → bash (exit 0). Verify: `cd /tmp/proj6 && python3 -m unittest` |

## Stage 7 — approval

```bash
AGENT_WORKSPACE=/tmp/appr7 .venv/bin/python -m agent
```

| # | Do | Expect |
|---|---|---|
| 1 | `delete data.txt using bash`, answer `n` | `$ rm data.txt` shown first; file survives; model explains instead of retrying |
| 2 | `create hello.txt saying hi`, answer `y` | Action shows `write hello.txt` with `+ hi`; file appears |
| 3 | `overwrite data.txt with the word replaced`, answer `n` | Header reads `[OVERWRITES existing file, 1 lines]` **before** anything is written |
| 4 | `run echo one` answer `a`, then `run echo two` | Second prints `[approved: always]` and does not prompt |
| 5 | Then `create a file called x.txt` | `write_file` still prompts — "always" is per-tool |
| 6 | `what files are here and what's in data.txt?` | Zero prompts; read-only tools auto-approve |
| 7 | Ctrl-C at an approval prompt | Whole turn cancelled and rolled back |

## Stage 8 — glob + grep

```bash
mkdir -p /tmp/g8/src/deep /tmp/g8/.git /tmp/g8/node_modules && cd /tmp/g8
printf 'import os\ndef handler(x):\n    return x\n' > src/app.py
printf 'def handler(y):\n    pass\n' > src/deep/util.py
printf 'const handler = 1;\n' > src/web.ts
printf 'handler in git\n' > .git/config
printf 'handler in node_modules\n' > node_modules/lib.js
printf 'x = foo([unclosed)\ny = a.b*c\n' > src/tricky.py
sleep 1 && touch src/app.py     # make it the newest file
cd /Users/aungbonaing/develop/personal/Apps/AungCode
AGENT_WORKSPACE=/tmp/g8 .venv/bin/python -m agent
```

| # | Do | Expect |
|---|---|---|
| 1 | `list every python file` | `glob` call; `src/app.py` first (newest); nothing from `.git` or `node_modules` |
| 2 | `where is handler defined?` | `grep` call returning `path:line: text`; summary names the backend |
| 3 | `find handler only in python files` | Uses `include: '*.py'`; `web.ts` absent |
| 4 | `use the grep tool with the regex pattern ([unclosed` | `!! invalid regex ... Pass literal=true to search for this text exactly` on the **first** call. Phrase it as a tool instruction — "search for `([unclosed`" reads as a literal-text request, so the model escapes it and the guard never fires |
| 5 | `find the exact text ([unclosed` | Uses `literal: true`, finds `src/tricky.py:1`. It should **not** shell out to bash — if it does, the description stopped steering |
| 6 | `explore this repo and tell me what it does` | glob/grep **before** read_file, and no approval prompts — both are read-only |
| 7 | `glob for files in ../` | Escapes the workspace root |

Backend check: `which rg`. With ripgrep installed the summary reads
`(ripgrep)`, without it `(python re)`. Results should be identical either way —
if they differ, the parity flags are wrong.

## Stage 9 — system prompt + AGENT.md

```bash
mkdir -p /tmp/p9/src /tmp/p9/tests && cd /tmp/p9
printf 'x\n' > src/main.py && printf 'x\n' > tests/test_main.py && printf 'SECRET=1\n' > .env
cat > AGENT.md <<'EOF'
# House rules

- Every Python module must begin with the line `# (c) ACME` as its first line.
- Never run `pytest` directly; this project uses `make test`.
EOF
cd /Users/aungbonaing/develop/personal/Apps/AungCode
AGENT_WORKSPACE=/tmp/p9 .venv/bin/python -m agent
```

| # | Do | Expect |
|---|---|---|
| 1 | Read the banner | `workspace: /private/tmp/p9 · AGENT.md loaded` |
| 2 | `/system` | Role, then `# Environment` with cwd + platform, then a depth-2 tree, then the AGENT.md rules |
| 3 | Check the tree in that output | No `.env`, no `.git`, no `node_modules`; nothing deeper than two levels |
| 4 | `create src/util.py with a function double(n)` | The written file starts with `# (c) ACME` — AGENT.md was obeyed |
| 5 | `run the test suite` | It tries `make test`, not `pytest` |
| 6 | `/clear`, then `/system` | Prompt is unchanged — it lives outside history |
| 7 | `rm /tmp/p9/AGENT.md`, restart | Banner no longer says `AGENT.md loaded`; `/system` ends at the tree |

## Stage 10 — compaction

Any workspace will do; compaction is about history, not files.

| # | Do | Expect |
|---|---|---|
| 1 | Send any turn, read the usage line | `ctx N/W (P%)` and `session N` are present |
| 2 | Send several more turns | `ctx` climbs, `session` climbs faster (it counts every call, cached or not) |
| 3 | `remember: my deploy target is fly.io and the magic number is 8823`, then 4 throwaway turns | — |
| 4 | `/compact` | `[compacted N messages into a summary; M msgs now, last 4 turns kept verbatim]`, and `msgs` drops sharply |
| 5 | `what is my deploy target and the magic number?` | Still correct — the facts survived in the summary |
| 6 | `/compact` twice more in a row | Both say `[nothing to compact — fewer than 4 earlier turns]` and `msgs` does **not** change. Regression: the synthetic summary used to count as a user turn, so each `/compact` re-summarized its own summary — a paid API call that changed nothing |
| 7 | Ask for the **exact wording** of an early turn | It cannot reproduce it. That is the cost, not a bug |
| 8 | Lower `context_window` in `agent/config.toml` to e.g. `3000`, restart, send 2 turns | Auto-compaction fires: `[context N over the M threshold]` |

Cut-point safety (no API key needed):

```bash
.venv/bin/python -c "
from agent.compact import find_cut
h=[{'role':'user','content':f'q{i}'} if i%2==0 else {'role':'assistant','content':'a'} for i in range(20)]
c=find_cut(h,4); print('cut',c,'role',h[c]['role'],'plain str',isinstance(h[c]['content'],str))"
```

## Stage 11 — rendering

Must be run in a real terminal; most of this is invisible when piped.

| # | Do | Expect |
|---|---|---|
| 1 | Ask anything slow | A braille spinner `⠋ thinking…` on stderr, replaced the instant the first token arrives — no leftover frame on the line |
| 2 | `ask for a **bold** word, \`inline code\`, and a bullet list` | Bold renders bold, inline code cyan, bullets as `•` — the markers themselves are gone |
| 3 | `show me a python function in a code block` | Fence dimmed; keywords magenta, strings green, comments dim |
| 4 | Any tool call | One collapsed line: `● read_file(path='x.py') → …`; green `●` on success, red `✗` on error |
| 5 | Any successful `edit_file` | Diff with green additions, red deletions, cyan `@@` hunk headers |
| 6 | An approval prompt for `bash` | The command in bold yellow; for `write_file` on an existing file, `OVERWRITES` in bold red |
| 7 | After a tool result | Spinner returns as `⠋ working…` while the next API call runs |

Degradation (no terminal needed):

```bash
echo "say hi" | .venv/bin/python -m agent > /tmp/o.txt 2>/tmp/e.txt
grep -c $'\033' /tmp/o.txt   # 0 — no ANSI on stdout
grep -c $'\033' /tmp/e.txt   # 0 — no ANSI on stderr either
head -c 40 /tmp/o.txt        # reply only: no banner, no '›' prompt
```

Layer separation — should print nothing:

```bash
grep -l 'from .render' agent/loop.py agent/providers.py agent/tools.py agent/compact.py
```

---

## Cleanup

```bash
rm -rf /tmp/sandbox /tmp/edit5 /tmp/proj6 /tmp/appr7 /tmp/g8 /tmp/p9
```
