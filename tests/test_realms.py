"""Channel-to-repo realm binding."""

from __future__ import annotations

from pathlib import Path

from agent_discord.contracts import DiscordMessage, TaskIntake
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.host.realms import (
    bind_channel_realm,
    is_bind_command,
    listen_channel_ids,
    parse_bind_command,
    parse_channel_realms,
    seed_channel_realms,
)
from agent_discord.host.repos import HostRepo
from agent_discord.orchestration.listen import drain_inbound
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def test_parse_channel_realms_json_and_csv(tmp_path: Path):
    pm = _git_repo(tmp_path / "Puppetmaster")
    repos = (HostRepo(name="puppetmaster", path=pm, aliases=("puppetmaster",)),)
    csv = parse_channel_realms("puppetmaster:111,dugout:222", repos)
    assert csv[0].name == "puppetmaster"
    assert csv[0].channel_id == "111"
    assert csv[0].cwd == pm
    blob = parse_channel_realms('{"marionette":"333"}', repos)
    assert blob[0].name == "marionette"
    assert blob[0].channel_id == "333"


def test_bind_command_and_seed(tmp_path: Path):
    pm = _git_repo(tmp_path / "Puppetmaster")
    repos = (HostRepo(name="puppetmaster", path=pm, aliases=("puppetmaster",)),)
    store = SQLiteStore(tmp_path / "r.sqlite3")
    store.initialize()
    assert is_bind_command("bind puppetmaster")
    assert parse_bind_command("bind puppetmaster") == "puppetmaster"
    chosen = bind_channel_realm(
        store,
        workspace_id="ws",
        channel_id="ch-pm",
        name="puppetmaster",
        repos=repos,
    )
    assert chosen is not None
    row = store.get_binding("ws", "ch-pm")
    assert row is not None
    assert "puppetmaster" in str(row.get("metadata_json") or "")
    seeded = seed_channel_realms(
        store,
        workspace_id="ws",
        env={"DISCORD_OS_CHANNELS": "dugout:ch-dug"},
        repos=repos,
    )
    assert seeded[0].channel_id == "ch-dug"
    ids = listen_channel_ids(
        "home",
        store,
        workspace_id="ws",
        env={"DISCORD_OS_CHANNELS": "dugout:ch-dug"},
        repos=repos,
    )
    assert ids[0] == "home"
    assert "ch-dug" in ids
    assert "ch-pm" in ids
    store.close()


def test_channel_realm_sets_cwd_without_naming_repo(tmp_path: Path):
    pm = _git_repo(tmp_path / "Puppetmaster")
    repos = (HostRepo(name="puppetmaster", path=pm, aliases=("puppetmaster",)),)
    store = SQLiteStore(tmp_path / "cwd.sqlite3")
    store.initialize()
    bind_channel_realm(
        store,
        workspace_id="ws",
        channel_id="ch-pm",
        name="puppetmaster",
        repos=repos,
    )
    backend = FakePuppetmasterBackend()
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        post_progress_to_discord=False,
        host_repos=repos,
    )
    orch.run_task(
        TaskIntake(text="list open pull requests", channel_id="ch-pm", workspace_id="ws")
    )
    assert backend.last_request is not None
    assert backend.last_request.metadata["cwd"] == str(pm)
    assert backend.last_request.metadata["repo"] == "puppetmaster"
    store.close()


def test_bind_message_updates_host_card(tmp_path: Path):
    pm = _git_repo(tmp_path / "Puppetmaster")
    repos = (HostRepo(name="puppetmaster", path=pm, aliases=("puppetmaster",)),)
    store = SQLiteStore(tmp_path / "bind.sqlite3")
    store.initialize()
    fake = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    orch = AgentOrchestrator(
        store=store,
        backend=FakePuppetmasterBackend(),
        discord=facade,
        post_progress_to_discord=True,
        host_repos=repos,
    )
    store.set_host_control("ch", armed=True)
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="bind puppetmaster",
            message_id="90",
            author_id="human-1",
        )
    )
    drain_inbound(
        orch,
        orch.discord,
        channel_id="ch",
        workspace_id="ws",
        since_ms=0,
    )
    row = store.get_binding("ws", "ch")
    assert row is not None
    assert "puppetmaster" in str(row.get("metadata_json") or "")
    store.close()
