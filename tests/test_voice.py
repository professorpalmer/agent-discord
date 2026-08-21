"""WAVE 7 voice + mobile hooks. No network."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent_discord.contracts import DiscordAttachment, DiscordMessage
from agent_discord.discord.voice import (
    available,
    detect_voice_intent,
    mobile_push_suffix,
    spoken_command_to_intake,
    transcribe_voice_attachment,
    widget_status_payload,
)


def test_spoken_command_strips_wake_words():
    assert spoken_command_to_intake("hey discord os, run tests") == "run tests"
    assert spoken_command_to_intake("Hey Discord OS run tests") == "run tests"
    assert spoken_command_to_intake("discord os, run tests") == "run tests"
    assert spoken_command_to_intake("Discord OS run tests") == "run tests"
    assert spoken_command_to_intake("hey discord-os run tests") == "run tests"
    assert spoken_command_to_intake("run tests") == "run tests"
    assert spoken_command_to_intake("hey discord os") == ""
    assert spoken_command_to_intake("") == ""


def test_detect_voice_intent_ogg_attachment():
    message = DiscordMessage(
        channel_id="ch",
        content="",
        attachments=(
            DiscordAttachment(
                attachment_id="a1",
                filename="clip.ogg",
                size=12,
                content_type="audio/ogg; codecs=opus",
            ),
        ),
    )
    intent = detect_voice_intent(message)
    assert intent is not None
    assert intent["kind"] == "voice_attachment"
    assert intent["filename"] == "clip.ogg"
    assert intent["attachment_id"] == "a1"


def test_detect_voice_intent_voice_message_filename():
    intent = detect_voice_intent(
        {
            "content": "",
            "attachments": [
                {
                    "filename": "voice-message.ogg",
                    "content_type": "application/octet-stream",
                    "id": "a2",
                }
            ],
        }
    )
    assert intent is not None
    assert intent["kind"] == "voice_attachment"
    assert intent["filename"] == "voice-message.ogg"


def test_detect_voice_intent_spoken_transcript():
    message = DiscordMessage(
        channel_id="ch",
        content="",
        metadata={"transcript": "hey discord os run tests"},
    )
    intent = detect_voice_intent(message)
    assert intent is not None
    assert intent["kind"] == "spoken_transcript"
    assert intent["intake"] == "run tests"


def test_detect_voice_intent_plain_text_is_none():
    message = DiscordMessage(channel_id="ch", content="run tests")
    assert detect_voice_intent(message) is None
    assert detect_voice_intent({"content": "hello", "attachments": []}) is None


def test_transcribe_missing_cli_empty(tmp_path: Path):
    audio = tmp_path / "voice-message.ogg"
    audio.write_bytes(b"not-audio")
    missing = "discord-os-missing-whisper-cli"
    assert transcribe_voice_attachment(audio, whisper_cmd=missing) == ""
    assert available(whisper_cmd=missing) is False
    assert transcribe_voice_attachment(tmp_path / "absent.ogg", whisper_cmd=missing) == ""


def test_transcribe_uses_argv_list(tmp_path: Path, monkeypatch):
    audio = tmp_path / "voice-message.ogg"
    audio.write_bytes(b"fake")
    recorded: dict = {}

    def fake_which(name: str):
        return "/usr/bin/whisper" if name == "whisper" else None

    def fake_run(argv, **kwargs):
        recorded["argv"] = argv
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="run tests\n", stderr="")

    monkeypatch.setattr("agent_discord.discord.voice.shutil.which", fake_which)
    monkeypatch.setattr("agent_discord.discord.voice.subprocess.run", fake_run)
    assert transcribe_voice_attachment(audio) == "run tests"
    assert recorded["argv"][0] == "/usr/bin/whisper"
    assert str(audio) in recorded["argv"]
    assert recorded["kwargs"].get("shell") is not True


def test_widget_status_payload_keys():
    payload = widget_status_payload("run-1", "plan", 40, "running tests")
    assert payload["run_id"] == "run-1"
    assert payload["stage"] == "plan"
    assert payload["percent"] == 40.0
    assert payload["summary"] == "running tests"
    assert set(payload) == {"run_id", "stage", "percent", "summary"}
    json.dumps(payload)
    assert widget_status_payload("r", "done", None, "")["percent"] is None


def test_mobile_push_suffix_is_discord_only():
    suffix = mobile_push_suffix()
    assert isinstance(suffix, str)
    assert suffix
    assert "apns" not in suffix.lower()
    assert "fcm" not in suffix.lower()
    assert "Discord OS" in suffix
