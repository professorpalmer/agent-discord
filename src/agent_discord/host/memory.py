"""Discord channels as a durable think-tank.

Other rooms hold state the way a group chat does. Hermes persist-then-settle
plus GrokBot's always-on stitch — own code, Discord is the store.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional, Sequence

from agent_discord.contracts import DiscordMessage
from agent_discord.host.realms import binding_metadata


MEMORY_NAMES = frozenset({"memory", "bank", "tank", "think-tank", "thinktank"})
MEMORY_PREFIXES = frozenset(
    {"/memory", "!memory", "memory", "/bank", "!bank", "bank"}
)


def is_memory_bind(name: str) -> bool:
    return (name or "").strip().lower() in MEMORY_NAMES


def bind_memory_channel(
    store: Any,
    *,
    workspace_id: str,
    channel_id: str,
    label: str = "",
) -> bool:
    writer = getattr(store, "merge_binding_metadata", None)
    if not callable(writer) or not channel_id:
        return False
    updates = {"memory": True}
    if label:
        updates["memory_label"] = label
    writer(workspace_id, channel_id, updates)
    return True


def seed_memory_channels(
    store: Any,
    *,
    workspace_id: str,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[str, ...]:
    source = dict(os.environ if env is None else env)
    ids = _split_ids(source.get("DISCORD_OS_MEMORY") or "")
    for channel_id in ids:
        bind_memory_channel(store, workspace_id=workspace_id, channel_id=channel_id)
    return ids


def memory_channel_ids(
    store: Any,
    *,
    workspace_id: str = "default",
    env: Optional[Mapping[str, str]] = None,
) -> tuple[str, ...]:
    seen: list[str] = []
    known = set()
    source = dict(os.environ if env is None else env)
    for channel_id in _split_ids(source.get("DISCORD_OS_MEMORY") or ""):
        if channel_id not in known:
            seen.append(channel_id)
            known.add(channel_id)
    lister = getattr(store, "list_bindings", None)
    if callable(lister):
        for row in lister(workspace_id) or ():
            meta = binding_metadata(row)
            channel_id = str(row.get("channel_id") or "").strip()
            if channel_id and meta.get("memory") and channel_id not in known:
                seen.append(channel_id)
                known.add(channel_id)
    return tuple(seen)


def channel_is_memory(
    store: Any,
    channel_id: str,
    *,
    workspace_id: str = "default",
) -> bool:
    reader = getattr(store, "get_binding", None)
    if callable(reader) and bool(binding_metadata(reader(workspace_id, channel_id)).get("memory")):
        return True
    lister = getattr(store, "list_bindings", None)
    if not callable(lister):
        return False
    for row in lister() or ():
        if str(row.get("channel_id") or "") == channel_id and binding_metadata(row).get("memory"):
            return True
    return False


def recall_think_tank(
    discord: Any,
    store: Any,
    query: str,
    *,
    workspace_id: str = "default",
    limit_per: int = 8,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    channels = memory_channel_ids(store, workspace_id=workspace_id, env=env)
    if not channels:
        return ""
    blocks: list[str] = []
    needles = [part for part in (query or "").lower().split() if len(part) > 2][:8]
    for channel_id in channels:
        lines = _channel_lines(
            discord,
            channel_id,
            needles=needles,
            limit=limit_per,
        )
        recaller = getattr(store, "recall", None)
        if callable(recaller):
            try:
                for row in recaller(
                    workspace_id=workspace_id,
                    channel_id=channel_id,
                    query=query,
                    limit=limit_per,
                ):
                    if str(row.get("source") or "") != "think-tank":
                        continue
                    text = str(row.get("content") or "").strip()
                    if text and text not in lines:
                        lines.append(text[:240])
            except Exception:
                pass
        if not lines:
            continue
        blocks.append(f"#{channel_id}")
        blocks.extend(f"- {line}" for line in lines)
    return "\n".join(blocks)


def post_think_tank_note(
    discord: Any,
    channel_id: str,
    text: str,
    *,
    source_channel: str = "",
) -> Optional[DiscordMessage]:
    from agent_discord.orchestration.cards import note_card, send_card

    body = (text or "").strip()
    if not body or not channel_id:
        return None
    card = note_card(body, source_channel=source_channel)
    try:
        posted = send_card(discord, channel_id, card)
    except Exception:
        return None
    if isinstance(posted, list):
        return posted[0] if posted else None
    return posted


def settle_think_tank(
    discord: Any,
    store: Any,
    *,
    workspace_id: str,
    origin_channel: str,
    summary: str,
    env: Optional[Mapping[str, str]] = None,
) -> list[str]:
    text = (summary or "").strip()
    if not text:
        return []
    posted: list[str] = []
    for channel_id in memory_channel_ids(store, workspace_id=workspace_id, env=env):
        if channel_id == origin_channel:
            continue
        msg = post_think_tank_note(
            discord,
            channel_id,
            text,
            source_channel=origin_channel,
        )
        if msg is not None:
            posted.append(channel_id)
            remember = getattr(store, "remember", None)
            if callable(remember):
                try:
                    remember(
                        workspace_id=workspace_id,
                        channel_id=channel_id,
                        content=text[:500],
                        source="think-tank",
                        provenance={"origin": origin_channel},
                    )
                except Exception:
                    pass
    return posted


def memory_reach_block(
    store: Any = None,
    *,
    workspace_id: str = "default",
    env: Optional[Mapping[str, str]] = None,
) -> str:
    ids = memory_channel_ids(store, workspace_id=workspace_id, env=env) if store is not None else ()
    lines = [
        "Think-tank (Discord is the durable store):",
        "- Bound channels are memory. Use discord-os recall / note.",
    ]
    if ids:
        lines.append("- Memory channels: " + ", ".join(ids))
    return "\n".join(lines)


def _channel_lines(
    discord: Any,
    channel_id: str,
    *,
    needles: Sequence[str],
    limit: int,
) -> list[str]:
    reader = getattr(discord, "read_messages", None)
    if not callable(reader):
        return []
    try:
        messages = list(reader(channel_id, limit=max(limit * 3, 12), skip_duplicates=False))
    except Exception:
        return []
    usable: list[str] = []
    matched: list[str] = []
    for message in messages:
        content = (getattr(message, "content", "") or "").strip()
        if not content:
            continue
        embeds = None
        components = None
        meta = getattr(message, "metadata", None)
        if isinstance(meta, dict):
            embeds = meta.get("embeds")
            components = meta.get("components")
        if _skip_harness_line(content, embeds, components):
            continue
        clipped = content[:240]
        usable.append(clipped)
        hay = content.lower()
        if needles and any(needle in hay for needle in needles):
            matched.append(clipped)
        if len(usable) >= limit and (not needles or len(matched) >= limit):
            break
    if matched:
        return matched[:limit]
    return usable[:limit]


def _skip_harness_line(content: str, embeds: Any, components: Any) -> bool:
    text = (content or "").strip()
    if text.startswith("**Card**") or text.startswith("**Receipt**"):
        return True
    _ = embeds
    _ = components
    return False


def _split_ids(raw: str) -> tuple[str, ...]:
    items = []
    seen = set()
    for part in (raw or "").replace(";", ",").split(","):
        channel_id = part.strip()
        if channel_id and channel_id not in seen:
            items.append(channel_id)
            seen.add(channel_id)
    return tuple(items)
