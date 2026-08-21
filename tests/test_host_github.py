"""Host GitHub status: Discord OS runs gh, analyze-mode cannot."""

from __future__ import annotations

from pathlib import Path

from agent_discord.host.github import (
    GITHUB_AUTHED,
    GITHUB_MISSING_BIN,
    GITHUB_UNAUTHED_LINE,
    GITHUB_UNAUTHENTICATED,
    gh_auth_state,
    host_github_report,
    is_github_status_ask,
    is_github_unauthed_report,
)


def test_is_github_status_ask():
    assert is_github_status_ask("check if my Puppetmaster repo has any open PRs or Issues")
    assert is_github_status_ask("list open issues")
    assert not is_github_status_ask("what time is it")


def test_host_github_report_uses_injected_runner(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "agent_discord.host.github.which_on_host",
        lambda name, env=None: "/opt/homebrew/bin/gh",
    )
    calls: list[list[str]] = []

    class _Proc:
        def __init__(self, stdout: str, returncode: int = 0) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def runner(args, **kwargs):
        calls.append(list(args))
        if "pr" in args:
            return _Proc("12\topen\tdocs")
        if "auth" in args:
            return _Proc("Logged in to github.com")
        return _Proc("(none)")

    text = host_github_report(repo, env={}, runner=runner)
    assert "Open PRs" in text
    assert "12" in text
    assert "Open issues" in text
    assert "gh auth login" not in text
    assert any("pr" in item for item in calls)


def test_gh_auth_state_missing_bin(monkeypatch):
    monkeypatch.setattr(
        "agent_discord.host.github.which_on_host",
        lambda name, env=None: "",
    )
    assert gh_auth_state(env={}) == GITHUB_MISSING_BIN


def test_gh_auth_state_token_from_env(monkeypatch):
    monkeypatch.setattr(
        "agent_discord.host.github.which_on_host",
        lambda name, env=None: "/opt/homebrew/bin/gh",
    )
    assert gh_auth_state(env={"GH_TOKEN": "ghp_test"}) == GITHUB_AUTHED


def test_host_github_report_unauthed_is_short(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "agent_discord.host.github.which_on_host",
        lambda name, env=None: "/opt/homebrew/bin/gh",
    )

    class _Proc:
        def __init__(self) -> None:
            self.stdout = ""
            self.stderr = "To get started with GitHub CLI, run: gh auth login"
            self.returncode = 1

    text = host_github_report(repo, env={}, runner=lambda *a, **k: _Proc())
    assert GITHUB_UNAUTHED_LINE in text
    assert "discord-os add github" in text
    assert is_github_unauthed_report(text)
    assert "To get started with GitHub CLI" not in text.split("\n")[0]


def test_token_from_env_passed_to_runner(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "agent_discord.host.github.which_on_host",
        lambda name, env=None: "/opt/homebrew/bin/gh",
    )
    seen: list[str] = []

    class _Proc:
        stdout = "ok"
        stderr = ""
        returncode = 0

    def runner(args, **kwargs):
        env = kwargs.get("env") or {}
        seen.append(env.get("GH_TOKEN") or "")
        return _Proc()

    host_github_report(repo, env={"GH_TOKEN": "ghp_from_env"}, runner=runner)
    assert "ghp_from_env" in seen
