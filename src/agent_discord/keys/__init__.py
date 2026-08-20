"""Stdlib key vault and Discord connect absorb — ciphertext only, no YAML."""

from __future__ import annotations

from agent_discord.keys.vault import KeyVault, fingerprint_secret

__all__ = [
    "KeyVault",
    "fingerprint_secret",
]
