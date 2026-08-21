"""Named git checkouts on the listen host.

Discord OS state (``.agent-discord``) is not a product repo. Workers need
the real trees plus ``gh`` so GitHub asks can run on this Mac.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence


DEFAULT_PROJECTS_DIR = Path.home() / "Projects"
_STATE_DIR_NAMES = frozenset({".agent-discord", "fake_discord"})
_FOLDER_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("puppetmaster", ("puppetmaster", "puppet master")),
    ("dugout", ("dugout",)),
    ("marionette", ("marionette", "pm-harness", "pmharness")),
    ("discord-os", ("discord os", "discord-os", "agent-discord")),
    ("portable-llm-wiki", ("portable llm wiki", "portable-llm-wiki")),
    ("my-portable-llm-wiki", ("my wiki", "wiki corpus")),
)
_FOLDER_CANDIDATES: dict[str, tuple[str, ...]] = {
    "puppetmaster": ("Puppetmaster", "puppetmaster"),
    "dugout": ("dugout",),
    "marionette": ("marionette",),
    "discord-os": ("agent-discord", "discord-os"),
    "portable-llm-wiki": ("portable-llm-wiki",),
    "my-portable-llm-wiki": ("my-portable-llm-wiki",),
}


@dataclass(frozen=True)
class HostRepo:
    name: str
    path: Path
    aliases: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        hay = (text or "").strip().lower()
        if not hay:
            return False
        needles = (self.name.lower(), *(item.lower() for item in self.aliases))
        return any(needle and needle in hay for needle in needles)


def load_host_repos(
    *,
    env: Optional[Mapping[str, str]] = None,
    projects_dir: Optional[Path] = None,
) -> tuple[HostRepo, ...]:
    """Read ``DISCORD_OS_REPOS`` then discover git roots under Projects."""

    source = dict(os.environ if env is None else env)
    found: dict[str, HostRepo] = {}
    for repo in _parse_repos_env(source.get("DISCORD_OS_REPOS") or ""):
        found[repo.name.lower()] = repo
    root = Path(projects_dir) if projects_dir is not None else DEFAULT_PROJECTS_DIR
    for name, folders in _FOLDER_CANDIDATES.items():
        if name in found:
            continue
        aliases = _aliases_for(name)
        for folder in folders:
            path = (root / folder).expanduser()
            if not _is_git_root(path):
                continue
            found[name] = HostRepo(name=name, path=path.resolve(), aliases=aliases)
            break
    return tuple(sorted(found.values(), key=lambda item: item.name))


def resolve_host_repo(
    prompt: str,
    repos: Sequence[HostRepo],
    *,
    default_cwd: Optional[Path] = None,
) -> Optional[HostRepo]:
    """Pick a named checkout when the ask names it. Do not guess."""

    hits = [repo for repo in repos if repo.matches(prompt)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        hits.sort(key=lambda item: len(item.name), reverse=True)
        return hits[0]
    fallback = Path(default_cwd).expanduser() if default_cwd is not None else None
    if fallback is not None and _is_git_root(fallback) and not _is_state_dir(fallback):
        if _mentions_current_repo(prompt):
            return HostRepo(name=fallback.name, path=fallback.resolve())
    return None


def host_reach_block(
    repos: Sequence[HostRepo],
    *,
    cwd: Optional[Path] = None,
    gh_bin: Optional[str] = None,
) -> str:
    """Tell the worker what this Mac can actually reach."""

    gh = (gh_bin or shutil.which("gh") or "").strip()
    lines = [
        "Host reach (this Mac; Discord is only the remote):",
        "- Network: yes. Use gh, curl, and git.",
        f"- gh: {'available at ' + gh if gh else 'not on PATH'}.",
    ]
    if gh:
        lines.append(
            "- GitHub: `gh pr list --state open` and `gh issue list --state open` "
            "from the repo cwd (or `gh pr list --repo owner/name`)."
        )
    if repos:
        lines.append("- Named git checkouts:")
        for repo in repos:
            lines.append(f"  - {repo.name}: {repo.path}")
    work = Path(cwd).expanduser() if cwd is not None else None
    if work is not None and _is_git_root(work):
        lines.append(f"- This run cwd: {work.resolve()}")
    else:
        lines.append(
            "- This run cwd is the Discord OS runtime, not a product repo. "
            "cd into a named checkout first when the ask names one."
        )
    lines.append("Do not treat .agent-discord as the subject repository.")
    return "\n".join(lines)


def _parse_repos_env(raw: str) -> tuple[HostRepo, ...]:
    text = (raw or "").strip()
    if not text:
        return ()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            repos = []
            for name, value in parsed.items():
                path = Path(str(value)).expanduser()
                if not _is_git_root(path):
                    continue
                key = str(name).strip().lower()
                repos.append(
                    HostRepo(name=key, path=path.resolve(), aliases=_aliases_for(key))
                )
            return tuple(repos)
    repos = []
    for item in text.split(","):
        name, sep, value = item.partition(":")
        if not sep:
            continue
        path = Path(value.strip()).expanduser()
        if not _is_git_root(path):
            continue
        key = name.strip().lower()
        repos.append(HostRepo(name=key, path=path.resolve(), aliases=_aliases_for(key)))
    return tuple(repos)


def _aliases_for(name: str) -> tuple[str, ...]:
    key = (name or "").strip().lower()
    for label, aliases in _FOLDER_ALIASES:
        if label == key:
            return aliases
    return (key,)


def _is_git_root(path: Path) -> bool:
    try:
        return path.is_dir() and (path / ".git").exists()
    except OSError:
        return False


def _is_state_dir(path: Path) -> bool:
    return path.name in _STATE_DIR_NAMES


def _mentions_current_repo(prompt: str) -> bool:
    hay = (prompt or "").lower()
    return any(token in hay for token in ("this repo", "the repo", "this checkout"))
