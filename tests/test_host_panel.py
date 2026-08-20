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
    OFF_ID,
    ON_ID,
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
    assert panel_action_from_interaction(
        {"type": 3, "data": {"custom_id": ON_ID}}
    ) == "on"
    assert panel_action_from_interaction(
        {"type": 3, "data": {"custom_id": OFF_ID}}
    ) == "off"
    assert panel_action_from_interaction({"type": 2, "data": {"custom_id": ON_ID}}) is None


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
    assert store.get_host_control("ch")["card_message_id"] == "panel-1"
    assert "ix-1/ix-token/callback" in str(captured["url"])
    body = captured["body"]
    assert body["type"] == 7
    store.close()


def test_publish_host_card_includes_buttons(tmp_path: Path):
    store = SQLiteStore(tmp_path / "card.sqlite3")
    store.initialize()
    fake = FakeDiscordMCPProvider()
    from agent_discord.discord.facade import DiscordFacade

    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    publish_host_card(facade, store, "ch")
    assert fake.sent
    meta = fake.sent[-1].metadata
    assert meta.get("components")
    assert any(
        item.get("custom_id") == ON_ID
        for row in meta["components"]
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
    assert "READY" in events


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
    assert "KeepAlive" in plist
