"""Voice-message detection, optional local whisper, and mobile widget hooks.

WAVE 7 helpers only. Listen dispatch stays on text; a later wave can call
these when a message already has a local transcript. This module never
downloads Discord CDN attachments or whisper models.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

TRANSCRIBE_TIMEOUT_S = 60

# Longest first so "hey discord os" wins over "discord os".
_WAKE_PHRASES = (
    "hey discord os",
    "hey discord-os",
    "discord os",
    "discord-os",
)

_WHISPER_COMMANDS = ("whisper-cli", "whisper.cpp", "whisper-cpp", "whisper")

_PUNCT_RE = re.compile(r"[,.!?;:]+")

__all__ = [
    "available",
    "detect_voice_intent",
    "mobile_push_suffix",
    "spoken_command_to_intake",
    "transcribe_voice_attachment",
    "widget_status_payload",
]


def available(*, whisper_cmd: Optional[str] = None) -> bool:
    """True when a local whisper CLI can be resolved. Never downloads models."""

    return _resolve_whisper_cmd(whisper_cmd) is not None


def detect_voice_intent(message: Any) -> Optional[dict[str, Any]]:
    """Return voice intent for a Discord voice-message or a local transcript.

    Attachment intent: ``content_type`` is ``audio/ogg`` (optional codecs
    suffix) or ``filename`` starts with ``voice-message``. Transcript intent:
    text that looks like a spoken command after a local whisper pass
    (wake-word prefix or ``metadata.transcript``).
    """

    for attachment in _attachments_of(message):
        if not _is_voice_attachment(attachment):
            continue
        return {
            "kind": "voice_attachment",
            "filename": attachment.get("filename", ""),
            "content_type": attachment.get("content_type", ""),
            "attachment_id": attachment.get("attachment_id", ""),
        }

    transcript = _transcript_of(message)
    if not transcript:
        return None
    if not _looks_like_spoken_command(transcript) and not _has_metadata_transcript(message):
        return None
    return {
        "kind": "spoken_transcript",
        "transcript": transcript,
        "intake": spoken_command_to_intake(transcript),
    }


def transcribe_voice_attachment(
    path: str | Path,
    *,
    whisper_cmd: Optional[str] = None,
) -> str:
    """Transcribe a local audio file with a PATH whisper CLI.

    If no whisper / whisper.cpp / ffmpeg+whisper CLI is installed, return
    ``""`` so ``available()`` is False. Invokes argv lists only — never
    ``shell=True``, never interpolates the path into a shell string, and
    never downloads a model.
    """

    audio = Path(path)
    if not audio.is_file():
        return ""
    cli = _resolve_whisper_cmd(whisper_cmd)
    if cli is None:
        return ""
    argv = _transcribe_argv(cli, audio)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=TRANSCRIBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (proc.stdout or "").strip()
    if text:
        return text
    return (proc.stderr or "").strip()


def spoken_command_to_intake(transcript: str) -> str:
    """Strip wake words such as ``hey discord os`` / ``discord os``.

    ``"hey discord os, run tests"`` becomes ``"run tests"``.
    """

    raw = (transcript or "").strip()
    if not raw:
        return ""
    collapsed = " ".join(raw.split())
    matchable = " ".join(_PUNCT_RE.sub(" ", collapsed).split())
    lower = matchable.lower()
    for phrase in _WAKE_PHRASES:
        if lower == phrase:
            return ""
        prefix = phrase + " "
        if lower.startswith(prefix):
            return matchable[len(phrase) :].strip()
    return collapsed


def mobile_push_suffix() -> str:
    """Short hint appended so Discord mobile notification previews are identifiable.

    Discord mobile already pushes on channel posts. This is not a second
    push vendor — no APNs, FCM, or side-channel notifier.
    """

    return " · Discord OS"


def widget_status_payload(
    run_id: Any,
    stage: Any,
    percent: Any,
    summary: Any,
) -> dict[str, Any]:
    """JSON-serializable companion-widget status. Persist nothing."""

    payload = {
        "run_id": str(run_id),
        "stage": str(stage),
        "percent": _json_percent(percent),
        "summary": str(summary),
    }
    json.dumps(payload)
    return payload


def _json_percent(percent: Any) -> Optional[float]:
    if percent is None:
        return None
    return float(percent)


def _resolve_whisper_cmd(whisper_cmd: Optional[str]) -> Optional[str]:
    if whisper_cmd:
        return _executable_path(whisper_cmd)
    for name in _WHISPER_COMMANDS:
        found = _executable_path(name)
        if found is not None:
            return found
    return None


def _executable_path(command: str) -> Optional[str]:
    """Resolve a single executable name or path. Never split on spaces."""

    name = (command or "").strip()
    if not name:
        return None
    found = shutil.which(name)
    if found:
        return found
    candidate = Path(name)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _transcribe_argv(cli: str, audio: Path) -> list[str]:
    name = Path(cli).name.lower()
    audio_s = str(audio)
    if "whisper-cli" in name or "whisper.cpp" in name or "whisper-cpp" in name:
        return [cli, "-f", audio_s]
    return [cli, audio_s]


def _is_voice_attachment(attachment: Mapping[str, str]) -> bool:
    content_type = attachment.get("content_type", "").lower().split(";", 1)[0].strip()
    filename = Path(attachment.get("filename", "")).name.lower()
    if content_type == "audio/ogg":
        return True
    return filename.startswith("voice-message")


def _looks_like_spoken_command(text: str) -> bool:
    collapsed = " ".join(_PUNCT_RE.sub(" ", (text or "").strip()).split())
    lower = collapsed.lower()
    return any(lower == phrase or lower.startswith(phrase + " ") for phrase in _WAKE_PHRASES)


def _attachments_of(message: Any) -> list[dict[str, str]]:
    raw: Sequence[Any] = ()
    if hasattr(message, "attachments") and message.attachments:
        raw = message.attachments
    elif isinstance(message, Mapping) and message.get("attachments"):
        raw = message.get("attachments") or ()
    if not raw:
        meta = _metadata_of(message)
        extra = meta.get("attachments")
        if extra:
            raw = extra
    parsed: list[dict[str, str]] = []
    for item in raw:
        filename = ""
        content_type = ""
        attachment_id = ""
        if hasattr(item, "filename"):
            filename = str(getattr(item, "filename", "") or "")
            content_type = str(getattr(item, "content_type", "") or "")
            attachment_id = str(getattr(item, "attachment_id", "") or "")
        elif isinstance(item, Mapping):
            filename = str(item.get("filename") or item.get("fileName") or item.get("name") or "")
            content_type = str(item.get("content_type") or item.get("contentType") or "")
            attachment_id = str(item.get("attachment_id") or item.get("id") or "")
        parsed.append(
            {
                "filename": filename,
                "content_type": content_type,
                "attachment_id": attachment_id,
            }
        )
    return parsed


def _metadata_of(message: Any) -> Mapping[str, Any]:
    if hasattr(message, "metadata") and isinstance(message.metadata, Mapping):
        return message.metadata
    if isinstance(message, Mapping):
        meta = message.get("metadata")
        if isinstance(meta, Mapping):
            return meta
    return {}


def _has_metadata_transcript(message: Any) -> bool:
    meta = _metadata_of(message)
    for key in ("transcript", "voice_transcript"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def _transcript_of(message: Any) -> str:
    meta = _metadata_of(message)
    for key in ("transcript", "voice_transcript"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if hasattr(message, "content"):
        return str(message.content or "").strip()
    if isinstance(message, Mapping):
        return str(message.get("content") or "").strip()
    return ""
