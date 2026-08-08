"""PuppetmasterCliBackend invokes `puppetmaster cursor` with adapter model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_discord.contracts import ContextSnapshot, DispatchRequest, TaskStatus
from agent_discord.puppetmaster.backend import (
    PuppetmasterCliBackend,
    _parse_safe_cli_completion,
    _safe_dispatch_prompt,
)
from agent_discord.puppetmaster.models import DEFAULT_MODEL_PIN


def _request() -> DispatchRequest:
    return DispatchRequest(
        task_id="t1",
        run_id="r1",
        prompt="hello world",
        model="cursor/grok-4-5",
        context=ContextSnapshot(
            task_id="t1",
            memories=[{"content": "note", "chain_of_thought": "secret"}],
            bindings={},
        ),
        metadata={"channel_id": "99"},
    )


def test_safe_dispatch_prompt_omits_hidden_keys():
    text = _safe_dispatch_prompt(_request())
    assert "hello world" in text
    assert "task_id=t1" in text
    assert "channel_id=99" in text
    assert "chain_of_thought" not in text
    assert "secret" not in text


def test_safe_dispatch_prompt_includes_optional_research_context():
    request = _request()
    request = DispatchRequest(
        task_id=request.task_id,
        run_id=request.run_id,
        prompt=request.prompt,
        model=request.model,
        context=ContextSnapshot(
            task_id=request.context.task_id,
            memories=request.context.memories,
            bindings=request.context.bindings,
            provenance={
                "research": {
                    "claims": [
                        {
                            "status": "verified",
                            "scope": "billing",
                            "claim_text": "Invoices are retained for 90 days.",
                        }
                    ],
                    "negative_findings": [
                        {
                            "status": "negative",
                            "scope": "billing",
                            "claim_text": "No export endpoint exists.",
                        }
                    ],
                }
            },
        ),
        metadata=request.metadata,
    )

    text = _safe_dispatch_prompt(request)

    assert "Research context:" in text
    assert "Invoices are retained for 90 days." in text
    assert "No export endpoint exists." in text


def test_parse_safe_cli_completion_strips_reasoning(tmp_path: Path):
    summary = tmp_path / "summary.json"
    summary.write_text(
        '{"summary":"all good","chain_of_thought":"nope","status":"ok"}',
        encoding="utf-8",
    )
    stdout = f"job_id: abc123\nartifacts: 2\nsummary: {summary}\n"
    meta = _parse_safe_cli_completion(stdout, "")
    assert meta["job_id"] == "abc123"
    assert meta["artifacts"] == 2
    assert meta["summary"] == "all good"
    assert "chain_of_thought" not in meta


def test_dispatch_uses_cursor_subcommand(monkeypatch, tmp_path: Path):
    calls: list[dict[str, Any]] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), **{k: kwargs.get(k) for k in ("cwd", "input")}})

        class Proc:
            returncode = 0
            stdout = "job_id: j1\nartifacts: 1\nsummary: done via cursor\n"
            stderr = ""

        return Proc()

    monkeypatch.setattr(
        "agent_discord.puppetmaster.backend.shutil.which",
        lambda _: "/usr/bin/puppetmaster",
    )
    monkeypatch.setattr("agent_discord.puppetmaster.backend.subprocess.run", fake_run)

    backend = PuppetmasterCliBackend(
        cli="puppetmaster",
        pin=DEFAULT_MODEL_PIN,
        cwd=tmp_path,
    )
    result = backend.dispatch(_request())
    assert result.status == TaskStatus.COMPLETED
    assert calls
    cmd = calls[0]["cmd"]
    assert cmd[0] == "puppetmaster"
    assert cmd[1] == "cursor"
    assert "--implement" in cmd
    assert "--allow-dirty" in cmd
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "grok-4.5"
    assert "--cwd" in cmd
    assert str(tmp_path) in cmd
    assert "run" not in cmd[1:3]
    assert "--json" not in cmd
    assert result.usage is not None
    assert result.usage.model == "cursor/grok-4-5"
    assert result.usage.adapter_name == "grok-4.5"
    assert "chain_of_thought" not in str(result.events[-1].payload)


def test_dispatch_fails_closed_when_cli_missing(monkeypatch):
    monkeypatch.setattr(
        "agent_discord.puppetmaster.backend.shutil.which",
        lambda _: None,
    )
    backend = PuppetmasterCliBackend()
    result = backend.dispatch(_request())
    assert result.status == TaskStatus.FAILED
    assert "not found" in (result.error or "")
