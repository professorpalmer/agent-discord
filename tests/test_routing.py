"""Analyze vs implement intake routing."""

from __future__ import annotations

from agent_discord.orchestration.routing import compute_dispatch_mode, swarm_worker_count


def test_questions_are_analyze():
    assert compute_dispatch_mode("what is Discord OS?") == "analyze"
    assert compute_dispatch_mode("explain the host card") == "analyze"
    assert compute_dispatch_mode("") == "analyze"


def test_file_work_is_implement():
    assert compute_dispatch_mode("fix the login timeout") == "implement"
    assert compute_dispatch_mode("edit src/cli.py to print version") == "implement"
    assert compute_dispatch_mode("add tests for the panel") == "implement"


def test_swarm_markers_and_worker_count():
    assert compute_dispatch_mode("swarm this repo") == "swarm"
    assert compute_dispatch_mode("audit this module") == "swarm"
    assert swarm_worker_count("review invoices") == 0
    assert swarm_worker_count("swarm the tests") == 3
    assert swarm_worker_count("fan out 4 workers") == 4
    assert swarm_worker_count("swarm", requested=5) == 5
    assert swarm_worker_count("hello", requested=1) == 0
