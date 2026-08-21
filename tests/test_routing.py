"""Analyze vs implement intake routing."""

from __future__ import annotations

from agent_discord.orchestration.routing import compute_dispatch_mode


def test_questions_are_analyze():
    assert compute_dispatch_mode("what is Discord OS?") == "analyze"
    assert compute_dispatch_mode("explain the host card") == "analyze"
    assert compute_dispatch_mode("") == "analyze"


def test_file_work_is_implement():
    assert compute_dispatch_mode("fix the login timeout") == "implement"
    assert compute_dispatch_mode("edit src/cli.py to print version") == "implement"
    assert compute_dispatch_mode("add tests for the panel") == "implement"
