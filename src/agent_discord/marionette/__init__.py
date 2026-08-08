"""Optional Marionette HTTP backend adapter (explicit opt-in; not the default)."""

from agent_discord.marionette.backend import (
    MarionetteBackend,
    MarionetteConfigError,
    MarionetteEndpointConfig,
    MarionetteTransportError,
)
from agent_discord.marionette.fake import FakeMarionetteTransport

__all__ = [
    "FakeMarionetteTransport",
    "MarionetteBackend",
    "MarionetteConfigError",
    "MarionetteEndpointConfig",
    "MarionetteTransportError",
]
