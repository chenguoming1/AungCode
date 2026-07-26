# AungCode

A coding agent CLI, built incrementally as a learning exercise. Python 3.11+,
official provider SDKs, stdlib everywhere else. No frameworks.

**Status: Stage 6** — a streaming REPL with conversation history, per-turn
token accounting, a tool-use loop, file tools (`list_files`, `read_file`,
`write_file`, `edit_file`), a `bash` tool, and `get_current_time`.
Successful edits print a unified diff to the terminal.

Tools live in [`agent/tools.py`](agent/tools.py). Adding one is an entry in
`build_registry`: a name, a description written for the model, a JSON Schema,
and a callable. Nothing else in the codebase needs to change.

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
