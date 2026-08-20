"""SQLite memory, provenance, events, dedupe, gateway."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_discord.contracts import EventKind
from agent_discord.discord.errors import GatewayOwnershipError
from agent_discord.discord.gateway import SqliteGatewayOwnerRegistry
from agent_discord.persistence.sqlite import SQLiteStore


def test_memory_provenance_and_recall(tmp_path: Path):
    store = SQLiteStore(tmp_path / "t.sqlite3")
    store.initialize()
    mid = store.remember(
        workspace_id="ws",
        channel_id="ch1",
        content="deploy the staging bot tonight",
        source="test",
        provenance={"message_id": "m1", "author": "user"},
    )
    assert mid
    hits = store.recall(workspace_id="ws", channel_id="ch1", query="staging bot", limit=5)
    assert hits
    assert hits[0]["provenance"]["message_id"] == "m1"
    assert hits[0]["source"] == "test"
    store.close()


def test_event_append_strips_chain_of_thought_recursively(tmp_path: Path):
    store = SQLiteStore(tmp_path / "e.sqlite3")
    store.initialize()
    store.create_task(
        task_id="t1",
        workspace_id="ws",
        channel_id="c",
        intake_text="hi",
    )
    store.create_run(run_id="r1", task_id="t1", model="cursor/grok-4-5", adapter_name="grok-4.5")
    store.append_event(
        task_id="t1",
        run_id="r1",
        kind=EventKind.PROGRESS,
        summary="<thinking>SECRET</thinking>",
        payload={
            "stage": "work",
            "chain_of_thought": "SECRET",
            "hidden_cot": "nope",
            "nested": {
                "ok": True,
                "reasoning_content": "nested-secret",
                "items": [{"cot": "deep", "value": 1}],
            },
        },
        source="test",
        provenance={"component": "unit", "private_reasoning": "no"},
    )
    events = store.list_events("r1")
    assert len(events) == 1
    assert events[0]["summary"] == "[redacted]"
    payload = events[0]["payload"]
    assert "chain_of_thought" not in payload
    assert "hidden_cot" not in payload
    assert payload["nested"]["ok"] is True
    assert "reasoning_content" not in payload["nested"]
    assert payload["nested"]["items"][0]["value"] == 1
    assert "cot" not in payload["nested"]["items"][0]
    assert "private_reasoning" not in events[0]["provenance"]
    assert events[0]["provenance"]["component"] == "unit"
    store.close()


def test_message_dedupe_persistence(tmp_path: Path):
    store = SQLiteStore(tmp_path / "d.sqlite3")
    store.initialize()
    assert store.mark_message_seen("msg-1", "ch") is True
    assert store.mark_message_seen("msg-1", "ch") is False
    store.close()


def test_inbound_message_linkage(tmp_path: Path):
    store = SQLiteStore(tmp_path / "link.sqlite3")
    store.initialize()
    assert store.claim_inbound_message("m-9", "ch") is True
    store.bind_inbound_message("m-9", task_id="t9", run_id="r9", channel_id="ch")
    row = store.get_inbound_message("m-9")
    assert row is not None
    assert row["task_id"] == "t9"
    assert row["run_id"] == "r9"
    assert store.claim_inbound_message("m-9", "ch") is False
    store.close()


def test_listen_watermark_seed_and_set(tmp_path: Path):
    store = SQLiteStore(tmp_path / "wm.sqlite3")
    store.initialize()
    first = store.seed_listen_watermark("ch", 1_750_000_000_000)
    assert first["last_created_ms"] == 1_750_000_000_000
    assert first["last_message_id"] == ""
    again = store.seed_listen_watermark("ch", 1_760_000_000_000)
    assert again["last_created_ms"] == 1_750_000_000_000
    store.set_listen_watermark("ch", created_ms=1_750_000_001_000, message_id="1400123456789012345")
    row = store.get_listen_watermark("ch")
    assert row is not None
    assert row["last_created_ms"] == 1_750_000_001_000
    assert row["last_message_id"] == "1400123456789012345"
    store.close()


def test_sqlite_gateway_ownership_across_registries(tmp_path: Path):
    db = tmp_path / "gw.sqlite3"
    store_a = SQLiteStore(db)
    store_a.initialize()
    store_b = SQLiteStore(db)
    store_b.initialize()

    reg_a = SqliteGatewayOwnerRegistry(store_a)
    reg_b = SqliteGatewayOwnerRegistry(store_b)

    reg_a.claim("tokfp", "owner-a")
    with pytest.raises(GatewayOwnershipError):
        reg_b.claim("tokfp", "owner-b")
    assert reg_b.current_owner("tokfp") == "owner-a"
    with pytest.raises(GatewayOwnershipError):
        reg_b.release("tokfp", "owner-b")
    reg_a.release("tokfp", "owner-a")
    reg_b.claim("tokfp", "owner-b")
    assert reg_a.current_owner("tokfp") == "owner-b"
    store_a.close()
    store_b.close()


def test_sqlite_gateway_steal_dead_cli_owner(tmp_path: Path):
    store = SQLiteStore(tmp_path / "gw-dead.sqlite3")
    store.initialize()
    store.claim_gateway("tokfp", "agent-discord-cli-99999999-dead0001")
    store.claim_gateway("tokfp", "agent-discord-cli-88888888-next0001")
    assert store.gateway_owner("tokfp") == "agent-discord-cli-88888888-next0001"
    store.claim_gateway("tokfp2", "discord-os-cli-99999999-dead0001")
    store.claim_gateway("tokfp2", "discord-os-cli-88888888-next0001")
    assert store.gateway_owner("tokfp2") == "discord-os-cli-88888888-next0001"
    store.close()
