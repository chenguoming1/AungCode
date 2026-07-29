# AungCode

A coding agent CLI, built incrementally as a learning exercise. Python 3.11+,
official provider SDKs, stdlib everywhere else. No frameworks.

**Status: Stage 15** — a streaming REPL with a system prompt, conversation
history, per-turn token accounting, a tool-use loop, discovery tools (`glob`,
`grep`), file tools (`list_files`, `read_file`, `write_file`, `edit_file`), a
`bash` tool, `get_current_time`, and an approval prompt before anything that
mutates the machine. Successful edits print a unified diff to the terminal.

## Slash commands

Markdown files in `commands/` at the workspace root (override with
`AGENT_COMMANDS_DIR`) become `/name`. A command is a prompt template — nothing
below `cli.py` knows they exist.

```markdown
---
description: Review a file for problems
---
Read $ARGUMENTS and list any bugs you find. Be brief.
```

`/review buggy.py` sends the body with `$ARGUMENTS` replaced. Without that
token, arguments are appended. Built-in commands take precedence, and an
unknown `/name` lists what is available instead of being sent to the model.

## Hooks

Shell commands fired around tool execution, configured in `.hooks.json` at the
workspace root (override with `AGENT_HOOKS_CONFIG`):

```json
{
  "pre":  [{"match": "bash", "command": "echo 'not permitted here' >&2; exit 1"}],
  "post": [{"match": ".", "command": "echo \"$AGENT_TOOL\" >> /tmp/audit.log"}]
}
```

`match` is a regex on the tool name, defaulting to everything. Hooks receive
`AGENT_TOOL` and `AGENT_TOOL_ARGS`; post hooks also get `AGENT_TOOL_RESULT` and
`AGENT_TOOL_ERROR`.

**A pre hook exiting non-zero blocks the tool**, and its output becomes the
error the model sees — so it can explain or try another way. A post hook runs
after the fact, so a failure there is reported, not fatal.

Hooks fire *after* approval, so a hook's side effects never run for a call you
were about to refuse. Subagents run with no hooks: their tools are read-only,
and a nested loop should not trigger a hook storm.

## Cost

Per-profile prices in [`agent/config.toml`](agent/config.toml), in USD per
million tokens:

```toml
price_in = 5.00
price_out = 25.00
price_cache_read = 0.50
price_cache_write = 6.25
```

The usage line then ends with the turn's cost and the session total:

```
in 4943 · out 149 · cached 2944 · … · session 5.1k · $0.0015 · total $0.0015
```

Cost is **derived from config, not reported by the API** — a profile with no
prices shows none rather than a wrong number. The total is stored in the
session file, so a resumed session keeps counting.

## MCP servers

[`agent/mcp.py`](agent/mcp.py) is a minimal MCP client over stdio: newline
delimited JSON-RPC 2.0, `initialize` → `notifications/initialized` →
`tools/list`, then `tools/call` on demand.

Servers are declared in `.mcp.json` at the workspace root (override with
`AGENT_MCP_CONFIG`):

```json
{
  "mcpServers": {
    "mock": { "command": "python3", "args": ["/tmp/mcp14/mock_server.py"] }
  }
}
```

Discovered tools are namespaced `mcp__<server>__<tool>`, so two servers can
both expose `search` without colliding, and they are merged into the same
registry as the built-ins — the agent loop cannot tell them apart.

Three deliberate choices:

- **Every MCP tool requires approval.** A server can do anything and there is
  no way to know what, so they are all treated as mutating. A consequence is
  that subagents never receive them: the read-only set is derived from that
  same flag.
- **A failing server is skipped, not fatal.** A missing command, a bad config,
  a server that exits or never answers — each produces a warning and the agent
  runs without it.
- **Requests have deadlines.** A reader thread drains stdout into a queue so a
  wedged server times out instead of hanging the REPL, and a server that dies
  is detected within ~0.25s rather than at the timeout.

## Subagents

The `task` tool ([`agent/subagent.py`](agent/subagent.py)) runs a nested agent
loop with a **fresh message list**, a read-only toolset, and its own system
prompt. Only its final message returns to the parent.

Read-only is derived from the Stage 7 approval flag rather than a name list,
so the subagent gets exactly the tools that do not mutate the machine —
`list_files`, `glob`, `grep`, `read_file`, `get_current_time`. `task` is not in
that set, so subagents cannot recurse.

The point is context economy. A real run:

```
● task(task='find every TIMEOUT constant…') → Here is the complete report. (+38 lines)
  subagent finished: 3 api calls, 6189 tokens, 12 msgs discarded
in 5506 · out 438 · 2 api calls · 4 msgs · ctx 5.9k/1000.0k
```

Seven files were read and twelve messages produced — none of which entered the
parent's history, which stayed at 4 messages. Those file contents would
otherwise have been re-sent on every later turn for the rest of the session.

Nested spend is folded into `session`, so a subagent cannot bill you invisibly.

## Sessions

Every turn is written to `~/.aungcode/sessions/<id>.jsonl` (override with
`AGENT_SESSION_DIR`). Line 1 is metadata; every later line is one message.

```bash
.venv/bin/python -m agent --continue      # most recent session for this workspace
.venv/bin/python -m agent --resume ID     # a specific session
.venv/bin/python -m agent --resume        # choose from a list
```

`/sessions` switches session mid-run. Cumulative token spend carries across
resumes, so `session N` keeps counting where it left off.

Messages are stored through the SDK's own `model_dump`, so an Anthropic
assistant turn — which lives in memory as content-block *objects* — reloads as
dicts that produce the same request bytes. History is rewritten whole on each
turn (rollback and compaction mutate it in place) via a temp file and
`os.replace`, so a crash mid-write cannot corrupt a session.

Two things deliberately do **not** persist: approval decisions (`a` for always
is re-earned every run — resuming must not silently re-grant yesterday's
trust), and the system prompt, which is rebuilt from the current workspace.
Resuming warns if the workspace or model differs from the one recorded.

### One process per session

A session is locked while open. A second tab resuming the same id is refused:

```
session 20260727-144126-9954 is already open in another process
(pid 53098 since 2026-07-27 14:42:32). Close it first, or resume a
different session.
```

Without this, both tabs load the same history and the last one to save wins —
silently discarding everything the other did, and rolling the token counter
backwards.

The lock is an advisory `flock` on a `<id>.lock` sidecar. Two consequences
worth knowing: the kernel drops it when the process dies, so a crashed or
killed tab never leaves a stale lock; and it is a sidecar rather than the
`.jsonl` itself because `save()` replaces that file's inode, which would
strand a lock taken on it. On non-POSIX platforms `fcntl` is unavailable and
locking degrades to a no-op.

## Terminal rendering

All terminal output goes through [`agent/render.py`](agent/render.py) — a
spinner while the model is working, collapsed colour-coded tool lines,
diff colouring, and line-buffered markdown for prose (headings, bold, inline
code, bullets, fenced blocks with light syntax colouring).

Nothing else in the package writes to a stream: `loop.py`, `providers.py`,
`tools.py` and `compact.py` are render-free, so the display can change without
touching the agent.

**It degrades on its own.** When stdout is not a terminal, markdown and colour
are dropped and the raw text is passed through; when stderr is not a terminal
the spinner never starts. Piping stays byte-clean:

```bash
echo "say hi" | .venv/bin/python -m agent > out.txt   # reply only, no ANSI
```

## System prompt

Built once at startup from three parts, and printable at any time with
`/system`:

1. **Role and tool policy** — `ROLE` in [`agent/prompt.py`](agent/prompt.py):
   search before reading, read before editing, `edit_file` over `write_file`,
   `bash` for running things rather than reading them.
2. **Environment** — working directory, OS and architecture, and a depth-2
   file tree so the agent starts oriented instead of spending its first calls
   on `list_files`. Hidden entries are left out, so a `.env` is never named.
3. **`AGENT.md`** — if the file exists in the workspace root it is appended,
   marked as taking precedence over the general guidance.

The prompt sits outside the message history: `/clear` does not touch it, and
on Anthropic it is marked cacheable, since the same bytes are re-sent every
call.

## Compaction

The usage line reports the measured prompt size against the model's window:

```
[in 2468 · out 54 · cached 2048 · 12 msgs · ctx 4.6k/65.5k (6%) · session 27.2k]
```

At 75% of `context_window` (set per profile in
[`agent/config.toml`](agent/config.toml)) the older part of the conversation is
summarized by a separate API call and replaced by a short synthetic exchange.
The most recent 4 user turns are kept verbatim. `/compact` forces it early.

The cut always lands on a plain user turn, never inside one, because a
`tool_use` separated from its `tool_result` is rejected by the API.

**What compaction destroys.** Exact file contents, full tool output, precise
wording of old turns, and the order things happened in. The summary keeps
goals, decisions, file paths, discovered facts and open threads — everything
else is gone and cannot be recovered, since the original messages are
discarded. Re-read a file rather than trusting a remembered version of it.

`grep` uses [ripgrep](https://github.com/BurntSushi/ripgrep) when `rg` is on
PATH and falls back to Python's `re` otherwise. Both backends are told to
ignore the same directories and to search hidden files, so results do not
change depending on what is installed. The trailing summary line names the
backend that ran.

## Approval

`write_file`, `edit_file`, and `bash` show the exact action and wait:

```
  $ rm -rf build
approve bash? [y/N/a=always]
```

`y` runs it once, `n` declines (the model is told, and asked not to retry),
`a` stops asking for that tool for the rest of the session. Anything else
re-prompts; a bare Enter declines. "Always" decisions are per-tool, held in
memory, and gone when the process exits. Read-only tools never prompt.

Approved-by-always actions are still printed before they run, so you can see
what happened even once you have stopped being asked.

Set `AGENT_APPROVE_ALL=1` to skip every prompt — needed for piped or scripted
runs, and dangerous everywhere else.

Tools live in [`agent/tools.py`](agent/tools.py). Adding one is an entry in
`build_registry`: a name, a description written for the model, a JSON Schema,
and a callable. Nothing else in the codebase needs to change.

Manual verification steps for every stage are in [NOTES.md](NOTES.md).
How context, the four caps, compaction and cost fit together — with real
numbers — is in [CONTEXT.md](CONTEXT.md).
Known gaps in the render layer, and the options for closing them, are in
[RENDERING.md](RENDERING.md).
Running against a local OpenAI-compatible server — config, testing and what
actually happened — is in [LOCAL-MODELS.md](LOCAL-MODELS.md).

## Workspace

File tools are confined to one directory — the current directory by default,
or `AGENT_WORKSPACE`:

```bash
AGENT_WORKSPACE=~/code/myproject .venv/bin/python -m agent
```

The root is printed at startup. Every model-supplied path goes through
`Workspace.resolve()`, which resolves symlinks and `..` before checking
containment, so neither can escape.

> **The agent can read every file under that root**, including `.env` and any
> credentials, and send the contents to your provider. Point it at a project
> directory, not your home directory.

> **`bash` is not confined by the workspace.** It starts in the root, but
> `cd /` and `cat ~/.ssh/id_rsa` work, as does anything else your user account
> can do — deleting files, network access, installing packages. There is no
> sandbox and no confirmation prompt. Run this against code you can afford to
> lose, ideally in a container or VM.

Each `bash` call is a fresh shell in the workspace root. Nothing persists
between calls — no `cd`, no variables, no background jobs. Chain within one
call instead: `cd src && pytest -q`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install anthropic openai

cp .env.example .env      # then add the key for the provider you want
```

## Run

```bash
.venv/bin/python -m agent
```

```
github:openai/gpt-4.1 — /exit or Ctrl-D to quit, Ctrl-C cancels a turn
> 
```

Type a message, press Enter, and the reply streams back token by token.

After each reply a usage line goes to stderr:

```
[in 28 · out 3 · 4 msgs in context]
```

`in` counts the whole re-sent history, so it grows every turn — that is the
cost of context, made visible. `cached` and `cache-write` appear when non-zero.

| Input | Effect |
|---|---|
| `/clear` | Forget the conversation and start fresh |
| `/exit`, `/quit` | Quit (exit code 0) |
| `Ctrl-D` | Quit — only on an empty prompt |
| `Ctrl-C` | Cancel the current answer, return to the prompt |

Prompts, the banner, and errors go to stderr, so piping gives clean output:

```bash
echo "explain a ring buffer in 3 sentences" | .venv/bin/python -m agent > out.txt
```

## Providers

Defined in [`agent/config.toml`](agent/config.toml). The active one is the
top-level `provider` key.

| Profile | Default model | Key | Notes |
|---|---|---|---|
| `anthropic` | `claude-opus-4-8` | `ANTHROPIC_API_KEY` | Anthropic SDK |
| `openai` | `gpt-5` | `OPENAI_API_KEY` | |
| `deepseek` | `deepseek-chat` | `DEEPSEEK_API_KEY` | OpenAI-compatible |
| `github` | `openai/gpt-4.1` | `GITHUB_TOKEN` | GitHub Models; PAT needs `models:read` |

Anything speaking the OpenAI wire format is a config entry, not new code — set
`kind = "openai"` and a `base_url`. GitHub Models model ids are
`publisher/model`; the catalog is at <https://models.github.ai/catalog/models>.

Two per-profile knobs worth knowing:

- `token_param` — newer OpenAI models reject `max_tokens` and want
  `max_completion_tokens`. Set it for `openai/gpt-5*` and `openai/o*`.
- `base_url` — omit it to use the SDK's default endpoint.

## Configuration precedence

Keys are named by `api_key_env`, never stored in `config.toml`, so the config
file is safe to commit. `.env` is gitignored.

For any setting, **shell environment beats `.env`, which beats `config.toml`**:

```bash
AGENT_PROVIDER=anthropic .venv/bin/python -m agent   # override for one run
```

Leave `AGENT_PROVIDER` commented out in `.env` and `config.toml` stays the
single source of truth. Also honoured: `AGENT_CONFIG` (path to an alternate
config file) and `AGENT_ENV_FILE` (path to an alternate `.env`).

`.env` syntax is `KEY=value` per line, with an optional `export ` prefix and
optional surrounding quotes. `#` starts a comment only at the start of a line,
and there is no `${VAR}` interpolation.

## Layout

| File | Role |
|---|---|
| [`agent/__main__.py`](agent/__main__.py) | `python -m agent` entry point |
| [`agent/cli.py`](agent/cli.py) | The REPL loop and signal handling |
| [`agent/config.py`](agent/config.py) | Resolves one validated `ProviderConfig` |
| [`agent/envfile.py`](agent/envfile.py) | Minimal `.env` parser (stdlib only) |
| [`agent/providers.py`](agent/providers.py) | `stream(prompt) -> Iterator[str]` per SDK |
| [`agent/config.toml`](agent/config.toml) | Provider profiles |

Providers expose one method — `stream(prompt)`, yielding text deltas. That
narrow contract keeps the CLI provider-agnostic and is where history and tool
use will hook in.

## Errors

Configuration problems are caught before the prompt opens and exit with code 2:

```
config error: $GITHUB_TOKEN is not set (required by providers.github)
config error: unknown provider 'gihub' (defined: anthropic, deepseek, github, openai)
config error: .env:3: expected KEY=value, got 'oops'
```

API failures are per-turn — reported to stderr, then back to the prompt, so one
bad request doesn't end the session:

```
[AuthenticationError] Error code: 401 - invalid x-api-key
```
