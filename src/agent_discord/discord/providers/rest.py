"""Default live Discord provider: official REST, no MCP and no Gateway."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from agent_discord.contracts import DiscordMessage, ToolDescriptor, ToolInvocationResult
from agent_discord.discord.rest import (
    UrlOpener,
    add_message_reaction,
    delete_channel_message,
    download_channel_attachment,
    edit_channel_message,
    fetch_channel_message,
    list_channel_messages,
    send_channel_attachment,
    send_channel_message,
)


class RestDiscordProvider:
    """Poll and post through Discord REST. Does not open a Gateway."""

    name = "rest"

    def __init__(self, *, bot_token: str, opener: Optional[UrlOpener] = None) -> None:
        self._bot_token = bot_token
        self._opener = opener

    def list_tools(self) -> Sequence[ToolDescriptor]:
        return (
            ToolDescriptor(name="send_message", description="POST channel message"),
            ToolDescriptor(name="read_messages", description="GET channel messages"),
            ToolDescriptor(name="edit_message", description="PATCH channel message"),
            ToolDescriptor(name="delete_message", description="DELETE channel message"),
            ToolDescriptor(name="send_file", description="POST channel attachment"),
            ToolDescriptor(name="get_message", description="GET channel message"),
            ToolDescriptor(name="get_attachment", description="GET attachment bytes"),
        )

    def invoke_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolInvocationResult:
        return ToolInvocationResult(
            name=name,
            ok=False,
            error="rest provider uses typed methods, not MCP tool names",
        )

    def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        thread_id: Optional[str] = None,
        components: Optional[list] = None,
        embeds: Optional[list] = None,
        flags: int = 0,
    ) -> DiscordMessage:
        return send_channel_message(
            token=self._bot_token,
            channel_id=channel_id,
            content=content,
            thread_id=thread_id,
            components=components,
            embeds=embeds,
            flags=flags,
            opener=self._opener,
        )

    def read_messages(
        self,
        channel_id: str,
        *,
        limit: int = 20,
        thread_id: Optional[str] = None,
    ) -> Sequence[DiscordMessage]:
        return list_channel_messages(
            token=self._bot_token,
            channel_id=channel_id,
            limit=limit,
            thread_id=thread_id,
            opener=self._opener,
        )

    def post_thread_task(
        self,
        channel_id: str,
        title: str,
        content: str,
    ) -> DiscordMessage:
        return self.send_message(channel_id, f"**{title}**\n{content}")

    def send_attachment(
        self,
        channel_id: str,
        filename: str,
        data: bytes,
        *,
        content: str = "",
        thread_id: Optional[str] = None,
        embeds: Optional[list] = None,
        components: Optional[list] = None,
        flags: int = 0,
    ) -> DiscordMessage:
        return send_channel_attachment(
            token=self._bot_token,
            channel_id=channel_id,
            filename=filename,
            data=data,
            content=content,
            thread_id=thread_id,
            embeds=embeds,
            components=components,
            flags=flags,
            opener=self._opener,
        )

    def get_message(self, channel_id: str, message_id: str) -> DiscordMessage:
        return fetch_channel_message(
            token=self._bot_token,
            channel_id=channel_id,
            message_id=message_id,
            opener=self._opener,
        )

    def download_attachment(
        self,
        channel_id: str,
        message_id: str,
        attachment_id: str,
    ) -> bytes:
        return download_channel_attachment(
            token=self._bot_token,
            channel_id=channel_id,
            message_id=message_id,
            attachment_id=attachment_id,
            opener=self._opener,
        )

    def edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str,
        *,
        components: Optional[list] = None,
        embeds: Optional[list] = None,
        flags: int = 0,
    ) -> DiscordMessage:
        return edit_channel_message(
            token=self._bot_token,
            channel_id=channel_id,
            message_id=message_id,
            content=content,
            components=components,
            embeds=embeds,
            flags=flags,
            opener=self._opener,
        )

    def start_thread_from_message(
        self,
        channel_id: str,
        message_id: str,
        name: str,
    ) -> str:
        from agent_discord.discord.rest import start_message_thread

        return start_message_thread(
            token=self._bot_token,
            channel_id=channel_id,
            message_id=message_id,
            name=name,
            opener=self._opener,
        )


    def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        add_message_reaction(
            token=self._bot_token,
            channel_id=channel_id,
            message_id=message_id,
            emoji=emoji,
            opener=self._opener,
        )

    def delete_message(self, channel_id: str, message_id: str) -> None:
        delete_channel_message(
            token=self._bot_token,
            channel_id=channel_id,
            message_id=message_id,
            opener=self._opener,
        )

    def close(self) -> None:
        return
