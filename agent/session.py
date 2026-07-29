from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from .providers import Message

try:
    import fcntl
except ImportError:  # not POSIX — locking degrades to a no-op
    fcntl = None

TITLE_CHARS = 60


class SessionBusy(RuntimeError):
    """The session is already open in another process."""


class Lock:
    """Advisory exclusive lock, held for the life of the process.

    Kept in a sidecar file, not on the .jsonl: save() replaces that inode via
    os.replace, so a lock taken on it would be stranded after the first save.
    flock is released by the kernel when the holder exits, so a crashed tab
    cannot leave a stale lock behind.
    """

    def __init__(self, session_path: Path) -> None:
        self.path = session_path.with_suffix(".lock")
        self._fh = None

    def acquire(self) -> None:
        if fcntl is None:
            return
        fh = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip() or "another process"
            fh.close()
            raise SessionBusy(holder) from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid {os.getpid()} since {time.strftime('%Y-%m-%d %H:%M:%S')}")
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


DEFAULT_DIR = Path.home() / ".aungcode" / "sessions"

# Set once from config at startup. Module state rather than a parameter on
# every call site, since it never changes during a run.
_configured: Path | None = None


def configure(path: str | None) -> None:
    global _configured
    _configured = Path(path).expanduser() if path else None


def session_dir() -> Path:
    """Environment overrides config, config overrides the default."""
    override = os.environ.get("AGENT_SESSION_DIR")
    if override:
        root = Path(override).expanduser()
    elif _configured is not None:
        root = _configured
    else:
        root = DEFAULT_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _jsonable(value):
    """Serialize through the SDK's own model_dump so a reloaded message
    produces the same request bytes as the object it replaces."""
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    dump = getattr(value, "model_dump", None)
    if dump is not None:
        # exclude_none drops optional fields the SDK also omits on the wire.
        return _jsonable(dump(mode="json", exclude_none=True))
    return str(value)


@dataclass
class Session:
    path: Path
    meta: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.meta.get("id", self.path.stem)


def new(cfg, workspace_root: Path) -> Session:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    sid = f"{stamp}-{secrets.token_hex(2)}"
    meta = {
        "type": "session",
        "id": sid,
        "created_at": time.time(),
        "updated_at": time.time(),
        "provider": cfg.profile,
        "model": cfg.model,
        "workspace": str(workspace_root),
        "session_tokens": 0,
        "session_cost": 0.0,
        "messages": 0,
        "title": "",
    }
    return Session(session_dir() / f"{sid}.jsonl", meta)


def _title(messages: list[Message]) -> str:
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            text = " ".join(m["content"].split())
            return text[:TITLE_CHARS] + ("…" if len(text) > TITLE_CHARS else "")
    return ""


def save(
    session: Session,
    messages: list[Message],
    session_tokens: int,
    session_cost: float = 0.0,
) -> None:
    session.meta.update(
        updated_at=time.time(),
        session_tokens=session_tokens,
        session_cost=round(session_cost, 6),
        messages=len(messages),
        title=session.meta.get("title") or _title(messages),
    )
    # History is mutated in place by rollback and compaction, so the file is
    # rewritten whole rather than appended to. Temp + rename keeps it atomic.
    tmp = session.path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(session.meta, ensure_ascii=False) + "\n")
        for message in messages:
            fh.write(
                json.dumps(
                    {"type": "message", "message": _jsonable(message)},
                    ensure_ascii=False,
                )
                + "\n"
            )
    os.replace(tmp, session.path)


def read(path: Path) -> tuple[Session, list[Message]]:
    meta: dict = {}
    messages: list[Message] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path.name}:{lineno}: {e}") from None
            if record.get("type") == "session":
                meta = record
            elif record.get("type") == "message":
                messages.append(record["message"])
    if not meta:
        raise ValueError(f"{path.name}: missing session header")
    return Session(path, meta), messages


def listing(workspace_root: Path | None = None) -> list[Session]:
    """Newest first. Only the header line of each file is parsed."""
    found = []
    for path in session_dir().glob("*.jsonl"):
        try:
            with path.open(encoding="utf-8") as fh:
                meta = json.loads(fh.readline() or "{}")
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("type") != "session":
            continue
        if workspace_root and meta.get("workspace") != str(workspace_root):
            continue
        found.append(Session(path, meta))
    found.sort(key=lambda s: s.meta.get("updated_at", 0), reverse=True)
    return found


def most_recent(workspace_root: Path) -> Session | None:
    sessions = listing(workspace_root)
    return sessions[0] if sessions else None


def describe(session: Session) -> str:
    meta = session.meta
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta.get("updated_at", 0)))
    return (
        f"{when}  {meta.get('messages', 0):>3} msgs  "
        f"{meta.get('provider', '?')}:{meta.get('model', '?')}  "
        f"{meta.get('title') or '(no title)'}"
    )
