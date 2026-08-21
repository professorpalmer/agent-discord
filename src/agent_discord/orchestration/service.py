"""Operator pairing, spend halt, and host cron — poverty steals, not a product suite."""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional, Sequence

from agent_discord.contracts import UsageReceipt

HOST_PREFS_WORKSPACE = "_host"
SPEND_HALT_KEY = "spend_halt"
SPEND_CAP_KEY = "spend_cap_usd"
WRITE_GATE_KEY = "write_gate"
DEFAULT_SPEND_CAP_USD = 10.0
_INPUT_USD_PER_MTOK = 0.50
_OUTPUT_USD_PER_MTOK = 1.50
_EVERY_RE = re.compile(
    r"^(?P<n>\d+(?:\.\d+)?)(?P<unit>s|m|h|d|sec|secs|min|mins|hr|hrs|hour|hours|day|days)?$",
    re.IGNORECASE,
)
_SCHEDULE_RE = re.compile(
    r"^(?:/)?schedule\s+every\s+(\d+(?:\.\d+)?[a-z]*)\s*[:\-]?\s+(.+)$",
    re.IGNORECASE,
)


def spend_usd_from_usage(usage: Optional[UsageReceipt]) -> float:
    """Provider cost first; otherwise a conservative token estimate. Never $0-snap."""

    if usage is None:
        return 0.0
    meta = usage.metadata if isinstance(usage.metadata, Mapping) else {}
    for key in ("cost", "total_cost", "cost_usd", "usd", "total_cost_usd"):
        raw = meta.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    inbound = _token_count(usage.input_tokens)
    outbound = _token_count(usage.output_tokens)
    if inbound is None and outbound is None:
        return 0.0
    return (
        (inbound or 0) * _INPUT_USD_PER_MTOK / 1_000_000.0
        + (outbound or 0) * _OUTPUT_USD_PER_MTOK / 1_000_000.0
    )


def format_usd(amount: float) -> str:
    value = max(0.0, float(amount))
    if value >= 0.01:
        return f"${value:.2f}"
    return f"${value:.4f}"


def session_spend_usd(store: Any, workspace_id: str = "") -> float:
    reader = getattr(store, "session_spend_usd", None)
    if not callable(reader):
        return 0.0
    try:
        return float(reader(workspace_id) or 0.0)
    except Exception:
        return 0.0


def spend_cap_usd(store: Any) -> Optional[float]:
    raw = _host_pref(store, SPEND_CAP_KEY)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_SPEND_CAP_USD
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_SPEND_CAP_USD


def is_spend_halted(store: Any, workspace_id: str = "") -> bool:
    if _truthy(_host_pref(store, SPEND_HALT_KEY)):
        return True
    cap = spend_cap_usd(store)
    if cap is None:
        return False
    return session_spend_usd(store, workspace_id) >= cap


def set_spend_halted(store: Any, halted: bool) -> None:
    writer = getattr(store, "set_preference", None)
    if not callable(writer):
        return
    writer(HOST_PREFS_WORKSPACE, SPEND_HALT_KEY, "1" if halted else "0")


def toggle_spend_halted(store: Any) -> bool:
    next_halted = not _truthy(_host_pref(store, SPEND_HALT_KEY))
    set_spend_halted(store, next_halted)
    return next_halted


def writes_need_approval(store: Any) -> bool:
    return _truthy(_host_pref(store, WRITE_GATE_KEY))


def set_write_gate(store: Any, gated: bool) -> None:
    writer = getattr(store, "set_preference", None)
    if not callable(writer):
        return
    writer(HOST_PREFS_WORKSPACE, WRITE_GATE_KEY, "1" if gated else "0")


def toggle_write_gate(store: Any) -> bool:
    next_gated = not writes_need_approval(store)
    set_write_gate(store, next_gated)
    return next_gated


def seed_write_gate_from_env(store: Any, env: Optional[Mapping[str, str]] = None) -> None:
    raw = str((env or os.environ).get("DISCORD_OS_WRITE_GATE") or "").strip()
    if not raw:
        return
    set_write_gate(store, _truthy(raw))


def set_spend_cap_usd(store: Any, cap: float) -> None:
    writer = getattr(store, "set_preference", None)
    if not callable(writer):
        return
    writer(HOST_PREFS_WORKSPACE, SPEND_CAP_KEY, f"{float(cap):.4f}")


def seed_spend_cap_from_env(store: Any, env: Optional[Mapping[str, str]] = None) -> None:
    raw = str((env or os.environ).get("DISCORD_OS_SPEND_CAP_USD") or "").strip()
    if not raw:
        return
    try:
        set_spend_cap_usd(store, float(raw))
    except (TypeError, ValueError):
        return


def author_is_operator(
    store: Any,
    user_id: Optional[str],
    *,
    role_ids: Optional[Sequence[str]] = None,
) -> bool:
    uid = str(user_id or "").strip()
    if not uid:
        return False
    checker = getattr(store, "is_operator", None)
    if callable(checker):
        try:
            return bool(checker(uid, role_ids=role_ids))
        except Exception:
            return False
    return False


def operators_configured(store: Any) -> bool:
    lister = getattr(store, "list_operators", None)
    if not callable(lister):
        return False
    try:
        return bool(list(lister()))
    except Exception:
        return False


def seed_owner_if_empty(store: Any, user_id: Optional[str]) -> bool:
    seeder = getattr(store, "seed_owner_if_empty", None)
    if not callable(seeder):
        return False
    try:
        return bool(seeder(user_id))
    except Exception:
        return False


def author_may_dispatch(store: Any, user_id: Optional[str], *, role_ids: Optional[Sequence[str]] = None) -> bool:
    """Fail closed after an owner exists. First armed human becomes owner."""

    uid = str(user_id or "").strip()
    if not uid:
        return False
    if not operators_configured(store):
        return seed_owner_if_empty(store, uid)
    return author_is_operator(store, uid, role_ids=role_ids)


def author_may_operate(
    store: Any,
    user_id: Optional[str],
    action: str = "",
    *,
    role_ids: Optional[Sequence[str]] = None,
) -> bool:
    _ = action
    if not operators_configured(store):
        return True
    uid = str(user_id or "").strip()
    if not uid:
        return False
    return author_is_operator(store, uid, role_ids=role_ids)


def parse_every_seconds(raw: str) -> int:
    text = (raw or "").strip().lower()
    match = _EVERY_RE.match(text)
    if not match:
        raise ValueError(f"unrecognized interval {raw!r}")
    amount = float(match.group("n"))
    unit = (match.group("unit") or "s").lower()
    if unit in {"s", "sec", "secs"}:
        seconds = amount
    elif unit in {"m", "min", "mins"}:
        seconds = amount * 60
    elif unit in {"h", "hr", "hrs", "hour", "hours"}:
        seconds = amount * 3600
    else:
        seconds = amount * 86400
    whole = int(seconds)
    if whole < 60:
        raise ValueError("interval must be at least 60 seconds")
    return whole


def parse_schedule_command(text: str) -> Optional[tuple[int, str]]:
    raw = (text or "").strip()
    match = _SCHEDULE_RE.match(raw)
    if not match:
        return None
    try:
        every_s = parse_every_seconds(match.group(1))
    except ValueError:
        return None
    prompt = match.group(2).strip()
    if not prompt:
        return None
    return every_s, prompt


def _host_pref(store: Any, key: str) -> Optional[str]:
    reader = getattr(store, "get_preference", None)
    if not callable(reader):
        return None
    try:
        value = reader(HOST_PREFS_WORKSPACE, key)
    except Exception:
        return None
    return None if value is None else str(value)


def _token_count(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _truthy(raw: Optional[str]) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on", "halted"}
