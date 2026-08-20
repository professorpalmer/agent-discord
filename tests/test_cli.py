"""CLI smoke tests with fakes."""

from __future__ import annotations

from pathlib import Path

from agent_discord.cli import build_parser, main
from agent_discord.contracts import DiscordMessage


def test_cli_help_says_discord_os():
    parser = build_parser()
    help_text = parser.format_help()
    assert parser.prog == "discord-os"
    assert "Discord OS" in help_text
    assert help_text.startswith("usage: discord-os")


def test_cli_check_live_channel(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / ".agent-discord"
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUPPETMASTER_MODEL", "cursor/grok-4-5")
    monkeypatch.setattr(
        "agent_discord.discord.rest.fetch_bot_identity",
        lambda token: {"id": "1", "username": "staff-bot"},
    )
    monkeypatch.setattr(
        "agent_discord.discord.rest.list_channel_messages",
        lambda **kwargs: [
            DiscordMessage(channel_id=kwargs["channel_id"], content="hello", message_id="9")
        ],
    )
    assert main(["bootstrap", "--workspace", str(ws)]) == 0
    assert main(["check", "--live", "--channel-id", "99"]) == 0
    out = capsys.readouterr().out
    assert "staff-bot" in out
    assert "live chan:" in out
    assert "content=yes" in out


def test_cli_bootstrap_check_run(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / ".agent-discord"
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUPPETMASTER_MODEL", "cursor/grok-4-5")

    assert main(["bootstrap", "--workspace", str(ws)]) == 0
    assert main(["check"]) == 0
    code = main(
        [
            "run",
            "say hello",
            "--channel-id",
            "99",
            "--fake",
            "--no-discord-post",
            "--json",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "run_id" in out
    assert "completed" in out
