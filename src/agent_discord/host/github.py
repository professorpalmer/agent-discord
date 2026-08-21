"""Host-side GitHub status. Analyze-mode Puppetmaster cannot run ``gh``."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Optional

from agent_discord.host.repos import host_path, which_on_host

GITHUB_AUTHED = "authed"
GITHUB_MISSING_BIN = "missing_bin"
GITHUB_UNAUTHENTICATED = "unauthenticated"

GITHUB_UNAUTHED_LINE = "GitHub CLI is installed but not signed in on this Mac."
GITHUB_MISSING_LINE = "GitHub CLI is not installed on this Mac."
GITHUB_HOWTO = (
    "Sign in on the host: discord-os add github\n"
    "or: gh auth login"
)

_AUTH_DUMP_MARKERS = (
    "gh auth login",
    "to get started with github cli",
    "you are not logged into any github hosts",
    "authentication required",
    "error: not logged in",
    "no github token",
)


def host_home(*, env: Optional[Mapping[str, str]] = None) -> Path:
    """HERMES-like runtime home. LaunchAgent lives here on this Mac."""

    source = os.environ if env is None else env
    raw = str(source.get("DISCORD_OS_HOME") or source.get("HERMES_HOME") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / "discord-os"


def host_env_files(*, env: Optional[Mapping[str, str]] = None) -> tuple[Path, ...]:
    seen: list[Path] = []
    for raw in (host_home(env=env) / ".env", Path.cwd() / ".env"):
        path = Path(raw)
        if path.is_file() and path not in seen:
            seen.append(path)
    return tuple(seen)


def load_host_tool_secrets(*, env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """GH_TOKEN / GITHUB_TOKEN from host .env then process env (env wins).

    When ``env`` is passed (tests), only that mapping is read — no host files.
    """

    values: dict[str, str] = {}
    source = os.environ if env is None else env
    if env is None:
        for path in host_env_files(env=source):
            values.update(_read_dotenv(path))
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        raw = str(source.get(key) or "").strip()
        if raw:
            values[key] = raw
    return {
        key: str(values[key]).strip()
        for key in ("GH_TOKEN", "GITHUB_TOKEN")
        if str(values.get(key) or "").strip()
    }


def github_host_env(*, env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """PATH + GitHub tokens the LaunchAgent and every worker should inherit."""

    child = dict(os.environ if env is None else env)
    child["PATH"] = host_path(child)
    secrets = load_host_tool_secrets(env=env)
    for key, value in secrets.items():
        child.setdefault(key, value)
    return child


def is_github_status_ask(text: str) -> bool:
    hay = (text or "").strip().lower()
    if not hay:
        return False
    wants_github = any(
        token in hay for token in (" pr", "prs", "pull request", "issue", "issues")
    )
    if not wants_github:
        return False
    return any(token in hay for token in ("open", "github", "repo", "check", "list"))


def is_github_auth_dump(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return False
    return any(marker in lower for marker in _AUTH_DUMP_MARKERS)


def is_github_unauthed_report(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if raw.startswith(GITHUB_UNAUTHED_LINE) or raw.startswith(GITHUB_MISSING_LINE):
        return True
    return is_github_auth_dump(raw)


def github_status_card(state: str) -> str:
    if state == GITHUB_MISSING_BIN:
        return f"{GITHUB_MISSING_LINE}\n{GITHUB_HOWTO}"
    return f"{GITHUB_UNAUTHED_LINE}\n{GITHUB_HOWTO}"


def github_host_row(state: str = "") -> str:
    if (state or gh_auth_state()) == GITHUB_AUTHED:
        return "ok"
    return "sign-in"


def gh_auth_state(
    *,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> str:
    child = github_host_env(env=env)
    gh = which_on_host("gh", env=child)
    if not gh:
        return GITHUB_MISSING_BIN
    if (child.get("GH_TOKEN") or child.get("GITHUB_TOKEN") or "").strip():
        return GITHUB_AUTHED
    run = runner or subprocess.run
    try:
        proc = run(
            [gh, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=15,
            env=child,
        )
    except Exception:
        return GITHUB_UNAUTHENTICATED
    blob = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if is_github_auth_dump(blob):
        return GITHUB_UNAUTHENTICATED
    if proc.returncode not in {0, None}:
        return GITHUB_UNAUTHENTICATED
    return GITHUB_AUTHED


def host_github_report(
    cwd: Path | str,
    *,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> str:
    """Run ``gh pr/issue list`` on this Mac. Short status when unsigned."""

    root = Path(cwd).expanduser()
    child = github_host_env(env=env)
    gh = which_on_host("gh", env=child)
    if not gh:
        return github_status_card(GITHUB_MISSING_BIN)
    if not root.is_dir():
        return ""
    state = gh_auth_state(env=env, runner=runner)
    if state != GITHUB_AUTHED:
        return github_status_card(state)
    run = runner or subprocess.run
    chunks: list[str] = []
    for title, args in (
        ("Open PRs", [gh, "pr", "list", "--state", "open", "--limit", "20"]),
        ("Open issues", [gh, "issue", "list", "--state", "open", "--limit", "20"]),
    ):
        try:
            proc = run(
                args,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=30,
                env=child,
            )
        except Exception as exc:
            chunks.append(f"{title}: {exc}")
            continue
        body = (proc.stdout or proc.stderr or "").strip() or "(none)"
        blob = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        if is_github_auth_dump(blob):
            return github_status_card(GITHUB_UNAUTHENTICATED)
        if proc.returncode not in {0, None}:
            body = body or f"exit {proc.returncode}"
        chunks.append(f"{title}:\n{body}")
    report = "\n\n".join(chunks).strip()
    if is_github_auth_dump(report):
        return github_status_card(GITHUB_UNAUTHENTICATED)
    return report


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
