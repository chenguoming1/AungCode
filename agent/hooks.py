from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_NAME = ".hooks.json"
TIMEOUT = 30.0
MAX_OUTPUT = 2000
NOTE_CHARS = 200


class HookError(Exception):
    """The hook configuration could not be read."""


@dataclass(frozen=True)
class Hook:
    pattern: re.Pattern
    command: str


class NoHooks:
    """Used when nothing is configured, and by nested subagent loops."""

    def before(self, call) -> str | None:
        return None

    def after(self, call, result) -> None:
        return None


@dataclass
class Hooks:
    pre: list[Hook] = field(default_factory=list)
    post: list[Hook] = field(default_factory=list)
    cwd: Path = field(default_factory=Path.cwd)
    notify: object = print

    def _run(self, hook: Hook, env: dict[str, str]):
        return subprocess.run(
            hook.command,
            shell=True,
            cwd=self.cwd,
            env={**os.environ, **env},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=TIMEOUT,
        )

    def before(self, call) -> str | None:
        """Returns an error string to block the call, or None to allow it."""
        env = {
            "AGENT_TOOL": call.name,
            "AGENT_TOOL_ARGS": json.dumps(call.args),
            "AGENT_HOOK_PHASE": "pre",
        }
        for hook in self.pre:
            if not hook.pattern.search(call.name):
                continue
            try:
                proc = self._run(hook, env)
            except (subprocess.TimeoutExpired, OSError) as e:
                self.notify(f"hook failed to run ({e}): {hook.command}")
                continue
            output = f"{proc.stdout}{proc.stderr}".strip()
            if proc.returncode != 0:
                return (
                    f"blocked by a pre-tool hook (exit {proc.returncode}): "
                    f"{output[:MAX_OUTPUT] or hook.command}"
                )
            if output:
                self.notify(f"hook: {output[:NOTE_CHARS]}")
        return None

    def after(self, call, result) -> None:
        env = {
            "AGENT_TOOL": call.name,
            "AGENT_TOOL_ARGS": json.dumps(call.args),
            "AGENT_TOOL_ERROR": "1" if result.is_error else "0",
            "AGENT_TOOL_RESULT": result.content[:MAX_OUTPUT],
            "AGENT_HOOK_PHASE": "post",
        }
        for hook in self.post:
            if not hook.pattern.search(call.name):
                continue
            try:
                proc = self._run(hook, env)
            except (subprocess.TimeoutExpired, OSError) as e:
                self.notify(f"hook failed to run ({e}): {hook.command}")
                continue
            output = f"{proc.stdout}{proc.stderr}".strip()
            # The tool already ran; a failing post hook is reported, not fatal.
            if proc.returncode != 0:
                self.notify(f"post hook exit {proc.returncode}: {output[:NOTE_CHARS]}")
            elif output:
                self.notify(f"hook: {output[:NOTE_CHARS]}")


def config_path(workspace_root: Path) -> Path:
    override = os.environ.get("AGENT_HOOKS_CONFIG")
    return Path(override) if override else workspace_root / CONFIG_NAME


def _parse(entries, where: str) -> list[Hook]:
    hooks = []
    for entry in entries or []:
        command = entry.get("command")
        if not command:
            raise HookError(f"{where}: entry without a 'command'")
        try:
            pattern = re.compile(entry.get("match") or ".")
        except re.error as e:
            raise HookError(f"{where}: bad 'match' regex: {e}") from None
        hooks.append(Hook(pattern, command))
    return hooks


def load(workspace_root: Path, notify) -> Hooks | NoHooks:
    path = config_path(workspace_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return NoHooks()
    except (OSError, json.JSONDecodeError) as e:
        notify(f"hooks config ignored: {path}: {e}")
        return NoHooks()
    try:
        pre = _parse(raw.get("pre"), "pre")
        post = _parse(raw.get("post"), "post")
    except HookError as e:
        notify(f"hooks config ignored: {e}")
        return NoHooks()
    if not pre and not post:
        return NoHooks()
    return Hooks(pre=pre, post=post, cwd=workspace_root, notify=notify)
