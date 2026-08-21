"""Hermetic Discord OS object-store tests (fake provider only)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from agent_discord.cli import main
from agent_discord.config import load_config
from agent_discord.contracts import (
    ArtifactRef,
    DiscordMessage,
    DiscordObjectRef,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectTooLargeError,
    TaskIntake,
    ToolInvocationResult,
    discord_jump_url,
)
from agent_discord.orchestration.listen import drain_inbound, should_dispatch_inbound
from agent_discord.discord.errors import ToolInvocationError
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.object_store import DiscordObjectStore
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.discord.providers.saseq import SaseQDiscordProvider, _dig_id, _parse_messages
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.orchestration.receipts import render_receipt
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend


def _store(tmp_path: Path) -> tuple[DiscordObjectStore, FakeDiscordMCPProvider, DiscordFacade]:
    fake = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    return DiscordObjectStore(facade), fake, facade


def test_put_get_roundtrip_bytes_and_sha256():
    store, fake, facade = _store(Path("."))
    payload = b"discord-os-roundtrip"
    ref = store.put(payload, channel_id="ch", filename="note.bin", kind="blob")
    assert ref.sha256 == hashlib.sha256(payload).hexdigest()
    assert ref.size == len(payload)
    assert ref.channel_id == "ch"
    assert ref.message_id
    assert ref.attachment_id
    assert store.get(ref) == payload
    msg = facade.get_message(ref.channel_id, ref.message_id)
    assert msg.attachments
    assert msg.attachments[0].attachment_id == ref.attachment_id
    assert not hasattr(ref, "url")
    assert "url" not in asdict(ref)
    posted = fake.sent[-1]
    assert posted.content == ""
    texts = "\n".join(
        item.get("content") or ""
        for row in (posted.metadata.get("components") or [])
        for item in row.get("components") or []
        if item.get("type") == 10
    )
    assert "### note.bin" in texts
    assert "Discord OS" in texts


def test_get_refuses_channel_id_mismatch():
    store, _, _ = _store(Path("."))
    ref = store.put(b"secret", channel_id="allowed", filename="x.bin", kind="blob")
    with pytest.raises(ObjectNotFoundError, match="channel_id mismatch"):
        store.get(replace(ref, channel_id="other-channel"))
    with pytest.raises(ObjectNotFoundError, match="channel_id mismatch"):
        store.get(ref, channel_id="other-channel")


def test_put_rejects_over_max_bytes():
    fake = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    store = DiscordObjectStore(facade, max_bytes=4)
    with pytest.raises(ObjectTooLargeError):
        store.put(b"12345", channel_id="ch", filename="big.bin", kind="blob")
    assert fake.sent == []


def test_get_integrity_error_when_blob_tampered():
    store, fake, _ = _store(Path("."))
    ref = store.put(b"original", channel_id="ch", filename="t.bin", kind="blob")
    fake.blobs[ref.attachment_id] = b"tampered"
    with pytest.raises(ObjectIntegrityError, match="sha256"):
        store.get(ref)


def test_pointer_has_no_cdn_url():
    store, _, facade = _store(Path("."))
    ref = store.put(b"abc", channel_id="ch", filename="a.bin", kind="note")
    assert not hasattr(DiscordObjectRef, "url")
    assert "url" not in DiscordObjectRef.__dataclass_fields__
    dumped = json.dumps(asdict(ref))
    assert "url" not in json.loads(dumped)
    refreshed = facade.get_message(ref.channel_id, ref.message_id)
    assert refreshed.attachments
    data = facade.download_attachment(ref.channel_id, ref.message_id, ref.attachment_id)
    assert data == b"abc"


def test_cli_put_get_ls_fake_json(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / ".agent-discord"
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUPPETMASTER_MODEL", "cursor/grok-4-5")
    src = tmp_path / "payload.bin"
    src.write_bytes(b"discord-os-bytes")

    assert main(["put", str(src), "--channel-id", "99", "--fake", "--json"]) == 0
    put_payload = json.loads(capsys.readouterr().out)
    assert "url" not in put_payload
    assert put_payload["message_id"]
    assert put_payload["attachment_id"]
    assert put_payload["sha256"] == hashlib.sha256(b"discord-os-bytes").hexdigest()

    dest = tmp_path / "got.bin"
    assert (
        main(
            [
                "get",
                put_payload["message_id"],
                "--channel-id",
                "99",
                "--attachment-id",
                put_payload["attachment_id"],
                "--out",
                str(dest),
                "--fake",
                "--json",
            ]
        )
        == 0
    )
    get_payload = json.loads(capsys.readouterr().out)
    assert "url" not in get_payload
    assert dest.read_bytes() == b"discord-os-bytes"

    assert main(["ls", "--channel-id", "99", "--fake", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert any(item.get("message_id") == put_payload["message_id"] for item in listed)
    for item in listed:
        assert "url" not in item


def test_cli_put_get_ls_json_includes_guild_jump_url(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / ".agent-discord"
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUPPETMASTER_MODEL", "cursor/grok-4-5")
    src = tmp_path / "payload.bin"
    src.write_bytes(b"guild-pointer")
    guild_id = "1400123456789012345"
    channel_id = "99"

    assert (
        main(
            [
                "put",
                str(src),
                "--channel-id",
                channel_id,
                "--guild-id",
                guild_id,
                "--fake",
                "--json",
            ]
        )
        == 0
    )
    put_payload = json.loads(capsys.readouterr().out)
    expected = discord_jump_url(guild_id, channel_id, put_payload["message_id"])
    assert put_payload["jump_url"] == expected
    assert "/@me/" not in put_payload["jump_url"]

    dest = tmp_path / "got.bin"
    assert (
        main(
            [
                "get",
                put_payload["message_id"],
                "--channel-id",
                channel_id,
                "--out",
                str(dest),
                "--fake",
                "--json",
            ]
        )
        == 0
    )
    get_payload = json.loads(capsys.readouterr().out)
    assert get_payload["jump_url"] == expected

    assert main(["ls", "--channel-id", channel_id, "--fake", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert any(item.get("jump_url") == expected for item in listed)


def test_orchestrator_puts_file_artifact_through_discord(tmp_path: Path):
    blob = tmp_path / "out.bin"
    blob.write_bytes(b"hello-artifact")
    store = SQLiteStore(tmp_path / "o.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    backend = FakePuppetmasterBackend(artifact_files=[str(blob)])
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        discord=facade,
        post_progress_to_discord=True,
    )
    receipt = orch.run_task(
        TaskIntake(
            text="make file",
            channel_id="ch",
            workspace_id="ws",
            guild_id="guild-1",
        )
    )
    assert receipt.artifacts
    art = receipt.artifacts[0]
    assert art.message_id
    assert art.attachment_id
    assert art.sha256 == hashlib.sha256(b"hello-artifact").hexdigest()
    rows = store.list_artifacts(receipt.run_id)
    assert rows[0]["message_id"] == art.message_id
    assert rows[0]["attachment_id"] == art.attachment_id
    assert any(m.attachments for m in fake_discord.sent)
    rendered = render_receipt(receipt)
    assert art.kind in rendered
    assert discord_jump_url("guild-1", "ch", art.message_id) in rendered
    assert "chain_of_thought" not in rendered
    store.close()


def test_orchestrator_keeps_local_path_when_put_fails(tmp_path: Path):
    blob = tmp_path / "out.bin"
    blob.write_bytes(b"keep-me")

    class BrokenFacade(DiscordFacade):
        def send_attachment(self, *args, **kwargs):
            raise RuntimeError("mcp down")

    store = SQLiteStore(tmp_path / "fail.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = BrokenFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    backend = FakePuppetmasterBackend(artifact_files=[str(blob)])
    orch = AgentOrchestrator(store=store, backend=backend, discord=facade)
    receipt = orch.run_task(TaskIntake(text="x", channel_id="ch", workspace_id="ws"))
    assert receipt.artifacts[0].path == str(blob)
    assert receipt.artifacts[0].message_id == ""
    rows = store.list_artifacts(receipt.run_id)
    assert rows[0]["path"] == str(blob)
    assert rows[0]["provenance"]["object_store_error"]
    store.close()


def test_live_provider_fails_closed_without_file_tool():
    class CatalogClient:
        def list_tools(self):
            from agent_discord.contracts import ToolDescriptor

            return [ToolDescriptor(name="send_message"), ToolDescriptor(name="read_messages")]

        def call_tool(self, name, arguments):
            raise AssertionError(f"should not call {name}")

    provider = SaseQDiscordProvider(client=CatalogClient())
    with pytest.raises(ToolInvocationError, match="send_file"):
        provider.send_attachment("ch", "x.bin", b"abc")
    with pytest.raises(ToolInvocationError, match="get_attachment"):
        provider.download_attachment("ch", "m1", "a1")


def test_live_provider_uses_rest_when_mcp_lacks_file_tool(monkeypatch):
    from agent_discord.contracts import DiscordAttachment, DiscordMessage

    class CatalogClient:
        def list_tools(self):
            from agent_discord.contracts import ToolDescriptor

            return [ToolDescriptor(name="send_message"), ToolDescriptor(name="read_messages")]

        def call_tool(self, name, arguments):
            raise AssertionError(f"should not call {name}")

    sent: dict[str, object] = {}

    def fake_send(**kwargs):
        sent.update(kwargs)
        return DiscordMessage(
            channel_id="ch",
            content="caption",
            message_id="m-rest",
            attachments=(
                DiscordAttachment(attachment_id="a-rest", filename="x.bin", size=3),
            ),
        )

    monkeypatch.setattr(
        "agent_discord.discord.providers.saseq.send_channel_attachment",
        fake_send,
    )
    provider = SaseQDiscordProvider(client=CatalogClient(), bot_token="tok")
    msg = provider.send_attachment("ch", "x.bin", b"abc", content="caption")
    assert msg.message_id == "m-rest"
    assert sent["filename"] == "x.bin"
    assert sent["token"] == "tok"


def test_live_provider_falls_back_to_rest_when_mcp_attachment_is_not_bytes(monkeypatch):
    from agent_discord.contracts import ToolDescriptor, ToolInvocationResult

    class AttachmentClient:
        def list_tools(self):
            return [ToolDescriptor(name="get_attachment")]

        def call_tool(self, name, arguments):
            return ToolInvocationResult(
                name=name, ok=True, content="not-valid-base64!!!"
            )

    monkeypatch.setattr(
        "agent_discord.discord.providers.saseq.download_channel_attachment",
        lambda **kwargs: b"RESTBYTES",
    )
    provider = SaseQDiscordProvider(client=AttachmentClient(), bot_token="tok")
    assert provider.download_attachment("ch", "m1", "a1") == b"RESTBYTES"


def test_parse_messages_keeps_attachment_metadata_without_url():
    msgs = _parse_messages(
        {
            "messages": [
                {
                    "id": "m9",
                    "channelId": "ch",
                    "content": "file",
                    "attachments": [
                        {
                            "id": "a9",
                            "filename": "x.bin",
                            "size": 3,
                            "content_type": "application/octet-stream",
                            "url": "https://cdn.discordapp.com/expired",
                        }
                    ],
                }
            ]
        },
        channel_id="ch",
        provider="saseq",
    )
    assert msgs[0].attachments[0].attachment_id == "a9"
    assert not hasattr(msgs[0].attachments[0], "url")
    assert "url" not in asdict(msgs[0].attachments[0])


def test_dig_id_prefers_explicit_snowflake_then_jump_url_then_standalone():
    explicit = "1111111111111111111"
    from_url = "2222222222222222222"
    standalone = "3333333333333333333"
    blob = (
        "**Message sent successfully**\n"
        f"https://discord.com/channels/1400111111111111111/1400222222222222222/{from_url}\n"
        f"also mentioned {standalone}\n"
        "```markdown that must never become message_id```"
    )
    assert (
        _dig_id(
            ToolInvocationResult(
                name="send_message",
                ok=True,
                content=blob,
                raw={"messageId": explicit},
            )
        )
        == explicit
    )
    assert (
        _dig_id(ToolInvocationResult(name="send_message", ok=True, content=blob))
        == from_url
    )
    assert (
        _dig_id(
            ToolInvocationResult(
                name="send_message",
                ok=True,
                content=f"sent as {standalone} with extra words",
            )
        )
        == standalone
    )
    assert (
        _dig_id(
            ToolInvocationResult(
                name="send_message",
                ok=True,
                content=blob,
                raw={"id": blob},
            )
        )
        == from_url
    )


def test_dig_id_blob_without_snowflake_is_not_message_id():
    blob = "**Message sent successfully**\nDelivered to the channel.\n```lots of markdown```"
    assert _dig_id(ToolInvocationResult(name="send_message", ok=True, content=blob)) is None
    assert _dig_id(ToolInvocationResult(name="send_message", ok=True, content=blob, raw={"id": blob})) is None


def test_saseq_send_message_extracts_snowflake_not_markdown_blob():
    snowflake = "1400123456789012345"

    class SuccessClient:
        def list_tools(self):
            from agent_discord.contracts import ToolDescriptor

            return [ToolDescriptor(name="send_message")]

        def call_tool(self, name, arguments):
            return ToolInvocationResult(
                name=name,
                ok=True,
                content=[
                    {
                        "type": "text",
                        "text": (
                            "Message sent successfully!\n"
                            f"https://discord.com/channels/1/2/{snowflake}\n"
                            "```markdown blob that must not become message_id```"
                        ),
                    }
                ],
            )

    provider = SaseQDiscordProvider(client=SuccessClient())
    msg = provider.send_message("ch", "hello")
    assert msg.message_id == snowflake
    assert "markdown" not in msg.message_id
    assert "Message sent" not in msg.message_id


def test_saseq_send_message_blob_without_snowflake_uses_synthetic():
    blob = "**Success**\nMessage delivered.\n```lots of markdown```"

    class BlobClient:
        def list_tools(self):
            from agent_discord.contracts import ToolDescriptor

            return [ToolDescriptor(name="send_message")]

        def call_tool(self, name, arguments):
            return ToolInvocationResult(name=name, ok=True, content=blob)

    provider = SaseQDiscordProvider(client=BlobClient())
    msg = provider.send_message("ch", "hello")
    assert msg.message_id.startswith("saseq-")
    assert blob not in msg.message_id
    assert len(msg.message_id) < 40


def test_parse_messages_saseq_markdown_digest():
    content = [
        {
            "type": "text",
            "text": (
                '"**Retrieved 2 messages:** \\n'
                "- (ID: 111) **[Marionette]** `2026-08-20T19:56:44.499Z`: ```/connect```\\n"
                "- (ID: 222) **[Marionette]** `2026-08-20T19:55:55.201Z`: ```**Card** PROGRESS\\nup```\""
            ),
        }
    ]
    msgs = _parse_messages(content, channel_id="ch", provider="saseq")
    assert [m.message_id for m in msgs] == ["111", "222"]
    assert msgs[0].content == "/connect"
    assert msgs[1].content.startswith("**Card** PROGRESS")


def test_parse_messages_unparsed_text_is_not_an_inbound_task():
    assert _parse_messages("just a blob", channel_id="ch", provider="saseq") == []


def test_artifact_as_object_ref_and_jump_url():
    art = ArtifactRef(
        artifact_id="a",
        kind="note",
        path="",
        channel_id="c",
        message_id="m",
        attachment_id="att",
        sha256="dead",
        size=4,
        filename="n.txt",
        provenance={"guild_id": "g1"},
    )
    ref = art.as_object_ref()
    assert ref is not None
    assert ref.guild_id == "g1"
    assert "url" not in asdict(ref)
    assert discord_jump_url(None, "c", "m") == "https://discord.com/channels/@me/c/m"
    assert discord_jump_url("g1", "c", "m") == "https://discord.com/channels/g1/c/m"
    assert ArtifactRef(artifact_id="x", kind="k", path="/tmp/x").as_object_ref() is None


def test_load_config_max_object_bytes_default(tmp_path: Path):
    cfg = load_config(
        env={"AGENT_DISCORD_WORKSPACE": str(tmp_path)},
        dotenv_path=tmp_path / "none",
    )
    assert cfg.discord_max_object_bytes == 10_485_760


def test_should_dispatch_skips_receipts_progress_and_object_captions():
    assert should_dispatch_inbound(
        DiscordMessage(channel_id="ch", content="fix the login bug", message_id="m1")
    )
    assert not should_dispatch_inbound(DiscordMessage(channel_id="ch", content=""))
    assert not should_dispatch_inbound(
        DiscordMessage(channel_id="ch", content="**Receipt** `run`\nStatus: **OK**")
    )
    assert not should_dispatch_inbound(
        DiscordMessage(channel_id="ch", content="[dispatch] starting worker")
    )
    caption = json.dumps(
        {"agent_discord_object": 1, "kind": "blob", "filename": "x.bin", "sha256": "ab", "size": 1}
    )
    assert not should_dispatch_inbound(DiscordMessage(channel_id="ch", content=caption))


def test_listen_once_dispatches_inbox(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / ".agent-discord"
    monkeypatch.setenv("AGENT_DISCORD_WORKSPACE", str(ws))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    monkeypatch.setenv("PUPPETMASTER_MODEL", "cursor/grok-4-5")
    persist = ws / "fake_discord"
    persist.mkdir(parents=True)
    fake = FakeDiscordMCPProvider(persist_dir=persist)
    fake.inbox.append(
        DiscordMessage(
            channel_id="99",
            content="review invoices from phone",
            message_id="phone-1",
            author_id="user-7",
        )
    )
    fake.inbox.append(
        DiscordMessage(channel_id="99", content="**Receipt** already posted", message_id="bot-1")
    )
    fake._save_persist()

    assert main(["listen", "--channel-id", "99", "--fake", "--once", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["status"] == "completed"

    # Durable watermark: same phone message is not even presented again.
    assert main(["listen", "--channel-id", "99", "--fake", "--once", "--json"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again == []


def test_drain_inbound_uses_message_id_and_author(tmp_path: Path):
    store = SQLiteStore(tmp_path / "listen.sqlite3")
    store.initialize()
    fake = FakeDiscordMCPProvider()
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="do the thing",
            message_id="in-9",
            author_id="human-1",
        )
    )
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    backend = FakePuppetmasterBackend()
    orch = AgentOrchestrator(store=store, backend=backend, discord=facade)
    receipts = drain_inbound(orch, facade, channel_id="ch", workspace_id="ws", guild_id="g")
    assert len(receipts) == 1
    task = store.get_task(receipts[0].task_id)
    assert task is not None
    assert task["requester_id"] == "human-1"
    store.close()


def test_list_objects_pointer_index(tmp_path: Path):
    store = SQLiteStore(tmp_path / "idx.sqlite3")
    store.initialize()
    store.add_artifact(
        artifact_id="local-only",
        task_id="t",
        run_id="r",
        kind="file",
        path="/tmp/x",
    )
    store.add_artifact(
        artifact_id="obj",
        task_id="t",
        run_id="r",
        kind="blob",
        path="",
        channel_id="ch",
        message_id="m1",
        attachment_id="a1",
        filename="x.bin",
        sha256="abc",
        size=3,
    )
    listed = store.list_objects("ch")
    assert len(listed) == 1
    assert listed[0]["message_id"] == "m1"
    assert listed[0]["attachment_id"] == "a1"
    store.close()
