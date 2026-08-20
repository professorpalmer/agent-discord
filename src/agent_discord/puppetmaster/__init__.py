"""Puppetmaster backend boundary with pinned Cursor or agentic model."""

from __future__ import annotations

from agent_discord.puppetmaster.agentic import AgenticPuppetmasterBackend
from agent_discord.puppetmaster.backend import PuppetmasterCliBackend
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend
from agent_discord.puppetmaster.models import (
    AGENTIC_CANONICAL_MODEL,
    AGENTIC_MODEL_PIN,
    CANONICAL_MODEL,
    DEFAULT_MODEL_PIN,
)

__all__ = [
    "AGENTIC_CANONICAL_MODEL",
    "AGENTIC_MODEL_PIN",
    "AgenticPuppetmasterBackend",
    "CANONICAL_MODEL",
    "DEFAULT_MODEL_PIN",
    "FakePuppetmasterBackend",
    "PuppetmasterCliBackend",
]
