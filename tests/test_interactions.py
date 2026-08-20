"""Opt-in Interactions HTTP engine. Poverty default stays message-prefix."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.request import Request, urlopen

from agent_discord.cli import main
from agent_discord.discord.interactions import (
    CONNECT_COMMAND,
    INTERACTION_APPLICATION_COMMAND,
    INTERACTION_PING,
    OPEN_COMMAND,
    RESPONSE_PONG,
    handle_interaction_payload,
    register_opt_in_commands,
    serve_interactions,
    verify_ed25519,
)
from agent_discord.orchestration.cards import CARD_PREFIX


PUBLIC_KEY = "00" * 32
SIGNATURE = "11" * 64


def test_slash_connect_has_no_secret_option():
    assert "options" not in CONNECT_COMMAND
    names = {item["name"] for item in OPEN_COMMAND["options"]}
    assert names == {"surface", "target"}


def test_verify_ed25519_injected(monkeypatch):
    seen: list[tuple[bytes, bytes, bytes]] = []

    def verify_fn(public_key: bytes, message: bytes, signature: bytes) -> bool:
        seen.append((public_key, message, signature))
        return True

    assert verify_ed25519(
        public_key_hex=PUBLIC_KEY,
        timestamp="ts",
        signature_hex=SIGNATURE,
        body=b"{}",
        verify_fn=verify_fn,
    )
    assert seen
    assert seen[0][1] == b"ts{}"
    assert not verify_ed25519(
        public_key_hex=PUBLIC_KEY,
        timestamp="ts",
        signature_hex=SIGNATURE,
        body=b"{}",
        verify_fn=lambda *a: False,
    )


def test_handle_ping_and_open_and_connect(tmp_path: Path):
    ping = handle_interaction_payload(
        {"type": INTERACTION_PING},
        workspace=tmp_path,
        roots=[tmp_path],
    )
    assert ping == {"type": RESPONSE_PONG}

    opened: list[tuple[list[str], str | None]] = []

    def runner(argv, *, cwd=None):
        opened.append((list(argv), cwd))

    open_reply = handle_interaction_payload(
        {
            "type": INTERACTION_APPLICATION_COMMAND,
            "data": {
                "name": "open",
                "options": [
                    {"name": "surface", "value": "files"},
                    {"name": "target", "value": "."},
                ],
            },
        },
        workspace=tmp_path,
        roots=[tmp_path],
        runner=runner,
    )
    assert opened
    assert open_reply["data"]["content"].startswith(f"{CARD_PREFIX} OPEN")
    assert open_reply["data"]["flags"] == 64

    connect_reply = handle_interaction_payload(
        {"type": INTERACTION_APPLICATION_COMMAND, "data": {"name": "connect"}},
        workspace=tmp_path,
        roots=[tmp_path],
    )
    content = connect_reply["data"]["content"]
    assert content.startswith(f"{CARD_PREFIX} CONNECT")
    assert "sk-" not in content.lower()


def test_register_opt_in_commands_posts_connect_and_open():
    posted: list[dict[str, object]] = []

    def opener(request, timeout=0):
        posted.append(json.loads(request.data.decode("utf-8")))

        class Resp:
            def read(self):
                return json.dumps({"name": posted[-1]["name"]}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Resp()

    names = register_opt_in_commands(
        token="tok",
        application_id="app-id",
        guild_id="guild-1",
        opener=opener,
    )
    assert names == ["connect", "open"]
    assert [item["name"] for item in posted] == ["connect", "open"]
    assert "options" not in posted[0]


def test_interactions_http_ping_and_bad_signature(tmp_path: Path):
    server = serve_interactions(
        public_key_hex=PUBLIC_KEY,
        workspace=tmp_path,
        roots=[tmp_path],
        host="127.0.0.1",
        port=0,
        verify_fn=lambda *a: True,
        runner=lambda *a, **k: None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        health = json.loads(urlopen(f"http://{host}:{port}/health", timeout=2).read())
        assert health == {"ok": True}
        ping_req = Request(
            f"http://{host}:{port}/interactions",
            data=b'{"type":1}',
            headers={
                "Content-Type": "application/json",
                "X-Signature-Timestamp": "1",
                "X-Signature-Ed25519": SIGNATURE,
            },
            method="POST",
        )
        ping = json.loads(urlopen(ping_req, timeout=2).read())
        assert ping == {"type": RESPONSE_PONG}
    finally:
        server.shutdown()
        thread.join(timeout=2)

    bad = serve_interactions(
        public_key_hex=PUBLIC_KEY,
        workspace=tmp_path,
        roots=[tmp_path],
        host="127.0.0.1",
        port=0,
        verify_fn=lambda *a: False,
    )
    bad_thread = threading.Thread(target=bad.serve_forever, daemon=True)
    bad_thread.start()
    host, port = bad.server_address
    try:
        req = Request(
            f"http://{host}:{port}/interactions",
            data=b'{"type":1}',
            headers={
                "Content-Type": "application/json",
                "X-Signature-Timestamp": "1",
                "X-Signature-Ed25519": SIGNATURE,
            },
            method="POST",
        )
        try:
            urlopen(req, timeout=2)
            raised = False
        except Exception as exc:
            raised = True
            assert getattr(exc, "code", None) == 401
        assert raised
    finally:
        bad.shutdown()
        bad_thread.join(timeout=2)


def test_cli_interactions_off_without_flags(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(tmp_path / ".agent-discord"))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUPPETMASTER_MODEL", "cursor/grok-4-5")
    monkeypatch.setenv("AGENT_DISCORD_INTERACTIONS", "off")
    assert main(["interactions"]) == 1
    err = capsys.readouterr().err
    assert "default is off" in err
