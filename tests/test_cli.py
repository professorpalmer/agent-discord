"""CLI smoke tests with fakes."""

from __future__ import annotations

from pathlib import Path

from agent_discord.cli import main


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
