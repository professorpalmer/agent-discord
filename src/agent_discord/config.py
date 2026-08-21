"""Configuration loading for local bootstrap (env + workspace files)."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional

DEFAULT_HOST_BOT_TOKEN_PATH = Path.home() / ".pmharness" / ".discord_token"


class ConfigError(ValueError):
    """Invalid or incomplete local configuration."""


DEFAULT_SASEQ_MCP_HTTP_URL = "http://127.0.0.1:8085/mcp"
DEFAULT_BRAINDAO_MCP_HTTP_URL = "http://127.0.0.1:3000/mcp"


@dataclass(frozen=True)
class AppConfig:
    workspace: Path
    discord_bot_token: str
    discord_mcp_provider: str  # rest | saseq | braindao
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
    discord_max_object_bytes: int = 10_485_760
    compute: str = "auto"
    openrouter_env_fingerprint: str = ""
    host_actions: bool = True
    interactions: str = "off"
    discord_application_id: str = ""
    discord_public_key: str = ""
    interactions_host: str = "127.0.0.1"
    interactions_port: int = 8743

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

    provider = (merged.get("DISCORD_MCP_PROVIDER") or "rest").strip().lower()
    if provider not in {"rest", "saseq", "braindao"}:
        raise ConfigError(
            f"DISCORD_MCP_PROVIDER must be 'rest', 'saseq', or 'braindao', got {provider!r}"
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

    compute = (merged.get("AGENT_DISCORD_COMPUTE") or "auto").strip().lower()
    if compute not in {"auto", "cursor", "agentic"}:
        raise ConfigError(
            f"AGENT_DISCORD_COMPUTE must be 'auto', 'cursor', or 'agentic', got {compute!r}"
        )

    openrouter_env = (merged.get("OPENROUTER_API_KEY") or "").strip()
    openrouter_env_fingerprint = openrouter_env[-4:] if openrouter_env else ""

    raw_max = (merged.get("DISCORD_MAX_OBJECT_BYTES") or "").strip()
    if raw_max:
        try:
            max_object_bytes = int(raw_max)
        except ValueError as exc:
            raise ConfigError(
                f"DISCORD_MAX_OBJECT_BYTES must be an integer, got {raw_max!r}"
            ) from exc
        if max_object_bytes < 1:
            raise ConfigError("DISCORD_MAX_OBJECT_BYTES must be >= 1")
    else:
        max_object_bytes = 10_485_760

    interactions = (merged.get("AGENT_DISCORD_INTERACTIONS") or "off").strip().lower()
    if interactions not in {"off", "http"}:
        raise ConfigError(
            f"AGENT_DISCORD_INTERACTIONS must be 'off' or 'http', got {interactions!r}"
        )
    host_actions_raw = (merged.get("AGENT_DISCORD_HOST_ACTIONS") or "on").strip().lower()
    if host_actions_raw not in {"on", "off", "1", "0", "true", "false"}:
        raise ConfigError(
            f"AGENT_DISCORD_HOST_ACTIONS must be on or off, got {host_actions_raw!r}"
        )
    host_actions = host_actions_raw in {"on", "1", "true"}
    raw_port = (merged.get("DISCORD_INTERACTIONS_PORT") or "").strip()
    if raw_port:
        try:
            interactions_port = int(raw_port)
        except ValueError as exc:
            raise ConfigError(
                f"DISCORD_INTERACTIONS_PORT must be an integer, got {raw_port!r}"
            ) from exc
        if interactions_port < 1 or interactions_port > 65535:
            raise ConfigError("DISCORD_INTERACTIONS_PORT must be 1..65535")
    else:
        interactions_port = 8743

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
        discord_max_object_bytes=max_object_bytes,
        compute=compute,
        openrouter_env_fingerprint=openrouter_env_fingerprint,
        host_actions=host_actions,
        interactions=interactions,
        discord_application_id=(merged.get("DISCORD_APPLICATION_ID") or "").strip(),
        discord_public_key=(merged.get("DISCORD_PUBLIC_KEY") or "").strip(),
        interactions_host=(merged.get("DISCORD_INTERACTIONS_HOST") or "127.0.0.1").strip()
        or "127.0.0.1",
        interactions_port=interactions_port,
    )


def resolve_puppetmaster_cli(configured: str = "puppetmaster") -> str:
    """Prefer the CLI next to this Python. LaunchAgents often have a tiny PATH."""

    name = (configured or "puppetmaster").strip() or "puppetmaster"
    sibling = Path(sys.executable).resolve().parent / Path(name).name
    if sibling.is_file():
        return str(sibling)
    found = shutil.which(name)
    if found:
        return found
    return name


def read_host_bot_token(*, path: Optional[Path] = None) -> str:
    """Read a bot token from a 0600 host file. Never used as a default in tests."""

    target = Path(path) if path is not None else DEFAULT_HOST_BOT_TOKEN_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.strip():
        return ""
    return text.strip().splitlines()[0].strip()


def resolve_runtime_bot_token(config: AppConfig) -> str:
    if config.discord_bot_token.strip():
        return config.discord_bot_token.strip()
    return read_host_bot_token()


def discord_token_source(config: AppConfig, *, host_path: Optional[Path] = None) -> str:
    """Where the bot token was resolved from: env | host-file | empty. Never the token."""

    if config.discord_bot_token.strip():
        return "env"
    if read_host_bot_token(path=host_path):
        return "host-file"
    return "empty"


def apply_runtime_secrets(config: AppConfig) -> AppConfig:
    """Overlay host-file token when repo `.env` left DISCORD_BOT_TOKEN empty."""

    token = resolve_runtime_bot_token(config)
    if token == config.discord_bot_token:
        return config
    return replace(config, discord_bot_token=token)


def check_config(config: AppConfig, *, require_token: bool = True) -> list[str]:
    """Return human-readable problems; empty list means OK for local checks."""
    problems: list[str] = []
    if require_token and not config.discord_bot_token:
        problems.append("DISCORD_BOT_TOKEN is empty")
    if config.discord_mcp_provider not in {"rest", "saseq", "braindao"}:
        problems.append("invalid DISCORD_MCP_PROVIDER")
    if config.discord_mcp_provider != "rest":
        if config.discord_mcp_transport not in {"http", "stdio"}:
            problems.append("invalid DISCORD_MCP_TRANSPORT")
        if config.discord_mcp_transport == "stdio" and not config.discord_mcp_stdio_command:
            problems.append(
                "DISCORD_MCP_STDIO_COMMAND is required when DISCORD_MCP_TRANSPORT=stdio "
                "(no fabricated default npm package; set an explicit command, e.g. "
                "'npx -y @iqai/mcp-discord' for BrainDAO)"
            )
    if config.compute not in {"auto", "cursor", "agentic"}:
        problems.append("invalid AGENT_DISCORD_COMPUTE")
    if config.interactions not in {"off", "http"}:
        problems.append("invalid AGENT_DISCORD_INTERACTIONS")
    if config.interactions == "http":
        if not config.discord_application_id:
            problems.append("DISCORD_APPLICATION_ID is required when interactions=http")
        if not config.discord_public_key:
            problems.append("DISCORD_PUBLIC_KEY is required when interactions=http")
    resolution = resolve_compute(config)
    if resolution.mode == "cursor" and config.puppetmaster_model != "cursor/grok-4-5":
        problems.append(
            "PUPPETMASTER_MODEL must be cursor/grok-4-5 (pinned; no silent fallback)"
        )
    if resolution.mode == "agentic" and not has_openrouter_key(config):
        problems.append(
            "no OpenRouter key; run discord-os connect"
        )
    if config.agent_backend not in {"puppetmaster", "marionette"}:
        problems.append("invalid AGENT_DISCORD_BACKEND")
    if config.agent_backend == "marionette" and not config.marionette_base_url:
        problems.append(
            "MARIONETTE_BASE_URL is required when AGENT_DISCORD_BACKEND=marionette "
            "(optional seam; default backend remains puppetmaster)"
        )
    return problems


@dataclass(frozen=True)
class ComputeResolution:
    mode: str
    requested: str
    model: str


def keys_dir(config: AppConfig) -> Path:
    return config.workspace / "keys"


def has_openrouter_key(config: AppConfig) -> bool:
    if config.openrouter_env_fingerprint:
        return True
    from agent_discord.keys.vault import KeyVault

    vault = KeyVault(keys_dir(config))
    return bool(vault.fingerprint("openrouter"))


def resolve_compute(config: AppConfig) -> ComputeResolution:
    """Resolve auto|cursor|agentic. auto uses agentic when a key is present."""

    from agent_discord.puppetmaster.models import AGENTIC_CANONICAL_MODEL

    requested = config.compute
    if requested == "cursor":
        return ComputeResolution(
            mode="cursor",
            requested="cursor",
            model=config.puppetmaster_model,
        )
    if requested == "agentic":
        return ComputeResolution(
            mode="agentic",
            requested="agentic",
            model=AGENTIC_CANONICAL_MODEL,
        )
    if has_openrouter_key(config):
        return ComputeResolution(
            mode="agentic",
            requested="auto",
            model=AGENTIC_CANONICAL_MODEL,
        )
    return ComputeResolution(
        mode="cursor",
        requested="auto",
        model=config.puppetmaster_model,
    )
