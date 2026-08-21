"""SQLite memory, provenance, events, dedupe, gateway."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent_discord.contracts import EventKind
from agent_discord.discord.errors import GatewayOwnershipError
from agent_discord.discord.gateway import SqliteGatewayOwnerRegistry
from agent_discord.persistence.sqlite import PREFERENCE_KINDS, SQLiteStore


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


def test_preference_set_get_list(tmp_path: Path):
    store = SQLiteStore(tmp_path / "prefs.sqlite3")
    store.initialize()
    store.set_preference("ws", "concise", "true")
    store.set_preference("ws", "formatter", "black", kind="style")
    assert store.get_preference("ws", "concise") == "true"
    assert store.get_preference("ws", "formatter") == "black"
    assert store.get_preference("ws", "missing") is None
    assert store.get_preference("other", "concise") is None

    listed = store.list_preferences("ws")
    keys = {row["key"] for row in listed}
    assert keys == {"concise", "formatter"}
    kinds = {row["kind"] for row in listed}
    assert kinds == {"preference", "style"}

    styles = store.list_preferences("ws", kind="style")
    assert len(styles) == 1
    assert styles[0]["key"] == "formatter"
    assert styles[0]["value"] == "black"
    assert styles[0]["kind"] == "style"

    store.set_preference("ws", "concise", "false")
    assert store.get_preference("ws", "concise") == "false"
    store.close()


def test_preference_failure_record_and_list(tmp_path: Path):
    store = SQLiteStore(tmp_path / "fail.sqlite3")
    store.initialize()
    store.record_failure("ws", "lint", "ImportError: missing module")
    store.record_failure("ws", "deploy", "timeout after 30s")
    store.set_preference("ws", "concise", "true")

    failures = store.list_failures("ws", limit=8)
    assert len(failures) == 2
    assert all(row["kind"] == "failure" for row in failures)
    keys = {row["key"] for row in failures}
    assert keys == {"lint", "deploy"}
    assert store.list_failures("ws", limit=1)[0]["key"] in keys
    store.close()


def test_preference_redaction(tmp_path: Path):
    store = SQLiteStore(tmp_path / "redact.sqlite3")
    store.initialize()
    store.set_preference("ws", "note", "keep <thinking>SECRET</thinking>")
    stored = store.get_preference("ws", "note")
    assert stored == "keep [redacted]"
    assert stored is not None and "SECRET" not in stored

    store.record_failure("ws", "fp1", "boom <thinking>SECRET</thinking>")
    failures = store.list_failures("ws")
    assert failures[0]["value"] == "boom [redacted]"
    assert "SECRET" not in failures[0]["value"]

    block = store.prompt_memory_block("ws")
    assert "SECRET" not in block
    assert "[redacted]" in block
    store.close()


def test_prompt_memory_block_is_short_and_capped(tmp_path: Path):
    store = SQLiteStore(tmp_path / "prompt.sqlite3")
    store.initialize()
    assert store.prompt_memory_block("ws") == ""
    store.set_preference("ws", "concise", "true")
    store.set_preference("ws", "formatter", "black", kind="style")
    store.record_failure("ws", "lint", "ImportError")
    block = store.prompt_memory_block("ws")
    assert "[preferences]" in block
    assert "concise=true" in block
    assert "[style]" in block
    assert "formatter=black" in block
    assert "[failures]" in block
    assert "lint: ImportError" in block

    for index in range(20):
        store.record_failure("ws", f"fp-{index}", "x" * 400)
    capped = store.prompt_memory_block("ws")
    assert len(capped) <= 1500
    store.close()


def test_preferences_migrate_on_existing_db(tmp_path: Path):
    db = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS workspace_bindings (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            guild_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(workspace_id, channel_id)
        );
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            path TEXT NOT NULL DEFAULT '',
            provenance_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(db)
    store.initialize()
    cols = {
        row["name"]
        for row in store._connection().execute("PRAGMA table_info(preferences)").fetchall()
    }
    assert {"workspace_id", "key", "value", "kind", "updated_at"} <= cols
    store.set_preference("ws", "concise", "true")
    assert store.get_preference("ws", "concise") == "true"
    store.close()


def test_preferences_migrate_adds_missing_columns(tmp_path: Path):
    db = tmp_path / "partial.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE preferences (
            workspace_id TEXT,
            key TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(db)
    store.initialize()
    store.set_preference("ws", "formatter", "black", kind="style")
    assert store.get_preference("ws", "formatter") == "black"
    styles = store.list_preferences("ws", kind="style")
    assert len(styles) == 1
    assert styles[0]["kind"] == "style"
    store.close()


def test_preference_rejects_unknown_kind(tmp_path: Path):
    store = SQLiteStore(tmp_path / "kind.sqlite3")
    store.initialize()
    with pytest.raises(ValueError):
        store.set_preference("ws", "x", "y", kind="unknown")
    assert PREFERENCE_KINDS == frozenset({"preference", "style", "failure"})
    store.close()
