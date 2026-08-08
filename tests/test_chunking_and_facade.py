"""Message chunking, provider selection, gateway exclusivity, sampling seam."""

from __future__ import annotations

import pytest

from agent_discord.config import load_config
from agent_discord.contracts import DiscordMessage, ToolDescriptor
from agent_discord.discord.chunking import chunk_message
from agent_discord.discord.errors import (
    GatewayOwnershipError,
    MessageDedupError,
    ProviderSelectionError,
)
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.gateway import InMemoryGatewayOwnerRegistry
from agent_discord.discord.providers import select_provider
from agent_discord.discord.providers.braindao import BrainDAODiscordProvider
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.discord.providers.saseq import SaseQDiscordProvider


class RecordingClient:
    def __init__(self):
        self.calls = []
        self.tools = [
            ToolDescriptor(name="send_message"),
            ToolDescriptor(name="read_messages"),
            ToolDescriptor(name="create_thread"),
        ]

    def list_tools(self):
        return list(self.tools)

    def call_tool(self, name, arguments):
        from agent_discord.contracts import ToolInvocationResult

        self.calls.append((name, dict(arguments)))
        return ToolInvocationResult(
            name=name, ok=True, content={"id": "m1"}, raw={"id": "m1", "messageId": "m1"}
        )


def test_chunk_message_respects_limit():
    text = "alpha beta gamma " * 50
    chunks = chunk_message(text, limit=40)
    assert len(chunks) > 1
    assert all(len(c) <= 40 for c in chunks)
    assert "alpha" in chunks[0]
    assert "gamma" in "".join(chunks)


def test_chunk_empty():
    assert chunk_message("") == [""]


def test_facade_chunks_and_dedupes():
    fake = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake, bot_token_fingerprint="abc", owner_id="o1")
    long = "word " * 100
    posted = facade.send_message("ch", long, chunk_limit=30)
    assert len(posted) > 1
    assert len(fake.sent) == len(posted)

    fake.inbox.append(
        DiscordMessage(channel_id="ch", content="hi", message_id="dup-1")
    )
    first = facade.read_messages("ch")
    assert len(first) == 1
    second = facade.read_messages("ch")
    assert second == []

    with pytest.raises(MessageDedupError):
        facade.observe_message_id("dup-1")


def test_gateway_owner_exclusivity():
    reg = InMemoryGatewayOwnerRegistry()
    reg.claim("tok", "owner-a")
    with pytest.raises(GatewayOwnershipError):
        reg.claim("tok", "owner-b")
    assert reg.current_owner("tok") == "owner-a"
    reg.release("tok", "owner-a")
    reg.claim("tok", "owner-b")
    assert reg.current_owner("tok") == "owner-b"


def test_provider_selection_saseq_and_braindao(tmp_path):
    client = RecordingClient()
    cfg = load_config(
        env={
            "AGENT_DISCORD_WORKSPACE": str(tmp_path),
            "DISCORD_MCP_PROVIDER": "saseq",
            "DISCORD_BOT_TOKEN": "x",
        },
        dotenv_path=tmp_path / "none",
    )
    p = select_provider(cfg, client=client)
    assert isinstance(p, SaseQDiscordProvider)
    p.send_message("1", "hello")
    assert client.calls

    cfg2 = load_config(
        env={
            "AGENT_DISCORD_WORKSPACE": str(tmp_path),
            "DISCORD_MCP_PROVIDER": "braindao",
            "DISCORD_BOT_TOKEN": "x",
        },
        dotenv_path=tmp_path / "none",
    )
    client2 = RecordingClient()
    p2 = select_provider(cfg2, client=client2)
    assert isinstance(p2, BrainDAODiscordProvider)
    out = p2.handle_sampling_request(
        {"method": "sampling/createMessage", "messages": [{"content": "ping"}]}
    )
    assert out["ok"] is True
    assert "echo" in out


def test_saseq_stdio_requires_explicit_command(tmp_path):
    cfg = load_config(
        env={
            "AGENT_DISCORD_WORKSPACE": str(tmp_path),
            "DISCORD_MCP_PROVIDER": "saseq",
            "DISCORD_MCP_TRANSPORT": "stdio",
            "DISCORD_MCP_STDIO_COMMAND": "",
            "DISCORD_BOT_TOKEN": "x",
        },
        dotenv_path=tmp_path / "none",
    )
    with pytest.raises(ProviderSelectionError, match="DISCORD_MCP_STDIO_COMMAND"):
        select_provider(cfg)


def test_facade_sampling_seam():
    fake = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake, bot_token_fingerprint="t", owner_id="cli")
    result = facade.handle_sampling_request({"messages": ["hi"]})
    assert result["ok"] is True
    assert fake.sampling_calls
