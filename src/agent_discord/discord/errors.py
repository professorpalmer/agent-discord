"""Normalized Discord/MCP error types."""

from __future__ import annotations


class DiscordMCPError(Exception):
    """Base error for Discord MCP facade operations."""


class ProviderSelectionError(DiscordMCPError):
    """Unknown or unavailable provider."""


class ToolInvocationError(DiscordMCPError):
    """Remote tool call failed or returned an error payload."""


class MessageDedupError(DiscordMCPError):
    """Duplicate Discord message ID rejected."""


class GatewayOwnershipError(DiscordMCPError):
    """Another owner already holds the Gateway for this bot token."""


class ChunkingError(DiscordMCPError):
    """Message content could not be chunked safely."""
