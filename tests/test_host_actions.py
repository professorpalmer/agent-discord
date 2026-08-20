"""Host surfaces: Discord is the remote; this process opens Terminal/files/browser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_discord.cli import main
from agent_discord.contracts import DiscordMessage
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.host.actions import (
    HostActionError,
    allow_browser_url,
    confine_host_path,
    run_host_action,
)
from agent_discord.host.verbs import handle_open_message, is_open_command, parse_open_command
from agent_discord.orchestration.cards import CARD_PREFIX
from agent_discord.orchestration.listen import drain_inbound, should_dispatch_inbound
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend


def test_confine_host_path_stays_inside_roots(tmp_path: Path):
    nested = tmp_path / "src"
    nested.mkdir()
    resolved = confine_host_path("src", [tmp_path])
    assert resolved == nested.resolve()
    assert confine_host_path(str(nested), [tmp_path]) == nested.resolve()


def test_confine_host_path_rejects_home_and_escape(tmp_path: Path):
    with pytest.raises(HostActionError, match="home-relative"):
        confine_host_path("~/.ssh", [tmp_path])
    outside = tmp_path.parent / "outside-host-root"
    with pytest.raises(HostActionError, match="outside host roots"):
        confine_host_path(str(outside), [tmp_path])
    with pytest.raises(HostActionError, match="no host roots"):
        confine_host_path(".", [])


def test_allow_browser_url_allowlist():
    assert allow_browser_url("http://127.0.0.1:8743/health") == "http://127.0.0.1:8743/health"
    assert allow_browser_url("https://localhost/ok") == "https://localhost/ok"
    jump = "https://discord.com/channels/1/2/3"
    assert allow_browser_url(jump) == jump
    with pytest.raises(HostActionError, match="allowlist"):
        allow_browser_url("https://example.com/")
    with pytest.raises(HostActionError, match="http"):
        allow_browser_url("javascript:alert(1)")
    with pytest.raises(HostActionError, match="jump"):
        allow_browser_url("https://discord.com/app")


def test_run_host_action_uses_injected_runner(tmp_path: Path):
    calls: list[tuple[list[str], str | None]] = []

    def runner(argv, *, cwd=None):
        calls.append((list(argv), cwd))

    opened_urls: list[str] = []
    files = run_host_action("files", ".", roots=[tmp_path], runner=runner)
    assert files.opened
    assert calls
    assert Path(files.target) == tmp_path.resolve()
    term = run_host_action("terminal", ".", roots=[tmp_path], runner=runner)
    assert term.opened
    assert term.argv
    browser = run_host_action(
        "browser",
        "http://127.0.0.1:9/health",
        roots=[tmp_path],
        browser_open=opened_urls.append,
    )
    assert browser.opened
    assert opened_urls == ["http://127.0.0.1:9/health"]
    with pytest.raises(HostActionError, match="unknown surface"):
        run_host_action("camera", ".", roots=[tmp_path], runner=runner)


def test_parse_open_command_surfaces():
    assert is_open_command("/open terminal")
    assert is_open_command("!open files src")
    parsed = parse_open_command("/open terminal src")
    assert parsed.surface == "terminal"
    assert parsed.target == "src"
    assert parse_open_command("/open https://discord.com/channels/1/2/3").surface == "browser"
    assert parse_open_command("/open").surface == "files"


def test_listen_open_does_not_dispatch_implement(tmp_path: Path):
    store = SQLiteStore(tmp_path / "open.sqlite3")
    store.initialize()
    fake = FakeDiscordMCPProvider()
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="/open terminal .",
            message_id="open-1",
            author_id="phone",
        )
    )
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    backend = FakePuppetmasterBackend()
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        discord=facade,
        workspace=tmp_path,
    )
    calls: list[tuple[list[str], str | None]] = []

    def runner(argv, *, cwd=None):
        calls.append((list(argv), cwd))

    receipts = drain_inbound(
        orch,
        facade,
        channel_id="ch",
        workspace_id="ws",
        workspace=tmp_path,
        host_roots=[tmp_path],
        host_runner=runner,
    )
    assert receipts == []
    assert backend.dispatch_count == 0
    assert calls
    cards = [m.content for m in fake.sent]
    assert any(c.startswith(f"{CARD_PREFIX} OPEN") for c in cards)
    store.close()


def test_should_dispatch_skips_open_cards():
    assert not should_dispatch_inbound(
        DiscordMessage(channel_id="ch", content=f"{CARD_PREFIX} OPEN\nSurface: `files`")
    )


def test_cli_open_json_uses_injected_handler(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / ".agent-discord"
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUPPETMASTER_MODEL", "cursor/grok-4-5")

    def fake_handle(text, *, roots, runner=None, browser_open=None):
        from agent_discord.host.verbs import OpenPublicResult

        return OpenPublicResult(
            surface="files",
            target=str(tmp_path),
            card=f"{CARD_PREFIX} OPEN\nSurface: `files`",
            opened=True,
        )

    monkeypatch.setattr("agent_discord.host.verbs.handle_open_message", fake_handle)
    assert main(["open", "files", ".", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["opened"] is True
    assert payload["surface"] == "files"


def test_handle_open_message_rejects_escape(tmp_path: Path):
    result = handle_open_message(
        "/open files /etc/passwd",
        roots=[tmp_path],
        runner=lambda *a, **k: None,
    )
    assert result.opened is False
    assert "outside" in result.error
    assert result.card.startswith(f"{CARD_PREFIX} OPEN")
