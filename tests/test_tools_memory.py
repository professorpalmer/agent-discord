"""Host CLI tools and Discord think-tank memory."""

from __future__ import annotations

import json
from pathlib import Path

from agent_discord.contracts import DiscordMessage, TaskIntake
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.host.memory import (
    bind_memory_channel,
    channel_is_memory,
    memory_channel_ids,
    recall_think_tank,
    settle_think_tank,
)
from agent_discord.host.tools import load_host_tools, tools_reach_block
from agent_discord.host.wiki import wiki_query
from agent_discord.orchestration.cards import is_harness_message, note_card
from agent_discord.orchestration.listen import drain_inbound
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend


def test_tools_catalog_from_env(tmp_path: Path):
    env = {
        "DISCORD_OS_TOOLS": json.dumps(
            {"custom": {"bin": str(tmp_path / "missing"), "hint": "my wrapper"}}
        ),
        "WIKI_BASE_URL": "http://127.0.0.1:9",
        "WIKI_OWNER_TOKEN": "tok",
    }
    tools = load_host_tools(env=env)
    names = {item.name: item for item in tools}
    assert names["wiki"].ready
    assert names["custom"].hint == "my wrapper"
    block = tools_reach_block(tools)
    assert "not MCP" in block
    assert "discord-os wiki query" in block
    assert "wiki (http)" in block


def test_wiki_query_uses_http_not_mcp():
    seen = []

    def opener(request, timeout=30):
        seen.append((request.get_method(), request.full_url, request.get_header("Authorization")))

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"answer": "Discord OS is the remote.", "citations": []}).encode()

        return _Resp()

    payload = wiki_query(
        "what is Discord OS?",
        env={"WIKI_BASE_URL": "http://wiki.test", "WIKI_OWNER_TOKEN": "secret"},
        opener=opener,
    )
    assert payload["answer"].startswith("Discord OS")
    assert seen[0][0] == "POST"
    assert seen[0][1] == "http://wiki.test/wiki/query"
    assert seen[0][2] == "Bearer secret"


def test_note_card_is_harness_not_a_task():
    card = note_card("staff decided bind memory is the bank")
    assert card.kind == "NOTE"
    assert is_harness_message(card.text, None, card.v2_components())


def test_bind_memory_and_recall(tmp_path: Path):
    store = SQLiteStore(tmp_path / "m.sqlite3")
    store.initialize()
    bind_memory_channel(store, workspace_id="ws", channel_id="tank")
    assert channel_is_memory(store, "tank", workspace_id="ws")
    assert "tank" in memory_channel_ids(store, workspace_id="ws")
    fake = FakeDiscordMCPProvider()
    fake.inbox.append(
        DiscordMessage(channel_id="tank", content="we shipped discord os 0.5.3", message_id="1")
    )
    fake.inbox.append(
        DiscordMessage(channel_id="tank", content="**Card** Working", message_id="2")
    )
    discord = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="t")
    text = recall_think_tank(discord, store, "shipped", workspace_id="ws")
    assert "0.5.3" in text
    assert "Working" not in text
    store.close()


def test_settle_writes_to_other_bank_not_origin(tmp_path: Path):
    store = SQLiteStore(tmp_path / "s.sqlite3")
    store.initialize()
    bind_memory_channel(store, workspace_id="ws", channel_id="tank")
    fake = FakeDiscordMCPProvider()
    discord = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="t")
    posted = settle_think_tank(
        discord,
        store,
        workspace_id="ws",
        origin_channel="pm",
        summary="Finished the wiki CLI.",
    )
    assert posted == ["tank"]
    blob = json.dumps([getattr(msg, "metadata", {}) for msg in fake.sent])
    assert "Finished the wiki CLI" in blob
    store.close()


def test_bind_memory_message(tmp_path: Path):
    store = SQLiteStore(tmp_path / "b.sqlite3")
    store.initialize()
    fake = FakeDiscordMCPProvider()
    orch = AgentOrchestrator(
        store=store,
        backend=FakePuppetmasterBackend(),
        discord=DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="t"),
        post_progress_to_discord=True,
        host_repos=(),
    )
    store.set_host_control("tank", armed=True)
    fake.inbox.append(
        DiscordMessage(
            channel_id="tank",
            content="bind memory",
            message_id="33",
            author_id="human-1",
        )
    )
    drain_inbound(orch, orch.discord, channel_id="tank", workspace_id="ws", since_ms=0)
    assert channel_is_memory(store, "tank", workspace_id="ws")
    store.close()


def test_run_injects_think_tank_and_host_tools(tmp_path: Path):
    store = SQLiteStore(tmp_path / "r.sqlite3")
    store.initialize()
    bind_memory_channel(store, workspace_id="ws", channel_id="tank")
    fake = FakeDiscordMCPProvider()
    fake.inbox.append(
        DiscordMessage(channel_id="tank", content="wiki is the personal graph", message_id="7")
    )
    backend = FakePuppetmasterBackend()
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        discord=DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="t"),
        post_progress_to_discord=False,
        host_repos=(),
    )
    orch.run_task(TaskIntake(text="what is the wiki?", channel_id="pm", workspace_id="ws"))
    assert backend.last_request is not None
    reach = backend.last_request.metadata["host_reach"]
    assert "not MCP" in reach
    assert "discord-os wiki query" in reach
    assert "Think-tank" in reach
    memories = backend.last_request.context.memories
    tank = [item for item in memories if item.get("source") == "think-tank"]
    assert tank
    assert "personal graph" in tank[0]["content"]
    store.close()
