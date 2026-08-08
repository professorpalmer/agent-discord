"""Puppetmaster backend boundary with pinned Cursor model."""

from agent_discord.puppetmaster.backend import PuppetmasterCliBackend
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend
from agent_discord.puppetmaster.models import CANONICAL_MODEL, DEFAULT_MODEL_PIN

__all__ = [
    "CANONICAL_MODEL",
    "DEFAULT_MODEL_PIN",
    "FakePuppetmasterBackend",
    "PuppetmasterCliBackend",
]
