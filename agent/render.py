from __future__ import annotations

import itertools
import re
import sys
import threading
import time

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
CLEAR = "\r\033[K"

KEYWORDS = {
    "def", "class", "return", "if", "elif", "else", "for", "while", "import",
    "from", "try", "except", "finally", "with", "as", "lambda", "yield", "pass",
    "raise", "async", "await", "func", "var", "const", "let", "function", "type",
    "struct", "interface", "package", "end", "do", "then", "fi", "echo", "module",
    "public", "private", "static", "void", "new", "self", "nil", "None", "true",
    "false", "True", "False", "null",
}


# Kept as module constants: a backslash inside an f-string expression is a
# syntax error before Python 3.12, and this project targets 3.11+.
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"


class Style:
    """ANSI codes, or plain passthrough when the stream is not a terminal."""

    def __init__(self, enabled: bool) -> None:
        self.on = enabled

    def _wrap(self, code: str, s: str) -> str:
        return f"{code}{s}{RESET}" if self.on else s

    def bold(self, s: str) -> str:
        return self._wrap(BOLD, s)

    def dim(self, s: str) -> str:
        return self._wrap(DIM, s)

    def red(self, s: str) -> str:
        return self._wrap(RED, s)

    def green(self, s: str) -> str:
        return self._wrap(GREEN, s)

    def yellow(self, s: str) -> str:
        return self._wrap(YELLOW, s)

    def blue(self, s: str) -> str:
        return self._wrap(BLUE, s)

    def magenta(self, s: str) -> str:
        return self._wrap(MAGENTA, s)

    def cyan(self, s: str) -> str:
        return self._wrap(CYAN, s)


class Spinner:
    """Runs on its own thread; every writer hides it before printing."""

    def __init__(self, stream, enabled: bool) -> None:
        self._stream = stream
        self._enabled = enabled
        self._visible = False
        self._label = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lock = threading.RLock()

    def start(self, label: str) -> None:
        if not self._enabled or self._thread:
            return
        self._label = label
        self._visible = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        for frame in itertools.cycle(FRAMES):
            if self._stop.is_set():
                return
            with self.lock:
                if self._visible:
                    self._stream.write("\r" + DIM + frame + " " + self._label + RESET)
                    self._stream.flush()
            time.sleep(0.08)

    def hide(self) -> None:
        """Erase the spinner line. Callers hold the lock across their write."""
        if self._visible:
            self._visible = False
            self._stream.write(CLEAR)
            self._stream.flush()

    def resume(self, label: str | None = None) -> None:
        if not self._enabled or not self._thread:
            return
        with self.lock:
            if label:
                self._label = label
            self._visible = True

    def stop(self) -> None:
        if not self._thread:
            return
        self._stop.set()
        self._thread.join(timeout=0.5)
        with self.lock:
            self.hide()
        self._thread = None


class Renderer:
    """Owns every byte written to the terminal."""

    def __init__(self, out=None, err=None) -> None:
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.rich = self.out.isatty()
        self.s = Style(self.rich)
        self.e = Style(self.err.isatty())
        self.spinner = Spinner(self.err, self.err.isatty())
        self._buf = ""
        self._in_fence = False
        self._fence_lang = ""

    # -- plumbing ---------------------------------------------------------

    def _write(self, stream, text: str) -> None:
        with self.spinner.lock:
            self.spinner.hide()
            stream.write(text)
            stream.flush()

    def note(self, text: str) -> None:
        self._write(self.err, f"{self.e.dim(text)}\n")

    def warn(self, text: str) -> None:
        self._write(self.err, f"{self.e.yellow(text)}\n")

    def error(self, text: str) -> None:
        self._write(self.err, f"{self.e.red(text)}\n")

    def plain(self, text: str) -> None:
        self._write(self.err, f"{text}\n")

    def prompt(self, text: str) -> None:
        self._write(self.err, text)

    # -- spinner ----------------------------------------------------------

    def thinking(self, label: str = "thinking…") -> None:
        self.spinner.start(label)

    def waiting(self, label: str = "working…") -> None:
        self.spinner.resume(label)

    def done(self) -> None:
        self.spinner.stop()

    # -- streamed prose ---------------------------------------------------

    def emit(self, text: str) -> None:
        """Buffer to line boundaries so markdown can be applied per line."""
        if not self.rich:
            self._write(self.out, text)
            return
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._write(self.out, self._markdown(line) + "\n")

    def flush(self) -> None:
        """Emit whatever never got a trailing newline."""
        if self._buf:
            tail, self._buf = self._buf, ""
            self._write(self.out, (self._markdown(tail) if self.rich else tail))
        self._write(self.out, "\n")
        self._in_fence = False

    def _markdown(self, line: str) -> str:
        fence = re.match(r"^\s*```(\w*)", line)
        if fence:
            self._in_fence = not self._in_fence
            self._fence_lang = fence.group(1) if self._in_fence else ""
            return self.s.dim(line)
        if self._in_fence:
            return self._highlight(line)

        heading = re.match(r"^(#{1,6})\s+(.*)", line)
        if heading:
            return self.s.bold(self.s.blue(heading.group(2)))
        if re.match(r"^\s*([-*+])\s+", line):
            return re.sub(r"^(\s*)[-*+]\s+", rf"\1{self.s.cyan('•')} ", self._inline(line))
        if re.match(r"^\s*>\s?", line):
            return self.s.dim(line)
        if re.match(r"^\s*([-*_]\s*){3,}$", line):
            return self.s.dim("─" * 40)
        return self._inline(line)

    def _inline(self, line: str) -> str:
        line = re.sub(r"`([^`]+)`", lambda m: self.s.cyan(m.group(1)), line)
        line = re.sub(r"\*\*([^*]+)\*\*", lambda m: self.s.bold(m.group(1)), line)
        return line

    def _highlight(self, line: str) -> str:
        """Light token colouring inside fenced code — not a real parser."""
        comment = re.search(r"(#|//)\s", line)
        head, tail = (line[: comment.start()], line[comment.start() :]) if comment else (line, "")
        head = re.sub(r"(\"[^\"]*\"|'[^']*')", lambda m: self.s.green(m.group(1)), head)
        head = re.sub(
            r"\b(\w+)\b",
            lambda m: self.s.magenta(m.group(1)) if m.group(1) in KEYWORDS else m.group(1),
            head,
        )
        return head + (self.s.dim(tail) if tail else "")

    # -- tool calls -------------------------------------------------------

    def tool(self, name: str, args: dict, summary: str, is_error: bool) -> None:
        mark = self.e.red("✗") if is_error else self.e.green("●")
        label = self.e.bold(self.e.red(name)) if is_error else self.e.cyan(name)
        body = self.e.dim(f"({fmt_args(args)})")
        text = self.e.red(summary) if is_error else self.e.dim(summary)
        self._write(self.err, f"{mark} {label}{body} {self.e.dim('→')} {text}\n")

    def diff(self, text: str) -> None:
        lines = []
        for line in text.splitlines():
            if line.startswith(("+++", "---")):
                lines.append(self.e.dim(line))
            elif line.startswith("@@"):
                lines.append(self.e.cyan(line))
            elif line.startswith("+"):
                lines.append(self.e.green(line))
            elif line.startswith("-"):
                lines.append(self.e.red(line))
            else:
                lines.append(self.e.dim(line))
        self._write(self.err, "\n".join(lines) + "\n")

    def action(self, text: str) -> None:
        """The approval preview: the exact thing about to happen."""
        out = []
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("$"):
                out.append(self.e.bold(self.e.yellow(line)))
            elif stripped.startswith("+"):
                out.append(self.e.green(line))
            elif stripped.startswith("-"):
                out.append(self.e.red(line))
            elif "OVERWRITES" in line:
                out.append(self.e.bold(self.e.red(line)))
            else:
                out.append(self.e.bold(line))
        self._write(self.err, "\n".join(out) + "\n")

    def usage(self, text: str) -> None:
        self._write(self.err, f"{self.e.dim(text)}\n")


def preview(text: str, width: int = 96) -> str:
    first, _, rest = text.partition("\n")
    if len(first) > width:
        first = first[:width] + "…"
    extra = text.count("\n")
    return f"{first} (+{extra} lines)" if rest else first


def fmt_args(args: dict, width: int = 44) -> str:
    bits = []
    for key, value in args.items():
        if not isinstance(value, str):
            bits.append(f"{key}={value!r}")
            continue
        text = value.replace("\n", "\\n")
        if len(text) > width:
            text = text[:width] + "…"
        bits.append(f"{key}={text!r}")
    return ", ".join(bits)
