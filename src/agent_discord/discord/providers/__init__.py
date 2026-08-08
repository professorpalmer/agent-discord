"""Provider adapters for external Discord MCP servers (no upstream source copied)."""

from __future__ import annotations

from typing import Any

from agent_discord.config import AppConfig
from agent_discord.discord.errors import ProviderSelectionError
from agent_discord.discord.providers.base import HttpJsonMCPClient, StdioMCPClient
from agent_discord.discord.providers.braindao import BrainDAODiscordProvider
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.discord.providers.saseq import SaseQDiscordProvider


def select_provider(config: AppConfig, *, client: Any | None = None):
    """Build a provider adapter from config. Inject `client` in tests."""
    name = config.discord_mcp_provider
    if name == "saseq":
        if client is None:
            client = _default_client(
                transport=config.discord_mcp_transport,
                http_url=config.saseq_mcp_http_url,
                stdio_command=config.discord_mcp_stdio_command,
                provider="saseq",
            )
        return SaseQDiscordProvider(client=client)
    if name == "braindao":
        if client is None:
            client = _default_client(
                transport=config.discord_mcp_transport,
                http_url=config.braindao_mcp_http_url,
                stdio_command=config.discord_mcp_stdio_command,
                provider="braindao",
            )
        return BrainDAODiscordProvider(client=client)
    raise ProviderSelectionError(f"unknown provider {name!r}")


def _default_client(
    *,
    transport: str,
    http_url: str,
    stdio_command: str,
    provider: str,
):
    if transport == "http":
        return HttpJsonMCPClient(base_url=http_url)
    if transport == "stdio":
        if not stdio_command.strip():
            raise ProviderSelectionError(
                f"DISCORD_MCP_STDIO_COMMAND is required for {provider} stdio transport; "
                "no default npm package is assumed. Set an explicit command or use "
                "DISCORD_MCP_TRANSPORT=http."
            )
        return StdioMCPClient(command=stdio_command)
    raise ProviderSelectionError(f"unknown transport {transport!r}")


__all__ = [
    "FakeDiscordMCPProvider",
    "SaseQDiscordProvider",
    "BrainDAODiscordProvider",
    "select_provider",
]
