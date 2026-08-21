"""Incremental add: realms, memory, wiki, tools. No wizard."""

from __future__ import annotations

import json
from pathlib import Path

from agent_discord.cli import main
from agent_discord.host.add import (
    add_memory,
    add_realm,
    add_repo,
    add_tool,
    add_wiki,
    list_added,
    merge_named_csv,
    read_dotenv,
    upsert_dotenv,
)
from agent_discord.host.repos import HostRepo
from agent_discord.persistence.sqlite import SQLiteStore


def test_upsert_dotenv_keeps_other_keys(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text("DISCORD_BOT_TOKEN=secret\n# comment\nFOO=1\n", encoding="utf-8")
    upsert_dotenv(path, {"FOO": "2", "WIKI_BASE_URL": "http://wiki.test"})
    text = path.read_text(encoding="utf-8")
    assert "DISCORD_BOT_TOKEN=secret" in text
    assert "# comment" in text
    assert "FOO=2" in text
    assert "WIKI_BASE_URL=http://wiki.test" in text
    assert read_dotenv(path)["FOO"] == "2"


def test_merge_named_csv_replaces_same_name():
    assert merge_named_csv("puppetmaster:1", "puppetmaster", "2") == "puppetmaster:2"
    assert "dugout:9" in merge_named_csv("puppetmaster:1", "dugout", "9")


def test_add_realm_and_memory_persist(tmp_path: Path):
    repo = tmp_path / "Puppetmaster"
    repo.mkdir()
    (repo / ".git").mkdir()
    store = SQLiteStore(tmp_path / "add.sqlite3")
    store.initialize()
    env = tmp_path / ".env"
    added = add_realm(
        store,
        name="puppetmaster",
        channel_id="ch-pm",
        env_file=env,
        repos=(HostRepo(name="puppetmaster", path=repo, aliases=("puppetmaster",)),),
    )
    assert added["cwd"] == str(repo)
    from agent_discord.host.realms import binding_metadata

    assert binding_metadata(store.get_binding("default", "ch-pm"))["repo"] == "puppetmaster"
    add_memory(store, channel_id="ch-tank", env_file=env)
    listed = list_added(store, env_file=env)
    assert listed["memory"] == ["ch-tank"]
    assert listed["realms"][0]["channel_id"] == "ch-pm"
    store.close()


def test_add_wiki_tool_repo_and_cli(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / ".agent-discord"
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    repo = tmp_path / "dugout"
    repo.mkdir()
    (repo / ".git").mkdir()
    assert main(["bootstrap", "--workspace", str(ws)]) == 0
    assert main(["add", "repo", "dugout", "--path", str(repo)]) == 0
    assert main(["add", "wiki", "--url", "https://portablellm.wiki/professorpalmer", "--token", "x"]) == 0
    assert main(["add", "tool", "aws", "--bin", "aws", "--hint", "sts"]) == 0
    env = read_dotenv(tmp_path / ".env")
    assert env["DISCORD_OS_REPOS"].endswith(str(repo)) or str(repo) in env["DISCORD_OS_REPOS"]
    assert env["WIKI_BASE_URL"] == "https://portablellm.wiki/professorpalmer"
    assert env["WIKI_OWNER_TOKEN"] == "x"
    tools = json.loads(env["DISCORD_OS_TOOLS"])
    assert tools["aws"]["bin"] == "aws"
    capsys.readouterr()
    assert main(["add", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["wiki"] == "https://portablellm.wiki/professorpalmer"
    assert any(row["name"] == "dugout" for row in payload["repos"])
    assert main(["add", "wiki"]) == 2


def test_add_github_writes_token(tmp_path: Path, monkeypatch):
    from agent_discord.host.add import add_github, read_dotenv

    monkeypatch.setattr(
        "agent_discord.host.github.which_on_host",
        lambda name, env=None: "/opt/homebrew/bin/gh",
    )
    env = tmp_path / ".env"
    added = add_github(token="ghp_secret", env_file=env)
    assert added["kind"] == "github"
    assert added["token"] is True
    stored = read_dotenv(env)
    assert stored["GH_TOKEN"] == "ghp_secret"
    assert "github" in stored.get("DISCORD_OS_TOOLS", "")
