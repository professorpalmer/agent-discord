"""On/Off Discord buttons, websocket framing, and login helper rendering."""

from __future__ import annotations

import json
from pathlib import Path

from agent_discord.discord.realtime import run_discord_gateway
from agent_discord.discord.rest import callback_interaction, send_channel_message
from agent_discord.discord.ws import decode_frame, encode_frame
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.host.install import render_launchd_plist
from agent_discord.host.panel import (
    ASK_ID,
    ASK_MODAL_ID,
    CANCEL_OFF_ID,
    CONFIRM_OFF_ID,
    BROWSER_ID,
    FILES_ID,
    GATE_ID,
    HALT_ID,
    JOBS_ID,
    MORE_ID,
    OFF_ID,
    ON_ID,
    PAIR_ID,
    ROLES_ID,
    ROLES_MODAL_ID,
    TERMINAL_ID,
    GITHUB_ID,
    ask_modal_payload,
    ask_text_from_interaction,
    handle_gateway_interaction,
    host_panel_components,
    panel_action_from_interaction,
)
from agent_discord.orchestration.listen import publish_host_card
from agent_discord.persistence.sqlite import SQLiteStore


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args) -> None:
        return None


class _FakeSocket:
    def __init__(self, incoming: list[str]) -> None:
        self.incoming = list(incoming)
        self.sent: list[dict] = []

    def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def recv_text(self, timeout: float = 1.0) -> str:
        if not self.incoming:
            from agent_discord.discord.ws import WebSocketError

            raise WebSocketError("closed")
        return self.incoming.pop(0)

    def close(self) -> None:
        return None


def test_websocket_frame_roundtrip():
    frame = encode_frame(b"hello")
    opcode, payload = decode_frame(bytearray(frame))
    assert opcode == 1
    assert payload == b"hello"


def test_panel_buttons_and_interaction_parse():
    buttons = host_panel_components(False)
    ids = [item["custom_id"] for item in buttons[0]["components"]]
    assert ids == [ON_ID, OFF_ID]
    more = buttons[1]["components"][0]
    assert more["custom_id"] == MORE_ID
    more_values = [item["value"] for item in more["options"]]
    assert more_values == [PAIR_ID, HALT_ID, GATE_ID, ROLES_ID, GITHUB_ID]
    assert more["options"][2]["label"] == "Gate writes"
    gated = host_panel_components(False, write_gate=True)
    assert gated[1]["components"][0]["options"][2]["label"] == "Auto writes"
    armed = host_panel_components(True)
    assert ASK_ID in [item["custom_id"] for item in armed[0]["components"]]
    armed_more = [item["value"] for item in armed[1]["components"][0]["options"]]
    assert armed_more == [
        PAIR_ID,
        HALT_ID,
        GATE_ID,
        ROLES_ID,
        GITHUB_ID,
        FILES_ID,
        TERMINAL_ID,
        BROWSER_ID,
    ]
    paired = host_panel_components(True, paired=True)
    paired_more = [item["value"] for item in paired[1]["components"][0]["options"]]
    assert PAIR_ID not in paired_more
    assert panel_action_from_interaction(
        {"type": 3, "data": {"custom_id": ON_ID}}
    ) == "on"
    assert panel_action_from_interaction(
        {"type": 3, "data": {"custom_id": OFF_ID}}
    ) == "off"
    assert panel_action_from_interaction(
        {"type": 3, "data": {"custom_id": MORE_ID, "values": [FILES_ID]}}
    ) == "files"
    assert panel_action_from_interaction({"type": 2, "data": {"custom_id": ON_ID}}) is None
    confirm = host_panel_components(True, confirm_off=True)
    confirm_ids = [item["custom_id"] for item in confirm[0]["components"]]
    assert confirm_ids == [CONFIRM_OFF_ID, CANCEL_OFF_ID]
    jobs = host_panel_components(
        True,
        jobs=[{"run_id": "run-1", "intake_text": "what is Discord OS?", "status": "completed"}],
    )
    assert jobs[2]["components"][0]["custom_id"] == JOBS_ID
    assert panel_action_from_interaction(
        {"type": 3, "data": {"custom_id": JOBS_ID, "values": ["run-1"]}}
    ) == "job"


def test_handle_gateway_interaction_acks_and_arms(tmp_path: Path):
    store = SQLiteStore(tmp_path / "panel.sqlite3")
    store.initialize()
    captured: dict[str, object] = {}

    def opener(request, timeout=10):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(b"")

    action = handle_gateway_interaction(
        store,
        "ch",
        {
            "type": 3,
            "id": "ix-1",
            "token": "ix-token",
            "data": {"custom_id": ON_ID},
            "message": {"id": "panel-1"},
        },
        opener=opener,
    )
    assert action == "on"
    assert store.host_is_armed("ch") is True
    assert captured["body"]["type"] == 6
    assert store.get_host_control("ch")["card_message_id"] == "panel-1"
    assert "ix-1/ix-token/callback" in str(captured["url"])
    body = captured["body"]
    assert body["type"] == 6
    store.close()


def test_off_requires_confirm(tmp_path: Path):
    store = SQLiteStore(tmp_path / "off.sqlite3")
    store.initialize()
    store.set_host_control("ch", armed=True)
    captured: list[dict] = []

    def opener(request, timeout=10):
        captured.append(
            {
                "url": request.full_url,
                "body": json.loads(request.data.decode("utf-8")) if request.data else {},
            }
        )
        return _FakeResponse(
            json.dumps(
                {
                    "id": "panel-1",
                    "channel_id": "ch",
                    "content": "",
                    "author": {"id": "bot"},
                }
            ).encode("utf-8")
        )

    action = handle_gateway_interaction(
        store,
        "ch",
        {
            "type": 3,
            "id": "ix-off",
            "token": "ix-token",
            "data": {"custom_id": OFF_ID},
            "message": {"id": "panel-1"},
        },
        token="tok",
        opener=opener,
    )
    assert action == "off"
    assert store.host_is_armed("ch") is True
    confirm = handle_gateway_interaction(
        store,
        "ch",
        {
            "type": 3,
            "id": "ix-ok",
            "token": "ix-token",
            "data": {"custom_id": CONFIRM_OFF_ID},
            "message": {"id": "panel-1"},
        },
        token="tok",
        opener=opener,
    )
    assert confirm == "off-confirm"
    assert store.host_is_armed("ch") is False
    store.close()


def test_publish_host_card_includes_buttons(tmp_path: Path):
    store = SQLiteStore(tmp_path / "card.sqlite3")
    store.initialize()
    store.set_host_control("ch", armed=False)
    fake = FakeDiscordMCPProvider()
    from agent_discord.discord.facade import DiscordFacade

    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    publish_host_card(facade, store, "ch")
    assert fake.sent
    meta = fake.sent[-1].metadata
    assert meta.get("components")
    assert meta["components"][0]["type"] == 17
    texts = "\n".join(
        item.get("content") or ""
        for item in meta["components"][0]["components"]
        if item.get("type") == 10
    )
    assert "### Stopped" in texts
    assert any(
        item.get("custom_id") == ON_ID
        for row in meta["components"][0].get("components", [])
        for item in row.get("components", [])
    )
    store.close()


def test_gateway_identifies_after_hello():
    events: list[str] = []
    sock = _FakeSocket(
        [
            json.dumps({"op": 10, "d": {"heartbeat_interval": 50}}),
            json.dumps({"op": 0, "t": "READY", "s": 1, "d": {}}),
        ]
    )
    try:
        run_discord_gateway(
            "tok",
            lambda event, payload: events.append(event),
            connect=lambda url: sock,
            gateway_url="wss://example.test/?v=10&encoding=json",
            heartbeat_scale=0.01,
        )
    except Exception:
        pass
    identify = next(item for item in sock.sent if item.get("op") == 2)
    assert identify["d"]["token"] == "tok"
    assert identify["d"]["intents"] == 1
    assert identify["d"]["presence"]["status"] == "idle"
    assert "READY" in events


def test_ask_modal_extracts_task_text():
    payload = ask_modal_payload()
    assert payload["type"] == 9
    assert payload["data"]["custom_id"] == ASK_MODAL_ID
    text = ask_text_from_interaction(
        {
            "type": 5,
            "data": {
                "custom_id": ASK_MODAL_ID,
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {"custom_id": "discord-os:ask-text", "value": "  list files  "}
                        ],
                    }
                ],
            },
        }
    )
    assert text == "list files"


def test_send_channel_message_posts_components():
    captured: dict[str, object] = {}

    def opener(request, timeout=60):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            json.dumps(
                {
                    "id": "1",
                    "channel_id": "ch",
                    "content": "panel",
                    "author": {"id": "bot"},
                }
            ).encode("utf-8")
        )

    send_channel_message(
        token="tok",
        channel_id="ch",
        content="panel",
        components=host_panel_components(False),
        opener=opener,
    )
    assert captured["body"]["components"][0]["components"][0]["custom_id"] == ON_ID


def test_callback_interaction_posts_without_bot_token():
    captured: dict[str, object] = {}

    def opener(request, timeout=10):
        captured["auth"] = request.headers.get("Authorization")
        captured["url"] = request.full_url
        return _FakeResponse(b"")

    callback_interaction(
        interaction_id="1",
        interaction_token="t",
        payload={"type": 7, "data": {"content": "x"}},
        opener=opener,
    )
    assert captured["auth"] in (None, "")
    assert captured["url"].endswith("/interactions/1/t/callback")


def test_launchd_plist_contains_channel_and_service_env(tmp_path: Path):
    plist = render_launchd_plist(
        argv=["/py", "-m", "agent_discord", "host", "run", "--channel-id", "99"],
        workspace=tmp_path,
        cwd=tmp_path,
        log=tmp_path / "host.log",
    )
    assert "99" in plist
    assert "DISCORD_OS_SERVICE" in plist
    assert "PYTHONUNBUFFERED" in plist
    assert "KeepAlive" in plist


def test_files_button_opens_workspace(tmp_path: Path):
    store = SQLiteStore(tmp_path / "files.sqlite3")
    store.initialize()
    store.set_host_control("ch", armed=True)
    store.add_operator("owner-7", role="owner")
    opened: list[tuple[str, str]] = []

    def runner(argv, *, cwd=None):
        opened.append((tuple(argv), cwd))

    def opener(request, timeout=10):
        return _FakeResponse(b"")

    action = handle_gateway_interaction(
        store,
        "ch",
        {
            "type": 3,
            "id": "ix",
            "token": "tok",
            "application_id": "app-1",
            "user": {"id": "owner-7"},
            "data": {"custom_id": FILES_ID},
            "message": {"id": "panel-1"},
        },
        opener=opener,
        host_roots=[tmp_path],
        host_runner=runner,
    )
    assert action == "files"
    assert opened
    store.close()


def test_roles_modal_adds_operator_role(tmp_path: Path):
    store = SQLiteStore(tmp_path / "roles.sqlite3")
    store.initialize()
    store.add_operator("owner-7", role="owner")

    def opener(request, timeout=10):
        return _FakeResponse(b"")

    action = handle_gateway_interaction(
        store,
        "ch",
        {
            "type": 5,
            "id": "ix",
            "token": "tok",
            "application_id": "app-1",
            "user": {"id": "owner-7"},
            "data": {
                "custom_id": ROLES_MODAL_ID,
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {"type": 4, "custom_id": "discord-os:roles-text", "value": "role-99"}
                        ],
                    }
                ],
            },
            "message": {"id": "panel-1"},
        },
        opener=opener,
    )
    assert action == "roles"
    assert "role-99" in store.list_operator_roles()
    store.close()
