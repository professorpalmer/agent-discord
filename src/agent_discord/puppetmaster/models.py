"""Pinned model constants — exact allowlist, no silent fallback."""

from __future__ import annotations

from agent_discord.contracts import ModelPin

CANONICAL_MODEL = "cursor/grok-4-5"
ADAPTER_NAME = "grok-4.5"

DEFAULT_MODEL_PIN = ModelPin(
    canonical=CANONICAL_MODEL,
    adapter_name=ADAPTER_NAME,
    allowlist=(CANONICAL_MODEL,),
)
