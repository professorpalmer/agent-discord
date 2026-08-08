"""Deterministic fake Discord MCP provider for tests (no network)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence
from uuid import uuid4

from agent_discord.contracts import DiscordMessage, ToolDescriptor, ToolInvocationResult


@dataclass
class FakeDiscordMCPProvider:
    name: str = "fake"
    tools: list[ToolDescriptor] = field(
        default_factory=lambda: [
            ToolDescriptor(name="send_message", description="Send a message"),
            ToolDescriptor(name="read_messages", description="Read messages"),
            ToolDescriptor(name="create_thread", description="Create a thread"),
        ]
    )
    sent: list[DiscordMessage] = field(default_factory=list)
    inbox: list[DiscordMessage] = field(default_factory=list)
    fail_tools: set[str] = field(default_factory=set)
    sampling_calls: list[Mapping[str, Any]] = field(default_factory=list)

    def list_tools(self) -> Sequence[ToolDescriptor]:
        return list(self.tools)

    def invoke_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolInvocationResult:
        if name in self.fail_tools:
            return ToolInvocationResult(name=name, ok=False, error="forced failure")
        if name == "send_message":
            msg = self.send_message(
                str(arguments.get("channel_id") or arguments.get("channelId") or ""),
                str(arguments.get("content") or ""),
                thread_id=arguments.get("thread_id") or arguments.get("threadId"),
            )
            return ToolInvocationResult(
                name=name, ok=True, content={"id": msg.message_id}, raw={"id": msg.message_id}
            )
        if name == "read_messages":
            msgs = self.read_messages(
                str(arguments.get("channel_id") or arguments.get("channelId") or ""),
                limit=int(arguments.get("limit") or 20),
            )
            return ToolInvocationResult(
                name=name,
                ok=True,
                content=[
                    {"id": m.message_id, "content": m.content, "channel_id": m.channel_id}
                    for m in msgs
                ],
            )
        return ToolInvocationResult(name=name, ok=True, content=dict(arguments))

    def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        thread_id: Optional[str] = None,
    ) -> DiscordMessage:
        msg = DiscordMessage(
            channel_id=channel_id,
            content=content,
            message_id=f"fake-{uuid4().hex[:10]}",
            thread_id=thread_id,
            metadata={"provider": self.name},
        )
        self.sent.append(msg)
        return msg

    def read_messages(
        self,
        channel_id: str,
        *,
        limit: int = 20,
        thread_id: Optional[str] = None,
    ) -> Sequence[DiscordMessage]:
        matched = [
            m
            for m in self.inbox
            if m.channel_id == channel_id
            and (thread_id is None or m.thread_id == thread_id)
        ]
        return matched[-limit:]

    def post_thread_task(
        self,
        channel_id: str,
        title: str,
        content: str,
    ) -> DiscordMessage:
        thread_id = f"thread-{uuid4().hex[:8]}"
        msg = DiscordMessage(
            channel_id=channel_id,
            content=f"**{title}**\n{content}",
            message_id=f"fake-{uuid4().hex[:10]}",
            thread_id=thread_id,
            metadata={"provider": self.name, "title": title},
        )
        self.sent.append(msg)
        return msg

    def handle_sampling_request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.sampling_calls.append(dict(payload))
        return {"ok": True, "provider": self.name, "echo": payload.get("messages", [])}
