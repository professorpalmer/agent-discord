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
        rows = [
            {
                "type": COMPONENT_ROW,
                "components": [
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
                + (
                    [
                        {
                            "type": BUTTON,
                            "style": STYLE_PRIMARY,
                            "custom_id": ASK_ID,
                            "label": "Ask",
                        }
                    ]
                    if armed
                    else []
                ),
            }
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
) -> dict[str, Any]:
    from agent_discord.orchestration.cards import host_card

    card = host_card(
        armed=armed,
        channel_id=channel_id,
        confirm_off=confirm_off,
        avatar_url=avatar_url,
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
) -> dict[str, Any]:
    panel = host_panel_payload(armed, channel_id=channel_id, confirm_off=confirm_off)
    return {
        "type": CALLBACK_UPDATE_MESSAGE,
        "data": {
            "flags": panel["flags"],
            "components": panel["components"],
        },
    }


def interaction_ids(payload: Mapping[str, Any]) -> tuple[str, str]:
    return str(payload.get("id") or ""), str(payload.get("token") or "")


def handle_gateway_interaction(
    store: Any,
    channel_id: str,
    payload: Mapping[str, Any],
    *,
    token: str = "",
    opener: Any = None,
    on_ask: Optional[Callable[[str], None]] = None,
    on_power: Optional[Callable[[bool], None]] = None,
) -> Optional[str]:
    """ACK within Discord's 3s window, then paint the panel. Best-effort."""

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
    interaction_id, ix_token = interaction_ids(payload)
    if action == "ask":
        if interaction_id and ix_token:
            try:
                from agent_discord.discord.rest import callback_interaction

                callback_interaction(
                    interaction_id=interaction_id,
                    interaction_token=ix_token,
                    payload=ask_modal_payload(),
                    opener=opener,
                )
            except Exception:
                pass
        return action
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
    try:
        if action == "job":
            _publish_job_card(store, channel_id, payload, token=token, opener=opener)
            return action
        if action == "off":
            message_id = _remember_panel_message(store, channel_id, payload)
            _paint_host_panel(
                store,
                channel_id,
                token=token,
                message_id=message_id,
                armed=True,
                confirm_off=True,
                opener=opener,
            )
            return action
        if action == "off-cancel":
            message_id = _remember_panel_message(store, channel_id, payload)
            _paint_host_panel(
                store,
                channel_id,
                token=token,
                message_id=message_id,
                armed=True,
                confirm_off=False,
                opener=opener,
            )
            return action
        apply_panel_action(store, channel_id, action)
        armed = action == "on"
        if callable(on_power) and action in {"on", "off-confirm"}:
            try:
                on_power(armed)
            except Exception:
                pass
        message_id = _remember_panel_message(store, channel_id, payload)
        _paint_host_panel(
            store,
            channel_id,
            token=token,
            message_id=message_id,
            armed=armed,
            confirm_off=False,
            opener=opener,
        )
    except Exception:
        return action
    return action


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
