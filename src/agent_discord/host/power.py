"""Poverty-path host power: ``/on``, ``/off``, ``/status`` in channel text."""

from __future__ import annotations

from dataclasses import dataclass


ON_PREFIXES = frozenset({"/on", "!on"})
OFF_PREFIXES = frozenset({"/off", "!off"})
STATUS_PREFIXES = frozenset({"/status", "!status"})
POWER_PREFIXES = ON_PREFIXES | OFF_PREFIXES | STATUS_PREFIXES


@dataclass(frozen=True)
class ParsedPower:
    action: str
    raw_command: str


def is_power_command(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    first = stripped.split(None, 1)[0].lower()
    return first in POWER_PREFIXES


def parse_power_command(text: str) -> ParsedPower:
    stripped = (text or "").strip()
    first = stripped.split(None, 1)[0].lower() if stripped else ""
    if first in ON_PREFIXES:
        return ParsedPower(action="on", raw_command=stripped)
    if first in OFF_PREFIXES:
        return ParsedPower(action="off", raw_command=stripped)
    return ParsedPower(action="status", raw_command=stripped)
