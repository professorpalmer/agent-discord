"""Parallel job pool: two asks cook at once."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from agent_discord.contracts import DiscordMessage, TaskIntake, TaskStatus
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.orchestration.jobs import JobPool
from agent_discord.orchestration.listen import drain_inbound
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend


class _SlowBackend(FakePuppetmasterBackend):
    def __init__(self, hold: float = 0.15) -> None:
        super().__init__()
        self.hold = hold
        self.started: list[float] = []
        self._gate = threading.Lock()

    def dispatch(self, request):  # type: ignore[override]
        with self._gate:
            self.started.append(time.monotonic())
        time.sleep(self.hold)
        return super().dispatch(request)


def test_job_pool_runs_two_asks_in_parallel(tmp_path: Path):
    store = SQLiteStore(tmp_path / "jobs.sqlite3")
    store.initialize()
    backend = _SlowBackend(hold=0.25)
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        post_progress_to_discord=False,
        host_repos=(),
    )
    pool = JobPool()
    started = time.monotonic()
    pool.submit(
        orch.run_task,
        TaskIntake(text="what is Discord OS?", channel_id="pm", workspace_id="ws"),
    )
    pool.submit(
        orch.run_task,
        TaskIntake(text="what is dugout?", channel_id="dugout", workspace_id="ws"),
    )
    receipts = pool.wait(timeout=3.0)
    elapsed = time.monotonic() - started
    assert len(receipts) == 2
    assert all(item.status == TaskStatus.COMPLETED for item in receipts)
    assert len(backend.started) == 2
    assert abs(backend.started[1] - backend.started[0]) < 0.12
    assert elapsed < 0.45
    store.close()


def test_implement_on_same_realm_serializes(tmp_path: Path):
    store = SQLiteStore(tmp_path / "write.sqlite3")
    store.initialize()
    backend = _SlowBackend(hold=0.08)
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        post_progress_to_discord=False,
        host_repos=(),
    )
    pool = JobPool()
    pool.submit(
        orch.run_task,
        TaskIntake(text="implement login timeout", channel_id="pm", workspace_id="ws"),
        write_key="/repo/puppetmaster",
    )
    pool.submit(
        orch.run_task,
        TaskIntake(text="implement second patch", channel_id="pm", workspace_id="ws"),
        write_key="/repo/puppetmaster",
    )
    receipts = pool.wait(timeout=3.0)
    assert len(receipts) == 2
    assert abs(backend.started[1] - backend.started[0]) >= 0.07
    store.close()


def test_drain_with_pool_returns_while_jobs_run(tmp_path: Path):
    store = SQLiteStore(tmp_path / "drain.sqlite3")
    store.initialize()
    backend = _SlowBackend(hold=0.2)
    fake = FakeDiscordMCPProvider()
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        discord=DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test"),
        post_progress_to_discord=False,
        host_repos=(),
    )
    store.set_host_control("ch", armed=True)
    fake.inbox.extend(
        [
            DiscordMessage(
                channel_id="ch",
                content="what is Discord OS?",
                message_id="101",
                author_id="human-1",
            ),
            DiscordMessage(
                channel_id="ch",
                content="what is a receipt?",
                message_id="102",
                author_id="human-1",
            ),
        ]
    )
    pool = JobPool()
    started = time.monotonic()
    immediate = list(
        drain_inbound(
            orch,
            orch.discord,
            channel_id="ch",
            workspace_id="ws",
            since_ms=0,
            job_pool=pool,
        )
    )
    assert immediate == []
    assert pool.live_count() == 2
    assert time.monotonic() - started < 0.15
    receipts = pool.wait(timeout=3.0)
    assert len(receipts) == 2
    store.close()


def test_job_pool_surfaces_a_crashed_runner():
    pool = JobPool()

    def boom(intake: TaskIntake):
        raise RuntimeError("backend died")

    pool.submit(boom, TaskIntake(text="x", channel_id="ch", workspace_id="ws"))
    receipts = pool.wait(timeout=2.0)
    assert len(receipts) == 1
    assert receipts[0].status == TaskStatus.FAILED
    assert "backend died" in (receipts[0].error or "")


def test_fail_stale_runs(tmp_path: Path):
    store = SQLiteStore(tmp_path / "stale.sqlite3")
    store.initialize()
    store.create_task(
        task_id="t1",
        workspace_id="ws",
        channel_id="ch",
        intake_text="left hanging",
    )
    store.create_run(
        run_id="r1",
        task_id="t1",
        model="openrouter/auto",
        adapter_name="openrouter/auto",
        status=TaskStatus.RUNNING,
    )
    assert store.fail_stale_runs() == 1
    assert store.get_run("r1")["status"] == "failed"
    store.close()
