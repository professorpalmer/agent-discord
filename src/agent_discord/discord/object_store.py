"""Discord OS object store — artifacts as Discord attachments addressed by snowflake IDs.

CDN URLs expire (~24h). Durable keys are channel/message/attachment IDs.
Retrieve by re-fetching the message for a fresh signed handle, then downloading.
Not an unlimited public S3 / WebDav / DiscordFS clone.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from agent_discord.contracts import (
    DiscordObjectRef,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectTooLargeError,
)


DEFAULT_MAX_OBJECT_BYTES = 10_485_760  # 10 MiB; conservative vs Discord free ~10-25MB


class DiscordObjectStore:
    """Put/get/stat agent artifacts as Discord objects. Fail closed with typed errors."""

    def __init__(
        self,
        facade: Any,
        *,
        max_bytes: int = DEFAULT_MAX_OBJECT_BYTES,
        workspace: Optional[Path] = None,
    ) -> None:
        self.facade = facade
        self.max_bytes = max_bytes
        self.workspace = Path(workspace) if workspace is not None else None

    def put(
        self,
        data: bytes,
        *,
        channel_id: str,
        filename: str,
        kind: str,
        thread_id: Optional[str] = None,
        guild_id: Optional[str] = None,
        author_id: Optional[str] = None,
    ) -> DiscordObjectRef:
        if len(data) > self.max_bytes:
            raise ObjectTooLargeError(
                f"object is {len(data)} bytes; exceeds max_bytes={self.max_bytes}"
            )
        return self._put_bytes(
            data,
            channel_id=channel_id,
            filename=filename,
            kind=kind,
            thread_id=thread_id,
            guild_id=guild_id,
            author_id=author_id,
        )

    def put_or_overflow(
        self,
        data: bytes,
        *,
        channel_id: str,
        filename: str,
        kind: str,
        thread_id: Optional[str] = None,
        guild_id: Optional[str] = None,
        author_id: Optional[str] = None,
        external_url: Optional[str] = None,
    ) -> DiscordObjectRef:
        """Put bytes, or a small overflow pointer + local stash when over the cap."""

        if len(data) <= self.max_bytes:
            return self.put(
                data,
                channel_id=channel_id,
                filename=filename,
                kind=kind,
                thread_id=thread_id,
                guild_id=guild_id,
                author_id=author_id,
            )
        if self.workspace is None:
            raise ObjectTooLargeError(
                f"object is {len(data)} bytes; exceeds max_bytes={self.max_bytes}"
            )
        digest = hashlib.sha256(data).hexdigest()
        stash_rel = f"stash/{digest}"
        stash_path = self.workspace / stash_rel
        stash_path.parent.mkdir(parents=True, exist_ok=True)
        stash_path.write_bytes(data)
        pointer: dict[str, Any] = {
            "agent_discord_object": 1,
            "kind": "overflow",
            "filename": filename,
            "sha256": digest,
            "size": len(data),
            "local_stash": stash_rel,
        }
        if external_url:
            pointer["external_url"] = external_url
        payload = json.dumps(pointer, separators=(",", ":")).encode("utf-8")
        return self._put_bytes(
            payload,
            channel_id=channel_id,
            filename=f"{filename}.overflow.json",
            kind="overflow",
            thread_id=thread_id,
            guild_id=guild_id,
            author_id=author_id,
        )

    def _put_bytes(
        self,
        data: bytes,
        *,
        channel_id: str,
        filename: str,
        kind: str,
        thread_id: Optional[str] = None,
        guild_id: Optional[str] = None,
        author_id: Optional[str] = None,
    ) -> DiscordObjectRef:
        digest = hashlib.sha256(data).hexdigest()
        from agent_discord.orchestration.cards import object_card

        card = object_card(filename=filename, size=len(data), kind=kind)
        payload = card.v2_payload()
        msg = self.facade.send_attachment(
            channel_id,
            filename,
            data,
            content="",
            thread_id=thread_id,
            components=payload["components"],
            flags=payload["flags"],
        )
        if not msg.attachments:
            raise ObjectNotFoundError(
                "send_attachment returned a message with no attachment metadata"
            )
        att = msg.attachments[0]
        return DiscordObjectRef(
            channel_id=msg.channel_id or channel_id,
            message_id=msg.message_id,
            attachment_id=att.attachment_id,
            filename=att.filename or filename,
            kind=kind,
            size=att.size or len(data),
            sha256=digest,
            guild_id=guild_id,
            thread_id=msg.thread_id or thread_id,
            content_type=att.content_type,
        )

    def get(self, ref: DiscordObjectRef, *, channel_id: Optional[str] = None) -> bytes:
        caller_channel = channel_id if channel_id is not None else ref.channel_id
        if caller_channel != ref.channel_id:
            raise ObjectNotFoundError(
                f"channel_id mismatch: caller {caller_channel!r} != stored {ref.channel_id!r}"
            )
        msg = self._fresh_message(ref)
        try:
            data = self.facade.download_attachment(
                ref.channel_id, ref.message_id, ref.attachment_id
            )
        except ObjectNotFoundError:
            raise
        except Exception as exc:
            raise ObjectNotFoundError(
                f"attachment {ref.attachment_id!r} not found on message {ref.message_id!r}"
            ) from exc
        if not isinstance(data, (bytes, bytearray)):
            raise ObjectIntegrityError("download_attachment did not return bytes")
        payload = bytes(data)
        overflow = _as_overflow_pointer(payload) if ref.kind == "overflow" else None
        if overflow is not None:
            # Pointer bytes are the durable get() result; original sha256 lives inside JSON.
            _ = msg
            return payload
        if ref.sha256:
            digest = hashlib.sha256(payload).hexdigest()
            if digest != ref.sha256:
                raise ObjectIntegrityError(
                    f"sha256 mismatch: expected {ref.sha256}, got {digest}"
                )
        # Fresh handle came from get_message; never persist msg metadata URLs.
        _ = msg
        return payload

    def stat(self, ref: DiscordObjectRef) -> DiscordObjectRef:
        """Refresh pointer metadata from get_message. Still no durable URL."""

        msg = self._fresh_message(ref)
        att = None
        for item in msg.attachments:
            if item.attachment_id == ref.attachment_id:
                att = item
                break
        if att is None:
            raise ObjectNotFoundError(
                f"attachment {ref.attachment_id!r} not found on message {ref.message_id!r}"
            )
        return DiscordObjectRef(
            channel_id=msg.channel_id or ref.channel_id,
            message_id=msg.message_id or ref.message_id,
            attachment_id=att.attachment_id,
            filename=att.filename or ref.filename,
            kind=ref.kind,
            size=att.size or ref.size,
            sha256=att.sha256 or ref.sha256,
            guild_id=ref.guild_id,
            thread_id=msg.thread_id or ref.thread_id,
            content_type=att.content_type or ref.content_type,
        )

    def _fresh_message(self, ref: DiscordObjectRef):
        try:
            msg = self.facade.get_message(ref.channel_id, ref.message_id)
        except ObjectNotFoundError:
            raise
        except Exception as exc:
            raise ObjectNotFoundError(
                f"message {ref.message_id!r} not found in channel {ref.channel_id!r}"
            ) from exc
        if msg.channel_id and msg.channel_id != ref.channel_id:
            raise ObjectNotFoundError(
                f"channel_id mismatch: message in {msg.channel_id!r} != ref {ref.channel_id!r}"
            )
        att_ids = {a.attachment_id for a in msg.attachments}
        if ref.attachment_id and att_ids and ref.attachment_id not in att_ids:
            raise ObjectNotFoundError(
                f"attachment {ref.attachment_id!r} not found on message {ref.message_id!r}"
            )
        return msg


def _as_overflow_pointer(payload: bytes) -> Optional[dict[str, Any]]:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict) and parsed.get("kind") == "overflow":
        return parsed
    return None
