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
MORE_ID = "discord-os:more"
PAIR_ID = "discord-os:pair"
HALT_ID = "discord-os:halt"
GATE_ID = "discord-os:gate"
ROLES_ID = "discord-os:roles"
ROLES_MODAL_ID = "discord-os:roles-modal"
ROLES_TEXT_ID = "discord-os:roles-text"
FILES_ID = "discord-os:files"
TERMINAL_ID = "discord-os:terminal"
BROWSER_ID = "discord-os:browser"
BROWSER_MODAL_ID = "discord-os:browser-modal"
BROWSER_TEXT_ID = "discord-os:browser-text"
GITHUB_ID = "discord-os:github"
COMPONENT_ROW = 1
BUTTON = 2
STYLE_PRIMARY = 1
STYLE_SECONDARY = 2
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
    paired: bool = False,
    write_gate: bool = False,
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
        rows = [{"type": COMPONENT_ROW, "components": power}]
        more = _more_select_options(
            armed=armed,
            paired=paired,
            write_gate=write_gate,
        )
        if more:
            rows.append(
                action_row(
                    [string_select(MORE_ID, more, placeholder="More")]
                )
            )
    options = _job_select_options(jobs or ())
    if options:
        rows.append(action_row([string_select(JOBS_ID, options)]))
    return rows


def _more_select_options(
    *,
    armed: bool,
    paired: bool,
    write_gate: bool,
) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    if not paired:
        options.append(
            {
                "label": "Pair",
                "value": PAIR_ID,
                "description": "First click becomes owner",
            }
        )
    options.append(
        {"label": "Halt", "value": HALT_ID, "description": "Stop new jobs"}
    )
    if write_gate:
        options.append(
            {
                "label": "Auto writes",
                "value": GATE_ID,
                "description": "Skip Approve",
            }
        )
    else:
        options.append(
            {
                "label": "Gate writes",
                "value": GATE_ID,
                "description": "Require Approve",
            }
        )
    options.append(
        {
            "label": "Roles",
            "value": ROLES_ID,
            "description": "Add an operator role",
        }
    )
    options.append(
        {
            "label": "GitHub",
            "value": GITHUB_ID,
            "description": "Host gh sign-in",
        }
    )
    if armed:
        options.extend(
            [
                {
                    "label": "Files",
                    "value": FILES_ID,
                    "description": "Open the folder on this Mac",
                },
                {
                    "label": "Terminal",
                    "value": TERMINAL_ID,
                    "description": "Open a shell on this Mac",
                },
                {
                    "label": "Browser",
                    "value": BROWSER_ID,
                    "description": "Open Chromium on this Mac",
                },
            ]
        )
    return options


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
    return _text_modal_payload(
        ASK_MODAL_ID,
        "Ask Discord OS",
        ASK_TEXT_ID,
        "Task",
        "What should this host do?",
        max_length=4000,
    )


def roles_modal_payload() -> dict[str, Any]:
    return _text_modal_payload(
        ROLES_MODAL_ID,
        "Add operator role",
        ROLES_TEXT_ID,
        "Discord role id",
        "Snowflake role id",
        max_length=32,
    )


def _text_modal_payload(
    custom_id: str,
    title: str,
    field_id: str,
    label: str,
    placeholder: str,
    *,
    max_length: int,
) -> dict[str, Any]:
    return {
        "type": CALLBACK_MODAL,
        "data": {
            "custom_id": custom_id,
            "title": title,
            "components": [
                {
                    "type": COMPONENT_ROW,
                    "components": [
                        {
                            "type": 4,
                            "custom_id": field_id,
                            "style": 2,
                            "label": label,
                            "min_length": 1,
                            "max_length": max_length,
                            "required": True,
                            "placeholder": placeholder,
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


def selected_more_id(payload: Mapping[str, Any]) -> str:
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    if str(data.get("custom_id") or "") != MORE_ID:
        return ""
    values = data.get("values")
    if not isinstance(values, list) or not values:
        return ""
    return str(values[0] or "").strip()


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
        if int(item.get("type") or 0) == 4:
            return str(item.get("value") or "").strip()
        if str(item.get("custom_id") or "") in {ASK_TEXT_ID, ROLES_TEXT_ID, BROWSER_TEXT_ID}:
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
        writes_need_approval,
    )

    spend_usd = 0.0
    cap_usd = None
    halted = False
    write_gate = False
    paired = False
    operator_count = 0
    role_count = 0
    last_job = ""
    realm = ""
    bank = False
    if store is not None:
        try:
            spend_usd = session_spend_usd(store)
            cap_usd = spend_cap_usd(store)
            halted = is_spend_halted(store)
            write_gate = writes_need_approval(store)
        except Exception:
            spend_usd = 0.0
            cap_usd = None
            halted = False
            write_gate = False
        paired = _panel_paired(store)
        operator_count, role_count = _panel_acl_counts(store)
        last_job = _panel_last_job(store, channel_id)
        realm = _panel_realm(store, channel_id)
        bank = _panel_bank(store, channel_id)
    card = host_card(
        armed=armed,
        channel_id=channel_id,
        confirm_off=confirm_off,
        avatar_url=avatar_url,
        spend_usd=spend_usd,
        cap_usd=cap_usd,
        halted=halted,
        paired=paired,
        operator_count=operator_count,
        role_count=role_count,
        last_job=last_job,
        write_gate=write_gate,
        realm=realm,
        bank=bank,
        github=_panel_github(),
    )
    return card.v2_payload(
        rows=host_panel_components(
            armed,
            confirm_off=confirm_off,
            jobs=jobs,
            paired=paired,
            write_gate=write_gate,
        )
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
    if raw == MORE_ID:
        return None
    if raw == PAIR_ID:
        return "pair"
    if raw == HALT_ID:
        return "halt"
    if raw == GATE_ID:
        return "gate"
    if raw == ROLES_ID:
        return "roles"
    if raw == FILES_ID:
        return "files"
    if raw == TERMINAL_ID:
        return "terminal"
    if raw == BROWSER_ID:
        return "browser"
    if raw == GITHUB_ID:
        return "github"
    return None


def panel_action_from_interaction(payload: Mapping[str, Any]) -> Optional[str]:
    if int(payload.get("type") or 0) != INTERACTION_MESSAGE_COMPONENT:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    custom_id = str(data.get("custom_id") or "")
    if custom_id == MORE_ID:
        return panel_action_from_custom_id(selected_more_id({"data": data}))
    return panel_action_from_custom_id(custom_id)


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


def interaction_channel_id(payload: Mapping[str, Any], fallback: str = "") -> str:
    raw = payload.get("channel_id")
    if raw:
        return str(raw)
    channel = payload.get("channel")
    if isinstance(channel, dict) and channel.get("id"):
        return str(channel["id"])
    return str(fallback or "")


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
    host_roots: Optional[list[Any]] = None,
    host_runner: Any = None,
    browser_open: Any = None,
) -> Optional[str]:
    """ACK within Discord's 3s window, then paint the panel. Best-effort."""

    from agent_discord.host.actions import job_action_from_custom_id

    channel_id = interaction_channel_id(payload, channel_id)
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
        return _handle_modal_submit(
            store,
            channel_id,
            payload,
            token=token,
            opener=opener,
            on_ask=on_ask,
            host_roots=host_roots,
            host_runner=host_runner,
            browser_open=browser_open,
        )

    action = panel_action_from_interaction(payload)
    if action is None:
        return None
    if action == "ask":
        _ack_interaction(payload, ask_modal_payload(), opener=opener)
        return action
    if action == "roles":
        _ack_interaction(payload, roles_modal_payload(), opener=opener)
        return action
    _ack_interaction(payload, {"type": CALLBACK_DEFERRED_UPDATE}, opener=opener)

    from agent_discord.orchestration.service import (
        author_may_operate,
        seed_owner_if_empty,
        toggle_spend_halted,
        toggle_write_gate,
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
        return "denied"
    if action == "halt":
        toggle_spend_halted(store)
    if action == "gate":
        toggle_write_gate(store)
    if action in {"files", "terminal", "browser"}:
        if _channel_armed(store, channel_id):
            _open_host_surface(
                action,
                "" if action == "browser" else ".",
                roots=host_roots,
                runner=host_runner,
                browser_open=browser_open,
            )
    if action == "job":
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
    try:
        _paint_after_ack(
            store,
            channel_id,
            payload,
            token=token,
            armed=armed,
            confirm_off=confirm_off,
            opener=opener,
        )
        print(
            f"panel painted action={action} paired={int(_panel_paired(store))}",
            flush=True,
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


def _handle_modal_submit(
    store: Any,
    channel_id: str,
    payload: Mapping[str, Any],
    *,
    token: str,
    opener: Any,
    on_ask: Optional[Callable[[str], None]],
    host_roots: Optional[list[Any]],
    host_runner: Any,
    browser_open: Any,
) -> Optional[str]:
    data = payload.get("data")
    custom_id = ""
    if isinstance(data, dict):
        custom_id = str(data.get("custom_id") or "")
    text = _first_text_input(data.get("components") if isinstance(data, dict) else None)
    _ack_interaction(payload, {"type": CALLBACK_DEFERRED_UPDATE}, opener=opener)
    if custom_id == ASK_MODAL_ID:
        if text and callable(on_ask):
            try:
                on_ask(text)
            except Exception:
                pass
        return "ask" if text else None
    from agent_discord.orchestration.service import author_may_operate

    user_id = interaction_user_id(payload)
    role_ids = interaction_role_ids(payload)
    if not author_may_operate(store, user_id, custom_id, role_ids=role_ids):
        return "denied"
    if custom_id == ROLES_MODAL_ID:
        role_id = text.strip()
        writer = getattr(store, "add_operator_role", None)
        if role_id and callable(writer):
            try:
                writer(role_id)
                print(f"panel role {role_id}", flush=True)
            except Exception as exc:
                print(f"panel role failed: {exc}", flush=True)
        _paint_interaction(
            store, channel_id, payload, token=token, opener=opener, confirm_off=False
        )
        return "roles"
    if custom_id == BROWSER_MODAL_ID:
        if _channel_armed(store, channel_id):
            _open_host_surface(
                "browser",
                text,
                roots=host_roots,
                runner=host_runner,
                browser_open=browser_open,
            )
        _paint_interaction(
            store, channel_id, payload, token=token, opener=opener, confirm_off=False
        )
        return "browser"
    return None


def _open_host_surface(
    surface: str,
    target: str,
    *,
    roots: Optional[list[Any]],
    runner: Any,
    browser_open: Any,
) -> None:
    from agent_discord.host.actions import run_host_action

    try:
        run_host_action(
            surface,
            target,
            roots=list(roots or ()),
            runner=runner,
            browser_open=browser_open,
        )
        print(f"panel opened {surface}", flush=True)
    except Exception as exc:
        print(f"panel open failed: {exc}", flush=True)


def _paint_interaction(
    store: Any,
    channel_id: str,
    payload: Mapping[str, Any],
    *,
    token: str,
    opener: Any,
    confirm_off: bool,
) -> None:
    try:
        _paint_after_ack(
            store,
            channel_id,
            payload,
            token=token,
            armed=_channel_armed(store, channel_id),
            confirm_off=confirm_off,
            opener=opener,
        )
    except Exception as exc:
        print(f"panel paint failed: {exc}", flush=True)


def _panel_acl_counts(store: Any) -> tuple[int, int]:
    operators = 0
    roles = 0
    lister = getattr(store, "list_operators", None)
    if callable(lister):
        try:
            operators = len(list(lister()))
        except Exception:
            operators = 0
    role_lister = getattr(store, "list_operator_roles", None)
    if callable(role_lister):
        try:
            roles = len(list(role_lister()))
        except Exception:
            roles = 0
    return operators, roles


def _panel_last_job(store: Any, channel_id: str) -> str:
    jobs = _panel_jobs(store, channel_id)
    if not jobs:
        return ""
    job = jobs[0]
    status = str(job.get("status") or "").strip()
    text = str(job.get("summary") or job.get("intake_text") or "").replace("\n", " ")
    text = " ".join(text.split())
    if len(text) > 80:
        text = text[:77] + "..."
    if status and text:
        return f"Last: {status} · {text}"
    if status:
        return f"Last: {status}"
    if text:
        return f"Last: {text}"
    return ""


def _panel_realm(store: Any, channel_id: str) -> str:
    reader = getattr(store, "get_binding", None)
    if not callable(reader):
        return ""
    try:
        from agent_discord.host.realms import binding_metadata

        return str(binding_metadata(reader("default", channel_id)).get("repo") or "")
    except Exception:
        return ""


def _panel_bank(store: Any, channel_id: str) -> bool:
    try:
        from agent_discord.host.memory import channel_is_memory

        return bool(channel_is_memory(store, channel_id))
    except Exception:
        return False


def _panel_github() -> str:
    try:
        from agent_discord.host.github import github_host_row

        return github_host_row()
    except Exception:
        return "sign-in"


def _panel_paired(store: Any) -> bool:
    if store is None:
        return False
    from agent_discord.orchestration.service import operators_configured

    try:
        return bool(operators_configured(store))
    except Exception:
        return False


def _panel_jobs(store: Any, channel_id: str) -> list[dict[str, Any]]:
    reader = getattr(store, "list_recent_jobs", None)
    if not callable(reader):
        return []
    try:
        return list(reader(channel_id, limit=5))
    except Exception:
        return []


def _paint_after_ack(
    store: Any,
    channel_id: str,
    payload: Mapping[str, Any],
    *,
    token: str,
    armed: bool,
    confirm_off: bool,
    opener: Any,
) -> None:
    panel = host_panel_payload(
        armed,
        channel_id=channel_id,
        confirm_off=confirm_off,
        jobs=_panel_jobs(store, channel_id),
        store=store,
    )
    message = {"flags": panel["flags"], "components": panel["components"]}
    application_id = str(payload.get("application_id") or "")
    _interaction_id, ix_token = interaction_ids(payload)
    _ = _interaction_id
    if application_id and ix_token:
        from agent_discord.discord.rest import edit_original_interaction

        edit_original_interaction(
            application_id=application_id,
            interaction_token=ix_token,
            payload=message,
            opener=opener,
        )
        _remember_panel_message(store, channel_id, payload)
        return
    _paint_host_panel(
        store,
        channel_id,
        token=token,
        message_id=_remember_panel_message(store, channel_id, payload),
        armed=armed,
        confirm_off=confirm_off,
        opener=opener,
        panel=panel,
    )


def _paint_host_panel(
    store: Any,
    channel_id: str,
    *,
    token: str,
    message_id: str,
    armed: bool,
    confirm_off: bool,
    opener: Any,
    panel: Optional[dict[str, Any]] = None,
) -> None:
    if not token.strip() or not message_id:
        return
    from agent_discord.discord.rest import edit_channel_message

    if panel is None:
        panel = host_panel_payload(
            armed,
            channel_id=channel_id,
            confirm_off=confirm_off,
            jobs=_panel_jobs(store, channel_id),
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
