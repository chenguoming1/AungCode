from __future__ import annotations

import itertools
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .tools import Tool, ToolError

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "aungcode", "version": "0.1"}
CONFIG_NAME = ".mcp.json"
START_TIMEOUT = 20.0
CALL_TIMEOUT = 60.0
MAX_TOOL_NAME = 64
NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")


class MCPError(Exception):
    """The server failed, timed out, or spoke something we cannot use."""


class Server:
    def __init__(self, name: str, spec: dict, cwd: Path) -> None:
        self.name = name
        self.command = spec.get("command")
        self.args = list(spec.get("args") or [])
        self.env = dict(spec.get("env") or {})
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        self.info: dict = {}
        self._inbox: queue.Queue = queue.Queue()
        self._ids = itertools.count(1)

    # -- transport --------------------------------------------------------

    def _pump_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._inbox.put(json.loads(line))
            except json.JSONDecodeError:
                log.debug("mcp %s: non-JSON on stdout: %r", self.name, line[:200])

    def _pump_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        # Servers log here freely; an unread pipe would eventually deadlock them.
        for line in self.proc.stderr:
            log.debug("mcp %s: %s", self.name, line.rstrip())

    def _send(self, message: dict) -> None:
        if not self.proc or self.proc.poll() is not None:
            raise MCPError(f"{self.name}: server is not running")
        try:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise MCPError(f"{self.name}: could not write to server ({e})") from None

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        rid = next(self._ids)
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise MCPError(f"{self.name}: {method} timed out after {timeout:g}s")
            try:
                # Short waits so a server that dies is noticed immediately
                # rather than after the full timeout.
                message = self._inbox.get(timeout=min(left, 0.25))
            except queue.Empty:
                if self.proc and self.proc.poll() is not None:
                    raise MCPError(
                        f"{self.name}: server exited with code {self.proc.returncode} "
                        f"during {method}"
                    ) from None
                continue
            # Anything with a "method" is the server talking to us; this client
            # answers no server requests, so skip it rather than mistaking the
            # id for one of ours.
            if "method" in message or message.get("id") != rid:
                continue
            if "error" in message:
                err = message["error"] or {}
                raise MCPError(f"{self.name}: {err.get('message')} (code {err.get('code')})")
            return message.get("result") or {}

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if not self.command:
            raise MCPError(f"{self.name}: no command in config")
        exe = shutil.which(self.command)
        if not exe:
            raise MCPError(f"{self.name}: command not found: {self.command}")

        self.proc = subprocess.Popen(
            [exe, *self.args],
            cwd=self.cwd,
            env={**os.environ, **self.env},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
            START_TIMEOUT,
        )
        self.info = result.get("serverInfo") or {}
        # The spec requires this notification before any other request.
        self._notify("notifications/initialized", {})

    def list_tools(self) -> list[dict]:
        return self._request("tools/list", {}, START_TIMEOUT).get("tools") or []

    def call(self, tool: str, arguments: dict) -> str:
        result = self._request(
            "tools/call", {"name": tool, "arguments": arguments}, CALL_TIMEOUT
        )
        parts = []
        for block in result.get("content") or []:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(f"[{block.get('type')} content omitted]")
        text = "\n".join(p for p in parts if p) or "(no output)"
        if result.get("isError"):
            raise ToolError(text)
        return text

    def close(self) -> None:
        if not self.proc:
            return
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None


# -- config and registry ---------------------------------------------------


def config_path(workspace_root: Path) -> Path:
    override = os.environ.get("AGENT_MCP_CONFIG")
    return Path(override) if override else workspace_root / CONFIG_NAME


def load_config(workspace_root: Path) -> dict:
    path = config_path(workspace_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        raise MCPError(f"{path}: {e}") from None
    servers = raw.get("mcpServers")
    if not isinstance(servers, dict):
        raise MCPError(f"{path}: expected an object under 'mcpServers'")
    return servers


def qualified(server: str, tool: str) -> str:
    """Namespaced so two servers exposing 'search' cannot collide."""
    return f"mcp__{NAME_OK.sub('_', server)}__{NAME_OK.sub('_', tool)}"


def connect(workspace_root: Path, warn) -> list[Server]:
    """Start every configured server. A server that fails is skipped, not fatal."""
    try:
        specs = load_config(workspace_root)
    except MCPError as e:
        warn(f"mcp config ignored: {e}")
        return []

    started = []
    for name, spec in specs.items():
        server = Server(name, spec, workspace_root)
        try:
            server.start()
        except MCPError as e:
            warn(f"mcp: {e}")
            server.close()
            continue
        started.append(server)
    return started


def tools_for(servers: list[Server], warn) -> list[Tool]:
    found: list[Tool] = []
    for server in servers:
        try:
            listed = server.list_tools()
        except MCPError as e:
            warn(f"mcp: {e}")
            continue
        for spec in listed:
            name = spec.get("name")
            if not name:
                continue
            full = qualified(server.name, name)
            if len(full) > MAX_TOOL_NAME:
                warn(f"mcp: skipping {full} — name longer than {MAX_TOOL_NAME} chars")
                continue
            found.append(
                Tool(
                    name=full,
                    description=(spec.get("description") or f"{name} (via {server.name})"),
                    schema=spec.get("inputSchema") or {"type": "object"},
                    run=_caller(server, name),
                    # We cannot know what a server does, so it always asks.
                    requires_approval=True,
                )
            )
    return found


def _caller(server: Server, tool: str):
    def run(args: dict) -> str:
        try:
            return server.call(tool, args)
        except MCPError as e:
            raise ToolError(str(e)) from None

    return run
