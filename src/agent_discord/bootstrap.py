"""Workspace bootstrap: directories, SQLite schema, config template."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_discord import PRODUCT_NAME
from agent_discord.config import AppConfig, load_config
from agent_discord.persistence.sqlite import SQLiteStore


BOOTSTRAP_MARKER = "bootstrap.json"
MINIMAL_ENV = """# Discord OS — fill these in. Never commit real tokens.
DISCORD_BOT_TOKEN=
DISCORD_APPLICATION_ID=
OPENROUTER_API_KEY=
"""


def bootstrap_workspace(
    *,
    workspace: Path | None = None,
    dotenv_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create local workspace, initialize SQLite, write bootstrap marker."""
    config = load_config(env=env, dotenv_path=dotenv_path, workspace=workspace)
    config.workspace.mkdir(parents=True, exist_ok=True)
    (config.workspace / "artifacts").mkdir(parents=True, exist_ok=True)
    (config.workspace / "logs").mkdir(parents=True, exist_ok=True)
    (config.workspace / "keys").mkdir(parents=True, exist_ok=True)
    (config.workspace / "stash").mkdir(parents=True, exist_ok=True)

    store = SQLiteStore(config.database_path)
    store.initialize()
    store.close()

    marker = {
        "product": PRODUCT_NAME,
        "version": "0.3.2",
        "workspace": str(config.workspace),
        "database": str(config.database_path),
        "discord_mcp_provider": config.discord_mcp_provider,
        "discord_mcp_transport": config.discord_mcp_transport,
        "agent_backend": config.agent_backend,
        "puppetmaster_model": config.puppetmaster_model,
        "puppetmaster_adapter_name": "grok-4.5",
    }
    marker_path = config.workspace / BOOTSTRAP_MARKER
    marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")

    env_example = Path.cwd() / ".env.example"
    env_target = Path.cwd() / ".env"
    created_env = False
    if not env_target.exists():
        if env_example.is_file():
            env_target.write_text(env_example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            env_target.write_text(MINIMAL_ENV, encoding="utf-8")
        created_env = True

    return {
        "workspace": str(config.workspace),
        "database": str(config.database_path),
        "marker": str(marker_path),
        "created_env": created_env,
        "config": config,
    }


def describe_bootstrap(config: AppConfig) -> dict[str, Any]:
    marker_path = config.workspace / BOOTSTRAP_MARKER
    if not marker_path.is_file():
        return {"bootstrapped": False, "workspace": str(config.workspace)}
    data = json.loads(marker_path.read_text(encoding="utf-8"))
    data["bootstrapped"] = True
    return data
