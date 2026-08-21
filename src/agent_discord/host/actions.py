"""Open Terminal, file manager, or browser on the listen host.

Paths stay inside configured roots. Browser URLs are allowlisted.
The runner is injectable so tests never spawn a GUI.

Job button custom_ids are parsed here as intent only — they never
toggle host power or dispatch Puppetmaster.
"""

from __future__ import annotations

import os
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence
from urllib.parse import urlparse

from agent_discord.discord.layout import CUSTOM_ID_MAX


class HostActionError(ValueError):
    """Rejected host open (path escape, bad URL, unknown surface)."""


@dataclass(frozen=True)
class HostActionResult:
    surface: str
    target: str
    argv: tuple[str, ...] = ()
    error: str = ""
    opened: bool = False


CommandRunner = Callable[..., object]

SURFACES = frozenset({"terminal", "files", "browser"})
JOB_ID_PREFIX = "discord-os:job:"
JOB_VERBS = frozenset({"approve", "cancel", "retry"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DISCORD_HOSTS = frozenset({"discord.com", "canary.discord.com", "ptb.discord.com"})


@dataclass(frozen=True)
class JobAction:
    """Parsed job-button intent. Does not start or stop a Puppetmaster run."""

    action: str
    run_id: str


def job_custom_id(action: str, run_id: str) -> str:
    verb = (action or "").strip().lower()
    prefix = f"{JOB_ID_PREFIX}{verb}:"
    budget = max(0, CUSTOM_ID_MAX - len(prefix))
    return prefix + (run_id or "").strip()[:budget]


def job_action_from_custom_id(custom_id: str) -> Optional[JobAction]:
    raw = (custom_id or "").strip()
    if not raw.startswith(JOB_ID_PREFIX):
        return None
    rest = raw[len(JOB_ID_PREFIX) :]
    verb, sep, run_id = rest.partition(":")
    if not sep or verb not in JOB_VERBS:
        return None
    run_id = run_id.strip()
    if not run_id:
        return None
    return JobAction(action=verb, run_id=run_id)


def run_host_action(
    surface: str,
    target: str,
    *,
    roots: Sequence[Path],
    runner: Optional[CommandRunner] = None,
    browser_open: Optional[Callable[[str], object]] = None,
) -> HostActionResult:
    kind = (surface or "").strip().lower()
    if kind not in SURFACES:
        raise HostActionError(f"unknown surface {surface!r}")
    if kind == "browser":
        url = allow_browser_url(target)
        opener = browser_open or webbrowser.open
        opener(url)
        return HostActionResult(surface="browser", target=url, opened=True)
    path = confine_host_path(target, roots)
    argv, cwd = _open_argv(kind, path)
    do_run = runner or _default_runner
    do_run(argv, cwd=str(cwd) if cwd is not None else None)
    return HostActionResult(
        surface=kind,
        target=str(path),
        argv=tuple(argv),
        opened=True,
    )


def confine_host_path(raw: str, roots: Sequence[Path]) -> Path:
    if not roots:
        raise HostActionError("no host roots configured")
    text = (raw or "").strip() or "."
    if text.startswith("~"):
        raise HostActionError("home-relative paths are not allowed")
    resolved_roots = [Path(root).expanduser().resolve() for root in roots]
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = (resolved_roots[0] / text).resolve()
    else:
        candidate = candidate.resolve()
    for root in resolved_roots:
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    raise HostActionError("path is outside host roots")


def allow_browser_url(raw: str) -> str:
    url = (raw or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HostActionError("browser target must be an http(s) URL")
    host = parsed.hostname.lower()
    if host in _LOOPBACK_HOSTS:
        return url
    if parsed.scheme == "https" and host in _DISCORD_HOSTS:
        if parsed.path.startswith("/channels/"):
            return url
        raise HostActionError("Discord URLs must be channel jump links")
    raise HostActionError("browser URL is not on the host allowlist")


def _open_argv(surface: str, path: Path) -> tuple[list[str], Optional[Path]]:
    location = str(path)
    platform = sys.platform
    if surface == "files":
        if platform == "darwin":
            return ["open", location], None
        if platform == "win32":
            return ["explorer", location], None
        return ["xdg-open", location], None
    if platform == "darwin":
        return ["open", "-a", "Terminal"], path
    if platform == "win32":
        # Visible console is the point. Do not hide this window.
        return ["cmd", "/k"], path
    terminal = os.environ.get("AGENT_DISCORD_TERMINAL") or "x-terminal-emulator"
    return [terminal, "--working-directory", location], path


def _default_runner(argv: Sequence[str], *, cwd: Optional[str] = None) -> None:
    import subprocess

    kwargs: dict[str, object] = {"check": False}
    if cwd:
        kwargs["cwd"] = cwd
    subprocess.run(list(argv), **kwargs)
