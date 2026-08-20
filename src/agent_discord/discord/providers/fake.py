"""Deterministic fake Discord MCP provider for tests (no network)."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from uuid import uuid4

from agent_discord.contracts import (
    DiscordAttachment,
    DiscordMessage,
    ToolDescriptor,
    ToolInvocationResult,
)
from agent_discord.discord.errors import ToolInvocationError


@dataclass
class FakeDiscordMCPProvider:
    name: str = "fake"
    tools: list[ToolDescriptor] = field(
        default_factory=lambda: [
            ToolDescriptor(name="send_message", description="Send a message"),
            ToolDescriptor(name="read_messages", description="Read messages"),
            ToolDescriptor(name="create_thread", description="Create a thread"),
            ToolDescriptor(name="send_file", description="Send a file attachment"),
            ToolDescriptor(name="get_message", description="Get a message by id"),
            ToolDescriptor(name="get_attachment", description="Download an attachment"),
            ToolDescriptor(name="edit_message", description="Edit a message"),
            ToolDescriptor(name="delete_message", description="Delete a message"),
        ]
    )
    sent: list[DiscordMessage] = field(default_factory=list)
    inbox: list[DiscordMessage] = field(default_factory=list)
    fail_tools: set[str] = field(default_factory=set)
    sampling_calls: list[Mapping[str, Any]] = field(default_factory=list)
    blobs: dict[str, bytes] = field(default_factory=dict)
    persist_dir: Optional[Path] = None

    def __post_init__(self) -> None:
        if self.persist_dir is not None:
            self.persist_dir = Path(self.persist_dir)
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            self._load_persist()

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
                    {
                        "id": m.message_id,
                        "content": m.content,
                        "channel_id": m.channel_id,
                        "attachments": [_attachment_payload(a) for a in m.attachments],
                    }
                    for m in msgs
                ],
            )
        if name in {"send_file", "send_attachment"}:
            raw = arguments.get("fileData") or arguments.get("file_data") or arguments.get("data") or b""
            data = _coerce_bytes(raw)
            msg = self.send_attachment(
                str(arguments.get("channel_id") or arguments.get("channelId") or ""),
                str(arguments.get("fileName") or arguments.get("filename") or "blob"),
                data,
                content=str(arguments.get("message") or arguments.get("content") or ""),
                thread_id=arguments.get("thread_id") or arguments.get("threadId"),
            )
            att = msg.attachments[0] if msg.attachments else None
            return ToolInvocationResult(
                name=name,
                ok=True,
                content={
                    "id": msg.message_id,
                    "attachments": [_attachment_payload(a) for a in msg.attachments],
                },
                raw={"id": msg.message_id, "attachment_id": att.attachment_id if att else ""},
            )
        if name == "get_message":
            msg = self.get_message(
                str(arguments.get("channel_id") or arguments.get("channelId") or ""),
                str(arguments.get("message_id") or arguments.get("messageId") or ""),
            )
            return ToolInvocationResult(
                name=name,
                ok=True,
                content={
                    "id": msg.message_id,
                    "content": msg.content,
                    "channel_id": msg.channel_id,
                    "attachments": [_attachment_payload(a) for a in msg.attachments],
                },
            )
        if name in {"edit_message", "discord_edit_message"}:
            msg = self.edit_message(
                str(arguments.get("channel_id") or arguments.get("channelId") or ""),
                str(arguments.get("message_id") or arguments.get("messageId") or ""),
                str(arguments.get("content") or ""),
            )
            return ToolInvocationResult(
                name=name, ok=True, content={"id": msg.message_id}, raw={"id": msg.message_id}
            )
        if name in {"delete_message", "discord_delete_message"}:
            self.delete_message(
                str(arguments.get("channel_id") or arguments.get("channelId") or ""),
                str(arguments.get("message_id") or arguments.get("messageId") or ""),
            )
            return ToolInvocationResult(name=name, ok=True, content={"ok": True})
        if name in {"get_attachment", "download_attachment"}:
            data = self.download_attachment(
                str(arguments.get("channel_id") or arguments.get("channelId") or ""),
                str(arguments.get("message_id") or arguments.get("messageId") or ""),
                str(arguments.get("attachment_id") or arguments.get("attachmentId") or ""),
            )
            return ToolInvocationResult(
                name=name,
                ok=True,
                content={"data": base64.b64encode(data).decode("ascii")},
            )
        return ToolInvocationResult(name=name, ok=True, content=dict(arguments))

    def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        thread_id: Optional[str] = None,
        components: Optional[list] = None,
    ) -> DiscordMessage:
        meta = {"provider": self.name}
        if components:
            meta["components"] = list(components)
        msg = DiscordMessage(
            channel_id=channel_id,
            content=content,
            message_id=f"fake-{uuid4().hex[:10]}",
            thread_id=thread_id,
            metadata=meta,
        )
        self.sent.append(msg)
        self._save_persist()
        return msg

    def send_attachment(
        self,
        channel_id: str,
        filename: str,
        data: bytes,
        *,
        content: str = "",
        thread_id: Optional[str] = None,
    ) -> DiscordMessage:
        attachment_id = f"att-{uuid4().hex[:10]}"
        payload = bytes(data)
        self.blobs[attachment_id] = payload
        att = DiscordAttachment(
            attachment_id=attachment_id,
            filename=filename,
            size=len(payload),
        )
        msg = DiscordMessage(
            channel_id=channel_id,
            content=content,
            message_id=f"fake-{uuid4().hex[:10]}",
            thread_id=thread_id,
            attachments=(att,),
            metadata={"provider": self.name},
        )
        self.sent.append(msg)
        self._save_persist()
        return msg

    def get_message(self, channel_id: str, message_id: str) -> DiscordMessage:
        for msg in (*self.sent, *self.inbox):
            if msg.message_id == message_id:
                return msg
        raise ToolInvocationError(f"message {message_id!r} not found")

    def edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str,
        *,
        components: Optional[list] = None,
    ) -> DiscordMessage:
        if "edit_message" in self.fail_tools:
            raise ToolInvocationError("forced failure")
        for collection in (self.sent, self.inbox):
            for index, msg in enumerate(collection):
                if msg.message_id != message_id:
                    continue
                meta = dict(msg.metadata)
                if components is not None:
                    meta["components"] = list(components)
                updated = DiscordMessage(
                    channel_id=msg.channel_id or channel_id,
                    content=content,
                    message_id=msg.message_id,
                    thread_id=msg.thread_id,
                    author_id=msg.author_id,
                    attachments=msg.attachments,
                    metadata=meta,
                )
                collection[index] = updated
                self._save_persist()
                return updated
        raise ToolInvocationError(f"message {message_id!r} not found")

    def delete_message(self, channel_id: str, message_id: str) -> None:
        if "delete_message" in self.fail_tools:
            raise ToolInvocationError("forced failure")
        before_sent = len(self.sent)
        before_inbox = len(self.inbox)
        self.sent = [m for m in self.sent if m.message_id != message_id]
        self.inbox = [m for m in self.inbox if m.message_id != message_id]
        if len(self.sent) == before_sent and len(self.inbox) == before_inbox:
            raise ToolInvocationError(f"message {message_id!r} not found")
        self._save_persist()

    def download_attachment(
        self,
        channel_id: str,
        message_id: str,
        attachment_id: str,
    ) -> bytes:
        msg = self.get_message(channel_id, message_id)
        ids = {a.attachment_id for a in msg.attachments}
        if attachment_id not in ids:
            raise ToolInvocationError(
                f"attachment {attachment_id!r} not found on message {message_id!r}"
            )
        if attachment_id not in self.blobs:
            raise ToolInvocationError(f"attachment {attachment_id!r} blob missing")
        return self.blobs[attachment_id]

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
        self._save_persist()
        return msg

    def handle_sampling_request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.sampling_calls.append(dict(payload))
        return {"ok": True, "provider": self.name, "echo": payload.get("messages", [])}

    def _load_persist(self) -> None:
        assert self.persist_dir is not None
        index = self.persist_dir / "messages.json"
        if index.is_file():
            raw = json.loads(index.read_text(encoding="utf-8"))
            if not self.sent:
                self.sent.extend(_messages_from_payload(raw.get("sent") or []))
            if not self.inbox:
                self.inbox.extend(_messages_from_payload(raw.get("inbox") or []))
        blobs_dir = self.persist_dir / "blobs"
        if blobs_dir.is_dir():
            for path in blobs_dir.iterdir():
                if path.is_file() and path.name not in self.blobs:
                    self.blobs[path.name] = path.read_bytes()

    def _save_persist(self) -> None:
        if self.persist_dir is None:
            return
        blobs_dir = self.persist_dir / "blobs"
        blobs_dir.mkdir(parents=True, exist_ok=True)
        for attachment_id, payload in self.blobs.items():
            (blobs_dir / attachment_id).write_bytes(payload)
        index = {
            "sent": [_message_payload(m) for m in self.sent],
            "inbox": [_message_payload(m) for m in self.inbox],
        }
        (self.persist_dir / "messages.json").write_text(
            json.dumps(index, indent=2) + "\n", encoding="utf-8"
        )


def _attachment_payload(att: DiscordAttachment) -> dict[str, Any]:
    return {
        "id": att.attachment_id,
        "filename": att.filename,
        "size": att.size,
        "content_type": att.content_type,
    }


def _message_payload(msg: DiscordMessage) -> dict[str, Any]:
    return {
        "channel_id": msg.channel_id,
        "content": msg.content,
        "message_id": msg.message_id,
        "thread_id": msg.thread_id,
        "author_id": msg.author_id,
        "attachments": [_attachment_payload(a) for a in msg.attachments],
        "metadata": dict(msg.metadata),
    }


def _messages_from_payload(items: Sequence[Any]) -> list[DiscordMessage]:
    out: list[DiscordMessage] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        attachments = []
        for raw in item.get("attachments") or ():
            if not isinstance(raw, Mapping):
                continue
            attachments.append(
                DiscordAttachment(
                    attachment_id=str(raw.get("id") or raw.get("attachment_id") or ""),
                    filename=str(raw.get("filename") or ""),
                    size=int(raw.get("size") or 0),
                    content_type=str(raw.get("content_type") or ""),
                )
            )
        out.append(
            DiscordMessage(
                channel_id=str(item.get("channel_id") or ""),
                content=str(item.get("content") or ""),
                message_id=str(item.get("message_id") or ""),
                thread_id=item.get("thread_id"),
                author_id=item.get("author_id"),
                attachments=tuple(attachments),
                metadata=dict(item.get("metadata") or {}),
            )
        )
    return out


def _coerce_bytes(raw: Any) -> bytes:
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        try:
            return base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            return raw.encode("utf-8")
    return b""
