"""SaseQ/discord-mcp provider adapter.

Upstream: https://github.com/SaseQ/discord-mcp (MIT). Source is not copied.
Tool names are resolved from the live MCP catalog with sensible fallbacks.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence
from uuid import uuid4

from agent_discord.contracts import DiscordMessage, ToolDescriptor, ToolInvocationResult
from agent_discord.discord.errors import ToolInvocationError
from agent_discord.discord.providers.base import MCPTransport, extract_text_content


class SaseQDiscordProvider:
    """Adapter for SaseQ discord-mcp over HTTP or stdio transport."""

    name = "saseq"

    # Candidate tool names discovered/normalized at runtime
    _SEND_CANDIDATES = ("send_message", "discord_send_message", "send-message")
    _READ_CANDIDATES = ("read_messages", "get_messages", "discord_read_messages")
    _THREAD_CANDIDATES = ("create_thread", "start_thread", "discord_create_thread")

    def __init__(self, client: MCPTransport) -> None:
        self._client = client
        self._catalog: Optional[list[ToolDescriptor]] = None

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
        args: dict[str, Any] = {"channelId": channel_id, "content": content}
        if thread_id:
            args["threadId"] = thread_id
        # Also try snake_case keys if catalog hints at them
        result = self._client.call_tool(tool, args)
        if not result.ok:
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
        message_id = _dig_id(result) or f"saseq-{uuid4().hex[:12]}"
        return DiscordMessage(
            channel_id=channel_id,
            content=content,
            message_id=str(message_id),
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
        args: dict[str, Any] = {"channelId": channel_id, "limit": limit}
        if thread_id:
            args["threadId"] = thread_id
        result = self._client.call_tool(tool, args)
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
                {"channelId": channel_id, "name": title, "message": content},
            )
            if result.ok:
                message_id = _dig_id(result) or f"saseq-thread-{uuid4().hex[:12]}"
                return DiscordMessage(
                    channel_id=channel_id,
                    content=content,
                    message_id=str(message_id),
                    thread_id=str(_dig(result.raw, "threadId", "thread_id") or message_id),
                    metadata={"provider": self.name, "tool": tool, "title": title},
                )
        # Fallback: post a titled message in-channel
        return self.send_message(channel_id, f"**{title}**\n{content}")

    def _resolve_tool(self, candidates: Sequence[str], *, required: bool = True) -> str:
        if self._catalog is None:
            self.list_tools()
        assert self._catalog is not None
        names = {t.name for t in self._catalog}
        for candidate in candidates:
            if candidate in names:
                return candidate
        # Fuzzy contains
        for candidate in candidates:
            for name in names:
                if candidate.replace("-", "_") in name.replace("-", "_"):
                    return name
        if required:
            # Still return first candidate for clients that do not list tools
            return candidates[0]
        return ""


def _dig(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return None


def _dig_id(result: ToolInvocationResult) -> Any:
    raw = result.raw or {}
    for key in ("messageId", "message_id", "id"):
        if key in raw:
            return raw[key]
    content = result.content
    if isinstance(content, Mapping):
        for key in ("messageId", "message_id", "id"):
            if key in content:
                return content[key]
    text = extract_text_content(content)
    return text or None


def _parse_messages(
    content: Any, *, channel_id: str, provider: str
) -> list[DiscordMessage]:
    items: list[Any]
    if isinstance(content, list):
        items = content
    elif isinstance(content, Mapping) and "messages" in content:
        items = list(content["messages"])
    else:
        text = extract_text_content(content)
        return [
            DiscordMessage(
                channel_id=channel_id,
                content=text,
                message_id=f"{provider}-read-0",
                metadata={"provider": provider},
            )
        ]
    messages: list[DiscordMessage] = []
    for idx, item in enumerate(items):
        if isinstance(item, Mapping):
            messages.append(
                DiscordMessage(
                    channel_id=str(item.get("channelId") or item.get("channel_id") or channel_id),
                    content=str(item.get("content") or item.get("text") or ""),
                    message_id=str(item.get("id") or item.get("messageId") or f"{provider}-{idx}"),
                    author_id=(
                        str(item["authorId"])
                        if item.get("authorId")
                        else str(item["author_id"])
                        if item.get("author_id")
                        else None
                    ),
                    metadata={"provider": provider},
                )
            )
        else:
            messages.append(
                DiscordMessage(
                    channel_id=channel_id,
                    content=str(item),
                    message_id=f"{provider}-{idx}",
                    metadata={"provider": provider},
                )
            )
    return messages
