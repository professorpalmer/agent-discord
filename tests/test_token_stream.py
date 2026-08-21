"""Token-line parse and flush interval."""

from __future__ import annotations

from agent_discord.contracts import EventKind
from agent_discord.orchestration.orchestrator import TOKEN_CARD_FLUSH_SECONDS
from agent_discord.puppetmaster.backend import (
    TokenStreamBuffer,
    _parse_progress_line,
    _parse_token_line,
)


def test_parse_token_and_reasoning_lines_redact_cot():
    event = _parse_token_line(
        '{"type":"token","content":"def foo():"}',
        "cursor/grok-4-5",
    )
    assert event is not None
    assert event.kind == EventKind.PROGRESS
    assert event.summary.details["token"] is True
    assert event.summary.details["stream_phase"] == "thinking"
    assert "def foo():" in event.summary.details["token_text"]

    plan = _parse_token_line(
        '{"type":"reasoning","plan":"edit backend.py","chain_of_thought":"SECRET"}',
        "cursor/grok-4-5",
    )
    assert plan is not None
    assert plan.summary.stage == "plan"
    assert "SECRET" not in plan.summary.message
    assert "chain_of_thought" not in plan.summary.details

    delta = _parse_token_line('{"type":"delta","text":" + 1"}', "cursor/grok-4-5")
    assert delta is not None
    assert delta.summary.details["token"] is True

    durable = _parse_token_line(
        '{"ts": 1, "kind": "text", "text": "live tokens"}',
        "openrouter/auto",
    )
    assert durable is not None
    assert "live tokens" in str(durable.summary.details["token_text"])

    progress = _parse_progress_line("progress: 40% stage: work running tools", "cursor/grok-4-5")
    assert progress is not None
    assert progress.summary.percent == 40.0


def test_token_buffer_bounds_and_phase():
    buf = TokenStreamBuffer()
    buf.extend("x" * 5000)
    assert len(buf.text) <= 1500
    assert buf.set_phase("CODE") == "code"
    assert buf.set_phase("nope") == "code"


def test_flush_interval_is_between_200_and_500ms():
    assert 0.2 <= TOKEN_CARD_FLUSH_SECONDS <= 0.5
