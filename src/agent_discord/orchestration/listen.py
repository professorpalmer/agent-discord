"""Inbound Discord drain — phone / staff-channel messages become TaskIntake."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from agent_discord.contracts import DiscordMessage, RunReceipt, TaskIntake
from agent_discord.host.power import is_power_command, parse_power_command
from agent_discord.host.memory import bind_memory_channel, is_memory_bind
from agent_discord.host.realms import bind_channel_realm, is_bind_command, parse_bind_command
from agent_discord.host.repos import load_host_repos
from agent_discord.host.verbs import handle_open_message, is_open_command
from agent_discord.keys.connect import (
    handle_connect_message,
    is_connect_command,
    parse_connect_command,
)
from agent_discord.orchestration.cards import (
    connect_card,
    edit_card,
    host_card,
    is_harness_card,
    is_harness_message,
    open_card,
    send_card,
)
from agent_discord.orchestration.service import (
    author_may_dispatch,
    is_spend_halted,
    parse_schedule_command,
    seed_spend_cap_from_env,
    seed_write_gate_from_env,
    session_spend_usd,
    spend_cap_usd,
    writes_need_approval,
)

DISCORD_EPOCH_MS = 1_420_070_400_000
LISTEN_HISTORY_SLACK_MS = 15_000


def snowflake_created_ms(message_id: str) -> Optional[int]:
    try:
        return (int(message_id) >> 22) + DISCORD_EPOCH_MS
    except (TypeError, ValueError):
        return None


def default_listen_since_ms(*, now_ms: Optional[int] = None) -> int:
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    return now - LISTEN_HISTORY_SLACK_MS


def _message_id_after(message_id: str, previous_id: str) -> bool:
    if not previous_id:
        return bool(message_id)
    if message_id == previous_id:
        return False
    try:
        return int(message_id) > int(previous_id)
    except (TypeError, ValueError):
        return message_id != previous_id


def _inbound_sort_key(message: DiscordMessage) -> tuple[object, ...]:
    created = snowflake_created_ms(message.message_id) if message.message_id else None
    try:
        numeric = int(message.message_id) if message.message_id else 0
    except (TypeError, ValueError):
        numeric = 0
    return (created is None, created or 0, numeric, message.message_id or "")


def _inbound_newer_than_watermark(
    message_id: str,
    created_ms: Optional[int],
    watermark: Mapping[str, Any],
) -> bool:
    last_ms = watermark.get("last_created_ms")
    last_id = str(watermark.get("last_message_id") or "")
    if created_ms is not None and last_ms is not None:
        if created_ms != int(last_ms):
            return created_ms > int(last_ms)
        return _message_id_after(message_id, last_id)
    if created_ms is not None:
        return True
    return bool(message_id) and message_id != last_id


def _watermark_after(
    watermark: Mapping[str, Any],
    created_ms: Optional[int],
    message_id: str,
) -> dict[str, Any]:
    last_ms = watermark.get("last_created_ms")
    last_id = str(watermark.get("last_message_id") or "")
    next_ms = last_ms
    next_id = last_id
    if created_ms is not None:
        if last_ms is None or created_ms > int(last_ms):
            next_ms = created_ms
            next_id = message_id or last_id
        elif created_ms == int(last_ms) and _message_id_after(message_id, last_id):
            next_id = message_id
    elif message_id and message_id != last_id:
        next_id = message_id
    return {
        "channel_id": watermark.get("channel_id"),
        "last_created_ms": next_ms,
        "last_message_id": next_id,
    }


def _advance_listen_watermark(
    store: Any,
    channel_id: str,
    created_ms: Optional[int],
    message_id: Optional[str],
    watermark: Mapping[str, Any],
) -> dict[str, Any]:
    updated = _watermark_after(watermark, created_ms, message_id or "")
    writer = getattr(store, "set_listen_watermark", None)
    if callable(writer):
        writer(
            channel_id,
            created_ms=updated.get("last_created_ms"),
            message_id=str(updated.get("last_message_id") or ""),
        )
        reader = getattr(store, "get_listen_watermark", None)
        if callable(reader):
            refreshed = reader(channel_id)
            if refreshed is not None:
                return refreshed
    return updated


def should_dispatch_inbound(message: DiscordMessage) -> bool:
    """Skip empty, bot receipts, progress lines, cards, and object-store captions."""

    content = (message.content or "").strip()
    embeds = None
    components = None
    meta = getattr(message, "metadata", None)
    if isinstance(meta, dict):
        raw_embeds = meta.get("embeds")
        if isinstance(raw_embeds, list):
            embeds = raw_embeds
        raw_components = meta.get("components")
        if isinstance(raw_components, list):
            components = raw_components
    if is_harness_message(content, embeds, components):
        return False
    if not content:
        if isinstance(meta, dict) and (
            meta.get("transcript")
            or meta.get("voice_transcript")
            or meta.get("voice_urls")
            or meta.get("local_audio_path")
            or meta.get("voice_bytes")
        ):
            return True
        return False
    if is_harness_card(content):
        return False
    if content.startswith("[") and "] " in content[:48]:
        return False
    if content.startswith("{") and content.endswith("}"):
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return True
        if isinstance(payload, dict) and payload.get("agent_discord_object") == 1:
            return False
    return True


def drain_inbound(
    orchestrator: Any,
    discord: Any,
    *,
    channel_id: str,
    workspace_id: str = "default",
    guild_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    limit: int = 20,
    workspace: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    since_ms: Optional[int] = None,
    host_roots: Optional[Sequence[Path]] = None,
    host_runner: Optional[Any] = None,
    browser_open: Optional[Any] = None,
    job_pool: Optional[Any] = None,
) -> Sequence[RunReceipt]:
    """Read recent channel messages and dispatch each new human task once.

    Connect and open commands are intercepted before TaskIntake. A shred
    payload is never dispatched as a prompt, even when delete fails. Host
    opens stay on this process; Discord is only the remote.
    """

    messages = discord.read_messages(
        channel_id,
        limit=limit,
        thread_id=thread_id,
        skip_duplicates=False,
    )
    receipts: list[RunReceipt] = []
    ws = Path(workspace) if workspace is not None else _workspace_from(orchestrator)
    store = getattr(orchestrator, "store", None)
    if store is not None:
        seed_spend_cap_from_env(store, env)
        seed_write_gate_from_env(store, env)
    seed_ms = since_ms if since_ms is not None else default_listen_since_ms()
    seeder = getattr(store, "seed_listen_watermark", None)
    if callable(seeder):
        watermark = seeder(channel_id, seed_ms)
    else:
        watermark = {"channel_id": channel_id, "last_created_ms": seed_ms, "last_message_id": ""}
    snapshot = dict(watermark)
    pending: list[DiscordMessage] = []
    for message in messages:
        created_ms = snowflake_created_ms(message.message_id) if message.message_id else None
        if not _inbound_newer_than_watermark(message.message_id or "", created_ms, snapshot):
            continue
        pending.append(message)
    pending.sort(key=_inbound_sort_key)
    for message in pending:
        created_ms = snowflake_created_ms(message.message_id) if message.message_id else None
        if is_connect_command(message.content or ""):
            _absorb_connect(
                message,
                discord=discord,
                orchestrator=orchestrator,
                channel_id=channel_id,
                thread_id=message.thread_id or thread_id,
                workspace=ws,
                env=env,
            )
            watermark = _advance_listen_watermark(
                store, channel_id, created_ms, message.message_id, watermark
            )
            continue
        if is_bind_command(message.content or ""):
            if not author_may_dispatch(
                store,
                message.author_id,
                role_ids=_author_role_ids(message),
            ):
                watermark = _advance_listen_watermark(
                    store, channel_id, created_ms, message.message_id, watermark
                )
                continue
            _absorb_bind(
                message,
                discord=discord,
                orchestrator=orchestrator,
                channel_id=channel_id,
                workspace_id=workspace_id,
                thread_id=message.thread_id or thread_id,
            )
            watermark = _advance_listen_watermark(
                store, channel_id, created_ms, message.message_id, watermark
            )
            continue
        if is_power_command(message.content or ""):
            _absorb_power(
                message,
                discord=discord,
                orchestrator=orchestrator,
                channel_id=channel_id,
                thread_id=message.thread_id or thread_id,
            )
            watermark = _advance_listen_watermark(
                store, channel_id, created_ms, message.message_id, watermark
            )
            continue
        if is_open_command(message.content or ""):
            if not author_may_dispatch(
                store,
                message.author_id,
                role_ids=_author_role_ids(message),
            ):
                watermark = _advance_listen_watermark(
                    store, channel_id, created_ms, message.message_id, watermark
                )
                continue
            if not _channel_is_armed(store, channel_id):
                _claim_inbound(store, discord, message, channel_id)
                publish_host_card(
                    discord,
                    store,
                    channel_id,
                    thread_id=message.thread_id or thread_id,
                )
                watermark = _advance_listen_watermark(
                    store, channel_id, created_ms, message.message_id, watermark
                )
                continue
            _absorb_open(
                message,
                discord=discord,
                orchestrator=orchestrator,
                channel_id=channel_id,
                thread_id=message.thread_id or thread_id,
                roots=host_roots or ((ws,) if ws is not None else ()),
                runner=host_runner,
                browser_open=browser_open,
            )
            watermark = _advance_listen_watermark(
                store, channel_id, created_ms, message.message_id, watermark
            )
            continue
        intake_text, intake_meta, skip_voice = _collab_intake(message, discord)
        if skip_voice:
            watermark = _advance_listen_watermark(
                store, channel_id, created_ms, message.message_id, watermark
            )
            continue
        scheduled = parse_schedule_command(intake_text or (message.content or ""))
        if scheduled is not None:
            _absorb_schedule(
                message,
                discord=discord,
                store=store,
                channel_id=channel_id,
                workspace_id=workspace_id,
                every_s=scheduled[0],
                prompt=scheduled[1],
                thread_id=message.thread_id or thread_id,
            )
            watermark = _advance_listen_watermark(
                store, channel_id, created_ms, message.message_id, watermark
            )
            continue
        if not intake_text and not should_dispatch_inbound(message):
            watermark = _advance_listen_watermark(
                store, channel_id, created_ms, message.message_id, watermark
            )
            continue
        if not _channel_is_armed(store, channel_id):
            watermark = _advance_listen_watermark(
                store, channel_id, created_ms, message.message_id, watermark
            )
            continue
        text = intake_text or (message.content or "").strip()
        if not text:
            watermark = _advance_listen_watermark(
                store, channel_id, created_ms, message.message_id, watermark
            )
            continue
        if is_spend_halted(store, workspace_id):
            watermark = _advance_listen_watermark(
                store, channel_id, created_ms, message.message_id, watermark
            )
            continue
        if not author_may_dispatch(
            store,
            message.author_id,
            role_ids=_author_role_ids(message),
        ):
            watermark = _advance_listen_watermark(
                store, channel_id, created_ms, message.message_id, watermark
            )
            continue
        intake = TaskIntake(
            text=text,
            channel_id=channel_id,
            workspace_id=workspace_id,
            guild_id=guild_id,
            thread_id=message.thread_id or thread_id,
            message_id=message.message_id or None,
            requester_id=message.author_id,
            metadata=intake_meta,
        )
        if job_pool is not None:
            job_pool.submit(orchestrator.run_task, intake, write_key=channel_id)
        else:
            receipts.append(orchestrator.run_task(intake))
        watermark = _advance_listen_watermark(
            store, channel_id, created_ms, message.message_id, watermark
        )
    receipts.extend(
        _fire_due_schedules(
            orchestrator,
            store,
            channel_id=channel_id,
            workspace_id=workspace_id,
            guild_id=guild_id,
            thread_id=thread_id,
        )
    )
    return receipts


def _workspace_from(orchestrator: Any) -> Optional[Path]:
    raw = getattr(orchestrator, "workspace", None)
    return Path(raw) if raw is not None else None


def _absorb_connect(
    message: DiscordMessage,
    *,
    discord: Any,
    orchestrator: Any,
    channel_id: str,
    thread_id: Optional[str],
    workspace: Optional[Path],
    env: Optional[Mapping[str, str]],
) -> None:
    store = getattr(orchestrator, "store", None)
    if store is not None and message.message_id:
        store.claim_inbound_message(message.message_id, channel_id)
    observe = getattr(discord, "observe_message_id", None)
    if callable(observe) and message.message_id:
        try:
            observe(message.message_id)
        except Exception:
            pass
    parsed = parse_connect_command(message.content or "")
    delete_ok = True
    if parsed.secret and message.message_id:
        try:
            discord.delete_message(channel_id, message.message_id)
        except Exception:
            delete_ok = False
    if workspace is None:
        return
    result = handle_connect_message(
        message.content or "",
        workspace=workspace,
        env=env,
        delete_ok=delete_ok,
    )
    if result.card:
        send_card(
            discord,
            channel_id,
            connect_card(
                provider=result.provider,
                fingerprint=result.fingerprint,
                source=result.source,
                ticket=result.ticket,
                error=result.error,
            ),
            thread_id=thread_id,
        )


def _absorb_open(
    message: DiscordMessage,
    *,
    discord: Any,
    orchestrator: Any,
    channel_id: str,
    thread_id: Optional[str],
    roots: Sequence[Path],
    runner: Any,
    browser_open: Any,
) -> None:
    store = getattr(orchestrator, "store", None)
    if store is not None and message.message_id:
        store.claim_inbound_message(message.message_id, channel_id)
    observe = getattr(discord, "observe_message_id", None)
    if callable(observe) and message.message_id:
        try:
            observe(message.message_id)
        except Exception:
            pass
    result = handle_open_message(
        message.content or "",
        roots=list(roots),
        runner=runner,
        browser_open=browser_open,
    )
    if result.card:
        send_card(
            discord,
            channel_id,
            open_card(
                surface=result.surface,
                target=result.target,
                error=result.error,
            ),
            thread_id=thread_id,
        )


def _channel_is_armed(store: Any, channel_id: str) -> bool:
    reader = getattr(store, "host_is_armed", None)
    if not callable(reader):
        return True
    return bool(reader(channel_id, default=True))


def _claim_inbound(store: Any, discord: Any, message: DiscordMessage, channel_id: str) -> None:
    if store is not None and message.message_id:
        claim = getattr(store, "claim_inbound_message", None)
        if callable(claim):
            claim(message.message_id, channel_id)
    observe = getattr(discord, "observe_message_id", None)
    if callable(observe) and message.message_id:
        try:
            observe(message.message_id)
        except Exception:
            pass


def _absorb_power(
    message: DiscordMessage,
    *,
    discord: Any,
    orchestrator: Any,
    channel_id: str,
    thread_id: Optional[str],
) -> None:
    store = getattr(orchestrator, "store", None)
    _claim_inbound(store, discord, message, channel_id)
    parsed = parse_power_command(message.content or "")
    if parsed.action == "on":
        from agent_discord.orchestration.service import seed_owner_if_empty

        seed_owner_if_empty(store, message.author_id)
    writer = getattr(store, "set_host_control", None)
    if parsed.action in {"on", "off"} and callable(writer):
        writer(channel_id, armed=parsed.action == "on")
    publish_host_card(discord, store, channel_id, thread_id=thread_id)


def _absorb_bind(
    message: DiscordMessage,
    *,
    discord: Any,
    orchestrator: Any,
    channel_id: str,
    workspace_id: str,
    thread_id: Optional[str],
) -> None:
    store = getattr(orchestrator, "store", None)
    _claim_inbound(store, discord, message, channel_id)
    name = parse_bind_command(message.content or "")
    repos = list(getattr(orchestrator, "host_repos", None) or load_host_repos())
    chosen = None
    if is_memory_bind(name):
        bind_memory_channel(
            store,
            workspace_id=workspace_id,
            channel_id=channel_id,
        )
    elif name:
        chosen = bind_channel_realm(
            store,
            workspace_id=workspace_id,
            channel_id=channel_id,
            name=name,
            repos=repos,
        )
    realm = ""
    if chosen is not None:
        realm = chosen.name
    elif name and not is_memory_bind(name):
        realm = name
    publish_host_card(
        discord,
        store,
        channel_id,
        thread_id=thread_id,
        realm=realm or _realm_name(store, channel_id, workspace_id),
    )


def _realm_name(store: Any, channel_id: str, workspace_id: str = "default") -> str:
    from agent_discord.host.realms import binding_metadata

    reader = getattr(store, "get_binding", None)
    if not callable(reader):
        return ""
    try:
        return str(binding_metadata(reader(workspace_id, channel_id)).get("repo") or "")
    except Exception:
        return ""


def publish_host_card(
    discord: Any,
    store: Any,
    channel_id: str,
    *,
    thread_id: Optional[str] = None,
    realm: str = "",
) -> None:
    """Post or edit the HOST card. Best-effort — never raise on the listen path."""

    from agent_discord.host.panel import host_panel_components

    armed = _channel_is_armed(store, channel_id)
    spend = 0.0
    cap = None
    halted = False
    write_gate = False
    try:
        spend = session_spend_usd(store)
        cap = spend_cap_usd(store)
        halted = is_spend_halted(store)
        write_gate = writes_need_approval(store)
    except Exception:
        spend = 0.0
        cap = None
        halted = False
        write_gate = False
    jobs: list[dict] = []
    lister = getattr(store, "list_recent_jobs", None)
    if callable(lister):
        try:
            jobs = list(lister(channel_id, limit=5))
        except Exception:
            jobs = []
    avatar_url = ""
    token = str(getattr(getattr(discord, "provider", None), "_bot_token", "") or "")
    if token:
        try:
            from agent_discord.discord.rest import bot_avatar_url, fetch_bot_identity

            avatar_url = bot_avatar_url(fetch_bot_identity(token=token))
        except Exception:
            avatar_url = ""
    from agent_discord.host.memory import channel_is_memory

    card = host_card(
        armed=armed,
        channel_id=channel_id,
        avatar_url=avatar_url,
        spend_usd=spend,
        cap_usd=cap,
        halted=halted,
        write_gate=write_gate,
        realm=realm or _realm_name(store, channel_id),
        bank=channel_is_memory(store, channel_id),
    )
    control = None
    reader = getattr(store, "get_host_control", None)
    if callable(reader):
        try:
            control = reader(channel_id)
        except Exception:
            control = None
    card_id = str((control or {}).get("card_message_id") or "")
    buttons = host_panel_components(armed, jobs=jobs, write_gate=write_gate)
    if card_id:
        try:
            edit_card(discord, channel_id, card_id, card, components=buttons)
            return
        except Exception:
            pass
    try:
        posted = send_card(
            discord,
            channel_id,
            card,
            thread_id=thread_id,
            components=buttons,
        )
    except Exception:
        return
    message_id = ""
    if isinstance(posted, list) and posted:
        message_id = str(getattr(posted[0], "message_id", "") or "")
    else:
        message_id = str(getattr(posted, "message_id", "") or "")
    writer = getattr(store, "set_host_control", None)
    if message_id and callable(writer):
        try:
            writer(channel_id, card_message_id=message_id)
        except Exception:
            pass


def _collab_intake(message: DiscordMessage, discord: Any) -> tuple[str, dict[str, Any], bool]:
    """Voice + thread-history context. Fetches bot-visible attachment bytes only."""

    meta: dict[str, Any] = {}
    if isinstance(message.metadata, Mapping):
        meta.update(dict(message.metadata))
    mentioned = "@" in (message.content or "")
    meta["mentioned"] = mentioned
    history: list[str] = []
    thread_id = message.thread_id
    if thread_id:
        reader = getattr(discord, "read_messages", None)
        if callable(reader):
            try:
                recent = reader(message.channel_id, limit=8, thread_id=thread_id)
                history = [
                    str(getattr(item, "content", "") or "").strip()
                    for item in list(recent or [])
                    if str(getattr(item, "content", "") or "").strip()
                ][-6:]
            except Exception:
                history = []
    if history:
        meta["thread_history"] = history
        meta["reading"] = f"thread {thread_id}"
    try:
        from agent_discord.discord.voice import (
            detect_voice_intent,
            materialize_voice_intake,
            spoken_command_to_intake,
        )
    except Exception:
        return "", meta, False
    try:
        intent = detect_voice_intent(message)
    except Exception:
        return "", meta, False
    if not intent:
        return "", meta, False
    if intent.get("kind") == "voice_attachment" and not (
        meta.get("transcript") or meta.get("voice_transcript")
    ):
        transcript = ""
        try:
            transcript = materialize_voice_intake(message, discord)
        except Exception:
            transcript = ""
        if not transcript:
            return "", meta, True
        meta["voice_transcript"] = transcript
        return spoken_command_to_intake(transcript) or transcript, meta, False
    transcript = str(intent.get("intake") or intent.get("transcript") or "")
    if not transcript and (meta.get("transcript") or meta.get("voice_transcript")):
        transcript = spoken_command_to_intake(
            str(meta.get("transcript") or meta.get("voice_transcript") or "")
        )
    return transcript.strip(), meta, False


def _author_role_ids(message: DiscordMessage) -> list[str]:
    meta = message.metadata if isinstance(message.metadata, Mapping) else {}
    raw = meta.get("author_roles") or meta.get("role_ids") or ()
    if isinstance(raw, str):
        return [bit for bit in raw.replace(",", " ").split() if bit]
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw if str(item).strip()]
    return []


def _absorb_schedule(
    message: DiscordMessage,
    *,
    discord: Any,
    store: Any,
    channel_id: str,
    workspace_id: str,
    every_s: int,
    prompt: str,
    thread_id: Optional[str],
) -> None:
    _claim_inbound(store, discord, message, channel_id)
    if not author_may_dispatch(store, message.author_id, role_ids=_author_role_ids(message)):
        return
    writer = getattr(store, "add_schedule", None)
    if not callable(writer):
        return
    try:
        writer(
            channel_id=channel_id,
            workspace_id=workspace_id,
            prompt=prompt,
            every_s=every_s,
            created_by=str(message.author_id or ""),
        )
    except Exception:
        return
    try:
        send_card(
            discord,
            channel_id,
            host_card(armed=_channel_is_armed(store, channel_id), channel_id=channel_id),
            thread_id=thread_id,
        )
    except Exception:
        pass


def _fire_due_schedules(
    orchestrator: Any,
    store: Any,
    *,
    channel_id: str,
    workspace_id: str,
    guild_id: Optional[str],
    thread_id: Optional[str],
) -> list[RunReceipt]:
    if store is None or is_spend_halted(store, workspace_id):
        return []
    if not _channel_is_armed(store, channel_id):
        return []
    due_reader = getattr(store, "due_schedules", None)
    bumper = getattr(store, "bump_schedule", None)
    if not callable(due_reader):
        return []
    now_ms = int(time.time() * 1000)
    try:
        due = list(due_reader(now_ms, channel_id))
    except Exception:
        return []
    receipts: list[RunReceipt] = []
    for row in due:
        prompt = str(row.get("prompt") or "").strip()
        schedule_id = str(row.get("schedule_id") or "")
        every_s = int(row.get("every_s") or 0)
        if not prompt or not schedule_id:
            continue
        created_by = str(row.get("created_by") or "")
        if created_by and not author_may_dispatch(store, created_by):
            continue
        try:
            receipts.append(
                orchestrator.run_task(
                    TaskIntake(
                        text=prompt,
                        channel_id=channel_id,
                        workspace_id=str(row.get("workspace_id") or workspace_id),
                        guild_id=guild_id,
                        thread_id=thread_id,
                        requester_id=created_by or None,
                        metadata={"scheduled": True, "schedule_id": schedule_id},
                    )
                )
            )
        except Exception:
            continue
        if callable(bumper) and every_s > 0:
            try:
                bumper(schedule_id, now_ms + every_s * 1000)
            except Exception:
                pass
    return receipts
