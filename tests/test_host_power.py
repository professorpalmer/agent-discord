"""Headless host power: /on /off in Discord, pidfile detach."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from agent_discord.cli import main
from agent_discord.contracts import DiscordMessage
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.host.power import is_power_command, parse_power_command
from agent_discord.host.service import (
    pid_is_alive,
    running_host_pid,
    start_detached,
    stop_host,
    write_host_meta,
)
from agent_discord.orchestration.listen import DISCORD_EPOCH_MS, drain_inbound
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend


def _snowflake_at(created_ms: int) -> str:
    return str((int(created_ms) - DISCORD_EPOCH_MS) << 22)


def test_parse_power_commands():
    assert is_power_command("/on")
    assert is_power_command("!off")
    assert is_power_command("/status please")
    assert not is_power_command("/open terminal")
    assert parse_power_command("/on").action == "on"
    assert parse_power_command("!off now").action == "off"
    assert parse_power_command("/status").action == "status"


def test_host_control_defaults_armed_until_row_exists(tmp_path: Path):
    store = SQLiteStore(tmp_path / "host.sqlite3")
    store.initialize()
    assert store.host_is_armed("ch") is True
    store.set_host_control("ch", default_armed=False)
    assert store.host_is_armed("ch") is False
    store.set_host_control("ch", armed=True)
    assert store.host_is_armed("ch") is True
    store.close()


def test_drain_off_swallows_tasks_on_dispatches(tmp_path: Path):
    store = SQLiteStore(tmp_path / "power.sqlite3")
    store.initialize()
    now_ms = 1_750_000_000_000
    fake = FakeDiscordMCPProvider()
    fake.inbox.extend(
        [
            DiscordMessage(
                channel_id="ch",
                content="/off",
                message_id=_snowflake_at(now_ms + 1_000),
                author_id="human-1",
            ),
            DiscordMessage(
                channel_id="ch",
                content="do not run this while idle",
                message_id=_snowflake_at(now_ms + 2_000),
                author_id="human-1",
            ),
            DiscordMessage(
                channel_id="ch",
                content="/on",
                message_id=_snowflake_at(now_ms + 3_000),
                author_id="human-1",
            ),
            DiscordMessage(
                channel_id="ch",
                content="run this after on",
                message_id=_snowflake_at(now_ms + 4_000),
                author_id="human-1",
            ),
        ]
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
    assert "run this after on" in (receipts[0].summary or "")
    assert store.host_is_armed("ch") is True
    def _host_title(msg) -> str:
        for row in (msg.metadata or {}).get("components") or []:
            for item in row.get("components") or []:
                text = item.get("content") or ""
                if text.startswith("### "):
                    return text.splitlines()[0][4:]
        return ""

    host_cards = [msg for msg in fake.sent if _host_title(msg) in {"Running", "Stopped"}]
    assert host_cards
    assert _host_title(host_cards[-1]) == "Running"
    store.close()


def test_host_card_edits_in_place(tmp_path: Path):
    store = SQLiteStore(tmp_path / "card.sqlite3")
    store.initialize()
    now_ms = 1_750_000_000_000
    fake = FakeDiscordMCPProvider()
    fake.inbox.extend(
        [
            DiscordMessage(
                channel_id="ch",
                content="/off",
                message_id=_snowflake_at(now_ms + 1_000),
                author_id="human-1",
            ),
            DiscordMessage(
                channel_id="ch",
                content="/on",
                message_id=_snowflake_at(now_ms + 2_000),
                author_id="human-1",
            ),
        ]
    )
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    orch = AgentOrchestrator(
        store=store,
        backend=FakePuppetmasterBackend(),
        discord=facade,
        workspace=tmp_path,
    )
    drain_inbound(orch, facade, channel_id="ch", workspace_id="ws", since_ms=now_ms)
    def _host_title(msg) -> str:
        for row in (msg.metadata or {}).get("components") or []:
            for item in row.get("components") or []:
                text = item.get("content") or ""
                if text.startswith("### "):
                    return text.splitlines()[0][4:]
        return ""

    host_cards = [msg for msg in fake.sent if _host_title(msg) in {"Running", "Stopped"}]
    assert len(host_cards) == 1
    assert _host_title(host_cards[0]) == "Running"
    store.close()


def test_open_while_off_does_not_open(tmp_path: Path):
    store = SQLiteStore(tmp_path / "open-off.sqlite3")
    store.initialize()
    store.set_host_control("ch", armed=False)
    now_ms = 1_750_000_000_000
    fake = FakeDiscordMCPProvider()
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="/open terminal",
            message_id=_snowflake_at(now_ms + 1_000),
            author_id="human-1",
        )
    )
    opened: list[str] = []
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    orch = AgentOrchestrator(
        store=store,
        backend=FakePuppetmasterBackend(),
        discord=facade,
        workspace=tmp_path,
    )
    drain_inbound(
        orch,
        facade,
        channel_id="ch",
        workspace_id="ws",
        since_ms=now_ms,
        host_roots=(tmp_path,),
        host_runner=lambda argv, **kwargs: opened.append(" ".join(argv)) or 0,
    )
    assert opened == []
    store.close()


def test_pidfile_dead_owner_is_not_running(tmp_path: Path):
    write_host_meta(tmp_path, pid=99999999, channel_id="ch")
    assert pid_is_alive(99999999) is False
    assert running_host_pid(tmp_path) is None


def test_start_and_stop_detached_process(tmp_path: Path):
    pid = start_detached(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        workspace=tmp_path,
        channel_id="ch",
    )
    assert running_host_pid(tmp_path) == pid
    stopped = stop_host(tmp_path)
    assert stopped == pid
    deadline = time.time() + 2
    while time.time() < deadline and pid_is_alive(pid):
        time.sleep(0.05)
    assert running_host_pid(tmp_path) is None


def test_cli_host_status_and_run_once(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / ".agent-discord"
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUPPETMASTER_MODEL", "cursor/grok-4-5")
    assert main(["bootstrap", "--workspace", str(ws)]) == 0
    assert main(["host", "status", "--json"]) == 0
    status = capsys.readouterr().out
    assert '"running": false' in status
    assert main(["host", "run", "--channel-id", "99", "--fake", "--once", "--no-discord-post"]) == 0
    assert main(["host", "stop"]) == 0
