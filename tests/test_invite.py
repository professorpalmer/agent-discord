"""Bot invite URL is bot-scope only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_discord.cli import main
from agent_discord.discord.invite import InviteError, bot_invite_url


def test_bot_invite_url_is_bot_scope_only():
    url = bot_invite_url("1523488316425506947")
    assert "client_id=1523488316425506947" in url
    assert "scope=bot" in url
    assert "applications.commands" not in url
    with pytest.raises(InviteError):
        bot_invite_url("")
    with pytest.raises(InviteError):
        bot_invite_url("not-a-snowflake")


def test_cli_invite_json(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(tmp_path / ".agent-discord"))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUPPETMASTER_MODEL", "cursor/grok-4-5")
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "1523488316425506947")
    assert main(["invite", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == "bot"
    assert "applications.commands" not in payload["url"]
