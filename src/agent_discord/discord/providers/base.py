"""Thin MCP transport clients — HTTP JSON and stdio JSON-RPC style.

These clients intentionally avoid copying upstream server source. They speak
generic MCP-shaped list/call tool RPCs and leave tool naming to adapters.
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

from agent_discord.contracts import ToolDescriptor, ToolInvocationResult
from agent_discord.discord.errors import DiscordMCPError, ToolInvocationError


@runtime_checkable
class MCPTransport(Protocol):
    def list_tools(self) -> Sequence[ToolDescriptor]: ...

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolInvocationResult: ...


_MCP_CLIENT_INFO = {"name": "discord-os", "version": "0.5.7"}
_MCP_PROTOCOL_VERSION = "2024-11-05"
_SESSION_HEADER = "Mcp-Session-Id"


def parse_mcp_http_body(raw: str) -> Any:
    """Parse a JSON-RPC body or a streamable-HTTP SSE envelope."""

    text = (raw or "").strip()
    if not text:
        raise json.JSONDecodeError("empty MCP body", raw or "", 0)
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    data_lines = [
        line[5:].strip()
        for line in text.splitlines()
        if line.startswith("data:")
    ]
    if not data_lines:
        raise json.JSONDecodeError("SSE MCP body had no data: line", raw, 0)
    return json.loads(data_lines[-1])


@dataclass
class HttpJsonMCPClient:
    """MCP-over-HTTP client using JSON-RPC POST envelopes.

    SaseQ HTTP endpoint convention: POST to the configured MCP URL (see
    `SASEQ_MCP_HTTP_URL`, default ``http://127.0.0.1:8085/mcp``). Exact upstream
    shapes may vary; adapters normalize tool names after catalog discovery.
    """

    base_url: str
    timeout_seconds: float = 30.0
    _id: int = field(default=0, init=False)
    _initialized: bool = field(default=False, init=False)
    _session_id: str = field(default="", init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def list_tools(self) -> Sequence[ToolDescriptor]:
        self._ensure_initialized()
        payload = self._rpc("tools/list", {})
        tools = payload.get("tools") or payload.get("result", {}).get("tools") or []
        return [
            ToolDescriptor(
                name=str(t.get("name", "")),
                description=str(t.get("description", "")),
                input_schema=dict(t.get("inputSchema") or t.get("input_schema") or {}),
            )
            for t in tools
            if t.get("name")
        ]

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolInvocationResult:
        self._ensure_initialized()
        try:
            payload = self._rpc("tools/call", {"name": name, "arguments": dict(arguments)})
        except DiscordMCPError as exc:
            return ToolInvocationResult(name=name, ok=False, error=str(exc))
        result = payload.get("result", payload)
        if not isinstance(result, dict):
            result = {"content": result}
        is_error = bool(result.get("isError") or result.get("error"))
        if is_error:
            err = result.get("error") or result.get("content") or "tool error"
            return ToolInvocationResult(name=name, ok=False, error=str(err), raw=result)
        return ToolInvocationResult(
            name=name,
            ok=True,
            content=result.get("content", result),
            raw=result,
        )

    def _ensure_initialized(self) -> None:
        with self._lock:
            if self._initialized:
                return
            try:
                self._rpc(
                    "initialize",
                    {
                        "protocolVersion": _MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": _MCP_CLIENT_INFO,
                    },
                )
                # notifications/initialized has no response body requirement
                self._notify("notifications/initialized", {})
            except DiscordMCPError:
                # Preserve prior HTTP behavior for servers that do not require handshake.
                pass
            self._initialized = True

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        body = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": dict(params)}
        ).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                self._capture_session(resp)
                resp.read()
        except urllib.error.URLError:
            # Notification best-effort; many HTTP MCP servers ignore it.
            return

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers[_SESSION_HEADER] = self._session_id
        return headers

    def _capture_session(self, resp: Any) -> None:
        session = resp.headers.get(_SESSION_HEADER) or resp.headers.get("mcp-session-id")
        if session:
            self._session_id = str(session)

    def _rpc(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self._id += 1
        req_id = self._id
        body = json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": dict(params)}
        ).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=body,
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                self._capture_session(resp)
                data = parse_mcp_http_body(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise DiscordMCPError(f"HTTP MCP request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise DiscordMCPError("HTTP MCP response was not JSON") from exc
        if isinstance(data, dict) and data.get("error"):
            raise ToolInvocationError(str(data["error"]))
        if isinstance(data, dict) and "id" in data and data["id"] not in (req_id, str(req_id)):
            raise DiscordMCPError(
                f"HTTP MCP response id mismatch: expected {req_id!r}, got {data.get('id')!r}"
            )
        if isinstance(data, dict) and "result" in data:
            result = data["result"]
            return result if isinstance(result, dict) else {"result": result}
        if isinstance(data, dict):
            return data
        return {"result": data}


@dataclass
class StdioMCPClient:
    """Persistent stdio MCP client with initialize/initialized sequencing.

    Matches JSON-RPC responses by request id. Suitable for local upstream
    servers launched via ``DISCORD_MCP_STDIO_COMMAND``. Unit tests inject fakes.
    """

    command: str
    timeout_seconds: float = 60.0
    _proc: Optional[subprocess.Popen[str]] = field(default=None, init=False, repr=False)
    _id: int = field(default=0, init=False)
    _initialized: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def list_tools(self) -> Sequence[ToolDescriptor]:
        payload = self._rpc("tools/list", {})
        tools = payload.get("tools") or []
        return [
            ToolDescriptor(
                name=str(t.get("name", "")),
                description=str(t.get("description", "")),
                input_schema=dict(t.get("inputSchema") or {}),
            )
            for t in tools
            if t.get("name")
        ]

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolInvocationResult:
        try:
            result = self._rpc("tools/call", {"name": name, "arguments": dict(arguments)})
        except DiscordMCPError as exc:
            return ToolInvocationResult(name=name, ok=False, error=str(exc))
        if result.get("isError") or result.get("error"):
            return ToolInvocationResult(
                name=name,
                ok=False,
                error=str(result.get("error") or result.get("content")),
                raw=result,
            )
        return ToolInvocationResult(
            name=name,
            ok=True,
            content=result.get("content", result),
            raw=result,
        )

    def close(self) -> None:
        with self._lock:
            self._terminate_process()

    def _ensure_session(self) -> None:
        if self._proc is not None and self._proc.poll() is None and self._initialized:
            return
        if self._proc is not None and self._proc.poll() is not None:
            self._proc = None
            self._initialized = False
        if self._proc is None:
            try:
                command_parts = shlex.split(self.command, posix=(os.name != "nt"))
                if not command_parts:
                    raise ValueError("stdio MCP command is empty")
                self._proc = subprocess.Popen(
                    command_parts,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except (OSError, ValueError) as exc:
                raise DiscordMCPError(f"stdio MCP failed to start: {exc}") from exc
            self._initialized = False
        if not self._initialized:
            self._request(
                "initialize",
                {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": _MCP_CLIENT_INFO,
                },
            )
            self._write(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            )
            self._initialized = True

    def _rpc(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_session()
            return self._request(method, params)

    def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self._id += 1
        req_id = self._id
        self._write(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": dict(params)}
        )
        data = self._read_response(req_id)
        if data.get("error"):
            raise ToolInvocationError(str(data["error"]))
        result = data.get("result", data)
        return result if isinstance(result, dict) else {"result": result}

    def _write(self, message: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise DiscordMCPError("stdio MCP process is not running")
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        except OSError as exc:
            raise DiscordMCPError(f"stdio MCP write failed: {exc}") from exc

    def _read_response(self, req_id: int) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise DiscordMCPError("stdio MCP process is not running")
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            try:
                line = self._readline_until(proc.stdout, deadline)
            except TimeoutError as exc:
                self._terminate_process()
                raise DiscordMCPError(
                    f"stdio MCP timed out waiting for JSON-RPC response id={req_id}"
                ) from exc
            if not line:
                if proc.poll() is not None:
                    raise DiscordMCPError(f"stdio MCP exited {proc.returncode}")
                continue
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            # Skip notifications / unrelated traffic; match by request id.
            if "id" not in data:
                continue
            if data["id"] not in (req_id, str(req_id)):
                continue
            return data
        self._terminate_process()
        raise DiscordMCPError(
            f"stdio MCP timed out waiting for JSON-RPC response id={req_id}"
        )

    @staticmethod
    def _readline_until(stream: Any, deadline: float) -> str:
        """Read one line without allowing an unresponsive server to hang forever."""
        result_queue: queue.Queue[object] = queue.Queue(maxsize=1)

        def read_line() -> None:
            try:
                result_queue.put(stream.readline())
            except BaseException as exc:  # pragma: no cover - defensive thread bridge
                result_queue.put(exc)

        threading.Thread(target=read_line, daemon=True).start()
        remaining = max(0.0, deadline - time.monotonic())
        try:
            result = result_queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if isinstance(result, BaseException):
            raise DiscordMCPError(f"stdio MCP read failed: {result}") from result
        return str(result)

    def _terminate_process(self) -> None:
        proc = self._proc
        self._proc = None
        self._initialized = False
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass


def extract_text_content(content: Any) -> str:
    """Normalize MCP content blocks or plain strings to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        if "text" in content:
            return str(content["text"])
        if "content" in content:
            return extract_text_content(content["content"])
        return json.dumps(content, sort_keys=True)
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts = [extract_text_content(item) for item in content]
        return "\n".join(p for p in parts if p)
    return str(content)
