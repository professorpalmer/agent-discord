"""Hermetic Discord OS connect, vault, agentic, and overflow tests."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from agent_discord.cli import main
from agent_discord.config import check_config, load_config, resolve_compute
from agent_discord.contracts import (
    ContextSnapshot,
    DiscordMessage,
    DispatchRequest,
    ObjectTooLargeError,
    TaskStatus,
)
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.object_store import DiscordObjectStore
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.keys.connect import (
    handle_connect_message,
    mint_pairing_ticket,
    parse_connect_command,
)
from agent_discord.keys.vault import KeyVault
from agent_discord.orchestration.cards import CARD_PREFIX
from agent_discord.orchestration.listen import (
    DISCORD_EPOCH_MS,
    LISTEN_HISTORY_SLACK_MS,
    drain_inbound,
    should_dispatch_inbound,
    snowflake_created_ms,
)
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.agentic import AgenticPuppetmasterBackend
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend
from agent_discord.puppetmaster.models import AGENTIC_CANONICAL_MODEL, AGENTIC_MODEL_PIN


FAKE_KEY = "sk-test-not-a-real-key-wxyz"


def _request() -> DispatchRequest:
    return DispatchRequest(
        task_id="t1",
        run_id="r1",
        prompt="hello world",
        model=AGENTIC_CANONICAL_MODEL,
        context=ContextSnapshot(task_id="t1", memories=[], bindings={}),
        metadata={"channel_id": "99"},
    )


def test_vault_roundtrip_and_public_fingerprint_only(tmp_path: Path):
    vault = KeyVault(tmp_path / "keys")
    public = vault.put("openrouter", FAKE_KEY, "env")
    assert public["fingerprint"] == "wxyz"
    assert public["source"] == "env"
    assert vault.get("openrouter") == FAKE_KEY
    assert vault.fingerprint("openrouter") == "wxyz"
    listed = vault.list_public()
    assert listed == [
        {
            "provider": "openrouter",
            "source": "env",
            "fingerprint": "wxyz",
            "created_at": public["created_at"],
        }
    ]
    raw = json.loads((tmp_path / "keys" / "vault.json").read_text(encoding="utf-8"))
    dumped = json.dumps(raw)
    assert FAKE_KEY not in dumped
    assert "sk-test" not in dumped
    assert "ciphertext" in dumped


def test_parse_connect_shred_vs_ticket():
    shred = parse_connect_command(f"/connect {FAKE_KEY}")
    assert shred.secret == FAKE_KEY
    assert shred.provider == "openrouter"
    bang = parse_connect_command(f"!connect {FAKE_KEY}")
    assert bang.secret == FAKE_KEY
    ticket = parse_connect_command("/connect")
    assert ticket.secret is None
    named = parse_connect_command("/connect openrouter")
    assert named.secret is None
    assert named.provider == "openrouter"


def test_shred_intercept_deletes_and_does_not_dispatch(tmp_path: Path):
    store = SQLiteStore(tmp_path / "c.sqlite3")
    store.initialize()
    fake = FakeDiscordMCPProvider()
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content=f"/connect {FAKE_KEY}",
            message_id="conn-1",
            author_id="human-1",
        )
    )
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    orch = AgentOrchestrator(
        store=store,
        backend=FakePuppetmasterBackend(),
        discord=facade,
        workspace=tmp_path,
    )
    receipts = drain_inbound(
        orch, facade, channel_id="ch", workspace_id="ws", workspace=tmp_path
    )
    assert receipts == []
    assert all(m.message_id != "conn-1" for m in fake.inbox)
    cards = [m.content for m in fake.sent]
    assert any(c.startswith(f"{CARD_PREFIX} CONNECT") for c in cards)
    assert all(FAKE_KEY not in c for c in cards)
    vault = KeyVault(tmp_path / "keys")
    assert vault.fingerprint("openrouter") == "wxyz"
    assert vault.get("openrouter") == FAKE_KEY
    store.close()


def test_shred_fail_closed_when_delete_fails(tmp_path: Path):
    store = SQLiteStore(tmp_path / "fail.sqlite3")
    store.initialize()
    fake = FakeDiscordMCPProvider()
    fake.fail_tools.add("delete_message")
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content=f"/connect {FAKE_KEY}",
            message_id="conn-fail",
        )
    )
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    orch = AgentOrchestrator(
        store=store,
        backend=FakePuppetmasterBackend(),
        discord=facade,
        workspace=tmp_path,
    )
    receipts = drain_inbound(
        orch, facade, channel_id="ch", workspace_id="ws", workspace=tmp_path
    )
    assert receipts == []
    vault = KeyVault(tmp_path / "keys")
    assert vault.get("openrouter") is None
    assert FAKE_KEY not in json.dumps([asdict(m) for m in fake.sent])
    store.close()


def test_ticket_connect_cli(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / ".agent-discord"
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUPPETMASTER_MODEL", "cursor/grok-4-5")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    ticket = mint_pairing_ticket(ws, provider="openrouter")
    monkeypatch.setattr("sys.stdin", StringIO(FAKE_KEY + "\n"))
    assert main(["connect", "--ticket", ticket, "--provider", "openrouter", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "openrouter"
    assert payload["source"] == "ticket"
    assert payload["fingerprint"] == "wxyz"
    assert FAKE_KEY not in json.dumps(payload)
    assert KeyVault(ws / "keys").get("openrouter") == FAKE_KEY


def test_connect_from_env_and_status(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / ".agent-discord"
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUPPETMASTER_MODEL", "cursor/grok-4-5")
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("AGENT_DISCORD_COMPUTE", "auto")
    assert main(["connect", "--provider", "openrouter", "--from-env", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "env"
    assert payload["fingerprint"] == "wxyz"
    assert FAKE_KEY not in json.dumps(payload)
    assert main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["compute_resolved"] == "agentic"
    assert status["model"] == AGENTIC_CANONICAL_MODEL
    assert status["discord_max_object_bytes"] == 10_485_760
    assert any(item["fingerprint"] == "wxyz" for item in status["providers"])
    assert status["mcp_url"] == "https://discord.com/api/v10"
    assert status["discord_mcp_provider"] == "rest"
    assert status["discord_token_source"] == "env"
    assert status["host_actions"] is True
    assert status["interactions"] == "off"
    assert status["product"] == "Discord OS"
    assert status["cli"] == "discord-os"
    assert FAKE_KEY not in json.dumps(status)
    dumped = json.dumps(status)
    assert "test-token" not in dumped
    assert FAKE_KEY not in dumped


def test_status_json_discord_token_source_host_file_and_empty(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / ".agent-discord"
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.setenv("PUPPETMASTER_MODEL", "cursor/grok-4-5")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    host = tmp_path / "host.discord_token"
    host.write_text("host-bot-token-value\n", encoding="utf-8")
    monkeypatch.setattr("agent_discord.config.DEFAULT_HOST_BOT_TOKEN_PATH", host)
    assert main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["discord_token_source"] == "host-file"
    dumped = json.dumps(status)
    assert "host-bot-token-value" not in dumped
    assert "xxx.yyy.zzz" not in dumped

    monkeypatch.setattr("agent_discord.config.DEFAULT_HOST_BOT_TOKEN_PATH", tmp_path / "missing.token")
    assert main(["status", "--json"]) == 0
    empty = json.loads(capsys.readouterr().out)
    assert empty["discord_token_source"] == "empty"


def test_inherit_connect_message_uses_env(tmp_path: Path):
    result = handle_connect_message(
        "/connect",
        workspace=tmp_path,
        env={"OPENROUTER_API_KEY": FAKE_KEY},
    )
    assert result.action == "inherit"
    assert result.source == "env"
    assert result.fingerprint == "wxyz"
    assert FAKE_KEY not in result.card
    assert result.card.startswith(f"{CARD_PREFIX} CONNECT")


def _snowflake_at(created_ms: int) -> str:
    return str((int(created_ms) - DISCORD_EPOCH_MS) << 22)


def test_snowflake_created_ms_roundtrip():
    created = 1_720_000_000_000
    assert snowflake_created_ms(_snowflake_at(created)) == created


def test_drain_inbound_skips_history_before_since_ms(tmp_path: Path):
    store = SQLiteStore(tmp_path / "hist.sqlite3")
    store.initialize()
    now_ms = 1_750_000_000_000
    fake = FakeDiscordMCPProvider()
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="old seeded staff post that must not dispatch",
            message_id=_snowflake_at(now_ms - 86_400_000),
            author_id="human-1",
        )
    )
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="fresh phone prompt",
            message_id=_snowflake_at(now_ms + 1_000),
            author_id="human-1",
        )
    )
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    orch = AgentOrchestrator(
        store=store,
        backend=FakePuppetmasterBackend(),
        discord=facade,
        workspace=tmp_path,
    )
    receipts = drain_inbound(
        orch,
        facade,
        channel_id="ch",
        workspace_id="ws",
        since_ms=now_ms,
    )
    assert len(receipts) == 1
    assert "fresh phone prompt" in (receipts[0].summary or "")
    store.close()


def test_first_listen_without_since_ms_seeds_now_minus_slack(tmp_path: Path):
    store = SQLiteStore(tmp_path / "slack.sqlite3")
    store.initialize()
    now_ms = int(time.time() * 1000)
    fake = FakeDiscordMCPProvider()
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="old seeded staff post that must not dispatch",
            message_id=_snowflake_at(now_ms - 86_400_000),
            author_id="staff",
        )
    )
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    orch = AgentOrchestrator(
        store=store,
        backend=FakePuppetmasterBackend(),
        discord=facade,
        workspace=tmp_path,
    )
    receipts = drain_inbound(orch, facade, channel_id="ch", workspace_id="ws")
    assert receipts == []
    watermark = store.get_listen_watermark("ch")
    assert watermark is not None
    assert watermark["last_created_ms"] <= now_ms
    assert watermark["last_created_ms"] >= now_ms - LISTEN_HISTORY_SLACK_MS - 2_000
    store.close()


def test_drain_newest_first_batch_still_dispatches_older_new_messages(tmp_path: Path):
    store = SQLiteStore(tmp_path / "newest.sqlite3")
    store.initialize()
    now_ms = 1_750_000_000_000
    fake = FakeDiscordMCPProvider()
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="newer phone prompt",
            message_id=_snowflake_at(now_ms + 2_000),
            author_id="human-1",
        )
    )
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="older still-new phone prompt",
            message_id=_snowflake_at(now_ms + 1_000),
            author_id="human-1",
        )
    )
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    orch = AgentOrchestrator(
        store=store,
        backend=FakePuppetmasterBackend(),
        discord=facade,
        workspace=tmp_path,
    )
    receipts = drain_inbound(
        orch, facade, channel_id="ch", workspace_id="ws", since_ms=now_ms
    )
    assert len(receipts) == 2
    texts = [r.summary or "" for r in receipts]
    assert any("older still-new" in t for t in texts)
    assert any("newer phone" in t for t in texts)
    store.close()


def test_listen_watermark_skips_seeded_staff_across_processes(tmp_path: Path):
    store = SQLiteStore(tmp_path / "wm.sqlite3")
    store.initialize()
    now_ms = 1_750_000_000_000
    fake = FakeDiscordMCPProvider()
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="old seeded staff post that must not dispatch",
            message_id=_snowflake_at(now_ms - 86_400_000),
            author_id="staff",
        )
    )
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    orch = AgentOrchestrator(
        store=store,
        backend=FakePuppetmasterBackend(),
        discord=facade,
        workspace=tmp_path,
    )
    first = drain_inbound(
        orch, facade, channel_id="ch", workspace_id="ws", since_ms=now_ms
    )
    assert first == []
    watermark = store.get_listen_watermark("ch")
    assert watermark is not None
    assert watermark["last_created_ms"] == now_ms

    later_store = SQLiteStore(tmp_path / "wm.sqlite3")
    later_store.initialize()
    later_orch = AgentOrchestrator(
        store=later_store,
        backend=FakePuppetmasterBackend(),
        discord=facade,
        workspace=tmp_path,
    )
    later_since = now_ms + 3_600_000
    second = drain_inbound(
        later_orch,
        facade,
        channel_id="ch",
        workspace_id="ws",
        since_ms=later_since,
    )
    assert second == []
    assert later_store.get_listen_watermark("ch")["last_created_ms"] == now_ms

    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="fresh after watermark",
            message_id=_snowflake_at(now_ms + 5_000),
            author_id="phone",
        )
    )
    third = drain_inbound(
        later_orch,
        facade,
        channel_id="ch",
        workspace_id="ws",
        since_ms=later_since,
    )
    assert len(third) == 1
    assert "fresh after watermark" in (third[0].summary or "")
    later_store.close()
    store.close()


def test_listen_watermark_advances_for_skipped_cards_and_connects(tmp_path: Path):
    store = SQLiteStore(tmp_path / "cards.sqlite3")
    store.initialize()
    now_ms = 1_750_000_000_000
    fake = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    orch = AgentOrchestrator(
        store=store,
        backend=FakePuppetmasterBackend(),
        discord=facade,
        workspace=tmp_path,
    )
    assert drain_inbound(orch, facade, channel_id="ch", workspace_id="ws", since_ms=now_ms) == []
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content=f"{CARD_PREFIX} PROGRESS\n[work] going",
            message_id=_snowflake_at(now_ms + 1_000),
        )
    )
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="please implement the fix",
            message_id=_snowflake_at(now_ms + 2_000),
            author_id="phone",
        )
    )
    receipts = drain_inbound(orch, facade, channel_id="ch", workspace_id="ws", since_ms=now_ms)
    assert len(receipts) == 1
    assert store.get_listen_watermark("ch")["last_created_ms"] == now_ms + 2_000

    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="late arriving older staff post",
            message_id=_snowflake_at(now_ms + 1_500),
            author_id="staff",
        )
    )
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="/connect",
            message_id=_snowflake_at(now_ms + 3_000),
            author_id="staff",
        )
    )
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="/open files .",
            message_id=_snowflake_at(now_ms + 4_000),
            author_id="staff",
        )
    )
    skipped = drain_inbound(
        orch,
        facade,
        channel_id="ch",
        workspace_id="ws",
        workspace=tmp_path,
        since_ms=now_ms,
        host_roots=[tmp_path],
        host_runner=lambda *a, **k: None,
    )
    assert skipped == []
    assert store.get_listen_watermark("ch")["last_created_ms"] == now_ms + 4_000
    store.close()


def test_listen_cli_durable_watermark_with_fake_provider(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / ".agent-discord"
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUPPETMASTER_MODEL", "cursor/grok-4-5")
    persist = ws / "fake_discord"
    persist.mkdir(parents=True)
    now_ms = int(time.time() * 1000)
    fake = FakeDiscordMCPProvider(persist_dir=persist)
    fake.inbox.append(
        DiscordMessage(
            channel_id="99",
            content="old seeded staff post that must not dispatch",
            message_id=_snowflake_at(now_ms - 86_400_000),
            author_id="staff",
        )
    )
    fake._save_persist()

    assert main(["listen", "--channel-id", "99", "--fake", "--once", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["listen", "--channel-id", "99", "--fake", "--once", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []

    again = FakeDiscordMCPProvider(persist_dir=persist)
    again.inbox.append(
        DiscordMessage(
            channel_id="99",
            content="fresh phone prompt after restart",
            message_id=_snowflake_at(now_ms + 2_000),
            author_id="phone",
        )
    )
    again._save_persist()
    assert main(["listen", "--channel-id", "99", "--fake", "--once", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["status"] == "completed"
    assert LISTEN_HISTORY_SLACK_MS == 15_000


def test_should_dispatch_skips_cards():
    assert not should_dispatch_inbound(
        DiscordMessage(channel_id="ch", content=f"{CARD_PREFIX} CONNECT\nProvider: `openrouter`")
    )
    assert not should_dispatch_inbound(
        DiscordMessage(channel_id="ch", content=f"{CARD_PREFIX} PROGRESS\n[work] going")
    )
    assert not should_dispatch_inbound(
        DiscordMessage(channel_id="ch", content=f"{CARD_PREFIX} RECEIPT\n**Receipt** `r`")
    )
    assert not should_dispatch_inbound(
        DiscordMessage(channel_id="ch", content=f"{CARD_PREFIX} OPEN\nSurface: `terminal`")
    )
    assert should_dispatch_inbound(
        DiscordMessage(channel_id="ch", content="please fix the login", message_id="m1")
    )


def test_agentic_backend_argv_and_env_never_on_argv(monkeypatch, tmp_path: Path):
    calls: list[dict[str, Any]] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), "env": kwargs.get("env")})

        class Proc:
            returncode = 0
            stdout = "job_id: j9\nsummary: done via agentic\n"
            stderr = ""

        return Proc()

    monkeypatch.setattr(
        "agent_discord.puppetmaster.agentic.shutil.which",
        lambda _: "/usr/bin/puppetmaster",
    )
    monkeypatch.setattr("agent_discord.puppetmaster.agentic.subprocess.run", fake_run)
    vault = KeyVault(tmp_path / "keys")
    vault.put("openrouter", FAKE_KEY, "env")
    backend = AgenticPuppetmasterBackend(
        cli="puppetmaster",
        pin=AGENTIC_MODEL_PIN,
        cwd=tmp_path,
        vault=vault,
        env={},
    )
    result = backend.dispatch(_request())
    assert result.status == TaskStatus.COMPLETED
    assert calls
    cmd = calls[0]["cmd"]
    assert cmd[0] == "puppetmaster"
    assert cmd[1] == "agentic"
    assert "hello world" in cmd[2]
    assert "--provider" in cmd
    assert cmd[cmd.index("--provider") + 1] == "openrouter"
    assert cmd[cmd.index("--model") + 1] == "openrouter/auto"
    assert "--mode" in cmd
    assert cmd[cmd.index("--mode") + 1] == "implement"
    assert "--allow-dirty" in cmd
    assert "--timeout-seconds" in cmd
    assert "--cwd" in cmd
    assert FAKE_KEY not in cmd
    env = calls[0]["env"]
    assert env is not None
    assert env["OPENROUTER_API_KEY"] == FAKE_KEY
    assert result.usage is not None
    assert result.usage.model == AGENTIC_CANONICAL_MODEL


def test_overflow_pointer_has_no_url_key(tmp_path: Path):
    fake = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    store = DiscordObjectStore(facade, max_bytes=8, workspace=tmp_path)
    payload = b"0123456789abcdef"
    ref = store.put_or_overflow(payload, channel_id="ch", filename="big.bin", kind="blob")
    assert ref.kind == "overflow"
    pointer = json.loads(store.get(ref).decode("utf-8"))
    assert pointer["kind"] == "overflow"
    assert "url" not in pointer
    assert pointer["sha256"]
    assert pointer["local_stash"].startswith("stash/")
    assert (tmp_path / pointer["local_stash"]).read_bytes() == payload
    dumped = json.dumps(asdict(ref))
    assert "url" not in json.loads(dumped)


def test_put_still_raises_when_over_max():
    fake = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    store = DiscordObjectStore(facade, max_bytes=4)
    with pytest.raises(ObjectTooLargeError):
        store.put(b"12345", channel_id="ch", filename="big.bin", kind="blob")


def test_check_config_agentic_skips_cursor_pin(tmp_path: Path):
    cfg = load_config(
        env={
            "AGENT_DISCORD_WORKSPACE": str(tmp_path),
            "DISCORD_BOT_TOKEN": "tok",
            "PUPPETMASTER_MODEL": "cursor/other",
            "AGENT_DISCORD_COMPUTE": "agentic",
            "OPENROUTER_API_KEY": FAKE_KEY,
        },
        dotenv_path=tmp_path / "missing.env",
    )
    assert resolve_compute(cfg).mode == "agentic"
    assert check_config(cfg, require_token=True) == []


def test_check_config_agentic_requires_connect_without_key(tmp_path: Path):
    cfg = load_config(
        env={
            "AGENT_DISCORD_WORKSPACE": str(tmp_path),
            "DISCORD_BOT_TOKEN": "tok",
            "PUPPETMASTER_MODEL": "cursor/other",
            "AGENT_DISCORD_COMPUTE": "agentic",
        },
        dotenv_path=tmp_path / "missing.env",
    )
    problems = check_config(cfg, require_token=True)
    assert any("run discord-os connect" in p for p in problems)
    assert not any("PUPPETMASTER_MODEL" in p for p in problems)
