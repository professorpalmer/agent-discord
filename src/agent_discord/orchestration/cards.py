"""Discord-safe harness cards. Stable skip prefix is ``**Card**``."""

from __future__ import annotations

from typing import Optional

from agent_discord.contracts import RunReceipt
from agent_discord.orchestration.receipts import render_receipt
from agent_discord.redaction import redact_text_markers


CARD_PREFIX = "**Card**"


def is_harness_card(content: str) -> bool:
    text = (content or "").strip()
    return text.startswith(CARD_PREFIX)


def render_connect_card(
    *,
    provider: str,
    fingerprint: str,
    source: str,
    ticket: str = "",
    error: str = "",
) -> str:
    lines = [f"{CARD_PREFIX} CONNECT", f"Provider: `{provider}`"]
    if source:
        lines.append(f"Source: `{source}`")
    if fingerprint:
        lines.append(f"Fingerprint: `…{fingerprint}`")
    if ticket:
        lines.append(f"Ticket: `{ticket}`")
        lines.append(
            f"Run on the host: `discord-os connect --ticket {ticket} "
            f"--provider {provider}`"
        )
        lines.append("Paste the key on stdin of the host. Ticket expires in 15 minutes.")
    if error:
        lines.append(f"Error: {error}")
    return redact_text_markers("\n".join(lines))


def render_progress_card(
    *,
    stage: str,
    message: str,
    percent: Optional[float] = None,
    run_id: str = "",
) -> str:
    pct = f" ({percent:.0f}%)" if percent is not None else ""
    lines = [f"{CARD_PREFIX} PROGRESS"]
    if run_id:
        lines.append(f"Run: `{run_id}`")
    lines.append(f"[{stage}] {message}{pct}")
    return redact_text_markers("\n".join(lines))


def render_receipt_card(receipt: RunReceipt, *, max_progress: int = 5) -> str:
    body = render_receipt(receipt, max_progress=max_progress)
    if body.startswith(CARD_PREFIX):
        return body
    return redact_text_markers(f"{CARD_PREFIX} RECEIPT\n{body}")


def render_open_card(
    *,
    surface: str,
    target: str,
    error: str = "",
) -> str:
    lines = [f"{CARD_PREFIX} OPEN", f"Surface: `{surface}`"]
    if target:
        lines.append(f"Target: `{target}`")
    if error:
        lines.append(f"Error: {error}")
    else:
        lines.append("Opened on the listen host.")
    return redact_text_markers("\n".join(lines))


def render_overflow_card(
    *,
    filename: str,
    sha256: str,
    size: int,
    jump_url: str,
    local_stash: str = "",
) -> str:
    lines = [
        f"{CARD_PREFIX} OVERFLOW",
        f"Kind: `overflow`",
        f"File: `{filename}`",
        f"SHA-256: `{sha256}`",
        f"Size: {size}",
        f"Jump: {jump_url}",
    ]
    if local_stash:
        lines.append(f"Stash: `{local_stash}`")
    return redact_text_markers("\n".join(lines))
