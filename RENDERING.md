# Rendering — known gaps and open decisions

State of [`agent/render.py`](agent/render.py) as of Stage 11. Written down so
the shortcuts are visible rather than mistaken for finished work.

**Status: deferred.** Nothing here is a bug in normal use; it is the difference
between "works" and "correct".

## What is solid — do not re-litigate

- **Layer separation.** Only `cli.py` imports the renderer. `loop.py`,
  `providers.py`, `tools.py` and `compact.py` never touch a stream, so the
  display can be replaced without touching the agent.
- **TTY degradation.** Not a terminal → no ANSI, no spinner, raw markdown
  passed through. Verified: zero escape bytes on either stream when piped.
- **Spinner safety.** Every write takes the spinner's lock and erases the line
  first, so a frame can never interleave with real output.
- **Diff colouring.** Diff-level colour (green/red/cyan hunk headers) is how
  diffs are actually rendered. Syntax-highlighting *inside* a diff is not
  standard practice — the two colour schemes fight. This is correct as-is.

## What is approximate: `_highlight()`

Fenced code blocks are coloured by regex over one language-agnostic keyword
set. Measured failures:

| Input | Result | Cause |
|---|---|---|
| `type = "x"` | `type` coloured as a keyword | Keyword set is not per-language |
| `const x = 1;` inside a ` ```python ` fence | `const` coloured | Same |
| `text = """multi` … `line string"""` | Each line guessed separately | No state carried across lines |
| `/* C block comment */` | Not recognised | Only `#` and `//` are handled |
| `"http://a.com#frag"` | Correct **by luck** | The regex happens to need whitespace after `#` / `//` |

**Dead field:** `_fence_lang` is captured from ` ```python ` and then never
read. Every language is highlighted identically. Either use it or delete it.

Doing this properly needs a lexer per language — something that knows Python
f-strings from Ruby `%w[]` from Go backticks. That is not a regex job.

## Also missing: terminal conventions

All stdlib, all cheap, and these break other people's tooling rather than just
looking wrong:

| Variable | Expected behaviour | Current |
|---|---|---|
| `NO_COLOR` | Set to anything → emit no colour ([no-color.org](https://no-color.org)) | Ignored |
| `FORCE_COLOR` | Emit colour even when piped (pagers, CI) | Ignored |
| `TERM=dumb` | Terminal cannot render ANSI | Ignored — colour still emitted |
| `COLORTERM=truecolor` | 24-bit palette available | Assumes 8 colours |

Arguably these matter more than highlight accuracy: a user with `NO_COLOR` set
has explicitly asked for something we are not doing.

## Options

| Option | Cost | Result |
|---|---|---|
| **Pygments** | 1 dependency, ~15 lines — only `_highlight()` changes | Correct highlighting for ~500 languages, uses the fence tag |
| **Rich** | Larger dependency; replaces most of `render.py` | Professional markdown/tables/live rendering, but discards the hand-written layer |
| **stdlib `tokenize`** | 0 deps, ~25 lines | Exact — Python only. No use for Ruby/PHP/Go |
| **Better regex** | 0 deps | Still approximate; writing lexers badly |
| **Delete `_highlight()`** | 0 deps, negative lines | Dim the whole block. Nothing wrong beats something subtly wrong |

The project rule is "stdlib where possible, no frameworks". Pygments is a
library rather than a framework, and multi-language highlighting is genuinely
not achievable in stdlib — so it is a defensible exception, but a real one.

## Recommendation when this is picked up

1. **Add the env conventions** (`NO_COLOR`, `FORCE_COLOR`, `TERM=dumb`) —
   stdlib, uncontroversial, respects an explicit user request.
2. **Then either** delete `_highlight()` and dim code blocks wholesale, **or**
   take the Pygments dependency and do it properly. Do not keep polishing the
   regex — that path ends in a bad lexer.

Whichever is chosen, remove or use `_fence_lang`; leaving a captured-and-
ignored field is the part that reads as unfinished.
