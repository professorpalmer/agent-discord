"""SaseQ/discord-mcp provider adapter.

Upstream: https://github.com/SaseQ/discord-mcp (MIT). Source is not copied.
Tool names are resolved from the live MCP catalog with sensible fallbacks.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Mapping, Optional, Sequence
from uuid import uuid4

from agent_discord.contracts import DiscordAttachment, DiscordMessage, ToolDescriptor, ToolInvocationResult
from agent_discord.discord.errors import ToolInvocationError
from agent_discord.discord.providers.base import MCPTransport, extract_text_content
from agent_discord.discord.rest import (
    download_channel_attachment,
    fetch_channel_message,
    send_channel_attachment,
)


class AttachmentMCPOperations:
    """Fail-closed send_file / get_message / get_attachment over a live MCP catalog.

    send_file is an unmerged SaseQ PR shape; get_message is a requested tool.
    If the catalog lacks a file tool we raise — never dump bytes into send_message.
    """

    _SEND_FILE_CANDIDATES = ("send_file", "send_attachment", "discord_send_file")
    _GET_MESSAGE_CANDIDATES = ("get_message", "retrieve_message", "discord_get_message")
    _GET_ATTACHMENT_CANDIDATES = ("get_attachment", "download_attachment")
    _EDIT_CANDIDATES = ("edit_message", "discord_edit_message")
    _DELETE_CANDIDATES = ("delete_message", "discord_delete_message")
    _prefer_camel_case = True
    _bot_token: str = ""

    def send_attachment(
        self,
        channel_id: str,
        filename: str,
        data: bytes,
        *,
        content: str = "",
        thread_id: Optional[str] = None,
    ) -> DiscordMessage:
        tool = self._resolve_tool(self._SEND_FILE_CANDIDATES, required=False)
        if tool:
            encoded = base64.b64encode(bytes(data)).decode("ascii")
            camel = {
                "channelId": channel_id,
                "fileName": filename,
                "fileData": encoded,
                "message": content,
            }
            snake = {
                "channel_id": channel_id,
                "filename": filename,
                "file_data": encoded,
                "content": content,
            }
            if thread_id:
                camel["threadId"] = thread_id
                snake["thread_id"] = thread_id
            first, second = (camel, snake) if self._prefer_camel_case else (snake, camel)
            result = self._client.call_tool(tool, first)
            if not result.ok:
                result = self._client.call_tool(tool, second)
            if result.ok:
                return _message_from_file_result(
                    result,
                    channel_id=channel_id,
                    content=content,
                    thread_id=thread_id,
                    provider=self.name,
                )
            if not self._bot_token:
                raise ToolInvocationError(result.error or "send_attachment failed")
        if self._bot_token:
            return send_channel_attachment(
                token=self._bot_token,
                channel_id=channel_id,
                filename=filename,
                data=data,
                content=content,
                thread_id=thread_id,
            )
        raise ToolInvocationError(
            "live MCP catalog has no send_file/send_attachment/discord_send_file tool; "
            "refusing to base64-dump into send_message"
        )

    def get_message(self, channel_id: str, message_id: str) -> DiscordMessage:
        """Fetch one message. Falls back to recent-window read_messages only."""

        tool = self._resolve_tool(self._GET_MESSAGE_CANDIDATES, required=False)
        if tool:
            camel = {"channelId": channel_id, "messageId": message_id}
            snake = {"channel_id": channel_id, "message_id": message_id}
            first, second = (camel, snake) if self._prefer_camel_case else (snake, camel)
            result = self._client.call_tool(tool, first)
            if not result.ok:
                result = self._client.call_tool(tool, second)
            if result.ok:
                parsed = _parse_messages(result.content, channel_id=channel_id, provider=self.name)
                if parsed:
                    return parsed[0]
                if isinstance(result.raw, Mapping):
                    parsed = _parse_messages(result.raw, channel_id=channel_id, provider=self.name)
                    if parsed:
                        return parsed[0]
        found = None
        try:
            for msg in self.read_messages(channel_id, limit=50):
                if msg.message_id == message_id:
                    found = msg
                    break
        except ToolInvocationError:
            if not self._bot_token:
                raise
        if self._bot_token and (found is None or not found.attachments):
            return fetch_channel_message(
                token=self._bot_token,
                channel_id=channel_id,
                message_id=message_id,
            )
        if found is not None:
            return found
        raise ToolInvocationError(
            f"message {message_id!r} not found via get_message or recent read_messages window"
        )

    def download_attachment(
        self,
        channel_id: str,
        message_id: str,
        attachment_id: str,
    ) -> bytes:
        tool = self._resolve_tool(self._GET_ATTACHMENT_CANDIDATES, required=False)
        if tool:
            camel = {
                "channelId": channel_id,
                "messageId": message_id,
                "attachmentId": attachment_id,
            }
            snake = {
                "channel_id": channel_id,
                "message_id": message_id,
                "attachment_id": attachment_id,
            }
            first, second = (camel, snake) if self._prefer_camel_case else (snake, camel)
            result = self._client.call_tool(tool, first)
            if not result.ok:
                result = self._client.call_tool(tool, second)
            if result.ok:
                try:
                    return _decode_attachment_bytes(result.content)
                except ToolInvocationError:
                    if not self._bot_token:
                        raise
            elif not self._bot_token:
                raise ToolInvocationError(result.error or "download_attachment failed")
        elif not self._bot_token:
            raise ToolInvocationError(
                "live MCP catalog has no get_attachment/download_attachment tool; "
                "cannot fetch attachment bytes (fake provider still proves the protocol)"
            )
        return download_channel_attachment(
            token=self._bot_token,
            channel_id=channel_id,
            message_id=message_id,
            attachment_id=attachment_id,
        )

    def edit_message(self, channel_id: str, message_id: str, content: str) -> DiscordMessage:
        tool = self._resolve_tool(self._EDIT_CANDIDATES, required=False)
        if not tool:
            raise ToolInvocationError(
                "live MCP catalog has no edit_message/discord_edit_message tool"
            )
        attempts = [
            {"channelId": channel_id, "messageId": message_id, "newMessage": content},
            {"channelId": channel_id, "messageId": message_id, "content": content},
            {"channel_id": channel_id, "message_id": message_id, "content": content},
        ]
        result = self._client.call_tool(tool, attempts[0])
        for args in attempts[1:]:
            if result.ok:
                break
            result = self._client.call_tool(tool, args)
        if not result.ok:
            raise ToolInvocationError(result.error or "edit_message failed")
        return DiscordMessage(
            channel_id=channel_id,
            content=content,
            message_id=message_id,
            metadata={"provider": self.name, "tool": tool},
        )

    def delete_message(self, channel_id: str, message_id: str) -> None:
        tool = self._resolve_tool(self._DELETE_CANDIDATES, required=False)
        if not tool:
            raise ToolInvocationError(
                "live MCP catalog has no delete_message/discord_delete_message tool"
            )
        camel = {"channelId": channel_id, "messageId": message_id}
        snake = {"channel_id": channel_id, "message_id": message_id}
        first, second = (camel, snake) if self._prefer_camel_case else (snake, camel)
        result = self._client.call_tool(tool, first)
        if not result.ok:
            result = self._client.call_tool(tool, second)
        if not result.ok:
            raise ToolInvocationError(result.error or "delete_message failed")

    # Implemented by SaseQ / BrainDAO adapters
    name: str
    _client: MCPTransport

    def _resolve_tool(self, candidates: Sequence[str], *, required: bool = True) -> str: ...

    def read_messages(
        self,
        channel_id: str,
        *,
        limit: int = 20,
        thread_id: Optional[str] = None,
    ) -> Sequence[DiscordMessage]: ...


class SaseQDiscordProvider(AttachmentMCPOperations):
    """Adapter for SaseQ discord-mcp over HTTP or stdio transport."""

    name = "saseq"
    _prefer_camel_case = True

    # Candidate tool names discovered/normalized at runtime
    _SEND_CANDIDATES = ("send_message", "discord_send_message", "send-message")
    _READ_CANDIDATES = ("read_messages", "get_messages", "discord_read_messages")
    _THREAD_CANDIDATES = ("create_thread", "start_thread", "discord_create_thread")

    def __init__(self, client: MCPTransport, *, bot_token: str = "") -> None:
        self._client = client
        self._bot_token = bot_token
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
        attempts: list[dict[str, Any]] = [
            {"channelId": channel_id, "message": content},
            {"channelId": channel_id, "content": content},
            {"channel_id": channel_id, "content": content},
        ]
        if thread_id:
            attempts[0]["threadId"] = thread_id
            attempts[1]["threadId"] = thread_id
            attempts[2]["thread_id"] = thread_id
        result = self._client.call_tool(tool, attempts[0])
        for args in attempts[1:]:
            if result.ok:
                break
            result = self._client.call_tool(tool, args)
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
        attempts: list[dict[str, Any]] = [
            {"channelId": channel_id, "count": str(limit)},
            {"channelId": channel_id, "limit": limit},
        ]
        if thread_id:
            attempts[0]["threadId"] = thread_id
            attempts[1]["threadId"] = thread_id
        result = self._client.call_tool(tool, attempts[0])
        if not result.ok:
            result = self._client.call_tool(tool, attempts[1])
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


_SNOWFLAKE_RE = re.compile(r"(?<!\d)(\d{17,20})(?!\d)")
_JUMP_URL_RE = re.compile(r"/channels/(?:@me|\d{17,20})/\d{17,20}/(\d{17,20})")


def _as_snowflake(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if _SNOWFLAKE_RE.fullmatch(text):
        return text
    return None


def _explicit_snowflake(raw: Any) -> Optional[str]:
    if not isinstance(raw, Mapping):
        return None
    for key in ("messageId", "message_id", "id"):
        if key not in raw:
            continue
        found = _as_snowflake(raw[key])
        if found:
            return found
    for wrap in ("message", "result", "data"):
        inner = raw.get(wrap)
        if isinstance(inner, Mapping):
            found = _explicit_snowflake(inner)
            if found:
                return found
    return None


def _snowflake_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    jump = _JUMP_URL_RE.search(text)
    if jump:
        return jump.group(1)
    matches = _SNOWFLAKE_RE.findall(text)
    if not matches:
        return None
    return matches[-1]


def _dig_id(result: ToolInvocationResult) -> Optional[str]:
    """Extract a Discord snowflake. Never return an MCP markdown success blob."""

    raw = result.raw if isinstance(result.raw, Mapping) else {}
    found = _explicit_snowflake(raw)
    if found:
        return found
    content = result.content
    if isinstance(content, Mapping):
        found = _explicit_snowflake(content)
        if found:
            return found
    found = _snowflake_from_text(extract_text_content(content))
    if found:
        return found
    if raw:
        return _snowflake_from_text(extract_text_content(raw))
    return None


def _parse_attachments(item: Mapping[str, Any]) -> tuple[DiscordAttachment, ...]:
    raw = item.get("attachments") or item.get("attachment") or ()
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    out: list[DiscordAttachment] = []
    for att in raw:
        if not isinstance(att, Mapping):
            continue
        att_id = str(att.get("id") or att.get("attachmentId") or att.get("attachment_id") or "")
        filename = str(att.get("filename") or att.get("fileName") or att.get("name") or "")
        if not att_id and not filename:
            continue
        try:
            size = int(att.get("size") or att.get("fileSize") or 0)
        except (TypeError, ValueError):
            size = 0
        # Deliberately ignore att.get("url") — never a durable pointer.
        out.append(
            DiscordAttachment(
                attachment_id=att_id,
                filename=filename,
                size=size,
                content_type=str(att.get("content_type") or att.get("contentType") or ""),
                sha256=str(att.get("sha256") or ""),
            )
        )
    return tuple(out)


_SASEQ_DIGEST_ITEM = re.compile(
    r"- \(ID: (?P<id>\d+)\) \*\*\[(?P<author>[^\]]*)\]\*\* `[^`]*`: ```(?P<body>.*?)```",
    re.DOTALL,
)


def _parse_saseq_message_digest(text: str, *, channel_id: str, provider: str) -> list[DiscordMessage]:
    """Parse SaseQ's markdown read_messages digest into DiscordMessage rows."""

    blob = (text or "").strip()
    if len(blob) >= 2 and blob[0] == blob[-1] == '"':
        blob = blob[1:-1]
    blob = blob.replace("\\n", "\n")
    messages: list[DiscordMessage] = []
    for match in _SASEQ_DIGEST_ITEM.finditer(blob):
        messages.append(
            DiscordMessage(
                channel_id=channel_id,
                content=match.group("body").strip(),
                message_id=match.group("id"),
                author_id=match.group("author") or None,
                metadata={"provider": provider, "digest": True},
            )
        )
    return messages


def _looks_like_mcp_text_blocks(items: Sequence[Any]) -> bool:
    if not items:
        return False
    return all(
        isinstance(item, Mapping)
        and item.get("type") == "text"
        and "text" in item
        and not (item.get("id") or item.get("messageId") or item.get("message_id"))
        for item in items
    )


def _parse_messages(
    content: Any, *, channel_id: str, provider: str
) -> list[DiscordMessage]:
    items: list[Any]
    if isinstance(content, list) and _looks_like_mcp_text_blocks(content):
        digest = _parse_saseq_message_digest(
            extract_text_content(content), channel_id=channel_id, provider=provider
        )
        if digest:
            return digest
        items = content
    elif isinstance(content, list):
        items = content
    elif isinstance(content, Mapping) and "messages" in content:
        items = list(content["messages"])
    elif isinstance(content, Mapping) and (
        content.get("id") or content.get("messageId") or content.get("message_id")
    ):
        items = [content]
    else:
        # Unparsed MCP text is not an inbound task. A synthetic id would
        # bypass the listen watermark and dispatch the whole digest.
        return []
    messages: list[DiscordMessage] = []
    for idx, item in enumerate(items):
        if isinstance(item, Mapping):
            messages.append(
                DiscordMessage(
                    channel_id=str(item.get("channelId") or item.get("channel_id") or channel_id),
                    content=str(item.get("content") or item.get("text") or ""),
                    message_id=str(item.get("id") or item.get("messageId") or f"{provider}-{idx}"),
                    thread_id=(
                        str(item["threadId"])
                        if item.get("threadId")
                        else str(item["thread_id"])
                        if item.get("thread_id")
                        else None
                    ),
                    author_id=(
                        str(item["authorId"])
                        if item.get("authorId")
                        else str(item["author_id"])
                        if item.get("author_id")
                        else None
                    ),
                    attachments=_parse_attachments(item),
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


def _message_from_file_result(
    result: ToolInvocationResult,
    *,
    channel_id: str,
    content: str,
    thread_id: Optional[str],
    provider: str,
) -> DiscordMessage:
    parsed = _parse_messages(result.content, channel_id=channel_id, provider=provider)
    attachments: tuple[DiscordAttachment, ...] = ()
    message_id = _dig_id(result)
    if parsed:
        message_id = message_id or parsed[0].message_id
        attachments = parsed[0].attachments
        if parsed[0].thread_id and not thread_id:
            thread_id = parsed[0].thread_id
    raw = result.raw if isinstance(result.raw, Mapping) else {}
    if not attachments:
        attachments = _parse_attachments(raw)
    if not attachments and isinstance(result.content, Mapping):
        attachments = _parse_attachments(result.content)
    att_id = _dig(raw, "attachmentId", "attachment_id")
    if not attachments and att_id:
        attachments = (
            DiscordAttachment(
                attachment_id=str(att_id),
                filename=str(_dig(raw, "fileName", "filename") or ""),
                size=int(_dig(raw, "size") or 0),
            ),
        )
    return DiscordMessage(
        channel_id=channel_id,
        content=content,
        message_id=str(message_id or f"{provider}-file-{uuid4().hex[:12]}"),
        thread_id=thread_id,
        attachments=attachments,
        metadata={"provider": provider},
    )


def _decode_attachment_bytes(content: Any) -> bytes:
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    if isinstance(content, str):
        try:
            return base64.b64decode(content, validate=True)
        except (ValueError, TypeError) as exc:
            raise ToolInvocationError("attachment tool returned non-base64 text") from exc
    if isinstance(content, Mapping):
        for key in ("data", "fileData", "file_data", "bytes", "content", "text"):
            if key in content:
                return _decode_attachment_bytes(content[key])
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        for item in content:
            try:
                return _decode_attachment_bytes(item)
            except ToolInvocationError:
                continue
    raise ToolInvocationError("attachment tool returned no bytes")
