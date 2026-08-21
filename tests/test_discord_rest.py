from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_discord.config import apply_runtime_secrets, load_config, read_host_bot_token
from agent_discord.discord.errors import ToolInvocationError
from agent_discord.discord.providers import select_provider
from agent_discord.discord.providers.rest import RestDiscordProvider
from agent_discord.discord.rest import (
    download_channel_attachment,
    fetch_bot_identity,
    list_channel_messages,
    message_from_rest_payload,
    send_channel_attachment,
    send_channel_message,
)


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_read_host_bot_token_uses_explicit_file(tmp_path: Path):
    path = tmp_path / ".discord_token"
    path.write_text("host-token-value\n", encoding="utf-8")
    assert read_host_bot_token(path=path) == "host-token-value"


def test_load_config_does_not_read_host_token_file(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    cfg = load_config(
        env={"AGENT_DISCORD_WORKSPACE": str(tmp_path / "ws")},
        dotenv_path=tmp_path / "missing.env",
    )
    assert cfg.discord_bot_token == ""
    host = tmp_path / "host.token"
    host.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setattr(
        "agent_discord.config.DEFAULT_HOST_BOT_TOKEN_PATH",
        host,
    )
    applied = apply_runtime_secrets(cfg)
    assert applied.discord_bot_token == "from-file"


def test_send_channel_attachment_posts_multipart_without_persisting_url():
    captured: dict[str, object] = {}

    def opener(request, timeout=60):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = request.data
        auth = request.get_header("Authorization")
        assert auth == "Bot tok"
        return _FakeResponse(
            json.dumps(
                {
                    "id": "1540090000000000001",
                    "channel_id": "ch",
                    "content": "caption",
                    "attachments": [
                        {
                            "id": "att-1",
                            "filename": "note.txt",
                            "size": 5,
                            "content_type": "text/plain",
                            "url": "https://cdn.discordapp.com/secret",
                        }
                    ],
                }
            ).encode("utf-8")
        )

    msg = send_channel_attachment(
        token="tok",
        channel_id="ch",
        filename="note.txt",
        data=b"hello",
        content="caption",
        opener=opener,
    )
    assert captured["url"].endswith("/channels/ch/messages")
    assert captured["method"] == "POST"
    body = captured["body"]
    assert isinstance(body, bytes)
    assert b"hello" in body
    assert b"payload_json" in body
    assert msg.message_id == "1540090000000000001"
    assert msg.attachments[0].attachment_id == "att-1"
    assert "url" not in msg.metadata
    assert all(not hasattr(att, "url") or not getattr(att, "url", "") for att in msg.attachments)


def test_message_from_rest_payload_strips_cdn_fields():
    msg = message_from_rest_payload(
        {
            "id": "m1",
            "channel_id": "ch",
            "content": "x",
            "attachments": [{"id": "a1", "filename": "f.bin", "size": 1, "url": "https://cdn.discordapp.com/x"}],
        },
        channel_id="ch",
    )
    dumped = json.dumps(msg.metadata)
    assert "cdn.discordapp.com" not in dumped
    assert msg.attachments[0].attachment_id == "a1"


def test_message_from_rest_payload_reads_v2_file_component():
    msg = message_from_rest_payload(
        {
            "id": "m2",
            "channel_id": "ch",
            "flags": 32768,
            "components": [
                {
                    "type": 17,
                    "components": [
                        {
                            "type": 13,
                            "id": 4,
                            "name": "note.txt",
                            "size": 24,
                            "file": {
                                "id": "not-the-attachment",
                                "url": "https://cdn.discordapp.com/attachments/ch/att-99/note.txt",
                            },
                        }
                    ],
                }
            ],
        },
        channel_id="ch",
    )
    assert msg.attachments[0].attachment_id == "att-99"
    assert msg.attachments[0].filename == "note.txt"
    assert "cdn.discordapp.com" not in json.dumps(msg.metadata)


def test_download_channel_attachment_uses_fresh_handle_then_drops_it():
    calls: list[str] = []

    def opener(request, timeout=60):
        calls.append(request.full_url)
        if request.full_url.endswith("/messages/m1"):
            return _FakeResponse(
                json.dumps(
                    {
                        "id": "m1",
                        "attachments": [
                            {
                                "id": "a1",
                                "filename": "f.bin",
                                "url": "https://cdn.discordapp.com/attachments/1/2/f.bin",
                            }
                        ],
                    }
                ).encode("utf-8")
            )
        return _FakeResponse(b"FILEBYTES")

    data = download_channel_attachment(
        token="tok",
        channel_id="ch",
        message_id="m1",
        attachment_id="a1",
        opener=opener,
    )
    assert data == b"FILEBYTES"
    assert calls[0].endswith("/channels/ch/messages/m1")
    assert calls[1].startswith("https://cdn.discordapp.com/")


def test_list_and_send_channel_messages_use_rest(tmp_path):
    captured: dict[str, object] = {}

    def opener(request, timeout=60):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        if request.get_method() == "GET":
            return _FakeResponse(
                json.dumps(
                    [
                        {
                            "id": "1540090000000000002",
                            "channel_id": "ch",
                            "content": "hello",
                            "author": {"id": "user-1"},
                        }
                    ]
                ).encode("utf-8")
            )
        return _FakeResponse(
            json.dumps(
                {
                    "id": "1540090000000000003",
                    "channel_id": "ch",
                    "content": "posted",
                    "author": {"id": "bot-1"},
                }
            ).encode("utf-8")
        )

    msgs = list_channel_messages(token="tok", channel_id="ch", limit=5, opener=opener)
    assert len(msgs) == 1
    assert msgs[0].author_id == "user-1"
    posted = send_channel_message(
        token="tok", channel_id="ch", content="posted", opener=opener
    )
    assert posted.message_id == "1540090000000000003"
    assert captured["method"] == "POST"


def test_start_message_thread_posts_name():
    captured: dict[str, object] = {}

    def opener(request, timeout=60):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            json.dumps({"id": "thread-9", "type": 11}).encode("utf-8")
        )

    from agent_discord.discord.rest import start_message_thread

    thread_id = start_message_thread(
        token="tok",
        channel_id="ch",
        message_id="msg-1",
        name="what is Discord OS?",
        opener=opener,
    )
    assert thread_id == "thread-9"
    assert captured["url"].endswith("/channels/ch/messages/msg-1/threads")
    assert captured["body"]["name"] == "what is Discord OS?"


def test_fetch_bot_identity_returns_public_fields_only():
    def opener(request, timeout=60):
        assert request.full_url.endswith("/users/@me")
        return _FakeResponse(json.dumps({"id": "bot-9", "username": "staff"}).encode("utf-8"))

    identity = fetch_bot_identity(token="tok", opener=opener)
    assert identity == {"id": "bot-9", "username": "staff", "avatar": ""}


def test_select_provider_defaults_to_rest(tmp_path):
    cfg = load_config(
        env={"AGENT_DISCORD_WORKSPACE": str(tmp_path), "DISCORD_BOT_TOKEN": "tok"},
        dotenv_path=tmp_path / "none",
    )
    provider = select_provider(cfg)
    assert isinstance(provider, RestDiscordProvider)


def test_download_rejects_non_cdn_url():
    def opener(request, timeout=60):
        return _FakeResponse(
            json.dumps(
                {
                    "id": "m1",
                    "attachments": [
                        {"id": "a1", "filename": "f.bin", "url": "https://evil.example/x"}
                    ],
                }
            ).encode("utf-8")
        )

    with pytest.raises(ToolInvocationError, match="CDN"):
        download_channel_attachment(
            token="tok",
            channel_id="ch",
            message_id="m1",
            attachment_id="a1",
            opener=opener,
        )
