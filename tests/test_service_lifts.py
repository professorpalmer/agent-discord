"""Operator pairing, approve-gated writes, spend halt, cron, and voice intake."""

from __future__ import annotations

from pathlib import Path

from agent_discord.cli import main
from agent_discord.contracts import DiscordAttachment, DiscordMessage, TaskIntake, TaskStatus, UsageReceipt
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.discord.rest import fetch_attachment_bytes
from agent_discord.discord.voice import materialize_voice_intake
from agent_discord.host.panel import HALT_ID, PAIR_ID, handle_gateway_interaction
from agent_discord.orchestration.listen import drain_inbound
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.orchestration.receipts import render_receipt
from agent_discord.orchestration.service import (
    format_usd,
    parse_every_seconds,
    parse_schedule_command,
    spend_usd_from_usage,
)
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend


def _orch(tmp_path: Path):
    store = SQLiteStore(tmp_path / "svc.sqlite3")
    store.initialize()
    fake = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    backend = FakePuppetmasterBackend()
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        discord=facade,
        post_progress_to_discord=True,
    )
    return orch, store, fake, backend


def test_stranger_text_does_not_dispatch_after_owner_exists(tmp_path: Path):
    orch, store, fake, backend = _orch(tmp_path)
    store.add_operator("owner-1", role="owner")
    store.set_host_control("ch", armed=True)
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="what is Discord OS?",
            message_id="10",
            author_id="stranger",
        )
    )
    receipts = drain_inbound(orch, orch.discord, channel_id="ch", workspace_id="ws", since_ms=0)
    assert receipts == []
    assert backend.dispatch_count == 0
    store.close()


def test_first_armed_author_becomes_owner(tmp_path: Path):
    orch, store, fake, backend = _orch(tmp_path)
    store.set_host_control("ch", armed=True)
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="what is Discord OS?",
            message_id="11",
            author_id="human-1",
        )
    )
    receipts = drain_inbound(orch, orch.discord, channel_id="ch", workspace_id="ws", since_ms=0)
    assert len(receipts) == 1
    assert store.is_operator("human-1")
    assert backend.dispatch_count == 1
    store.close()


def test_implement_waits_for_approve(tmp_path: Path):
    orch, store, _fake, backend = _orch(tmp_path)
    parked = orch.run_task(
        TaskIntake(text="implement the login timeout fix", channel_id="ch", workspace_id="ws")
    )
    assert parked.status == TaskStatus.PENDING
    assert "Approve" in parked.summary
    assert backend.dispatch_count == 0
    result = orch.apply_job_action("approve", parked.run_id)
    assert result["status"] == TaskStatus.COMPLETED.value
    assert backend.dispatch_count == 1
    store.close()


def test_receipt_shows_dollars():
    receipt_usage = UsageReceipt(
        model="openrouter/auto",
        adapter_name="agentic",
        input_tokens=1000,
        output_tokens=2000,
        metadata={"cost": 1.25},
    )
    from agent_discord.contracts import RunReceipt

    text = render_receipt(
        RunReceipt(
            task_id="t",
            run_id="r",
            status=TaskStatus.COMPLETED,
            summary="done",
            usage=receipt_usage,
        )
    )
    assert "Cost: $1.25" in text
    assert spend_usd_from_usage(receipt_usage) == 1.25
    assert format_usd(0.0004) == "$0.0004"


def test_spend_halt_blocks_new_dispatch(tmp_path: Path):
    orch, store, _fake, backend = _orch(tmp_path)
    store.set_preference("_host", "spend_halt", "1")
    receipt = orch.run_task(
        TaskIntake(text="what is Discord OS?", channel_id="ch", workspace_id="ws")
    )
    assert receipt.status == TaskStatus.FAILED
    assert receipt.error == "spend halted"
    assert backend.dispatch_count == 0
    store.close()


def test_due_schedule_fires_from_listen(tmp_path: Path):
    orch, store, fake, backend = _orch(tmp_path)
    store.add_operator("owner-1", role="owner")
    store.set_host_control("ch", armed=True)
    store.add_schedule(
        channel_id="ch",
        workspace_id="ws",
        prompt="what is Discord OS?",
        every_s=3600,
        created_by="owner-1",
        next_ms=0,
    )
    receipts = drain_inbound(orch, orch.discord, channel_id="ch", workspace_id="ws", since_ms=0)
    assert receipts
    assert backend.dispatch_count >= 1
    assert store.due_schedules(0, "ch") == []
    store.close()
    _ = fake


def test_schedule_command_parser():
    parsed = parse_schedule_command("schedule every 1h: run tests")
    assert parsed == (3600, "run tests")
    assert parse_every_seconds("30m") == 1800
    assert parse_schedule_command("not a schedule") is None


def test_voice_bytes_become_intake(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "agent_discord.discord.voice.transcribe_voice_attachment",
        lambda path, whisper_cmd=None: "hey discord os run tests",
    )
    text = materialize_voice_intake(
        DiscordMessage(
            channel_id="ch",
            content="",
            message_id="v1",
            attachments=(
                DiscordAttachment(
                    attachment_id="a1",
                    filename="voice-message.ogg",
                    size=12,
                    content_type="audio/ogg",
                ),
            ),
            metadata={"voice_bytes": b"ogg-bytes"},
        )
    )
    assert text == "run tests"
    _ = tmp_path


def test_listen_transcribes_local_voice_memo(monkeypatch, tmp_path: Path):
    audio = tmp_path / "voice-message.ogg"
    audio.write_bytes(b"ogg")
    monkeypatch.setattr(
        "agent_discord.discord.voice.transcribe_voice_attachment",
        lambda path, whisper_cmd=None: "hey discord os run tests",
    )
    orch, store, fake, backend = _orch(tmp_path)
    store.set_host_control("ch", armed=True)
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="",
            message_id="v2",
            author_id="phone",
            attachments=(
                DiscordAttachment(
                    attachment_id="a1",
                    filename="voice-message.ogg",
                    size=3,
                    content_type="audio/ogg",
                ),
            ),
            metadata={"local_audio_path": str(audio)},
        )
    )
    receipts = drain_inbound(orch, orch.discord, channel_id="ch", workspace_id="ws", since_ms=0)
    assert receipts
    assert backend.last_request is not None
    assert "run tests" in backend.last_request.prompt
    store.close()


def test_fetch_attachment_bytes_requires_discord_cdn():
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"ogg"

    def opener(request, timeout=60):
        assert request.get_header("Authorization") == "Bot tok"
        return _Resp()

    data = fetch_attachment_bytes(
        "tok",
        "https://cdn.discordapp.com/attachments/1/2/voice-message.ogg",
        opener=opener,
    )
    assert data == b"ogg"
    try:
        fetch_attachment_bytes("tok", "https://example.com/voice.ogg", opener=opener)
    except Exception as exc:
        assert "Discord CDN" in str(exc)
    else:
        raise AssertionError("expected CDN host rejection")


def test_pair_and_spend_cli(tmp_path: Path, monkeypatch, capsys):
    ws = tmp_path / ".agent-discord"
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.chdir(tmp_path)
    assert main(["bootstrap"]) == 0
    capsys.readouterr()
    assert main(["pair", "--user-id", "99", "--role", "owner"]) == 0
    out = capsys.readouterr().out
    assert "paired 99" in out
    assert main(["spend", "--cap", "5", "--json"]) == 0
    payload = capsys.readouterr().out
    assert "5" in payload
    assert main(["schedule", "--every", "1h", "--channel-id", "ch", "run", "tests"]) == 0
    assert "scheduled" in capsys.readouterr().out


def test_pair_and_halt_buttons_parse(tmp_path: Path):
    store = SQLiteStore(tmp_path / "panel.sqlite3")
    store.initialize()
    result = handle_gateway_interaction(
        store,
        "ch",
        {
            "type": 3,
            "id": "ix",
            "token": "tok",
            "user": {"id": "owner-7"},
            "data": {"custom_id": PAIR_ID},
        },
    )
    assert result == "pair"
    assert store.is_operator("owner-7")
    halt = handle_gateway_interaction(
        store,
        "ch",
        {
            "type": 3,
            "id": "ix2",
            "token": "tok",
            "user": {"id": "owner-7"},
            "data": {"custom_id": HALT_ID},
        },
    )
    assert halt == "halt"
    assert store.get_preference("_host", "spend_halt") == "1"
    store.close()


def test_store_usable_from_another_thread(tmp_path: Path):
    import threading

    store = SQLiteStore(tmp_path / "thread.sqlite3")
    store.initialize()
    errors: list[str] = []

    def worker() -> None:
        try:
            store.add_operator("u-thread", role="owner")
            assert store.is_operator("u-thread")
        except Exception as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert errors == []
    assert store.is_operator("u-thread")
    store.close()
