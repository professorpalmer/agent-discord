"""Incremental host wiring. Not a setup wizard.

``discord-os setup`` invites the bot and starts the helper once.
``discord-os add`` writes one workflow seam at a time: realm, memory,
repo, wiki, or a named CLI/HTTP tool. Discord ``bind`` does the same
for realm and memory without leaving the phone.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

from agent_discord.host.memory import bind_memory_channel, memory_channel_ids
from agent_discord.host.realms import bind_channel_realm, parse_channel_realms
from agent_discord.host.repos import HostRepo, load_host_repos
from agent_discord.host.tools import load_host_tools
from agent_discord.host.wiki import wiki_base_url


SECRET_KEYS = frozenset(
    {
        "WIKI_OWNER_TOKEN",
        "OWNER_TOKEN",
        "WIKI_SHARE_TOKEN",
        "DISCORD_BOT_TOKEN",
        "OPENROUTER_API_KEY",
        "MARIONETTE_API_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    }
)


def dotenv_path(*, cwd: Optional[Path] = None) -> Path:
    return (cwd or Path.cwd()) / ".env"


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value
    return values


def upsert_dotenv(path: Path, updates: Mapping[str, str]) -> None:
    """Replace or append KEY=value lines. Preserve comments and other keys."""

    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    if any(key in SECRET_KEYS for key in updates):
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def merge_named_csv(raw: str, name: str, value: str) -> str:
    mapping: dict[str, str] = {}
    text = (raw or "").strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            mapping = {str(k).strip().lower(): str(v).strip() for k, v in parsed.items()}
    else:
        for item in text.split(","):
            key, sep, rest = item.partition(":")
            if not sep:
                continue
            mapping[key.strip().lower()] = rest.strip()
    mapping[name.strip().lower()] = value.strip()
    return ",".join(f"{key}:{val}" for key, val in mapping.items() if key and val)


def merge_id_csv(raw: str, channel_id: str) -> str:
    seen: list[str] = []
    known: set[str] = set()
    text = (raw or "").strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = []
        items = parsed if isinstance(parsed, list) else []
    else:
        items = text.split(",")
    for item in items:
        value = str(item).strip()
        if value and value not in known:
            seen.append(value)
            known.add(value)
    if channel_id not in known:
        seen.append(channel_id)
    return ",".join(seen)


def merge_tools_json(raw: str, name: str, spec: Mapping[str, str]) -> str:
    catalog: dict[str, Any] = {}
    text = (raw or "").strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            catalog = dict(parsed)
    catalog[name.strip().lower()] = dict(spec)
    return json.dumps(catalog, separators=(",", ":"))


def add_realm(
    store: Any,
    *,
    name: str,
    channel_id: str,
    workspace_id: str = "default",
    env_file: Optional[Path] = None,
    repos: Optional[tuple[HostRepo, ...]] = None,
) -> dict[str, Any]:
    catalog = repos if repos is not None else load_host_repos()
    chosen = bind_channel_realm(
        store,
        workspace_id=workspace_id,
        channel_id=channel_id,
        name=name,
        repos=catalog,
    )
    path = env_file or dotenv_path()
    current = read_dotenv(path)
    upsert_dotenv(
        path,
        {
            "DISCORD_OS_CHANNELS": merge_named_csv(
                current.get("DISCORD_OS_CHANNELS") or os.environ.get("DISCORD_OS_CHANNELS") or "",
                name,
                channel_id,
            )
        },
    )
    return {
        "kind": "realm",
        "name": chosen.name if chosen is not None else name.strip().lower(),
        "channel_id": channel_id,
        "cwd": str(chosen.path) if chosen is not None else "",
        "env": str(path),
        "live": chosen is not None,
    }


def add_memory(
    store: Any,
    *,
    channel_id: str,
    workspace_id: str = "default",
    env_file: Optional[Path] = None,
) -> dict[str, Any]:
    bind_memory_channel(store, workspace_id=workspace_id, channel_id=channel_id)
    path = env_file or dotenv_path()
    current = read_dotenv(path)
    upsert_dotenv(
        path,
        {
            "DISCORD_OS_MEMORY": merge_id_csv(
                current.get("DISCORD_OS_MEMORY") or os.environ.get("DISCORD_OS_MEMORY") or "",
                channel_id,
            )
        },
    )
    return {
        "kind": "memory",
        "channel_id": channel_id,
        "env": str(path),
        "live": True,
    }


def add_repo(
    *,
    name: str,
    path: Path,
    env_file: Optional[Path] = None,
) -> dict[str, Any]:
    root = path.expanduser().resolve()
    dest = env_file or dotenv_path()
    current = read_dotenv(dest)
    upsert_dotenv(
        dest,
        {
            "DISCORD_OS_REPOS": merge_named_csv(
                current.get("DISCORD_OS_REPOS") or os.environ.get("DISCORD_OS_REPOS") or "",
                name,
                str(root),
            )
        },
    )
    return {
        "kind": "repo",
        "name": name.strip().lower(),
        "path": str(root),
        "git": (root / ".git").exists(),
        "env": str(dest),
        "restart": True,
    }


def add_wiki(
    *,
    url: str = "",
    token: str = "",
    env_file: Optional[Path] = None,
) -> dict[str, Any]:
    dest = env_file or dotenv_path()
    updates: dict[str, str] = {}
    if url.strip():
        updates["WIKI_BASE_URL"] = url.strip().rstrip("/")
    if token.strip():
        updates["WIKI_OWNER_TOKEN"] = token.strip()
    if not updates:
        raise ValueError("add wiki needs --url and/or --token")
    upsert_dotenv(dest, updates)
    return {
        "kind": "wiki",
        "url": updates.get("WIKI_BASE_URL") or wiki_base_url(read_dotenv(dest)),
        "token": bool(token.strip() or read_dotenv(dest).get("WIKI_OWNER_TOKEN")),
        "env": str(dest),
        "restart": True,
    }


def add_tool(
    *,
    name: str,
    bin: str = "",
    url: str = "",
    hint: str = "",
    env_file: Optional[Path] = None,
) -> dict[str, Any]:
    key = name.strip().lower()
    spec: dict[str, str] = {}
    if url.strip():
        spec["kind"] = "http"
        spec["url"] = url.strip()
    elif bin.strip():
        spec["kind"] = "cli"
        spec["bin"] = bin.strip()
    else:
        raise ValueError("add tool needs --bin or --url")
    if hint.strip():
        spec["hint"] = hint.strip()
    dest = env_file or dotenv_path()
    current = read_dotenv(dest)
    upsert_dotenv(
        dest,
        {
            "DISCORD_OS_TOOLS": merge_tools_json(
                current.get("DISCORD_OS_TOOLS") or os.environ.get("DISCORD_OS_TOOLS") or "",
                key,
                spec,
            )
        },
    )
    return {
        "kind": "tool",
        "name": key,
        "spec": spec,
        "env": str(dest),
        "restart": True,
    }



def add_github(
    *,
    token: str = "",
    env_file: Optional[Path] = None,
) -> dict[str, Any]:
    """Write GH_TOKEN into the host .env. Auth is inherited by every worker."""

    from agent_discord.host.github import gh_auth_state, host_home

    dest = env_file
    if dest is None:
        home = host_home()
        dest = (home / ".env") if home.is_dir() else dotenv_path()
    updates: dict[str, str] = {}
    if token.strip():
        updates["GH_TOKEN"] = token.strip()
    current = read_dotenv(dest)
    updates["DISCORD_OS_TOOLS"] = merge_tools_json(
        current.get("DISCORD_OS_TOOLS") or os.environ.get("DISCORD_OS_TOOLS") or "",
        "github",
        {"kind": "cli", "bin": "gh", "hint": "gh pr list --state open"},
    )
    upsert_dotenv(dest, updates)
    stored = read_dotenv(dest)
    return {
        "kind": "github",
        "env": str(dest),
        "token": bool(
            token.strip()
            or stored.get("GH_TOKEN")
            or stored.get("GITHUB_TOKEN")
        ),
        "state": gh_auth_state(),
        "restart": True,
    }


def list_added(
    store: Any = None,
    *,
    workspace_id: str = "default",
    env_file: Optional[Path] = None,
) -> dict[str, Any]:
    path = env_file or dotenv_path()
    file_env = read_dotenv(path)
    merged = dict(os.environ)
    merged.update(file_env)
    repos = load_host_repos(env=merged)
    realms = [
        {"name": item.name, "channel_id": item.channel_id, "cwd": str(item.cwd or "")}
        for item in parse_channel_realms(merged.get("DISCORD_OS_CHANNELS") or "", repos)
    ]
    tools = [
        {
            "name": item.name,
            "kind": item.kind,
            "ready": item.ready,
            "hint": item.hint or item.bin or item.url,
        }
        for item in load_host_tools(env=merged)
        if item.ready
    ]
    return {
        "env": str(path) if path.is_file() else "",
        "repos": [{"name": repo.name, "path": str(repo.path)} for repo in repos],
        "realms": realms,
        "memory": list(memory_channel_ids(store, workspace_id=workspace_id, env=merged)),
        "wiki": wiki_base_url(merged),
        "tools": tools,
    }
