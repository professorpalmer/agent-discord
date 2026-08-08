"""Configuration loading for local bootstrap (env + workspace files)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


class ConfigError(ValueError):
    """Invalid or incomplete local configuration."""


DEFAULT_SASEQ_MCP_HTTP_URL = "http://127.0.0.1:8085/mcp"
DEFAULT_BRAINDAO_MCP_HTTP_URL = "http://127.0.0.1:3000/mcp"


@dataclass(frozen=True)
class AppConfig:
    workspace: Path
    discord_bot_token: str
    discord_mcp_provider: str  # saseq | braindao
    discord_mcp_transport: str  # http | stdio
    saseq_mcp_http_url: str
    braindao_mcp_http_url: str
    discord_mcp_stdio_command: str
    puppetmaster_model: str
    puppetmaster_cli: str
    puppetmaster_cwd: Path
    database_path: Path
    # Backend selector: puppetmaster (default) | marionette (explicit opt-in)
    agent_backend: str = "puppetmaster"
    marionette_base_url: str = ""
    marionette_sessions_path: str = "/v1/sessions"
    marionette_jobs_path: str = "/v1/jobs"
    marionette_api_token: str = ""

    @property
    def bot_token_fingerprint(self) -> str:
        token = self.discord_bot_token.strip()
        if not token:
            return "empty"
        # Stable, non-reversible-enough fingerprint for Gateway exclusivity keys.
        import hashlib

        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _parse_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


def load_config(
    *,
    env: Optional[Mapping[str, str]] = None,
    dotenv_path: Optional[Path] = None,
    workspace: Optional[Path] = None,
) -> AppConfig:
    """Load config from process env, optionally overlaying a .env file first."""
    merged: dict[str, str] = {}
    if dotenv_path is None:
        dotenv_path = Path.cwd() / ".env"
    merged.update(_parse_dotenv(dotenv_path))
    source = dict(os.environ if env is None else env)
    merged.update({k: v for k, v in source.items() if v is not None})

    ws = Path(
        workspace
        or merged.get("AGENT_DISCORD_WORKSPACE")
        or ".agent-discord"
    ).expanduser().resolve()

    provider = (merged.get("DISCORD_MCP_PROVIDER") or "saseq").strip().lower()
    if provider not in {"saseq", "braindao"}:
        raise ConfigError(
            f"DISCORD_MCP_PROVIDER must be 'saseq' or 'braindao', got {provider!r}"
        )

    transport = (merged.get("DISCORD_MCP_TRANSPORT") or "http").strip().lower()
    if transport not in {"http", "stdio"}:
        raise ConfigError(
            f"DISCORD_MCP_TRANSPORT must be 'http' or 'stdio', got {transport!r}"
        )

    model = (merged.get("PUPPETMASTER_MODEL") or "cursor/grok-4-5").strip()
    db_path = ws / "agent_discord.sqlite3"
    cwd_raw = (merged.get("PUPPETMASTER_CWD") or "").strip()
    puppetmaster_cwd = Path(cwd_raw).expanduser().resolve() if cwd_raw else Path.cwd()

    backend = (merged.get("AGENT_DISCORD_BACKEND") or "puppetmaster").strip().lower()
    if backend not in {"puppetmaster", "marionette"}:
        raise ConfigError(
            f"AGENT_DISCORD_BACKEND must be 'puppetmaster' or 'marionette', got {backend!r}"
        )

    return AppConfig(
        workspace=ws,
        discord_bot_token=(merged.get("DISCORD_BOT_TOKEN") or "").strip(),
        discord_mcp_provider=provider,
        discord_mcp_transport=transport,
        saseq_mcp_http_url=(
            merged.get("SASEQ_MCP_HTTP_URL") or DEFAULT_SASEQ_MCP_HTTP_URL
        ).strip(),
        braindao_mcp_http_url=(
            merged.get("BRAINDAO_MCP_HTTP_URL") or DEFAULT_BRAINDAO_MCP_HTTP_URL
        ).strip(),
        discord_mcp_stdio_command=(merged.get("DISCORD_MCP_STDIO_COMMAND") or "").strip(),
        puppetmaster_model=model,
        puppetmaster_cli=(merged.get("PUPPETMASTER_CLI") or "puppetmaster").strip(),
        puppetmaster_cwd=puppetmaster_cwd,
        database_path=db_path,
        agent_backend=backend,
        marionette_base_url=(merged.get("MARIONETTE_BASE_URL") or "").strip(),
        marionette_sessions_path=(
            merged.get("MARIONETTE_SESSIONS_PATH") or "/v1/sessions"
        ).strip(),
        marionette_jobs_path=(merged.get("MARIONETTE_JOBS_PATH") or "/v1/jobs").strip(),
        marionette_api_token=(merged.get("MARIONETTE_API_TOKEN") or "").strip(),
    )


def check_config(config: AppConfig, *, require_token: bool = True) -> list[str]:
    """Return human-readable problems; empty list means OK for local checks."""
    problems: list[str] = []
    if require_token and not config.discord_bot_token:
        problems.append("DISCORD_BOT_TOKEN is empty")
    if config.discord_mcp_provider not in {"saseq", "braindao"}:
        problems.append("invalid DISCORD_MCP_PROVIDER")
    if config.discord_mcp_transport not in {"http", "stdio"}:
        problems.append("invalid DISCORD_MCP_TRANSPORT")
    if config.discord_mcp_transport == "stdio" and not config.discord_mcp_stdio_command:
        problems.append(
            "DISCORD_MCP_STDIO_COMMAND is required when DISCORD_MCP_TRANSPORT=stdio "
            "(no fabricated default npm package; set an explicit command, e.g. "
            "'npx -y @iqai/mcp-discord' for BrainDAO)"
        )
    if config.puppetmaster_model != "cursor/grok-4-5":
        problems.append(
            "PUPPETMASTER_MODEL must be cursor/grok-4-5 (pinned; no silent fallback)"
        )
    if config.agent_backend not in {"puppetmaster", "marionette"}:
        problems.append("invalid AGENT_DISCORD_BACKEND")
    if config.agent_backend == "marionette" and not config.marionette_base_url:
        problems.append(
            "MARIONETTE_BASE_URL is required when AGENT_DISCORD_BACKEND=marionette "
            "(optional seam; default backend remains puppetmaster)"
        )
    return problems
