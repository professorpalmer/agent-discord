"""Parse and absorb /connect without returning secrets to Discord renderers.

Honest residual: a shred absorb (``/connect <secret>`` or ``!connect <secret>``
in channel text) is visible to Discord once, between send and delete. This is
not a real Discord slash Interaction; Gateway 3s ACK is out of scope.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from agent_discord.keys.vault import KeyVault


KNOWN_PROVIDERS = frozenset({"openrouter"})
TICKET_TTL = timedelta(minutes=15)
TICKET_LENGTH = 8
DEFAULT_PROVIDER = "openrouter"
OPENROUTER_ENV = "OPENROUTER_API_KEY"
CONNECT_CARD_KIND = "CONNECT"


@dataclass(frozen=True)
class ParsedConnect:
    """Internal parse. ``secret`` is never copied onto a Discord card."""

    provider: str
    secret: Optional[str]
    raw_command: str


@dataclass(frozen=True)
class ConnectPublicResult:
    """Safe fields for CLI / Discord cards — never includes the raw secret."""

    action: str
    provider: str
    source: str = ""
    fingerprint: str = ""
    ticket: str = ""
    card: str = ""
    error: str = ""
    stored: bool = False


def is_connect_command(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    first = stripped.split(None, 1)[0].lower()
    return first in {"/connect", "!connect"}


def parse_connect_command(text: str, *, default_provider: str = DEFAULT_PROVIDER) -> ParsedConnect:
    stripped = (text or "").strip()
    parts = stripped.split()
    if not parts:
        return ParsedConnect(provider=default_provider, secret=None, raw_command=stripped)
    rest = parts[1:]
    provider = default_provider
    secret: Optional[str] = None
    if rest and rest[0].lower() in KNOWN_PROVIDERS:
        provider = rest[0].lower()
        rest = rest[1:]
    if rest:
        secret = rest[0]
    return ParsedConnect(provider=provider, secret=secret, raw_command=stripped)


def host_provider_secret(
    provider: str,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    source = dict(os.environ if env is None else env)
    if provider == "openrouter":
        return (source.get(OPENROUTER_ENV) or "").strip()
    return ""


def mint_pairing_ticket(
    workspace: Path,
    *,
    provider: str = DEFAULT_PROVIDER,
    now: Optional[datetime] = None,
) -> str:
    tickets = _read_tickets(workspace)
    moment = now or datetime.now(timezone.utc)
    ticket = secrets.token_hex(TICKET_LENGTH // 2)
    tickets[ticket] = {
        "provider": provider,
        "created_at": moment.replace(microsecond=0).isoformat(),
        "expires_at": (moment + TICKET_TTL).replace(microsecond=0).isoformat(),
    }
    _write_tickets(workspace, tickets)
    return ticket


def redeem_pairing_ticket(
    workspace: Path,
    ticket: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[dict[str, str]]:
    code = (ticket or "").strip()
    tickets = _read_tickets(workspace)
    raw = tickets.get(code)
    if not isinstance(raw, Mapping):
        return None
    expires_raw = str(raw.get("expires_at") or "")
    moment = now or datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(expires_raw)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except ValueError:
        tickets.pop(code, None)
        _write_tickets(workspace, tickets)
        return None
    if moment > expires:
        tickets.pop(code, None)
        _write_tickets(workspace, tickets)
        return None
    tickets.pop(code, None)
    _write_tickets(workspace, tickets)
    return {
        "provider": str(raw.get("provider") or DEFAULT_PROVIDER),
        "created_at": str(raw.get("created_at") or ""),
    }


def handle_connect_message(
    text: str,
    *,
    workspace: Path,
    env: Optional[Mapping[str, str]] = None,
    delete_ok: bool = True,
) -> ConnectPublicResult:
    """Absorb a channel connect command. Never returns the raw secret.

    Callers that shred must delete the inbound message before calling this
    with ``delete_ok=True``. If delete failed, pass ``delete_ok=False`` so the
    secret is not stored while it remains visible.
    """

    from agent_discord.orchestration.cards import render_connect_card

    parsed = parse_connect_command(text)
    vault = KeyVault(workspace / "keys")
    if parsed.secret:
        if not delete_ok:
            card = render_connect_card(
                provider=parsed.provider,
                fingerprint="",
                source="shred",
                error="delete failed; secret not stored",
            )
            return ConnectPublicResult(
                action="shred",
                provider=parsed.provider,
                source="shred",
                card=card,
                error="delete failed; secret not stored",
                stored=False,
            )
        public = vault.put(parsed.provider, parsed.secret, "shred")
        card = render_connect_card(
            provider=public["provider"],
            fingerprint=public["fingerprint"],
            source="shred",
        )
        return ConnectPublicResult(
            action="shred",
            provider=public["provider"],
            source="shred",
            fingerprint=public["fingerprint"],
            card=card,
            stored=True,
        )
    inherited = host_provider_secret(parsed.provider, env=env)
    if inherited:
        public = vault.put(parsed.provider, inherited, "env")
        card = render_connect_card(
            provider=public["provider"],
            fingerprint=public["fingerprint"],
            source="env",
        )
        return ConnectPublicResult(
            action="inherit",
            provider=public["provider"],
            source="env",
            fingerprint=public["fingerprint"],
            card=card,
            stored=True,
        )
    ticket = mint_pairing_ticket(workspace, provider=parsed.provider)
    card = render_connect_card(
        provider=parsed.provider,
        fingerprint="",
        source="ticket",
        ticket=ticket,
    )
    return ConnectPublicResult(
        action="ticket",
        provider=parsed.provider,
        source="ticket",
        ticket=ticket,
        card=card,
        stored=False,
    )


def bind_host_key(
    *,
    workspace: Path,
    provider: str = DEFAULT_PROVIDER,
    secret: Optional[str] = None,
    source: str,
    env: Optional[Mapping[str, str]] = None,
) -> ConnectPublicResult:
    """CLI/host bind. ``secret`` is never placed on the returned card."""

    from agent_discord.orchestration.cards import render_connect_card

    vault = KeyVault(workspace / "keys")
    material = (secret or "").strip()
    if not material and source == "env":
        material = host_provider_secret(provider, env=env)
    if not material:
        return ConnectPublicResult(
            action=source,
            provider=provider,
            source=source,
            error="no OpenRouter key; run discord-os connect",
        )
    public = vault.put(provider, material, source)
    card = render_connect_card(
        provider=public["provider"],
        fingerprint=public["fingerprint"],
        source=public["source"],
    )
    return ConnectPublicResult(
        action=source,
        provider=public["provider"],
        source=public["source"],
        fingerprint=public["fingerprint"],
        card=card,
        stored=True,
    )


def _tickets_path(workspace: Path) -> Path:
    return Path(workspace) / "keys" / "tickets.json"


def _read_tickets(workspace: Path) -> dict[str, Any]:
    path = _tickets_path(workspace)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    tickets = raw.get("tickets") if isinstance(raw, Mapping) else raw
    return dict(tickets) if isinstance(tickets, Mapping) else {}


def _write_tickets(workspace: Path, tickets: Mapping[str, Any]) -> None:
    path = _tickets_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "tickets": dict(tickets)}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
