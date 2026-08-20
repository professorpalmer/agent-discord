"""Product-owned Discord/MCP facade: normalize providers, chunk, dedupe, Gateway."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from agent_discord.contracts import DiscordMessage, GatewayOwnerRegistry, ToolDescriptor, ToolInvocationResult
from agent_discord.discord.chunking import chunk_message
from agent_discord.discord.errors import MessageDedupError, ToolInvocationError
from agent_discord.discord.gateway import InMemoryGatewayOwnerRegistry

_CARD_PREFIX = "**Card**"
_RECEIPT_PREFIX = "**Receipt**"


class DiscordFacade:
    """Facade over a single MCP provider with Gateway exclusivity and dedupe."""

    def __init__(
        self,
        provider: Any,
        *,
        gateway: Optional[GatewayOwnerRegistry] = None,
        owner_id: str = "discord-os",
        bot_token_fingerprint: str = "",
        dedupe: bool = True,
    ) -> None:
        self.provider = provider
        self.gateway: GatewayOwnerRegistry = gateway or InMemoryGatewayOwnerRegistry()
        self.owner_id = owner_id
        self.bot_token_fingerprint = bot_token_fingerprint
        self.dedupe = dedupe
        self._seen_message_ids: set[str] = set()
        self._gateway_claimed = False

    def claim_gateway(self) -> None:
        if not self.bot_token_fingerprint:
            raise MessageDedupError("bot_token_fingerprint required to claim Gateway")
        self.gateway.claim(self.bot_token_fingerprint, self.owner_id)
        self._gateway_claimed = True

    def release_gateway(self) -> None:
        if self._gateway_claimed and self.bot_token_fingerprint:
            self.gateway.release(self.bot_token_fingerprint, self.owner_id)
            self._gateway_claimed = False

    def list_tools(self) -> Sequence[ToolDescriptor]:
        return self.provider.list_tools()

    def invoke_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolInvocationResult:
        return self.provider.invoke_tool(name, arguments)

    def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        thread_id: Optional[str] = None,
        chunk_limit: int = 2000,
        components: Optional[list] = None,
    ) -> list[DiscordMessage]:
        """Send content, chunking as needed; return all posted messages."""
        chunks = _chunks_for_inbound_skip(content, chunk_limit)
        posted: list[DiscordMessage] = []
        last = len(chunks) - 1
        for index, chunk in enumerate(chunks):
            kwargs: dict = {"thread_id": thread_id}
            if components is not None and index == last:
                kwargs["components"] = components
            try:
                msg = self.provider.send_message(channel_id, chunk, **kwargs)
            except TypeError:
                msg = self.provider.send_message(channel_id, chunk, thread_id=thread_id)
            self._remember_outbound(msg)
            posted.append(msg)
        return posted

    def read_messages(
        self,
        channel_id: str,
        *,
        limit: int = 20,
        thread_id: Optional[str] = None,
        skip_duplicates: bool = True,
    ) -> list[DiscordMessage]:
        messages = list(
            self.provider.read_messages(channel_id, limit=limit, thread_id=thread_id)
        )
        if not skip_duplicates or not self.dedupe:
            return messages
        fresh: list[DiscordMessage] = []
        for msg in messages:
            if msg.message_id and msg.message_id in self._seen_message_ids:
                continue
            if msg.message_id:
                self._seen_message_ids.add(msg.message_id)
            fresh.append(msg)
        return fresh

    def post_thread_task(
        self,
        channel_id: str,
        title: str,
        content: str,
    ) -> DiscordMessage:
        msg = self.provider.post_thread_task(channel_id, title, content)
        self._remember_outbound(msg)
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
        msg = self.provider.send_attachment(
            channel_id, filename, data, content=content, thread_id=thread_id
        )
        self._remember_outbound(msg)
        return msg

    def get_message(self, channel_id: str, message_id: str) -> DiscordMessage:
        """Fetch a message for a fresh attachment handle. Do not cache CDN URLs."""

        return self.provider.get_message(channel_id, message_id)

    def download_attachment(
        self,
        channel_id: str,
        message_id: str,
        attachment_id: str,
    ) -> bytes:
        return self.provider.download_attachment(channel_id, message_id, attachment_id)

    def edit_message(
        self,
        channel_id: str,
        message_id: str,
        content: str,
        *,
        components: Optional[list] = None,
    ) -> DiscordMessage:
        method = getattr(self.provider, "edit_message", None)
        if callable(method):
            try:
                msg = method(
                    channel_id,
                    message_id,
                    content,
                    components=components,
                )
            except TypeError:
                msg = method(channel_id, message_id, content)
            self._remember_outbound(msg)
            return msg
        result = self._invoke_first(
            ("edit_message", "discord_edit_message"),
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "content": content,
            },
        )
        if result is None:
            raise ToolInvocationError(
                "live MCP catalog has no edit_message/discord_edit_message tool"
            )
        return DiscordMessage(
            channel_id=channel_id,
            content=content,
            message_id=message_id,
            metadata={"tool": result.name},
        )

    def delete_message(self, channel_id: str, message_id: str) -> None:
        method = getattr(self.provider, "delete_message", None)
        if callable(method):
            method(channel_id, message_id)
            return
        result = self._invoke_first(
            ("delete_message", "discord_delete_message"),
            {"channel_id": channel_id, "message_id": message_id},
        )
        if result is None:
            raise ToolInvocationError(
                "live MCP catalog has no delete_message/discord_delete_message tool"
            )

    def _invoke_first(
        self,
        names: Sequence[str],
        arguments: Mapping[str, Any],
    ) -> Optional[ToolInvocationResult]:
        last_error = ""
        for name in names:
            result = self.invoke_tool(name, arguments)
            if result.ok:
                return result
            last_error = result.error or last_error
        if last_error:
            raise ToolInvocationError(last_error)
        return None

    def observe_message_id(self, message_id: str) -> None:
        """Register an inbound message id for process-local deduplication."""
        if not message_id:
            return
        if message_id in self._seen_message_ids and self.dedupe:
            raise MessageDedupError(f"duplicate message id {message_id!r}")
        self._seen_message_ids.add(message_id)

    def handle_sampling_request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Delegate BrainDAO sampling-compatible ingress when the provider supports it."""
        handler = getattr(self.provider, "handle_sampling_request", None)
        if handler is None:
            return {
                "ok": False,
                "error": "provider does not expose sampling ingress",
                "provider": getattr(self.provider, "name", "unknown"),
            }
        return handler(payload)

    def close(self) -> None:
        """Release provider resources such as a persistent stdio MCP process."""
        closer = getattr(self.provider, "close", None)
        if callable(closer):
            closer()

    def _remember_outbound(self, msg: DiscordMessage) -> None:
        if msg.message_id:
            self._seen_message_ids.add(msg.message_id)


def _chunks_for_inbound_skip(content: str, chunk_limit: int) -> list[str]:
    """Chunk text. Repeat Card/Receipt prefixes so listen skips every piece."""

    prefix = ""
    if content.startswith(_CARD_PREFIX):
        prefix = _CARD_PREFIX
    elif content.startswith(_RECEIPT_PREFIX):
        prefix = _RECEIPT_PREFIX
    chunks = chunk_message(content, limit=chunk_limit)
    if not prefix or len(chunks) <= 1:
        return chunks
    carry = f"{prefix}\n"
    room = max(1, chunk_limit - len(carry))
    out = [chunks[0]]
    for piece in chunks[1:]:
        if piece.startswith(prefix):
            out.append(piece)
            continue
        out.extend(f"{carry}{part}" for part in chunk_message(piece, limit=room))
    return out
