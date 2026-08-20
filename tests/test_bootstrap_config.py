"""Bootstrap + config checks."""

from __future__ import annotations

from pathlib import Path

from agent_discord.bootstrap import bootstrap_workspace, describe_bootstrap
from agent_discord.config import (
    DEFAULT_SASEQ_MCP_HTTP_URL,
    ConfigError,
    check_config,
    load_config,
)
from agent_discord.puppetmaster.models import CANONICAL_MODEL


def test_load_config_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    cfg = load_config(env={"AGENT_DISCORD_WORKSPACE": str(tmp_path / "ws")})
    assert cfg.discord_mcp_provider == "rest"
    assert cfg.puppetmaster_model == CANONICAL_MODEL
    assert cfg.workspace == (tmp_path / "ws").resolve()
    assert cfg.saseq_mcp_http_url == DEFAULT_SASEQ_MCP_HTTP_URL
    assert "8085" in cfg.saseq_mcp_http_url
    assert cfg.puppetmaster_cwd == tmp_path.resolve()
    assert cfg.agent_backend == "puppetmaster"
    assert cfg.marionette_base_url == ""
    assert cfg.host_actions is True
    assert cfg.interactions == "off"


def test_check_config_requires_token_and_pin(tmp_path: Path):
    cfg = load_config(
        env={
            "AGENT_DISCORD_WORKSPACE": str(tmp_path),
            "DISCORD_BOT_TOKEN": "",
            "PUPPETMASTER_MODEL": "cursor/other",
        },
        dotenv_path=tmp_path / "missing.env",
    )
    problems = check_config(cfg, require_token=True)
    assert any("DISCORD_BOT_TOKEN" in p for p in problems)
    assert any("PUPPETMASTER_MODEL" in p for p in problems)


def test_check_config_requires_stdio_command(tmp_path: Path):
    cfg = load_config(
        env={
            "AGENT_DISCORD_WORKSPACE": str(tmp_path),
            "DISCORD_BOT_TOKEN": "tok",
            "DISCORD_MCP_PROVIDER": "saseq",
            "DISCORD_MCP_TRANSPORT": "stdio",
            "DISCORD_MCP_STDIO_COMMAND": "",
            "PUPPETMASTER_MODEL": "cursor/grok-4-5",
        },
        dotenv_path=tmp_path / "missing.env",
    )
    problems = check_config(cfg, require_token=True)
    assert any("DISCORD_MCP_STDIO_COMMAND" in p for p in problems)


def test_bootstrap_creates_workspace_and_db(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / "workspace"
    result = bootstrap_workspace(
        workspace=ws,
        env={"AGENT_DISCORD_WORKSPACE": str(ws), "DISCORD_BOT_TOKEN": "test-token"},
        dotenv_path=tmp_path / "nope.env",
    )
    assert Path(result["database"]).is_file()
    assert Path(result["marker"]).is_file()
    cfg = result["config"]
    info = describe_bootstrap(cfg)
    assert info["bootstrapped"] is True
    assert info["product"] == "Discord OS"
    assert info["puppetmaster_adapter_name"] == "grok-4.5"
    assert info["agent_backend"] == "puppetmaster"


def test_marionette_backend_requires_base_url(tmp_path: Path):
    cfg = load_config(
        env={
            "AGENT_DISCORD_WORKSPACE": str(tmp_path),
            "DISCORD_BOT_TOKEN": "tok",
            "PUPPETMASTER_MODEL": "cursor/grok-4-5",
            "AGENT_DISCORD_BACKEND": "marionette",
            "MARIONETTE_BASE_URL": "",
        },
        dotenv_path=tmp_path / "missing.env",
    )
    problems = check_config(cfg, require_token=True)
    assert any("MARIONETTE_BASE_URL" in p for p in problems)


def test_marionette_backend_ok_when_configured(tmp_path: Path):
    cfg = load_config(
        env={
            "AGENT_DISCORD_WORKSPACE": str(tmp_path),
            "DISCORD_BOT_TOKEN": "tok",
            "PUPPETMASTER_MODEL": "cursor/grok-4-5",
            "AGENT_DISCORD_BACKEND": "marionette",
            "MARIONETTE_BASE_URL": "http://127.0.0.1:8787",
        },
        dotenv_path=tmp_path / "missing.env",
    )
    assert cfg.agent_backend == "marionette"
    assert check_config(cfg, require_token=True) == []


def test_interactions_http_requires_application_id_and_public_key(tmp_path: Path):
    cfg = load_config(
        env={
            "AGENT_DISCORD_WORKSPACE": str(tmp_path),
            "DISCORD_BOT_TOKEN": "tok",
            "PUPPETMASTER_MODEL": "cursor/grok-4-5",
            "AGENT_DISCORD_INTERACTIONS": "http",
        },
        dotenv_path=tmp_path / "missing.env",
    )
    problems = check_config(cfg, require_token=True)
    assert any("DISCORD_APPLICATION_ID" in p for p in problems)
    assert any("DISCORD_PUBLIC_KEY" in p for p in problems)
    ok = load_config(
        env={
            "AGENT_DISCORD_WORKSPACE": str(tmp_path),
            "DISCORD_BOT_TOKEN": "tok",
            "PUPPETMASTER_MODEL": "cursor/grok-4-5",
            "AGENT_DISCORD_INTERACTIONS": "http",
            "DISCORD_APPLICATION_ID": "app",
            "DISCORD_PUBLIC_KEY": "aa" * 32,
        },
        dotenv_path=tmp_path / "missing.env",
    )
    assert ok.interactions == "http"
    assert check_config(ok, require_token=True) == []


def test_load_config_rejects_unknown_interactions(tmp_path: Path):
    try:
        load_config(
            env={
                "AGENT_DISCORD_WORKSPACE": str(tmp_path),
                "AGENT_DISCORD_INTERACTIONS": "gateway",
            },
            dotenv_path=tmp_path / "missing.env",
        )
        raised = False
    except ConfigError as exc:
        raised = True
        assert "off" in str(exc)
    assert raised
