"""Gateway owner exclusivity: one active owner per bot token."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING, Optional

from agent_discord.discord.errors import GatewayOwnershipError

if TYPE_CHECKING:
    from agent_discord.persistence.sqlite import SQLiteStore


class InMemoryGatewayOwnerRegistry:
    """Process-local registry enforcing one Gateway owner per token fingerprint."""

    def __init__(self) -> None:
        self._owners: dict[str, str] = {}
        self._lock = Lock()

    def claim(self, bot_token_fingerprint: str, owner_id: str) -> None:
        if not bot_token_fingerprint:
            raise GatewayOwnershipError("bot_token_fingerprint is required")
        if not owner_id:
            raise GatewayOwnershipError("owner_id is required")
        with self._lock:
            current = self._owners.get(bot_token_fingerprint)
            if current is not None and current != owner_id:
                raise GatewayOwnershipError(
                    f"token {bot_token_fingerprint} already owned by {current!r}; "
                    f"refusing claim by {owner_id!r}"
                )
            self._owners[bot_token_fingerprint] = owner_id

    def release(self, bot_token_fingerprint: str, owner_id: str) -> None:
        with self._lock:
            current = self._owners.get(bot_token_fingerprint)
            if current is None:
                return
            if current != owner_id:
                raise GatewayOwnershipError(
                    f"token {bot_token_fingerprint} owned by {current!r}; "
                    f"cannot release as {owner_id!r}"
                )
            del self._owners[bot_token_fingerprint]

    def current_owner(self, bot_token_fingerprint: str) -> Optional[str]:
        with self._lock:
            return self._owners.get(bot_token_fingerprint)


class SqliteGatewayOwnerRegistry:
    """Durable Gateway ownership across concurrent local processes via SQLite."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def claim(self, bot_token_fingerprint: str, owner_id: str) -> None:
        self._store.claim_gateway(bot_token_fingerprint, owner_id)

    def release(self, bot_token_fingerprint: str, owner_id: str) -> None:
        self._store.release_gateway(bot_token_fingerprint, owner_id)

    def current_owner(self, bot_token_fingerprint: str) -> Optional[str]:
        return self._store.gateway_owner(bot_token_fingerprint)
