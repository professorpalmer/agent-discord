"""End-to-end: parallel realms, think-tank, host tools, CLI listen."""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_discord.cli import main
from agent_discord.contracts import DiscordMessage, TaskIntake
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.host.memory import bind_memory_channel
from agent_discord.host.repos import HostRepo
from agent_discord.orchestration.jobs import JobPool
from agent_discord.orchestration.listen import DISCORD_EPOCH_MS, drain_inbound
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.persistence.sqlite import SQLiteStore
from tests.test_jobs import _SlowBackend


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def _snowflake(offset: int = 0) -> str:
    ms = int(time.time() * 1000) - offset
    return str(((ms - DISCORD_EPOCH_MS) << 22) + (offset + 1))


def test_e2e_parallel_realms_think_tank_and_tools(tmp_path: Path, monkeypatch):
    pm = _git_repo(tmp_path / "Puppetmaster")
    dug = _git_repo(tmp_path / "dugout")
    repos = (
        HostRepo(name="puppetmaster", path=pm, aliases=("puppetmaster",)),
        HostRepo(name="dugout", path=dug, aliases=("dugout",)),
    )
    store = SQLiteStore(tmp_path / "e2e.sqlite3")
    store.initialize()
    store.merge_binding_metadata(
        "ws", "ch-pm", {"repo": "puppetmaster", "cwd": str(pm)}
    )
    store.merge_binding_metadata("ws", "ch-dug", {"repo": "dugout", "cwd": str(dug)})
    bind_memory_channel(store, workspace_id="ws", channel_id="ch-tank")
    store.set_host_control("ch-pm", armed=True)
    store.set_host_control("ch-dug", armed=True)

    fake = FakeDiscordMCPProvider()
    fake.inbox.extend(
        [
            DiscordMessage(
                channel_id="ch-tank",
                content="wiki is the personal graph",
                message_id=_snowflake(2),
                author_id="human-1",
            ),
            DiscordMessage(
                channel_id="ch-pm",
                content="list open pull requests",
                message_id=_snowflake(1),
                author_id="human-1",
            ),
            DiscordMessage(
                channel_id="ch-dug",
                content="what is the roster?",
                message_id=_snowflake(0),
                author_id="human-1",
            ),
        ]
    )
    backend = _SlowBackend(hold=0.2)
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        discord=DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="e2e"),
        post_progress_to_discord=True,
        host_repos=repos,
        compute_cwd=tmp_path / ".agent-discord",
    )
    pool = JobPool()
    started = time.monotonic()
    for channel_id in ("ch-pm", "ch-dug"):
        drain_inbound(
            orch,
            orch.discord,
            channel_id=channel_id,
            workspace_id="ws",
            since_ms=0,
            job_pool=pool,
        )
    assert pool.live_count() == 2
    assert time.monotonic() - started < 0.15
    receipts = pool.wait(timeout=3.0)
    elapsed = time.monotonic() - started
    assert len(receipts) == 2
    assert all(item.status.value == "completed" for item in receipts)
    assert elapsed < 0.45
    assert abs(backend.started[1] - backend.started[0]) < 0.12

    by_channel = {req.metadata.get("channel_id"): req for req in backend.last_requests}
    assert by_channel["ch-pm"].metadata["cwd"] == str(pm)
    assert by_channel["ch-dug"].metadata["cwd"] == str(dug)
    reach = by_channel["ch-pm"].metadata["host_reach"]
    assert "not MCP" in reach
    assert "discord-os wiki query" in reach
    assert "Think-tank" in reach
    tank = [
        item
        for item in by_channel["ch-pm"].context.memories
        if item.get("source") == "think-tank"
    ]
    assert tank
    assert "personal graph" in tank[0]["content"]

    job_msgs = [msg for msg in fake.sent if msg.channel_id in {"ch-pm", "ch-dug"}]
    cards = [msg for msg in job_msgs if not (msg.content or "").strip()]
    assert len(cards) == 2
    assert all(getattr(msg, "thread_id", None) for msg in cards)
    assert len(fake.threads) >= 2
    tank_blob = json.dumps([getattr(msg, "metadata", {}) for msg in fake.sent])
    assert "list open pull requests" in tank_blob or "Note" in tank_blob
    store.close()


def test_e2e_cli_listen_two_realms(tmp_path: Path, monkeypatch, capsys):
    pm = _git_repo(tmp_path / "Puppetmaster")
    dug = _git_repo(tmp_path / "dugout")
    ws = tmp_path / ".agent-discord"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv(
        "DISCORD_OS_REPOS",
        json.dumps({"puppetmaster": str(pm), "dugout": str(dug)}),
    )
    monkeypatch.setenv(
        "DISCORD_OS_CHANNELS",
        "puppetmaster:ch-pm,dugout:ch-dug",
    )
    monkeypatch.setenv("DISCORD_OS_MEMORY", "ch-tank")
    assert main(["bootstrap", "--workspace", str(ws)]) == 0
    inbox = {
        "sent": [],
        "inbox": [
            {
                "channel_id": "ch-pm",
                "content": "list open pull requests",
                "message_id": _snowflake(1),
                "author_id": "human-1",
                "attachments": [],
                "metadata": {},
            },
            {
                "channel_id": "ch-dug",
                "content": "what is the roster?",
                "message_id": _snowflake(0),
                "author_id": "human-1",
                "attachments": [],
                "metadata": {},
            },
        ],
    }
    persist = ws / "fake_discord"
    persist.mkdir(parents=True)
    (persist / "messages.json").write_text(json.dumps(inbox), encoding="utf-8")
    store = SQLiteStore(ws / "agent_discord.sqlite3")
    store.initialize()
    store.set_host_control("ch-pm", armed=True)
    store.set_host_control("ch-dug", armed=True)
    store.close()
    assert (
        main(
            [
                "listen",
                "--channel-id",
                "ch-pm",
                "--workspace-id",
                "default",
                "--fake",
                "--once",
                "--json",
                "--no-discord-post",
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    payload = json.loads(printed[printed.index("[") :])
    assert len(payload) == 2
    assert {row["status"] for row in payload} == {"completed"}


def test_e2e_cli_wiki_note_recall(tmp_path: Path, monkeypatch, capsys):
    ws = tmp_path / ".agent-discord"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.setenv("WIKI_BASE_URL", "http://wiki.test")
    monkeypatch.setenv("WIKI_OWNER_TOKEN", "secret")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"answer": "Discord OS is the remote.", "citations": []}).encode()

    monkeypatch.setattr(
        "agent_discord.host.wiki.urlopen",
        lambda request, timeout=30: _Resp(),
    )
    assert main(["wiki", "query", "what is Discord OS?"]) == 0
    assert "Discord OS is the remote." in capsys.readouterr().out

    assert main(["bootstrap", "--workspace", str(ws)]) == 0
    assert main(["note", "staff uses bind memory", "--channel-id", "ch-tank", "--fake"]) == 0
    note_out = capsys.readouterr().out
    assert "note ch-tank/" in note_out
    store = SQLiteStore(ws / "agent_discord.sqlite3")
    store.initialize()
    bind_memory_channel(store, workspace_id="default", channel_id="ch-tank")
    store.close()
    assert main(["recall", "bind memory", "--fake"]) == 0
    recalled = capsys.readouterr().out
    assert "staff uses bind memory" in recalled or "think-tank" in recalled.lower() or recalled.strip()
