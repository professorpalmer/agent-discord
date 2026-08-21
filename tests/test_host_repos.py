"""Named host checkouts for worker cwd + gh reach."""

from __future__ import annotations

from pathlib import Path

from agent_discord.host.repos import (
    HostRepo,
    host_reach_block,
    load_host_repos,
    resolve_host_repo,
)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / ".git").mkdir()
    return path


def test_load_host_repos_from_env_and_projects(tmp_path: Path):
    pm = _git_repo(tmp_path / "projects" / "Puppetmaster")
    dugout = _git_repo(tmp_path / "dugout")
    repos = load_host_repos(
        env={"DISCORD_OS_REPOS": f"dugout:{dugout}"},
        projects_dir=tmp_path / "projects",
    )
    names = {repo.name: repo.path for repo in repos}
    assert names["puppetmaster"] == pm.resolve()
    assert names["dugout"] == dugout.resolve()


def test_resolve_host_repo_matches_prompt_not_state_dir(tmp_path: Path):
    pm = _git_repo(tmp_path / "Puppetmaster")
    state = tmp_path / ".agent-discord"
    state.mkdir()
    repos = (HostRepo(name="puppetmaster", path=pm, aliases=("puppetmaster",)),)
    hit = resolve_host_repo("check my puppetmaster repo for open issues", repos)
    assert hit is not None
    assert hit.path == pm
    assert resolve_host_repo("what time is it", repos, default_cwd=state) is None


def test_host_reach_block_lists_gh_and_checkouts(tmp_path: Path):
    pm = _git_repo(tmp_path / "Puppetmaster")
    text = host_reach_block(
        (HostRepo(name="puppetmaster", path=pm),),
        cwd=pm,
        gh_bin="/usr/bin/gh",
    )
    assert "gh pr list" in text
    assert str(pm) in text
    assert ".agent-discord" in text
