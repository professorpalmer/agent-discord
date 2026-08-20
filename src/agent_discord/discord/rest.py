"""Discord HTTP REST — default live transport (no MCP, no Gateway).

Object put/get, channel poll, and send/edit/delete use the official API.
CDN URLs are ephemeral handles only — never stored as durable keys.
Token is sent as Authorization and never returned.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent_discord.contracts import DiscordAttachment, DiscordMessage
from agent_discord.discord.errors import ToolInvocationError

DISCORD_API_BASE = "https://discord.com/api/v10"
USER_AGENT = "discord-os (https://github.com/professorpalmer/agent-discord)"


UrlOpener = Callable[..., Any]


def call_discord_json(
    token: str,
    method: str,
    path: str,
    *,
    payload: Optional[dict[str, Any]] = None,
    opener: Optional[UrlOpener] = None,
) -> Any:
    """JSON Discord REST helper. Token is sent as Authorization only."""

    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _discord_request(
        token,
        method,
        path,
        body=body,
        content_type="application/json",
        opener=opener,
    )


def fetch_bot_identity(
    *,
    token: str,
    opener: Optional[UrlOpener] = None,
) -> dict[str, str]:
    """GET /users/@me. Returns public bot fields only — never the token."""

    raw = call_discord_json(token, "GET", "/users/@me", opener=opener)
    if not isinstance(raw, dict):
        raise ToolInvocationError("Discord REST identity was not an object")
    return {
        "id": str(raw.get("id") or ""),
        "username": str(raw.get("username") or ""),
    }


def list_channel_messages(
    *,
    token: str,
    channel_id: str,
    limit: int = 20,
    thread_id: Optional[str] = None,
    opener: Optional[UrlOpener] = None,
) -> list[DiscordMessage]:
    dest = thread_id or channel_id
    capped = max(1, min(int(limit), 100))
    raw = call_discord_json(
        token,
        "GET",
        f"/channels/{dest}/messages?limit={capped}",
        opener=opener,
    )
    if not isinstance(raw, list):
        raise ToolInvocationError("Discord REST message list was not an array")
    return [
        message_from_rest_payload(item, channel_id=channel_id, thread_id=thread_id)
        for item in raw
        if isinstance(item, dict)
    ]


def send_channel_message(
    *,
    token: str,
    channel_id: str,
    content: str,
    thread_id: Optional[str] = None,
    components: Optional[list[dict[str, Any]]] = None,
    opener: Optional[UrlOpener] = None,
) -> DiscordMessage:
    dest = thread_id or channel_id
    payload: dict[str, Any] = {"content": content}
    if components:
        payload["components"] = list(components)
    raw = call_discord_json(
        token,
        "POST",
        f"/channels/{dest}/messages",
        payload=payload,
        opener=opener,
    )
    return message_from_rest_payload(
        raw,
        channel_id=channel_id,
        thread_id=thread_id,
        fallback_content=content,
    )


def edit_channel_message(
    *,
    token: str,
    channel_id: str,
    message_id: str,
    content: str,
    components: Optional[list[dict[str, Any]]] = None,
    opener: Optional[UrlOpener] = None,
) -> DiscordMessage:
    payload: dict[str, Any] = {"content": content}
    if components is not None:
        payload["components"] = list(components)
    raw = call_discord_json(
        token,
        "PATCH",
        f"/channels/{channel_id}/messages/{message_id}",
        payload=payload,
        opener=opener,
    )
    return message_from_rest_payload(
        raw, channel_id=channel_id, fallback_content=content
    )


def callback_interaction(
    *,
    interaction_id: str,
    interaction_token: str,
    payload: dict[str, Any],
    opener: Optional[UrlOpener] = None,
) -> None:
    """ACK a button click. Uses the interaction token, not the bot token."""

    if not interaction_id.strip() or not interaction_token.strip():
        raise ToolInvocationError("Discord interaction callback missing id/token")
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(
        f"{DISCORD_API_BASE}/interactions/{interaction_id}/{interaction_token}/callback",
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    do_open = opener or urlopen
    try:
        with do_open(request, timeout=10) as resp:
            resp.read()
    except HTTPError as exc:
        exc.read()
        raise ToolInvocationError(f"Discord interaction callback HTTP {exc.code}") from None
    except URLError as exc:
        raise ToolInvocationError("Discord interaction callback unreachable") from exc


def delete_channel_message(
    *,
    token: str,
    channel_id: str,
    message_id: str,
    opener: Optional[UrlOpener] = None,
) -> None:
    call_discord_json(
        token,
        "DELETE",
        f"/channels/{channel_id}/messages/{message_id}",
        opener=opener,
    )


def send_channel_attachment(
    *,
    token: str,
    channel_id: str,
    filename: str,
    data: bytes,
    content: str = "",
    thread_id: Optional[str] = None,
    opener: Optional[UrlOpener] = None,
) -> DiscordMessage:
    """POST a file to a channel (or thread) via Discord REST multipart."""

    dest = thread_id or channel_id
    safe_name = _safe_filename(filename)
    payload = {
        "content": content or "",
        "attachments": [{"id": 0, "filename": safe_name}],
    }
    body, content_type = _multipart_message(payload, safe_name, data)
    raw = _discord_request(
        token,
        "POST",
        f"/channels/{dest}/messages",
        body=body,
        content_type=content_type,
        opener=opener,
    )
    return message_from_rest_payload(
        raw,
        channel_id=channel_id,
        thread_id=thread_id,
        fallback_content=content,
    )


def fetch_channel_message(
    *,
    token: str,
    channel_id: str,
    message_id: str,
    opener: Optional[UrlOpener] = None,
) -> DiscordMessage:
    raw = _discord_request(
        token,
        "GET",
        f"/channels/{channel_id}/messages/{message_id}",
        opener=opener,
    )
    return message_from_rest_payload(raw, channel_id=channel_id)


def download_attachment_url(
    *,
    token: str,
    url: str,
    opener: Optional[UrlOpener] = None,
) -> bytes:
    """GET an ephemeral attachment URL. Caller must not persist the URL."""

    if not url.startswith("https://cdn.discordapp.com/") and not url.startswith(
        "https://media.discordapp.net/"
    ):
        raise ToolInvocationError("attachment URL is not a Discord CDN handle")
    return _discord_request_bytes(token, "GET", url, opener=opener, absolute=True)


def download_channel_attachment(
    *,
    token: str,
    channel_id: str,
    message_id: str,
    attachment_id: str,
    opener: Optional[UrlOpener] = None,
) -> bytes:
    """Re-fetch the message for a fresh CDN handle, then download. Do not store the URL."""

    raw = _discord_request(
        token,
        "GET",
        f"/channels/{channel_id}/messages/{message_id}",
        opener=opener,
    )
    if not isinstance(raw, dict):
        raise ToolInvocationError("Discord REST returned a non-object message")
    for att in raw.get("attachments") or ():
        if not isinstance(att, dict):
            continue
        if str(att.get("id") or "") != str(attachment_id):
            continue
        url = str(att.get("url") or att.get("proxy_url") or "")
        if not url:
            raise ToolInvocationError("attachment had no ephemeral CDN handle")
        return download_attachment_url(token=token, url=url, opener=opener)
    raise ToolInvocationError(
        f"attachment {attachment_id!r} not on message {message_id!r}"
    )


def message_from_rest_payload(
    raw: Any,
    *,
    channel_id: str,
    thread_id: Optional[str] = None,
    fallback_content: str = "",
) -> DiscordMessage:
    if not isinstance(raw, dict):
        raise ToolInvocationError("Discord REST returned a non-object message")
    attachments: list[DiscordAttachment] = []
    for att in raw.get("attachments") or ():
        if not isinstance(att, dict):
            continue
        att_id = str(att.get("id") or "")
        name = str(att.get("filename") or "")
        if not att_id and not name:
            continue
        try:
            size = int(att.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        attachments.append(
            DiscordAttachment(
                attachment_id=att_id,
                filename=name,
                size=size,
                content_type=str(att.get("content_type") or ""),
            )
        )
    msg_thread = thread_id
    if raw.get("thread") and isinstance(raw["thread"], dict) and raw["thread"].get("id"):
        msg_thread = str(raw["thread"]["id"])
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    return DiscordMessage(
        channel_id=str(raw.get("channel_id") or channel_id),
        content=str(raw.get("content") or fallback_content),
        message_id=str(raw.get("id") or ""),
        thread_id=msg_thread,
        author_id=str(author.get("id") or "") or None,
        attachments=tuple(attachments),
        metadata={"provider": "discord-rest"},
    )


def _safe_filename(filename: str) -> str:
    name = (filename or "object.bin").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch not in "\r\n\x00\"")
    return name or "object.bin"


def _multipart_message(
    payload: dict[str, Any], filename: str, data: bytes
) -> tuple[bytes, str]:
    boundary = f"----agentdiscord{uuid.uuid4().hex}"
    crlf = b"\r\n"
    chunks: list[bytes] = []
    chunks.extend(
        (
            f"--{boundary}".encode("ascii"),
            crlf,
            b'Content-Disposition: form-data; name="payload_json"',
            crlf,
            b"Content-Type: application/json",
            crlf,
            crlf,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            crlf,
            f"--{boundary}".encode("ascii"),
            crlf,
            (
                f'Content-Disposition: form-data; name="files[0]"; '
                f'filename="{filename}"'
            ).encode("utf-8"),
            crlf,
            b"Content-Type: application/octet-stream",
            crlf,
            crlf,
            data,
            crlf,
            f"--{boundary}--".encode("ascii"),
            crlf,
        )
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _discord_request(
    token: str,
    method: str,
    path: str,
    *,
    body: Optional[bytes] = None,
    content_type: str = "application/json",
    opener: Optional[UrlOpener] = None,
) -> Any:
    raw = _discord_request_bytes(
        token,
        method,
        path,
        body=body,
        content_type=content_type,
        opener=opener,
        absolute=False,
    )
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ToolInvocationError("Discord REST returned non-JSON") from exc
    if isinstance(parsed, dict) and parsed.get("message") and parsed.get("code"):
        raise ToolInvocationError(f"Discord REST error {parsed.get('code')}")
    return parsed


def _discord_request_bytes(
    token: str,
    method: str,
    path: str,
    *,
    body: Optional[bytes] = None,
    content_type: str = "application/json",
    opener: Optional[UrlOpener] = None,
    absolute: bool = False,
) -> bytes:
    if not token.strip():
        raise ToolInvocationError("Discord REST requires a bot token")
    url = path if absolute else f"{DISCORD_API_BASE}{path}"
    headers = {
        "Authorization": f"Bot {token.strip()}",
        "User-Agent": USER_AGENT,
    }
    if body is not None:
        headers["Content-Type"] = content_type
    request = Request(url, data=body, headers=headers, method=method)
    do_open = opener or urlopen
    try:
        with do_open(request, timeout=60) as resp:
            return resp.read()
    except HTTPError as exc:
        exc.read()
        raise ToolInvocationError(f"Discord REST HTTP {exc.code}") from None
    except URLError as exc:
        raise ToolInvocationError("Discord REST unreachable") from exc
