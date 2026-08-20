"""Native Discord On/Off buttons. Users do not type power commands."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence



ON_ID = "discord-os:on"
OFF_ID = "discord-os:off"
COMPONENT_ROW = 1
BUTTON = 2
STYLE_SUCCESS = 3
STYLE_DANGER = 4
INTERACTION_MESSAGE_COMPONENT = 3
CALLBACK_UPDATE_MESSAGE = 7


def host_panel_components(armed: bool) -> list[dict[str, Any]]:
    return [
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
            ],
        }
    ]


def host_panel_payload(armed: bool, *, channel_id: str = "") -> dict[str, Any]:
    from agent_discord.orchestration.cards import render_host_card

    return {
        "content": render_host_card(armed=armed, channel_id=channel_id),
        "components": host_panel_components(armed),
    }


def panel_action_from_custom_id(custom_id: str) -> Optional[str]:
    raw = (custom_id or "").strip()
    if raw == ON_ID:
        return "on"
    if raw == OFF_ID:
        return "off"
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
    if action in {"on", "off"} and callable(writer):
        return writer(channel_id, armed=action == "on")
    reader = getattr(store, "get_host_control", None)
    if callable(reader):
        current = reader(channel_id)
        if current is not None:
            return current
    return {"channel_id": channel_id, "armed": action != "off", "card_message_id": ""}


def interaction_callback_payload(armed: bool, *, channel_id: str = "") -> dict[str, Any]:
    panel = host_panel_payload(armed, channel_id=channel_id)
    return {
        "type": CALLBACK_UPDATE_MESSAGE,
        "data": {
            "content": panel["content"],
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
    opener: Any = None,
) -> Optional[str]:
    """Apply an On/Off click and ACK Discord. Best-effort; never raise."""

    action = panel_action_from_interaction(payload)
    if action is None:
        return None
    try:
        apply_panel_action(store, channel_id, action)
        message = payload.get("message")
        if isinstance(message, dict) and message.get("id"):
            writer = getattr(store, "set_host_control", None)
            if callable(writer):
                writer(channel_id, card_message_id=str(message["id"]))
        interaction_id, token = interaction_ids(payload)
        if interaction_id and token:
            from agent_discord.discord.rest import callback_interaction

            callback_interaction(
                interaction_id=interaction_id,
                interaction_token=token,
                payload=interaction_callback_payload(action == "on", channel_id=channel_id),
                opener=opener,
            )
    except Exception:
        return action
    return action
