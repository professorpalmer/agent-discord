"""PuppetmasterCliBackend invokes `puppetmaster cursor` with adapter model."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from agent_discord.contracts import ContextSnapshot, DispatchRequest, EventKind, TaskStatus
from agent_discord.puppetmaster.agentic import AgenticPuppetmasterBackend
from agent_discord.puppetmaster.backend import (
    PuppetmasterCliBackend,
    TokenStreamBuffer,
    _event_from_cli_line,
    _parse_progress_line,
    _parse_safe_cli_completion,
    _parse_token_line,
    _safe_dispatch_prompt,
    usable_worker_text,
)
from agent_discord.puppetmaster.models import AGENTIC_MODEL_PIN, DEFAULT_MODEL_PIN


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


def test_parse_skips_stitched_summary_heading(tmp_path: Path):
    summary = tmp_path / "summary.md"
    summary.write_text(
        "# Puppetmaster Stitched Summary\n\nGoal: In one sentence: what is Discord OS?\n\nDiscord OS is the harness UI.\n",
        encoding="utf-8",
    )
    meta = _parse_safe_cli_completion(f"job_id: j2\nsummary: {summary}\n", "")
    assert meta["summary"] == "Discord OS is the harness UI."


def test_parse_skips_task_id_echo_and_keeps_answer(tmp_path: Path):
    summary = tmp_path / "summary.md"
    summary.write_text(
        "# Puppetmaster Stitched Summary\n\n"
        "Goal: check my puppetmaster repo\n\n"
        "task_id=5ff50a3793004ee38b9628602d7969da\n"
        "run_id=ef9c5401cf034c428aaf02e720261ece\n\n"
        "Open PRs: none. Open issues: #12 docs drift.\n",
        encoding="utf-8",
    )
    meta = _parse_safe_cli_completion(f"job_id: j3\nsummary: {summary}\n", "")
    assert "task_id=" not in meta["summary"]
    assert "Open PRs: none." in meta["summary"]
    assert usable_worker_text("task_id=abc") == ""


def test_github_ask_prompt_uses_host_gh_output():
    text = _safe_dispatch_prompt(
        DispatchRequest(
            task_id="t1",
            run_id="r1",
            prompt="check open PRs and issues",
            model="openrouter/auto",
            context=ContextSnapshot(task_id="t1", memories=[], bindings={}),
            metadata={"host_github": "Open PRs:\n#12 docs drift"},
        )
    )
    assert "Host already queried GitHub" in text
    assert "#12 docs drift" in text
    assert "Named git checkouts" not in text


def test_prose_tokens_join_as_a_paragraph():
    buffer = TokenStreamBuffer()
    first = _event_from_cli_line("The", "openrouter/auto", buffer)
    second = _event_from_cli_line("checkout", "openrouter/auto", buffer)
    third = _event_from_cli_line("has no open PRs.", "openrouter/auto", buffer)
    assert first is not None and second is not None and third is not None
    assert "The checkout has no open PRs." in str(third.summary.details["token_text"])
    assert "The\ncheckout" not in str(third.summary.details["token_text"])


def test_choose_spoken_answer_skips_host_reach_echo():
    from agent_discord.puppetmaster.backend import choose_spoken_answer, is_prompt_echo

    dump = (
        "Network: yes. Use gh, curl, and git.\n"
        "Named git checkouts:\n"
        "Host tools (CLI or HTTP — not MCP inside Discord):\n"
        "Think-tank (Discord is the durable store):\n"
        "Do not treat .agent-discord as the subject repository.\n"
    )
    assert is_prompt_echo(dump)
    assert choose_spoken_answer(dump, "Open PRs: none.") == "Open PRs: none."


def test_prose_cli_line_becomes_token_stream():
    buffer = TokenStreamBuffer()
    event = _event_from_cli_line(
        "Open PRs: none. Two issues need labels.",
        "openrouter/auto",
        buffer,
    )
    assert event is not None
    assert event.kind == EventKind.PROGRESS
    assert event.summary.details["token"] is True
    assert "Open PRs: none." in str(event.summary.details["token_text"])
    assert _event_from_cli_line("task_id=abc", "openrouter/auto", buffer) is None
    assert (
        _event_from_cli_line(
            '{"ts": 1, "worker_id": "worker-1", "kind": "reasoning", "text": "\\n"}',
            "openrouter/auto",
            buffer,
        )
        is None
    )
    assert (
        _event_from_cli_line(
            "puppetmaster: mode=edit — workers may modify files in the working tree.",
            "openrouter/auto",
            buffer,
        )
        is None
    )


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


def test_parse_token_line_accepts_token_reasoning_and_delta():
    buffer = TokenStreamBuffer()
    token = _parse_token_line(
        '{"type":"token","content":"Hello"}',
        "cursor/grok-4-5",
        buffer=buffer,
    )
    assert token is not None
    assert token.kind == EventKind.PROGRESS
    assert token.summary.stage in {"thinking", "plan", "code", "dispatch", "done"}
    assert token.summary.details["token"] is True
    assert token.summary.details["stream_phase"] == token.summary.stage
    assert "Hello" in str(token.summary.details["token_text"])

    reasoning = _parse_token_line(
        '{"type":"reasoning","summary":"outline the approach"}',
        "cursor/grok-4-5",
        buffer=buffer,
    )
    assert reasoning is not None
    assert reasoning.summary.stage == "thinking"
    assert reasoning.summary.details["stream_phase"] == "thinking"
    assert "outline the approach" in reasoning.summary.message
    assert "chain_of_thought" not in reasoning.summary.details

    delta = _parse_token_line(
        '{"type":"delta","text":" world"}',
        "cursor/grok-4-5",
        buffer=buffer,
    )
    assert delta is not None
    assert delta.summary.details["token"] is True
    assert "Hello" in str(delta.summary.details["token_text"])
    assert "world" in str(delta.summary.details["token_text"])
    assert len(str(delta.summary.details["token_text"])) <= 1500


def test_parse_token_line_rejects_raw_thinking():
    leaked = _parse_token_line(
        '{"type":"token","thinking":"secret chain","hidden_cot":"nope"}',
        "cursor/grok-4-5",
    )
    assert leaked is None

    mixed = _parse_token_line(
        '{"type":"token","content":"visible","chain_of_thought":"hidden","thinking":"raw"}',
        "cursor/grok-4-5",
    )
    assert mixed is not None
    dumped = str(mixed.summary.details) + mixed.summary.message
    assert "visible" in dumped
    assert "hidden" not in dumped
    assert "raw" not in dumped
    assert "chain_of_thought" not in dumped
    assert "secret" not in dumped


def test_parse_progress_line_still_reads_percent_and_stage():
    event = _parse_progress_line("progress: 42% stage: code", "cursor/grok-4-5")
    assert event is not None
    assert event.kind == EventKind.PROGRESS
    assert event.summary.percent == 42.0
    assert event.summary.stage == "code"

    json_event = _parse_progress_line(
        '{"percent": 18, "stage": "plan", "message": "drafting"}',
        "cursor/grok-4-5",
    )
    assert json_event is not None
    assert json_event.summary.percent == 18.0
    assert json_event.summary.stage == "plan"
    assert json_event.summary.message == "drafting"
    assert _parse_progress_line('{"type":"token","content":"x"}', "m") is None


class _FakePopen:
    def __init__(self, cmd, **kwargs):
        self.args = list(cmd)
        if "deltas" in cmd:
            self.stdout = io.StringIO(
                '{"ts": 1, "kind": "text", "text": "Open PRs: none."}\n'
            )
        else:
            self.stdout = io.StringIO(
                '{"type":"reasoning","summary":"think first"}\n'
                '{"type":"token","content":"Hi"}\n'
                '{"type":"delta","text":" there"}\n'
                '{"percent": 40, "stage": "code", "message": "writing"}\n'
                "job_id: j-stream\nsummary: streamed ok\n"
            )
        self.stderr = io.StringIO("")
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_cli_stream_yields_token_progress_from_popen(monkeypatch, tmp_path: Path):
    captured: dict[str, Any] = {}

    def fake_popen(cmd, **kwargs):
        proc = _FakePopen(cmd, **kwargs)
        captured.setdefault("cmds", []).append(list(proc.args))
        return proc

    monkeypatch.setenv("PUPPETMASTER_STATE_DIR", str(tmp_path / "pm-state"))
    monkeypatch.setattr(
        "agent_discord.puppetmaster.backend.shutil.which",
        lambda _: "/usr/bin/puppetmaster",
    )
    monkeypatch.setattr(
        "agent_discord.puppetmaster.backend.subprocess.Popen",
        fake_popen,
    )
    monkeypatch.setattr(
        "agent_discord.puppetmaster.backend.cli_supports_flag",
        lambda *args, **kwargs: False,
    )
    backend = PuppetmasterCliBackend(
        cli="puppetmaster",
        pin=DEFAULT_MODEL_PIN,
        cwd=tmp_path,
    )
    events = list(backend.stream(_request()))
    worker = next(cmd for cmd in captured["cmds"] if "cursor" in cmd)
    assert "--json-lines" not in worker
    assert "--emit-job-id-early" in worker
    assert "--state-dir" in worker
    follower = next(cmd for cmd in captured["cmds"] if "deltas" in cmd)
    assert "--state-dir" in follower
    assert str(tmp_path / "pm-state") in follower
    assert any("deltas" in cmd for cmd in captured["cmds"])
    token_events = [event for event in events if event.summary.details.get("token")]
    assert token_events
    assert all(event.kind == EventKind.PROGRESS for event in token_events)
    assert any("Hi" in str(event.summary.details.get("token_text")) for event in token_events)
    assert any(event.summary.stage == "code" and event.summary.percent == 40 for event in events)
    assert events[-1].kind == EventKind.RECEIPT
    dumped = "".join(str(event.summary.details) + event.summary.message for event in events)
    assert "secret" not in dumped
    assert "chain_of_thought" not in dumped


def test_agentic_stream_passes_json_lines_and_parses_tokens(monkeypatch, tmp_path: Path):
    captured: dict[str, Any] = {}

    def fake_popen(cmd, **kwargs):
        proc = _FakePopen(cmd, **kwargs)
        captured.setdefault("cmds", []).append(list(proc.args))
        return proc

    monkeypatch.setattr(
        "agent_discord.puppetmaster.agentic.shutil.which",
        lambda _: "/usr/bin/puppetmaster",
    )
    monkeypatch.setattr(
        "agent_discord.puppetmaster.agentic.subprocess.Popen",
        fake_popen,
    )
    monkeypatch.setattr(
        "agent_discord.puppetmaster.agentic.cli_supports_flag",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "agent_discord.puppetmaster.backend.subprocess.Popen",
        fake_popen,
    )
    backend = AgenticPuppetmasterBackend(
        cli="puppetmaster",
        pin=AGENTIC_MODEL_PIN,
        cwd=tmp_path,
        env={},
    )
    request = DispatchRequest(
        task_id="t1",
        run_id="r1",
        prompt="hello world",
        model="openrouter/auto",
        context=ContextSnapshot(task_id="t1", memories=[], bindings={}),
    )
    events = list(backend.stream(request))
    worker = next(cmd for cmd in captured["cmds"] if "agentic" in cmd)
    assert "--json-lines" not in worker
    assert "--emit-job-id-early" in worker
    assert worker[worker.index("--worker-mode") + 1] == "inline"
    assert any(event.summary.details.get("token") for event in events)


def test_public_card_text_strips_inline_cary_blob():
    from agent_discord.puppetmaster.backend import public_card_text

    blob = (
        "ollow the first turn requirement - make a tool call first. "
        "Let me quickly verify context by examining the repo. "
        "Actually the requirement says to answer from the host output. "
        "But I must call submit_findings at the end, and first response must include a tool call. "
        "Here's the answer for Discord: all clear. Open PRs: none. "
        "(These are the live GitHub numbers as queried on your Mac just"
    )
    kept = public_card_text(blob)
    assert "all clear" in kept
    assert "Open PRs: none" in kept
    assert "on your Mac just" not in kept
    lower = kept.lower()
    assert "tool call" not in lower
    assert "first turn" not in lower
    assert "submit_findings" not in lower
    assert "here's the answer for discord" not in lower
    assert "requirement says" not in lower


def test_public_card_text_dedups_repeated_paragraph():
    from agent_discord.puppetmaster.backend import public_card_text

    blob = (
        "Here's the answer for Discord: all clear. Open PRs: none.\n\n"
        "Here's the answer for Discord: all clear. Open PRs: none."
    )
    kept = public_card_text(blob)
    assert kept.count("all clear") == 1
    assert kept.count("Open PRs: none") == 1
    assert "Here's the answer for Discord" not in kept


def test_usable_worker_text_clips_at_sentence_boundary():
    from agent_discord.puppetmaster.backend import RECEIPT_TEXT_LIMIT, usable_worker_text

    text = " ".join(f"Sentence number {i} is complete." for i in range(80))
    text = text + " These are the live GitHub numbers as queried on your Mac just leftover"
    assert len(text) > RECEIPT_TEXT_LIMIT
    cleaned = usable_worker_text(text)
    assert cleaned.endswith(".")
    assert "on your Mac just" not in cleaned
    assert "Sentence number 0 is complete." in cleaned
