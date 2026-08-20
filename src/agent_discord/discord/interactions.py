"""Opt-in Discord Interactions HTTP engine.

Default listen stays message-prefix (poverty path). This module is the same
host verbs behind slash chrome. It does not open a second Gateway.

Discord requires a public HTTPS URL and a 3s ACK. Bind loopback; tunnel if
you opt in. Slash ``/connect`` never accepts a secret option.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlparse

from agent_discord.host.verbs import handle_open_message
from agent_discord.keys.connect import handle_connect_message


INTERACTION_PING = 1
INTERACTION_APPLICATION_COMMAND = 2
RESPONSE_PONG = 1
RESPONSE_CHANNEL_MESSAGE = 4
EPHEMERAL = 64

CONNECT_COMMAND = {
    "name": "connect",
    "description": "Bind an OpenRouter key on the listen host (no secret in Discord)",
    "type": 1,
}
OPEN_COMMAND = {
    "name": "open",
    "description": "Open Terminal, files, or an allowlisted URL on the listen host",
    "type": 1,
    "options": [
        {
            "name": "surface",
            "description": "Host surface to open",
            "type": 3,
            "required": True,
            "choices": [
                {"name": "terminal", "value": "terminal"},
                {"name": "files", "value": "files"},
                {"name": "browser", "value": "browser"},
            ],
        },
        {
            "name": "target",
            "description": "Workspace-relative path, or allowlisted http(s) URL",
            "type": 3,
            "required": False,
        },
    ],
}


class InteractionError(ValueError):
    """Bad signature, payload, or missing opt-in config."""


def verify_ed25519(
    *,
    public_key_hex: str,
    timestamp: str,
    signature_hex: str,
    body: bytes,
    verify_fn: Optional[Callable[[bytes, bytes, bytes], bool]] = None,
) -> bool:
    message = timestamp.encode("utf-8") + body
    try:
        signature = bytes.fromhex(signature_hex)
        public_key = bytes.fromhex(public_key_hex)
    except ValueError as exc:
        raise InteractionError("invalid signature encoding") from exc
    if verify_fn is not None:
        return bool(verify_fn(public_key, message, signature))
    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
    except ImportError as exc:
        raise InteractionError(
            "PyNaCl is required for interactions; pip install 'discord-os[interactions]'"
        ) from exc
    try:
        VerifyKey(public_key).verify(message, signature)
    except BadSignatureError:
        return False
    return True


def handle_interaction_payload(
    payload: Mapping[str, Any],
    *,
    workspace: Path,
    roots: Sequence[Path],
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Callable[..., object]] = None,
    browser_open: Optional[Callable[[str], object]] = None,
) -> dict[str, Any]:
    kind = int(payload.get("type") or 0)
    if kind == INTERACTION_PING:
        return {"type": RESPONSE_PONG}
    if kind != INTERACTION_APPLICATION_COMMAND:
        return _ephemeral("unsupported interaction")
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    name = str(data.get("name") or "").lower()
    if name == "connect":
        result = handle_connect_message("/connect", workspace=workspace, env=env)
        return _ephemeral(result.card or result.error or "connect")
    if name == "open":
        options = _option_map(data.get("options"))
        surface = str(options.get("surface") or "files")
        target = str(options.get("target") or ".")
        opened = handle_open_message(
            f"/open {surface} {target}".strip(),
            roots=roots,
            runner=runner,
            browser_open=browser_open,
        )
        return _ephemeral(opened.card or opened.error or "open")
    return _ephemeral("unknown command")


def register_opt_in_commands(
    *,
    token: str,
    application_id: str,
    guild_id: str = "",
    opener: Optional[Callable[..., Any]] = None,
) -> list[str]:
    from agent_discord.discord.rest import call_discord_json

    app_id = (application_id or "").strip()
    if not app_id:
        raise InteractionError("DISCORD_APPLICATION_ID is required to register slash commands")
    guild = (guild_id or "").strip()
    if guild:
        path = f"/applications/{app_id}/guilds/{guild}/commands"
    else:
        path = f"/applications/{app_id}/commands"
    names: list[str] = []
    for command in (CONNECT_COMMAND, OPEN_COMMAND):
        result = call_discord_json(
            token, "POST", path, payload=command, opener=opener
        )
        names.append(str((result or {}).get("name") or command["name"]))
    return names


def serve_interactions(
    *,
    public_key_hex: str,
    workspace: Path,
    roots: Sequence[Path],
    host: str = "127.0.0.1",
    port: int = 8743,
    env: Optional[Mapping[str, str]] = None,
    verify_fn: Optional[Callable[[bytes, bytes, bytes], bool]] = None,
    runner: Optional[Callable[..., object]] = None,
    browser_open: Optional[Callable[[str], object]] = None,
) -> ThreadingHTTPServer:
    key = public_key_hex

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/health":
                self.send_error(404)
                return
            self._write(200, {"ok": True})

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/interactions":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            timestamp = self.headers.get("X-Signature-Timestamp") or ""
            signature = self.headers.get("X-Signature-Ed25519") or ""
            try:
                ok = verify_ed25519(
                    public_key_hex=key,
                    timestamp=timestamp,
                    signature_hex=signature,
                    body=body,
                    verify_fn=verify_fn,
                )
            except InteractionError as exc:
                self._write(401, {"error": str(exc)})
                return
            if not ok:
                self._write(401, {"error": "bad signature"})
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                self._write(400, {"error": "invalid json"})
                return
            if not isinstance(payload, dict):
                self._write(400, {"error": "invalid json"})
                return
            response = handle_interaction_payload(
                payload,
                workspace=workspace,
                roots=roots,
                env=env,
                runner=runner,
                browser_open=browser_open,
            )
            self._write(200, response)

        def log_message(self, format: str, *args: object) -> None:
            return

        def _write(self, status: int, payload: Mapping[str, Any]) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return ThreadingHTTPServer((host, port), Handler)


def _option_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return {}
    out: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        out[name] = str(item.get("value") or "")
    return out


def _ephemeral(content: str) -> dict[str, Any]:
    return {
        "type": RESPONSE_CHANNEL_MESSAGE,
        "data": {"content": content[:2000], "flags": EPHEMERAL},
    }
