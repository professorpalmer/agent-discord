"""Protocol-level tests for the dependency-free stdio MCP transport."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from agent_discord.discord.providers.base import StdioMCPClient


def test_stdio_client_initializes_and_matches_response_ids(tmp_path: Path):
    server = tmp_path / "mcp_server.py"
    server.write_text(
        """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"protocolVersion": "2024-11-05"},
        }
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        print(json.dumps({"jsonrpc": "2.0", "id": 999, "result": {}}), flush=True)
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [{"name": "send_message"}]},
        }
    elif method == "tools/call":
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": "ok"}]},
        }
    else:
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {},
        }
    print(json.dumps(response), flush=True)
""",
        encoding="utf-8",
    )
    client = StdioMCPClient(
        command=f"{shlex.quote(sys.executable)} {shlex.quote(str(server))}",
        timeout_seconds=2,
    )
    try:
        tools = client.list_tools()
        result = client.call_tool("send_message", {"content": "hello"})
    finally:
        client.close()

    assert [tool.name for tool in tools] == ["send_message"]
    assert result.ok is True
    assert result.content == [{"type": "text", "text": "ok"}]
import io
import json
from typing import Any


class _FakeProc:
    def __init__(self) -> None:
        self._out_lines: list[str] = []
        # stdin/stdout both route through this object (write vs readline).
        self.stdin = self
        self.stdout = self
        self.stderr = io.StringIO()
        self._returncode: int | None = None
        self._read_idx = 0
        self.writes: list[dict[str, Any]] = []

    def write(self, data: str) -> int:
        # stdin.write path — record outbound messages and enqueue responses.
        for line in data.splitlines():
            if line.strip():
                msg = json.loads(line)
                self.writes.append(msg)
                if msg.get("method") == "initialize":
                    self._out_lines.append(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": msg["id"],
                                "result": {
                                    "protocolVersion": "2024-11-05",
                                    "capabilities": {},
                                    "serverInfo": {"name": "fake"},
                                },
                            }
                        )
                    )
                elif msg.get("method") == "tools/list":
                    # Unrelated notification first, then mismatched id, then match
                    self._out_lines.append(
                        json.dumps(
                            {"jsonrpc": "2.0", "method": "notifications/progress"}
                        )
                    )
                    self._out_lines.append(
                        json.dumps({"jsonrpc": "2.0", "id": 999999, "result": {}})
                    )
                    self._out_lines.append(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": msg["id"],
                                "result": {
                                    "tools": [
                                        {
                                            "name": "send_message",
                                            "description": "send",
                                            "inputSchema": {},
                                        }
                                    ]
                                },
                            }
                        )
                    )
                elif msg.get("method") == "tools/call":
                    self._out_lines.append(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": msg["id"],
                                "result": {"content": [{"type": "text", "text": "ok"}]},
                            }
                        )
                    )
        return len(data)

    def flush(self) -> None:
        return None

    def readline(self) -> str:
        if self._read_idx >= len(self._out_lines):
            self._returncode = 0
            return ""
        line = self._out_lines[self._read_idx]
        self._read_idx += 1
        return line + "\n"

    def readable(self) -> bool:
        return True

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        self._returncode = 0
        return 0

    def kill(self) -> None:
        self._returncode = -9


def test_stdio_initialize_and_match_by_id(monkeypatch):
    fake = _FakeProc()

    def fake_popen(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(
        "agent_discord.discord.providers.base.subprocess.Popen",
        fake_popen,
    )

    client = StdioMCPClient(command="fake-mcp")
    tools = client.list_tools()
    assert [t.name for t in tools] == ["send_message"]

    methods = [m.get("method") for m in fake.writes]
    assert "initialize" in methods
    assert "notifications/initialized" in methods
    assert "tools/list" in methods

    # initialize id must precede tools/list and be matched
    init_msg = next(m for m in fake.writes if m.get("method") == "initialize")
    assert "id" in init_msg

    result = client.call_tool("send_message", {"channel_id": "1", "content": "hi"})
    assert result.ok is True
    client.close()
