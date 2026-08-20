"""Poverty-path host verbs: ``/open`` and ``!open`` in channel text."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from agent_discord.host.actions import (
    CommandRunner,
    HostActionError,
    HostActionResult,
    run_host_action,
)


OPEN_PREFIXES = frozenset({"/open", "!open"})


@dataclass(frozen=True)
class ParsedOpen:
    surface: str
    target: str
    raw_command: str


@dataclass(frozen=True)
class OpenPublicResult:
    surface: str
    target: str
    card: str
    error: str = ""
    opened: bool = False


def is_open_command(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    first = stripped.split(None, 1)[0].lower()
    return first in OPEN_PREFIXES


def parse_open_command(text: str) -> ParsedOpen:
    stripped = (text or "").strip()
    parts = stripped.split()
    if not parts:
        return ParsedOpen(surface="files", target=".", raw_command=stripped)
    rest = parts[1:]
    if not rest:
        return ParsedOpen(surface="files", target=".", raw_command=stripped)
    head = rest[0].lower()
    if head in {"terminal", "term", "shell"}:
        target = rest[1] if len(rest) > 1 else "."
        return ParsedOpen(surface="terminal", target=target, raw_command=stripped)
    if head in {"files", "finder", "explorer", "folder"}:
        target = rest[1] if len(rest) > 1 else "."
        return ParsedOpen(surface="files", target=target, raw_command=stripped)
    if head in {"browser", "url"}:
        target = rest[1] if len(rest) > 1 else ""
        return ParsedOpen(surface="browser", target=target, raw_command=stripped)
    if head.startswith("http://") or head.startswith("https://"):
        return ParsedOpen(surface="browser", target=rest[0], raw_command=stripped)
    return ParsedOpen(surface="files", target=rest[0], raw_command=stripped)


def handle_open_message(
    text: str,
    *,
    roots: Sequence[Path],
    runner: Optional[CommandRunner] = None,
    browser_open: Optional[Callable[[str], object]] = None,
) -> OpenPublicResult:
    from agent_discord.orchestration.cards import render_open_card

    parsed = parse_open_command(text)
    try:
        result = run_host_action(
            parsed.surface,
            parsed.target,
            roots=roots,
            runner=runner,
            browser_open=browser_open,
        )
    except HostActionError as exc:
        card = render_open_card(
            surface=parsed.surface,
            target=parsed.target,
            error=str(exc),
        )
        return OpenPublicResult(
            surface=parsed.surface,
            target=parsed.target,
            card=card,
            error=str(exc),
            opened=False,
        )
    card = render_open_card(surface=result.surface, target=_public_target(result))
    return OpenPublicResult(
        surface=result.surface,
        target=result.target,
        card=card,
        opened=result.opened,
    )


def _public_target(result: HostActionResult) -> str:
    if result.surface == "browser":
        return result.target
    return result.target
