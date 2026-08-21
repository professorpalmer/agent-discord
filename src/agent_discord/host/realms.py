"""Channel-to-repo realms.

One Discord channel is one checkout. Steal Hermes session isolation
(platform + channel = room), not Bot Mode chrome.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from agent_discord.host.repos import HostRepo, load_host_repos


BIND_PREFIXES = frozenset(
    {
        "/bind",
        "!bind",
        "bind",
        "/realm",
        "!realm",
        "realm",
        "/memory",
        "!memory",
        "memory",
        "/bank",
        "!bank",
        "bank",
    }
)


@dataclass(frozen=True)
class ChannelRealm:
    name: str
    channel_id: str
    cwd: Optional[Path] = None


def binding_metadata(row: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if not row:
        return {}
    raw = row.get("metadata_json") if "metadata_json" in row else row.get("metadata")
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def parse_channel_realms(
    raw: str,
    repos: Sequence[HostRepo] = (),
) -> tuple[ChannelRealm, ...]:
    text = (raw or "").strip()
    if not text:
        return ()
    mapping: dict[str, str] = {}
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            mapping = {str(k).strip().lower(): str(v).strip() for k, v in parsed.items()}
    else:
        for item in text.split(","):
            name, sep, channel_id = item.partition(":")
            if not sep:
                continue
            mapping[name.strip().lower()] = channel_id.strip()
    by_name = {repo.name.lower(): repo for repo in repos}
    found = []
    for name, channel_id in mapping.items():
        if not name or not channel_id:
            continue
        repo = by_name.get(name)
        found.append(
            ChannelRealm(
                name=name,
                channel_id=channel_id,
                cwd=repo.path if repo is not None else None,
            )
        )
    return tuple(found)


def seed_channel_realms(
    store: Any,
    *,
    workspace_id: str,
    env: Optional[Mapping[str, str]] = None,
    repos: Optional[Sequence[HostRepo]] = None,
) -> tuple[ChannelRealm, ...]:
    source = dict(os.environ if env is None else env)
    catalog = tuple(repos) if repos is not None else load_host_repos(env=source)
    realms = parse_channel_realms(source.get("DISCORD_OS_CHANNELS") or "", catalog)
    writer = getattr(store, "merge_binding_metadata", None)
    if not callable(writer):
        return realms
    for realm in realms:
        updates = {"repo": realm.name}
        if realm.cwd is not None:
            updates["cwd"] = str(realm.cwd)
        writer(
            workspace_id,
            realm.channel_id,
            updates,
        )
    return realms


def listen_channel_ids(
    primary: str,
    store: Any = None,
    *,
    workspace_id: str = "default",
    env: Optional[Mapping[str, str]] = None,
    repos: Optional[Sequence[HostRepo]] = None,
) -> tuple[str, ...]:
    ids: list[str] = []
    seen = set()
    for item in (primary,):
        channel = str(item or "").strip()
        if channel and channel not in seen:
            ids.append(channel)
            seen.add(channel)
    catalog = tuple(repos) if repos is not None else ()
    source = dict(os.environ if env is None else env)
    for realm in parse_channel_realms(source.get("DISCORD_OS_CHANNELS") or "", catalog):
        if realm.channel_id not in seen:
            ids.append(realm.channel_id)
            seen.add(realm.channel_id)
    lister = getattr(store, "list_bindings", None) if store is not None else None
    if callable(lister):
        for row in lister(workspace_id) or ():
            channel = str(row.get("channel_id") or "").strip()
            meta = binding_metadata(row)
            if channel and (meta.get("repo") or meta.get("memory")) and channel not in seen:
                ids.append(channel)
                seen.add(channel)
    return tuple(ids)


def realm_for_channel(
    store: Any,
    channel_id: str,
    *,
    workspace_id: str = "default",
    repos: Sequence[HostRepo] = (),
) -> Optional[HostRepo]:
    reader = getattr(store, "get_binding", None)
    if not callable(reader):
        return None
    meta = binding_metadata(reader(workspace_id, channel_id))
    name = str(meta.get("repo") or "").strip().lower()
    cwd = str(meta.get("cwd") or "").strip()
    if cwd:
        path = Path(cwd).expanduser()
        aliases = ()
        for repo in repos:
            if repo.name.lower() == name or str(repo.path) == str(path):
                aliases = repo.aliases
                name = name or repo.name
                break
        if path.is_dir():
            return HostRepo(name=name or path.name, path=path, aliases=aliases)
    if name:
        for repo in repos:
            if repo.name.lower() == name:
                return repo
    return None


def is_bind_command(text: str) -> bool:
    first = (text or "").strip().split(None, 1)[0].lower() if (text or "").strip() else ""
    return first in BIND_PREFIXES


def parse_bind_command(text: str) -> str:
    parts = (text or "").strip().split()
    if not parts:
        return ""
    first = parts[0].lower().lstrip("/!")
    if first in {"memory", "bank"}:
        return "memory"
    if len(parts) < 2:
        return ""
    return parts[1].strip().lower()


def bind_channel_realm(
    store: Any,
    *,
    workspace_id: str,
    channel_id: str,
    name: str,
    repos: Sequence[HostRepo],
) -> Optional[HostRepo]:
    key = (name or "").strip().lower()
    chosen = None
    for repo in repos:
        if repo.name.lower() == key or key in {item.lower() for item in repo.aliases}:
            chosen = repo
            break
    if chosen is None:
        return None
    writer = getattr(store, "merge_binding_metadata", None)
    if callable(writer):
        writer(
            workspace_id,
            channel_id,
            {"repo": chosen.name, "cwd": str(chosen.path)},
        )
    return chosen
