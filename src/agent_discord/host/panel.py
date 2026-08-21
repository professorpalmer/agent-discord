"""Native Discord On/Off/Ask. Users do not type power commands."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from agent_discord.discord.layout import action_row, string_select


ON_ID = "discord-os:on"
OFF_ID = "discord-os:off"
CONFIRM_OFF_ID = "discord-os:off-confirm"
CANCEL_OFF_ID = "discord-os:off-cancel"
ASK_ID = "discord-os:ask"
ASK_MODAL_ID = "discord-os:ask-modal"
ASK_TEXT_ID = "discord-os:ask-text"
JOBS_ID = "discord-os:jobs"
PAIR_ID = "discord-os:pair"
HALT_ID = "discord-os:halt"
COMPONENT_ROW = 1
BUTTON = 2
STYLE_PRIMARY = 1
STYLE_SUCCESS = 3
STYLE_DANGER = 4
INTERACTION_MESSAGE_COMPONENT = 3
INTERACTION_MODAL_SUBMIT = 5
CALLBACK_DEFERRED_UPDATE = 6
CALLBACK_UPDATE_MESSAGE = 7
CALLBACK_MODAL = 9


def host_panel_components(
    armed: bool,
    *,
    confirm_off: bool = False,
    jobs: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    if confirm_off:
        rows = [
            {
                "type": COMPONENT_ROW,
                "components": [
                    {
                        "type": BUTTON,
                        "style": STYLE_DANGER,
                        "custom_id": CONFIRM_OFF_ID,
                        "label": "Confirm",
                    },
                    {
                        "type": BUTTON,
                        "style": STYLE_PRIMARY,
                        "custom_id": CANCEL_OFF_ID,
                        "label": "Cancel",
                    },
                ],
            }
        ]
    else:
        power = [
            {
                "type": BUTTON,
                "style": STYLE_SUCCESS,
                "custom_id": ON_ID,
                "label": "On",
                "disabled": bool(armed),
            },
            {
                "type": BUTTON,
                "style": STYLE_DANGER,
                "custom_id": OFF_ID,
                "label": "Off",
                "disabled": not bool(armed),
            },
        ]
        if armed:
            power.append(
                {
                    "type": BUTTON,
                    "style": STYLE_PRIMARY,
                    "custom_id": ASK_ID,
                    "label": "Ask",
                }
            )
        rows = [
            {"type": COMPONENT_ROW, "components": power},
            {
                "type": COMPONENT_ROW,
                "components": [
                    {
                        "type": BUTTON,
                        "style": STYLE_PRIMARY,
                        "custom_id": PAIR_ID,
                        "label": "Pair",
                    },
                    {
                        "type": BUTTON,
                        "style": STYLE_DANGER,
                        "custom_id": HALT_ID,
                        "label": "Halt",
                    },
                ],
            },
        ]
    options = _job_select_options(jobs or ())
    if options:
        rows.append(action_row([string_select(JOBS_ID, options)]))
    return rows


def _job_select_options(jobs: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen: set[str] = set()
    for job in jobs:
        run_id = str(job.get("run_id") or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        label = str(job.get("intake_text") or job.get("summary") or run_id).replace("\n", " ")
        status = str(job.get("status") or "").strip()
        options.append(
            {
                "label": label[:80] or run_id[:80],
                "value": run_id[:100],
                "description": status[:100],
            }
        )
    return options


def ask_modal_payload() -> dict[str, Any]:
    return {
        "type": CALLBACK_MODAL,
        "data": {
            "custom_id": ASK_MODAL_ID,
            "title": "Ask Discord OS",
            "components": [
                {
                    "type": COMPONENT_ROW,
                    "components": [
                        {
                            "type": 4,
                            "custom_id": ASK_TEXT_ID,
                            "style": 2,
                            "label": "Task",
                            "min_length": 1,
                            "max_length": 4000,
                            "required": True,
                            "placeholder": "What should this host do?",
                        }
                    ],
                }
            ],
        },
    }


def ask_text_from_interaction(payload: Mapping[str, Any]) -> str:
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    if str(data.get("custom_id") or "") != ASK_MODAL_ID:
        return ""
    return _first_text_input(data.get("components"))


def selected_job_id(payload: Mapping[str, Any]) -> str:
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    if str(data.get("custom_id") or "") != JOBS_ID:
        return ""
    values = data.get("values")
    if not isinstance(values, list) or not values:
        return ""
    return str(values[0] or "").strip()


def _first_text_input(components: Any) -> str:
    for item in components or ():
        if not isinstance(item, dict):
            continue
        if str(item.get("custom_id") or "") == ASK_TEXT_ID:
            return str(item.get("value") or "").strip()
        nested = _first_text_input(item.get("components"))
        if nested:
            return nested
        inner = item.get("component")
        if isinstance(inner, dict):
            nested = _first_text_input([inner])
            if nested:
                return nested
    return ""


def host_panel_payload(
    armed: bool,
    *,
    channel_id: str = "",
    confirm_off: bool = False,
    jobs: Optional[list[dict[str, Any]]] = None,
    avatar_url: str = "",
    store: Any = None,
) -> dict[str, Any]:
    from agent_discord.orchestration.cards import host_card
    from agent_discord.orchestration.service import (
        is_spend_halted,
        session_spend_usd,
        spend_cap_usd,
    )

    spend_usd = 0.0
    cap_usd = None
    halted = False
    if store is not None:
        try:
            spend_usd = session_spend_usd(store)
            cap_usd = spend_cap_usd(store)
            halted = is_spend_halted(store)
        except Exception:
            spend_usd = 0.0
            cap_usd = None
            halted = False
    card = host_card(
        armed=armed,
        channel_id=channel_id,
        confirm_off=confirm_off,
        avatar_url=avatar_url,
        spend_usd=spend_usd,
        cap_usd=cap_usd,
        halted=halted,
    )
    return card.v2_payload(
        rows=host_panel_components(armed, confirm_off=confirm_off, jobs=jobs)
    )


def panel_action_from_custom_id(custom_id: str) -> Optional[str]:
    raw = (custom_id or "").strip()
    if raw == ON_ID:
        return "on"
    if raw == OFF_ID:
        return "off"
    if raw == CONFIRM_OFF_ID:
        return "off-confirm"
    if raw == CANCEL_OFF_ID:
        return "off-cancel"
    if raw == ASK_ID:
        return "ask"
    if raw == JOBS_ID:
        return "job"
    if raw == PAIR_ID:
        return "pair"
    if raw == HALT_ID:
        return "halt"
    return None


def panel_action_from_interaction(payload: Mapping[str, Any]) -> Optional[str]:
    if int(payload.get("type") or 0) != INTERACTION_MESSAGE_COMPONENT:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    return panel_action_from_custom_id(str(data.get("custom_id") or ""))


def apply_panel_action(store: Any, channel_id: str, action: str) -> dict[str, Any]:
    writer = getattr(store, "set_host_control", None)
    if action in {"on", "off-confirm"} and callable(writer):
        return writer(channel_id, armed=action == "on")
    reader = getattr(store, "get_host_control", None)
    if callable(reader):
        current = reader(channel_id)
        if current is not None:
            return current
    return {
        "channel_id": channel_id,
        "armed": action not in {"off", "off-confirm"},
        "card_message_id": "",
    }


def interaction_callback_payload(
    armed: bool,
    *,
    channel_id: str = "",
    confirm_off: bool = False,
    store: Any = None,
    jobs: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    panel = host_panel_payload(
        armed,
        channel_id=channel_id,
        confirm_off=confirm_off,
        jobs=jobs if jobs is not None else (_panel_jobs(store, channel_id) if store is not None else []),
        store=store,
    )
    return {
        "type": CALLBACK_UPDATE_MESSAGE,
        "data": {
            "flags": panel["flags"],
            "components": panel["components"],
        },
    }


def _ack_interaction(
    payload: Mapping[str, Any],
    callback_payload: Mapping[str, Any],
    *,
    opener: Any = None,
) -> bool:
    interaction_id, ix_token = interaction_ids(payload)
    if not interaction_id or not ix_token:
        return False
    try:
        from agent_discord.discord.rest import callback_interaction

        callback_interaction(
            interaction_id=interaction_id,
            interaction_token=ix_token,
            payload=dict(callback_payload),
            opener=opener,
        )
        return True
    except Exception as exc:
        print(f"panel callback failed: {exc}", flush=True)
        return False


def interaction_ids(payload: Mapping[str, Any]) -> tuple[str, str]:
    return str(payload.get("id") or ""), str(payload.get("token") or "")


def interaction_user_id(payload: Mapping[str, Any]) -> str:
    member = payload.get("member")
    if isinstance(member, dict):
        user = member.get("user")
        if isinstance(user, dict) and user.get("id"):
            return str(user.get("id") or "")
        roles = member.get("roles")
        _ = roles
    user = payload.get("user")
    if isinstance(user, dict):
        return str(user.get("id") or "")
    return ""


def interaction_role_ids(payload: Mapping[str, Any]) -> list[str]:
    member = payload.get("member")
    if not isinstance(member, dict):
        return []
    roles = member.get("roles")
    if isinstance(roles, list):
        return [str(item) for item in roles if str(item).strip()]
    return []


def handle_gateway_interaction(
    store: Any,
    channel_id: str,
    payload: Mapping[str, Any],
    *,
    token: str = "",
    opener: Any = None,
    on_ask: Optional[Callable[[str], None]] = None,
    on_power: Optional[Callable[[bool], None]] = None,
    on_job: Optional[Callable[[str, str], None]] = None,
) -> Optional[str]:
    """ACK within Discord's 3s window, then paint the panel. Best-effort."""

    from agent_discord.host.actions import job_action_from_custom_id

    data = payload.get("data")
    custom_id = ""
    if isinstance(data, dict):
        custom_id = str(data.get("custom_id") or "")
    job = job_action_from_custom_id(custom_id)
    if job is not None:
        interaction_id, ix_token = interaction_ids(payload)
        if interaction_id and ix_token:
            try:
                from agent_discord.discord.rest import callback_interaction

                callback_interaction(
                    interaction_id=interaction_id,
                    interaction_token=ix_token,
                    payload={"type": CALLBACK_DEFERRED_UPDATE},
                    opener=opener,
                )
            except Exception:
                pass
        if callable(on_job):
            try:
                on_job(job.action, job.run_id)
            except Exception:
                pass
        return job.action

    if int(payload.get("type") or 0) == INTERACTION_MODAL_SUBMIT:
        text = ask_text_from_interaction(payload)
        interaction_id, ix_token = interaction_ids(payload)
        if interaction_id and ix_token:
            try:
                from agent_discord.discord.rest import callback_interaction

                callback_interaction(
                    interaction_id=interaction_id,
                    interaction_token=ix_token,
                    payload={"type": CALLBACK_DEFERRED_UPDATE},
                    opener=opener,
                )
            except Exception:
                pass
        if text and callable(on_ask):
            try:
                on_ask(text)
            except Exception:
                pass
        return "ask" if text else None

    action = panel_action_from_interaction(payload)
    if action is None:
        return None
    from agent_discord.orchestration.service import (
        author_may_operate,
        seed_owner_if_empty,
        toggle_spend_halted,
    )

    user_id = interaction_user_id(payload)
    role_ids = interaction_role_ids(payload)
    if action in {"pair", "on"}:
        seeded = seed_owner_if_empty(store, user_id)
        print(
            f"panel {action} user={user_id or '-'} seeded={int(bool(seeded))}",
            flush=True,
        )
    if not author_may_operate(store, user_id, action, role_ids=role_ids):
        _ack_interaction(
            payload,
            interaction_callback_payload(
                _channel_armed(store, channel_id),
                channel_id=channel_id,
                store=store,
            ),
            opener=opener,
        )
        return "denied"
    if action == "ask":
        _ack_interaction(payload, ask_modal_payload(), opener=opener)
        return action
    if action == "halt":
        toggle_spend_halted(store)
    if action == "job":
        _ack_interaction(
            payload,
            {"type": CALLBACK_DEFERRED_UPDATE},
            opener=opener,
        )
        try:
            _publish_job_card(store, channel_id, payload, token=token, opener=opener)
        except Exception as exc:
            print(f"panel job card failed: {exc}", flush=True)
        return action

    confirm_off = action == "off"
    if action in {"on", "off-confirm"}:
        apply_panel_action(store, channel_id, action)
        if callable(on_power):
            try:
                on_power(action == "on")
            except Exception:
                pass
    armed = _channel_armed(store, channel_id)
    if confirm_off:
        armed = True
    _remember_panel_message(store, channel_id, payload)
    acked = _ack_interaction(
        payload,
        interaction_callback_payload(
            armed,
            channel_id=channel_id,
            confirm_off=confirm_off,
            store=store,
        ),
        opener=opener,
    )
    if not acked:
        try:
            _paint_host_panel(
                store,
                channel_id,
                token=token,
                message_id=_remember_panel_message(store, channel_id, payload),
                armed=armed,
                confirm_off=confirm_off,
                opener=opener,
            )
        except Exception as exc:
            print(f"panel paint failed: {exc}", flush=True)
    return action


def _channel_armed(store: Any, channel_id: str) -> bool:
    reader = getattr(store, "host_is_armed", None)
    if not callable(reader):
        return True
    try:
        return bool(reader(channel_id, default=True))
    except Exception:
        return True


def _remember_panel_message(store: Any, channel_id: str, payload: Mapping[str, Any]) -> str:
    message = payload.get("message")
    message_id = ""
    if isinstance(message, dict) and message.get("id"):
        message_id = str(message["id"])
        writer = getattr(store, "set_host_control", None)
        if callable(writer):
            writer(channel_id, card_message_id=message_id)
    return message_id


def _panel_jobs(store: Any, channel_id: str) -> list[dict[str, Any]]:
    reader = getattr(store, "list_recent_jobs", None)
    if not callable(reader):
        return []
    try:
        return list(reader(channel_id, limit=5))
    except Exception:
        return []


def _panel_avatar(token: str, opener: Any) -> str:
    if not token.strip():
        return ""
    try:
        from agent_discord.discord.rest import bot_avatar_url, fetch_bot_identity

        return bot_avatar_url(fetch_bot_identity(token=token, opener=opener))
    except Exception:
        return ""


def _paint_host_panel(
    store: Any,
    channel_id: str,
    *,
    token: str,
    message_id: str,
    armed: bool,
    confirm_off: bool,
    opener: Any,
) -> None:
    if not token.strip() or not message_id:
        return
    from agent_discord.discord.rest import edit_channel_message

    panel = host_panel_payload(
        armed,
        channel_id=channel_id,
        confirm_off=confirm_off,
        jobs=_panel_jobs(store, channel_id),
        avatar_url=_panel_avatar(token, opener),
        store=store,
    )
    edit_channel_message(
        token=token,
        channel_id=channel_id,
        message_id=message_id,
        content="",
        components=panel["components"],
        flags=panel["flags"],
        opener=opener,
    )


def _publish_job_card(
    store: Any,
    channel_id: str,
    payload: Mapping[str, Any],
    *,
    token: str,
    opener: Any,
) -> None:
    run_id = selected_job_id(payload)
    if not run_id or not token.strip():
        return
    getter = getattr(store, "get_run", None)
    if not callable(getter):
        return
    run = getter(run_id)
    if not isinstance(run, dict):
        return
    from agent_discord.contracts import RunReceipt, TaskStatus
    from agent_discord.discord.rest import send_channel_message
    from agent_discord.orchestration.cards import receipt_card

    status_raw = str(run.get("status") or "completed")
    try:
        status = TaskStatus(status_raw)
    except ValueError:
        status = TaskStatus.COMPLETED
    card = receipt_card(
        RunReceipt(
            task_id=str(run.get("task_id") or ""),
            run_id=run_id,
            status=status,
            summary=str(run.get("summary") or "No summary."),
            error=str(run.get("error") or "") or None,
        )
    )
    send_channel_message(
        token=token,
        channel_id=channel_id,
        content="",
        components=card.v2_payload()["components"],
        flags=card.v2_payload()["flags"],
        opener=opener,
    )
