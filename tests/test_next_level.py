"""Swarm, memory injection, job buttons, retry, voice collab, rollback."""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_discord.contracts import DiscordMessage, TaskIntake, TaskStatus
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.host.actions import job_action_from_custom_id, job_custom_id
from agent_discord.host.panel import handle_gateway_interaction
from agent_discord.orchestration.cards import code_card, diff_card
from agent_discord.orchestration.listen import drain_inbound
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend


def _orch(tmp_path: Path, **kwargs):
    store = SQLiteStore(tmp_path / "n.sqlite3")
    store.initialize()
    fake = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    backend = FakePuppetmasterBackend()
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        discord=facade,
        post_progress_to_discord=True,
        **kwargs,
    )
    return orch, store, fake, backend


def test_code_and_diff_cards_fence():
    code = code_card("python", "print(1)")
    assert "```python" in code.description
    assert "print(1)" in code.description
    diff = diff_card("@@ -1 +1 @@\n-a\n+b\n", filename="x.py")
    assert "```diff" in diff.description
    assert "`x.py`" in diff.description


def test_preferences_inject_into_dispatch(tmp_path: Path):
    orch, store, _fake, backend = _orch(tmp_path)
    store.set_preference("ws", "tone", "concise")
    store.set_preference("ws", "formatter", "black", kind="style")
    store.record_failure("ws", "old", "429 timeout")
    orch.run_task(TaskIntake(text="what is Discord OS?", channel_id="ch", workspace_id="ws"))
    assert backend.last_request is not None
    contents = [str(m.get("content") or "") for m in backend.last_request.context.memories]
    block = "\n".join(contents)
    assert "tone=concise" in block
    assert "formatter=black" in block
    assert "429 timeout" not in block
    store.close()


def test_dispatch_swarm_fans_out_and_aggregates(tmp_path: Path):
    orch, store, fake, backend = _orch(tmp_path)
    receipt = orch.run_task(
        TaskIntake(
            text="swarm this module",
            channel_id="ch",
            workspace_id="ws",
            metadata={"workers": 3},
        )
    )
    assert receipt.status == TaskStatus.COMPLETED
    assert backend.dispatch_count >= 3
    roles = [req.metadata.get("role") for req in backend.last_requests if req.metadata.get("role")]
    assert "explore" in roles
    assert "explore:" in receipt.summary
    assert fake.sent
    store.close()


def test_rate_limit_retries_once(tmp_path: Path):
    orch, store, _fake, backend = _orch(tmp_path, retry_backoff_s=0.0)
    backend.rate_limit_next = True
    receipt = orch.run_task(
        TaskIntake(text="what is Discord OS?", channel_id="ch", workspace_id="ws")
    )
    assert receipt.status == TaskStatus.COMPLETED
    assert backend.dispatch_count >= 2
    store.close()


def test_job_button_custom_ids_do_not_collide_with_host():
    from agent_discord.host.panel import ON_ID, OFF_ID

    cid = job_custom_id("retry", "abc123")
    assert cid.startswith("discord-os:job:retry:")
    assert cid != ON_ID
    assert cid != OFF_ID
    parsed = job_action_from_custom_id(cid)
    assert parsed is not None
    assert parsed.action == "retry"
    assert parsed.run_id == "abc123"


def test_panel_gateway_routes_job_buttons():
    seen: list[tuple[str, str]] = []

    def on_job(action: str, run_id: str) -> None:
        seen.append((action, run_id))

    result = handle_gateway_interaction(
        store=None,
        channel_id="ch",
        payload={
            "type": 3,
            "id": "ix",
            "token": "tok",
            "data": {"custom_id": job_custom_id("approve", "run9")},
        },
        on_job=on_job,
    )
    assert result == "approve"
    assert seen == [("approve", "run9")]


def test_listen_uses_voice_transcript_and_thread_history(tmp_path: Path):
    orch, store, fake, backend = _orch(tmp_path)
    store.set_host_control("ch", armed=True)
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="prior note",
            message_id="100",
            thread_id="th1",
        )
    )
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="@bot hey",
            message_id="200",
            thread_id="th1",
            author_id="human-1",
            metadata={"transcript": "hey discord os run tests"},
        )
    )
    receipts = drain_inbound(
        orch,
        orch.discord,
        channel_id="ch",
        workspace_id="ws",
        since_ms=0,
    )
    assert receipts
    assert backend.last_request is not None
    assert "run tests" in backend.last_request.prompt
    assert backend.last_request.metadata.get("mentioned") is True
    store.close()


def test_rollback_on_red_restores_git_diff(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    target = repo / "note.txt"
    target.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "note.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    target.write_text("dirty\n", encoding="utf-8")
    orch, store, _fake, backend = _orch(tmp_path, workspace=repo)
    backend.fail_next = True
    # stream() consumes fail_next via dispatch; disable stream tokens path
    backend.token_chunks = []
    orch.run_task(TaskIntake(text="what is Discord OS?", channel_id="ch", workspace_id="ws"))
    assert target.read_text(encoding="utf-8") == "clean\n"
    store.close()
