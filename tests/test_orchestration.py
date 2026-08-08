"""Task dispatch, event replay, receipt rendering, inbound dedupe."""

from __future__ import annotations

from pathlib import Path

from agent_discord.contracts import ProgressSummary, RunReceipt, TaskIntake, TaskStatus
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.orchestration.receipts import render_receipt
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend
from agent_discord.redaction import strip_forbidden_keys


def _orch(tmp_path: Path, *, fail: bool = False):
    store = SQLiteStore(tmp_path / "o.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    backend = FakePuppetmasterBackend()
    backend.fail_next = fail
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        discord=facade,
        post_progress_to_discord=True,
    )
    return orch, store, fake_discord, backend


def test_dispatch_persists_events_and_posts_receipt(tmp_path: Path):
    orch, store, fake_discord, backend = _orch(tmp_path)
    store.remember(
        workspace_id="ws",
        channel_id="ch",
        content="prior context about invoices",
        source="seed",
        provenance={"seed": True},
    )
    receipt = orch.run_task(
        TaskIntake(
            text="review invoices",
            channel_id="ch",
            workspace_id="ws",
            message_id="inbound-1",
        )
    )
    assert receipt.status == TaskStatus.COMPLETED
    assert backend.last_request is not None
    assert backend.last_request.model == "cursor/grok-4-5"
    assert backend.last_request.context.memories

    events = store.list_events(receipt.run_id)
    kinds = [e["kind"] for e in events]
    assert "intake" in kinds
    assert "context_snapshot" in kinds
    assert "dispatch" in kinds
    assert "receipt" in kinds

    assert fake_discord.sent
    assert any("Receipt" in m.content for m in fake_discord.sent)

    rendered = render_receipt(receipt)
    assert "cursor/grok-4-5" in rendered or "grok-4.5" in rendered
    assert "chain_of_thought" not in rendered
    store.close()


def test_inbound_message_dedupe_reuses_prior_receipt(tmp_path: Path):
    orch, store, _, backend = _orch(tmp_path)
    first = orch.run_task(
        TaskIntake(
            text="do work",
            channel_id="ch",
            workspace_id="ws",
            message_id="same-msg",
        )
    )
    assert first.status == TaskStatus.COMPLETED
    assert backend.dispatch_count == 1

    second = orch.run_task(
        TaskIntake(
            text="do work again",
            channel_id="ch",
            workspace_id="ws",
            message_id="same-msg",
        )
    )
    assert backend.dispatch_count == 1
    assert second.run_id == first.run_id
    assert second.task_id == first.task_id
    assert second.status == first.status
    store.close()


def test_failed_dispatch_receipt(tmp_path: Path):
    orch, store, _, _ = _orch(tmp_path, fail=True)
    receipt = orch.run_task(
        TaskIntake(text="boom", channel_id="ch", workspace_id="ws")
    )
    assert receipt.status == TaskStatus.FAILED
    assert receipt.error
    assert orch.status(receipt.run_id) == TaskStatus.FAILED
    store.close()


def test_cancel_interface(tmp_path: Path):
    orch, store, _, _ = _orch(tmp_path)
    receipt = orch.run_task(
        TaskIntake(text="ok", channel_id="ch", workspace_id="ws")
    )
    assert orch.cancel(receipt.run_id) is True
    assert orch.status(receipt.run_id) == TaskStatus.CANCELLED
    store.close()


def test_receipt_redacts_thinking_markers():
    text = render_receipt(
        RunReceipt(
            task_id="t",
            run_id="r",
            status=TaskStatus.COMPLETED,
            summary='done <thinking>secret</thinking> {"reasoning":"hidden"}',
            progress=(
                ProgressSummary(
                    stage="x",
                    message="progress",
                    details={"chain_of_thought": "nope", "ok": True},
                ),
            ),
        )
    )
    assert "<thinking>" not in text
    assert "secret" not in text
    assert "hidden" not in text
    assert "[redacted]" in text
    assert "chain_of_thought" not in text


def test_strip_forbidden_keys_recursive():
    cleaned = strip_forbidden_keys(
        {
            "a": 1,
            "cot": "x",
            "nested": {"hidden_cot": "y", "keep": [1, {"reasoning": "z", "v": 2}]},
        }
    )
    assert cleaned == {"a": 1, "nested": {"keep": [1, {"v": 2}]}}
