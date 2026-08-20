"""BrainDAO/@iqai/mcp-discord provider adapter.

Upstream: https://github.com/BrainDAO/mcp-discord (MIT). Source is not copied.
Exposes a sampling-compatible ingress seam without requiring a second Gateway.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence
from uuid import uuid4

from agent_discord.contracts import DiscordMessage, ToolDescriptor, ToolInvocationResult
from agent_discord.discord.errors import ToolInvocationError
from agent_discord.discord.providers.base import MCPTransport, extract_text_content
from agent_discord.discord.providers.saseq import AttachmentMCPOperations, _parse_messages


class BrainDAODiscordProvider(AttachmentMCPOperations):
    """Adapter for BrainDAO mcp-discord (@iqai) over HTTP or stdio."""

    name = "braindao"
    _prefer_camel_case = False

    _SEND_CANDIDATES = ("send_message", "DISCORD_SEND_MESSAGE", "discord_send")
    _READ_CANDIDATES = ("read_messages", "DISCORD_READ_MESSAGES", "get_channel_messages")
    _THREAD_CANDIDATES = ("create_thread", "DISCORD_CREATE_THREAD", "start_thread")

    def __init__(self, client: MCPTransport, *, bot_token: str = "") -> None:
        self._client = client
        self._bot_token = bot_token
        self._catalog: Optional[list[ToolDescriptor]] = None
        self._sampling_handlers: list[Any] = []

    def list_tools(self) -> Sequence[ToolDescriptor]:
        self._catalog = list(self._client.list_tools())
        return list(self._catalog)

    def invoke_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolInvocationResult:
        return self._client.call_tool(name, arguments)

    def close(self) -> None:
        closer = getattr(self._client, "close", None)
        if callable(closer):
            closer()

    def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        thread_id: Optional[str] = None,
    ) -> DiscordMessage:
        tool = self._resolve_tool(self._SEND_CANDIDATES)
        result = self._client.call_tool(
            tool,
            {
                "channel_id": channel_id,
                "content": content,
                **({"thread_id": thread_id} if thread_id else {}),
            },
        )
        if not result.ok:
            raise ToolInvocationError(result.error or "send_message failed")
        message_id = None
        if isinstance(result.raw, Mapping):
            message_id = result.raw.get("message_id") or result.raw.get("id")
        if isinstance(result.content, Mapping):
            message_id = message_id or result.content.get("message_id") or result.content.get("id")
        return DiscordMessage(
            channel_id=channel_id,
            content=content,
            message_id=str(message_id or f"braindao-{uuid4().hex[:12]}"),
            thread_id=thread_id,
            metadata={"provider": self.name, "tool": tool},
        )

    def read_messages(
        self,
        channel_id: str,
        *,
        limit: int = 20,
        thread_id: Optional[str] = None,
    ) -> Sequence[DiscordMessage]:
        tool = self._resolve_tool(self._READ_CANDIDATES)
        result = self._client.call_tool(
            tool,
            {
                "channel_id": channel_id,
                "limit": limit,
                **({"thread_id": thread_id} if thread_id else {}),
            },
        )
        if not result.ok:
            raise ToolInvocationError(result.error or "read_messages failed")
        return _parse_messages(result.content, channel_id=channel_id, provider=self.name)

    def post_thread_task(
        self,
        channel_id: str,
        title: str,
        content: str,
    ) -> DiscordMessage:
        tool = self._resolve_tool(self._THREAD_CANDIDATES, required=False)
        if tool:
            result = self._client.call_tool(
                tool,
                {"channel_id": channel_id, "name": title, "content": content},
            )
            if result.ok:
                message_id = f"braindao-thread-{uuid4().hex[:12]}"
                thread_id = message_id
                if isinstance(result.raw, Mapping):
                    message_id = str(result.raw.get("message_id") or message_id)
                    thread_id = str(result.raw.get("thread_id") or thread_id)
                return DiscordMessage(
                    channel_id=channel_id,
                    content=content,
                    message_id=message_id,
                    thread_id=thread_id,
                    metadata={"provider": self.name, "tool": tool, "title": title},
                )
        return self.send_message(channel_id, f"**{title}**\n{content}")

    def handle_sampling_request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Sampling-compatible ingress seam (BrainDAO/@iqai convention).

        Does not open a second Discord Gateway. Callers route sampling tool
        traffic through the same facade/Gateway owner.
        """
        method = str(payload.get("method") or payload.get("type") or "sampling")
        messages = payload.get("messages") or payload.get("params", {}).get("messages") or []
        text_parts = []
        for msg in messages:
            if isinstance(msg, Mapping):
                text_parts.append(extract_text_content(msg.get("content")))
            else:
                text_parts.append(str(msg))
        return {
            "ok": True,
            "provider": self.name,
            "method": method,
            "echo": "\n".join(p for p in text_parts if p),
            "note": (
                "sampling ingress accepted on adapter seam; "
                "Gateway ownership remains exclusive to the facade owner"
            ),
        }

    def _resolve_tool(self, candidates: Sequence[str], *, required: bool = True) -> str:
        if self._catalog is None:
            self.list_tools()
        assert self._catalog is not None
        names = {t.name for t in self._catalog}
        for candidate in candidates:
            if candidate in names:
                return candidate
        lowered = {n.lower(): n for n in names}
        for candidate in candidates:
            if candidate.lower() in lowered:
                return lowered[candidate.lower()]
        if required:
            return candidates[0]
        return ""
