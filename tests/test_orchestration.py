"""Task dispatch, event replay, receipt rendering, inbound dedupe."""

from __future__ import annotations

import json
from pathlib import Path

from agent_discord.contracts import (
    DiscordMessage,
    DispatchEvent,
    EventKind,
    ProgressSummary,
    RunReceipt,
    TaskIntake,
    TaskStatus,
)
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.orchestration.jobs import JobPool
from agent_discord.orchestration.listen import drain_inbound
from agent_discord.orchestration.orchestrator import (
    TOKEN_CARD_FLUSH_SECONDS,
    AgentOrchestrator,
    _settle_bubbles,
)
from agent_discord.orchestration.receipts import render_receipt
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend
from agent_discord.puppetmaster.models import DEFAULT_MODEL_PIN
from agent_discord.redaction import strip_forbidden_keys


def _orch(tmp_path: Path, *, fail: bool = False):
    store = SQLiteStore(tmp_path / "o.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    backend = FakePuppetmasterBackend()
    backend.fail_next = fail
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        discord=facade,
        post_progress_to_discord=True,
    )
    return orch, store, fake_discord, backend


def test_dispatch_persists_events_and_posts_receipt(tmp_path: Path):
    orch, store, fake_discord, backend = _orch(tmp_path)
    store.remember(
        workspace_id="ws",
        channel_id="ch",
        content="prior context about invoices",
        source="seed",
        provenance={"seed": True},
    )
    receipt = orch.run_task(
        TaskIntake(
            text="review invoices",
            channel_id="ch",
            workspace_id="ws",
            message_id="inbound-1",
        )
    )
    assert receipt.status == TaskStatus.COMPLETED
    assert backend.last_request is not None
    assert backend.last_request.model == "cursor/grok-4-5"
    assert backend.last_request.context.memories
    assert backend.last_request.metadata["compute_mode"] == "analyze"
    assert fake_discord.threads
    assert "inbound-1" in str(fake_discord.threads)
    assert any(getattr(m, "thread_id", None) for m in fake_discord.sent)

    events = store.list_events(receipt.run_id)
    kinds = [e["kind"] for e in events]
    assert "intake" in kinds
    assert "context_snapshot" in kinds
    assert "dispatch" in kinds
    assert "receipt" in kinds

    assert fake_discord.sent
    assert any(
        "### Done" in (item.get("content") or "")
        or "Done" in (m.content or "")
        for m in fake_discord.sent
        for row in ((m.metadata or {}).get("components") or [])
        for item in (row.get("components") or [row])
    )

    rendered = render_receipt(receipt)
    assert "cursor/grok-4-5" in rendered or "grok-4.5" in rendered
    assert "chain_of_thought" not in rendered
    jobs = store.list_recent_jobs("ch", limit=5)
    assert jobs
    assert jobs[0]["run_id"] == receipt.run_id
    store.close()


def test_inbound_message_dedupe_reuses_prior_receipt(tmp_path: Path):
    orch, store, _, backend = _orch(tmp_path)
    first = orch.run_task(
        TaskIntake(
            text="do work",
            channel_id="ch",
            workspace_id="ws",
            message_id="same-msg",
        )
    )
    assert first.status == TaskStatus.COMPLETED
    assert backend.dispatch_count == 1

    second = orch.run_task(
        TaskIntake(
            text="do work again",
            channel_id="ch",
            workspace_id="ws",
            message_id="same-msg",
        )
    )
    assert backend.dispatch_count == 1
    assert second.run_id == first.run_id
    assert second.task_id == first.task_id
    assert second.status == first.status
    store.close()


def test_failed_dispatch_receipt(tmp_path: Path):
    orch, store, _, _ = _orch(tmp_path, fail=True)
    receipt = orch.run_task(
        TaskIntake(text="boom", channel_id="ch", workspace_id="ws")
    )
    assert receipt.status == TaskStatus.FAILED
    assert receipt.error
    assert orch.status(receipt.run_id) == TaskStatus.FAILED
    store.close()


def test_cancel_interface(tmp_path: Path):
    orch, store, _, _ = _orch(tmp_path)
    receipt = orch.run_task(
        TaskIntake(text="ok", channel_id="ch", workspace_id="ws")
    )
    assert orch.cancel(receipt.run_id) is True
    assert orch.status(receipt.run_id) == TaskStatus.CANCELLED
    store.close()


def test_receipt_redacts_thinking_markers():
    text = render_receipt(
        RunReceipt(
            task_id="t",
            run_id="r",
            status=TaskStatus.COMPLETED,
            summary='done <thinking>secret</thinking> {"reasoning":"hidden"}',
            progress=(
                ProgressSummary(
                    stage="x",
                    message="progress",
                    details={"chain_of_thought": "nope", "ok": True},
                ),
            ),
        )
    )
    assert "<thinking>" not in text
    assert "secret" not in text
    assert "hidden" not in text
    assert "[redacted]" in text
    assert "chain_of_thought" not in text


def test_strip_forbidden_keys_recursive():
    cleaned = strip_forbidden_keys(
        {
            "a": 1,
            "cot": "x",
            "nested": {"hidden_cot": "y", "keep": [1, {"reasoning": "z", "v": 2}]},
        }
    )
    assert cleaned == {"a": 1, "nested": {"keep": [1, {"v": 2}]}}


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _CountingFacade:
    def __init__(self, inner: DiscordFacade) -> None:
        self._inner = inner
        self.edit_count = 0
        self.thread_sends = 0
        self.edit_blobs: list[str] = []

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def edit_message(self, *args, **kwargs):
        self.edit_count += 1
        self.edit_blobs.append(json.dumps({"args": args, "kwargs": kwargs}, default=str))
        return self._inner.edit_message(*args, **kwargs)

    def send_message(self, channel_id, content, *, thread_id=None, **kwargs):
        if thread_id:
            self.thread_sends += 1
        return self._inner.send_message(
            channel_id, content, thread_id=thread_id, **kwargs
        )


class _TokenStreamBackend:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.pin = DEFAULT_MODEL_PIN
        self.last_request = None

    def resolve_model(self, requested: str):
        return self.pin

    def stream(self, request):
        self.last_request = request
        accumulated = ""
        for index in range(8):
            accumulated += f"a{index}"
            yield DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage="thinking",
                    message=f"a{index}",
                    details={
                        "token": True,
                        "stream_phase": "thinking",
                        "token_text": accumulated,
                    },
                ),
            )
        self.clock.advance(TOKEN_CARD_FLUSH_SECONDS + 0.05)
        accumulated = ""
        for index in range(8):
            accumulated += f"b{index}"
            yield DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage="code",
                    message=f"b{index}",
                    details={
                        "token": True,
                        "stream_phase": "code",
                        "token_text": accumulated,
                    },
                ),
            )
        yield DispatchEvent(
            kind=EventKind.RECEIPT,
            summary=ProgressSummary(stage="done", message="completed", percent=100.0),
        )

    def dispatch(self, request):
        raise AssertionError("stream should be used")

    def cancel(self, run_id: str) -> bool:
        return False

    def status(self, run_id: str) -> TaskStatus:
        return TaskStatus.COMPLETED


def test_token_stream_flushes_card_on_interval_not_per_token(tmp_path: Path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(
        "agent_discord.orchestration.orchestrator._monotonic",
        clock,
    )
    store = SQLiteStore(tmp_path / "token.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = _CountingFacade(
        DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    )
    orch = AgentOrchestrator(
        store=store,
        backend=_TokenStreamBackend(clock),
        discord=facade,
        post_progress_to_discord=True,
    )
    receipt = orch.run_task(
        TaskIntake(text="stream tokens", channel_id="ch", workspace_id="ws")
    )
    assert receipt.status == TaskStatus.COMPLETED
    assert 1 <= facade.edit_count <= 4
    assert facade.edit_count < 16
    assert facade.thread_sends == 0
    store.close()


def test_percent_progress_still_edits_immediately(tmp_path: Path):
    orch, store, fake_discord, _backend = _orch(tmp_path)
    receipt = orch.run_task(
        TaskIntake(text="review invoices", channel_id="ch", workspace_id="ws")
    )
    assert receipt.status == TaskStatus.COMPLETED
    assert any(item.percent == 50.0 for item in receipt.progress)
    edited = [
        message
        for message in fake_discord.sent
        if "Work" in (message.content or "")
        or any(
            "Work" in str(child)
            for row in ((message.metadata or {}).get("components") or [])
            for child in (row.get("components") or [row])
        )
    ]
    assert edited or fake_discord.sent
    store.close()


def test_live_card_stays_one_message(tmp_path: Path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(
        "agent_discord.orchestration.orchestrator._monotonic",
        clock,
    )
    store = SQLiteStore(tmp_path / "one.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = _CountingFacade(
        DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    )
    orch = AgentOrchestrator(
        store=store,
        backend=_TokenStreamBackend(clock),
        discord=facade,
        post_progress_to_discord=True,
        host_repos=(),
    )
    orch.run_task(TaskIntake(text="stream tokens", channel_id="ch", workspace_id="ws"))
    assert len(fake_discord.sent) == 1
    assert facade.thread_sends == 0
    body = "\n".join(
        str(child.get("content") or "")
        for row in ((fake_discord.sent[0].metadata or {}).get("components") or [])
        for child in (row.get("components") or [row])
        if isinstance(child, dict)
    )
    assert "a0" in body or "b0" in body or "a0" in (fake_discord.sent[0].content or "")
    store.close()


def test_live_card_keeps_markdown_dialogue(tmp_path: Path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(
        "agent_discord.orchestration.orchestrator._monotonic",
        clock,
    )

    class _MarkdownBackend(_TokenStreamBackend):
        def stream(self, request):
            self.last_request = request
            clock.advance(TOKEN_CARD_FLUSH_SECONDS + 0.05)
            yield DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage="thinking",
                    message="# Open PRs",
                    details={
                        "token": True,
                        "stream_phase": "thinking",
                        "token_text": "# Open PRs\nNone in puppetmaster.",
                    },
                ),
            )
            yield DispatchEvent(
                kind=EventKind.RECEIPT,
                summary=ProgressSummary(stage="done", message="completed", percent=100.0),
            )

    store = SQLiteStore(tmp_path / "md.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = _CountingFacade(
        DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    )
    orch = AgentOrchestrator(
        store=store,
        backend=_MarkdownBackend(clock),
        discord=facade,
        post_progress_to_discord=True,
        host_repos=(),
    )
    orch.run_task(TaskIntake(text="list prs", channel_id="ch", workspace_id="ws"))
    assert len(fake_discord.sent) == 1
    assert any("Open PRs" in blob for blob in facade.edit_blobs)
    store.close()


def test_named_repo_sets_worker_cwd(tmp_path: Path):
    from agent_discord.host.repos import HostRepo

    repo = tmp_path / "Puppetmaster"
    repo.mkdir()
    (repo / ".git").mkdir()
    orch, store, _fake, backend = _orch(tmp_path)
    orch.host_repos = (HostRepo(name="puppetmaster", path=repo, aliases=("puppetmaster",)),)
    orch.compute_cwd = tmp_path / ".agent-discord"
    orch.run_task(
        TaskIntake(
            text="check my puppetmaster repo for open PRs",
            channel_id="ch",
            workspace_id="ws",
        )
    )
    assert backend.last_request is not None
    assert backend.last_request.metadata["cwd"] == str(repo)
    assert backend.last_request.metadata["repo"] == "puppetmaster"
    assert "gh pr list" in backend.last_request.metadata["host_reach"]
    store.close()


def test_host_github_report_paints_the_live_card(tmp_path: Path):
    orch, store, fake_discord, backend = _orch(tmp_path)
    orch.host_github = lambda cwd: "Open PRs:\n#9 dest\n\nOpen issues:\n(none)"
    repo = tmp_path / "marionette"
    repo.mkdir()
    (repo / ".git").mkdir()
    from agent_discord.host.repos import HostRepo

    orch.host_repos = (HostRepo(name="marionette", path=repo, aliases=("marionette",)),)
    receipt = orch.run_task(
        TaskIntake(
            text="Can you check if my Marionette repo has any open PRs or Issues?",
            channel_id="ch",
            workspace_id="ws",
        )
    )
    assert backend.last_request is not None
    assert "#9 dest" in str(backend.last_request.metadata.get("host_github"))
    assert receipt.status == TaskStatus.COMPLETED
    assert "Open PRs:" in receipt.summary
    assert "#9 dest" in receipt.summary
    assert "Completed:" not in receipt.summary
    store.close()


def test_receipt_keeps_report_not_host_reach_dump(tmp_path: Path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(
        "agent_discord.orchestration.orchestrator._monotonic",
        clock,
    )
    dump = (
        "Network: yes. Use gh, curl, and git.\n"
        "Named git checkouts:\n"
        "puppetmaster: /tmp/pm\n"
        "Host tools (CLI or HTTP — not MCP inside Discord):\n"
        "Think-tank (Discord is the durable store):\n"
        "Do not treat .agent-discord as the subject repository.\n"
    )
    report = "Open PRs: none. Issues: #12 docs drift."

    class _DumpBackend(_TokenStreamBackend):
        def stream(self, request):
            self.last_request = request
            clock.advance(TOKEN_CARD_FLUSH_SECONDS + 0.05)
            yield DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage="thinking",
                    message=report,
                    percent=18.0,
                    details={
                        "token": True,
                        "stream_phase": "thinking",
                        "token_text": report,
                    },
                ),
            )
            yield DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage="done",
                    message=dump,
                    percent=100.0,
                    details={
                        "token": True,
                        "stream_phase": "done",
                        "token_text": dump,
                    },
                ),
            )
            yield DispatchEvent(
                kind=EventKind.RECEIPT,
                summary=ProgressSummary(stage="done", message=dump, percent=100.0),
            )

    store = SQLiteStore(tmp_path / "dump.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = _CountingFacade(
        DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    )
    orch = AgentOrchestrator(
        store=store,
        backend=_DumpBackend(clock),
        discord=facade,
        post_progress_to_discord=True,
        host_repos=(),
    )
    receipt = orch.run_task(
        TaskIntake(text="check marionette prs", channel_id="ch", workspace_id="ws")
    )
    assert "Open PRs: none" in receipt.summary
    assert "Named git checkouts" not in receipt.summary
    assert "Think-tank" not in receipt.summary
    rows = store._connection().execute("SELECT content FROM memory_entries").fetchall()
    blob = " ".join(str(row["content"] or "") for row in rows)
    assert "Named git checkouts" not in blob
    store.close()

def test_new_job_opens_thread_and_posts_card_there(tmp_path: Path):
    orch, store, fake_discord, _backend = _orch(tmp_path)
    receipt = orch.run_task(
        TaskIntake(
            text="review invoices",
            channel_id="ch",
            workspace_id="ws",
            message_id="ask-1",
        )
    )
    assert receipt.status == TaskStatus.COMPLETED
    assert fake_discord.threads
    thread_id = next(iter(fake_discord.threads))
    assert fake_discord.threads[thread_id]["message_id"] == "ask-1"
    assert any(getattr(m, "thread_id", None) == thread_id for m in fake_discord.sent)
    store.close()


def test_in_thread_followup_does_not_start_nested_thread(tmp_path: Path):
    orch, store, fake_discord, _backend = _orch(tmp_path)
    orch.run_task(
        TaskIntake(
            text="steer this",
            channel_id="ch",
            workspace_id="ws",
            message_id="follow-1",
            thread_id="existing-thread",
        )
    )
    assert "existing-thread" not in fake_discord.threads
    assert all(
        not str(key).startswith("thread-follow") for key in fake_discord.threads
    )
    assert any(getattr(m, "thread_id", None) == "existing-thread" for m in fake_discord.sent)
    store.close()


def test_token_flushes_edit_same_card_settles_only_beats(tmp_path: Path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(
        "agent_discord.orchestration.orchestrator._monotonic",
        clock,
    )
    store = SQLiteStore(tmp_path / "flush.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = _CountingFacade(
        DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    )
    orch = AgentOrchestrator(
        store=store,
        backend=_TokenStreamBackend(clock),
        discord=facade,
        post_progress_to_discord=True,
        host_repos=(),
    )
    orch.run_task(
        TaskIntake(
            text="stream tokens",
            channel_id="ch",
            workspace_id="ws",
            message_id="ask-stream",
        )
    )
    cards = [m for m in fake_discord.sent if not (m.content or "").strip()]
    settles = [
        m
        for m in fake_discord.sent
        if (m.content or "").strip() and getattr(m, "thread_id", None)
    ]
    assert len(cards) == 1
    assert facade.edit_count >= 1
    assert len(settles) <= 2
    store.close()


def test_done_settles_final_answer_in_thread(tmp_path: Path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(
        "agent_discord.orchestration.orchestrator._monotonic",
        clock,
    )

    class _AnswerBackend(_TokenStreamBackend):
        def stream(self, request):
            self.last_request = request
            clock.advance(TOKEN_CARD_FLUSH_SECONDS + 0.05)
            yield DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage="thinking",
                    message="Open PRs: none. Issues: #12 docs drift.",
                    details={
                        "token": True,
                        "stream_phase": "thinking",
                        "token_text": "Open PRs: none. Issues: #12 docs drift.",
                    },
                ),
            )
            yield DispatchEvent(
                kind=EventKind.RECEIPT,
                summary=ProgressSummary(stage="done", message="completed", percent=100.0),
            )

    store = SQLiteStore(tmp_path / "settle.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = _CountingFacade(
        DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    )
    orch = AgentOrchestrator(
        store=store,
        backend=_AnswerBackend(clock),
        discord=facade,
        post_progress_to_discord=True,
        host_repos=(),
    )
    receipt = orch.run_task(
        TaskIntake(
            text="check marionette prs",
            channel_id="ch",
            workspace_id="ws",
            message_id="ask-done",
        )
    )
    assert "Open PRs: none" in receipt.summary
    settles = [
        m
        for m in fake_discord.sent
        if "Open PRs: none" in (m.content or "") and getattr(m, "thread_id", None)
    ]
    assert not settles
    store.close()


def test_public_card_text_strips_monologue():
    from agent_discord.puppetmaster.backend import public_card_text

    kept = public_card_text(
        "Let me write the Discord answer\n\nTwo open PRs in marionette."
    )
    assert "Two open PRs" in kept
    assert "Let me write" not in kept
    assert public_card_text("report: internal") == ""
    assert public_card_text("Here's the straight answer for Discord") == ""
    assert "Named git checkouts" not in public_card_text(
        "Named git checkouts:\npuppetmaster: /tmp/pm"
    )


def test_unauthed_github_ask_does_not_dispatch_worker(tmp_path: Path):
    orch, store, fake_discord, backend = _orch(tmp_path)
    orch.host_github = lambda cwd: (
        "GitHub CLI is installed but not signed in on this Mac.\n"
        "Sign in on the host: discord-os add github"
    )
    repo = tmp_path / "marionette"
    repo.mkdir()
    (repo / ".git").mkdir()
    from agent_discord.host.repos import HostRepo

    orch.host_repos = (HostRepo(name="marionette", path=repo, aliases=("marionette",)),)
    receipt = orch.run_task(
        TaskIntake(
            text="Can you check if my Marionette repo has any open PRs or Issues?",
            channel_id="ch",
            workspace_id="ws",
            message_id="ask-gh",
        )
    )
    assert backend.dispatch_count == 0
    assert "not signed in" in receipt.summary
    assert "essay" not in receipt.summary.lower()
    store.close()

def test_github_ask_done_card_prefers_host_report(tmp_path: Path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(
        "agent_discord.orchestration.orchestrator._monotonic",
        clock,
    )
    essay = (
        "Follow the first turn requirement - make a tool call first. "
        "Let me write a long essay about the repo history and guess at PRs."
    )

    class _EssayBackend(_TokenStreamBackend):
        def stream(self, request):
            self.last_request = request
            clock.advance(TOKEN_CARD_FLUSH_SECONDS + 0.05)
            yield DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage="thinking",
                    message=essay,
                    percent=40.0,
                    details={
                        "token": True,
                        "stream_phase": "thinking",
                        "token_text": essay,
                    },
                ),
            )
            yield DispatchEvent(
                kind=EventKind.RECEIPT,
                summary=ProgressSummary(stage="done", message=essay, percent=100.0),
            )

    store = SQLiteStore(tmp_path / "gh-host.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = _CountingFacade(
        DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    )
    orch = AgentOrchestrator(
        store=store,
        backend=_EssayBackend(clock),
        discord=facade,
        post_progress_to_discord=True,
        host_repos=(),
    )
    repo = tmp_path / "puppetmaster"
    repo.mkdir()
    (repo / ".git").mkdir()
    from agent_discord.host.repos import HostRepo

    orch.host_repos = (HostRepo(name="puppetmaster", path=repo, aliases=("puppetmaster",)),)
    orch.host_github = lambda cwd: "Open PRs: (none)\nOpen issues: (none)"
    receipt = orch.run_task(
        TaskIntake(
            text="check if my Puppetmaster repo has any open PRs or Issues",
            channel_id="ch",
            workspace_id="ws",
            message_id="ask-host-gh",
        )
    )
    assert "Open PRs: (none)" in receipt.summary
    assert "Open issues: (none)" in receipt.summary
    assert "first turn" not in receipt.summary
    assert "essay" not in receipt.summary.lower()
    store.close()


def test_settle_does_not_duplicate_done_body(tmp_path: Path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(
        "agent_discord.orchestration.orchestrator._monotonic",
        clock,
    )
    prior = "Looking at the repo history now."
    done_body = "Open PRs: none. Issues: #12 docs drift."

    class _TwoBeatBackend(_TokenStreamBackend):
        def stream(self, request):
            self.last_request = request
            clock.advance(TOKEN_CARD_FLUSH_SECONDS + 0.05)
            yield DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage="thinking",
                    message=prior,
                    details={
                        "token": True,
                        "stream_phase": "thinking",
                        "token_text": prior,
                    },
                ),
            )
            clock.advance(TOKEN_CARD_FLUSH_SECONDS + 0.05)
            yield DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage="done",
                    message=done_body,
                    percent=100.0,
                    details={
                        "token": True,
                        "stream_phase": "done",
                        "token_text": done_body,
                    },
                ),
            )
            yield DispatchEvent(
                kind=EventKind.RECEIPT,
                summary=ProgressSummary(stage="done", message=done_body, percent=100.0),
            )

    store = SQLiteStore(tmp_path / "no-dup.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = _CountingFacade(
        DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    )
    orch = AgentOrchestrator(
        store=store,
        backend=_TwoBeatBackend(clock),
        discord=facade,
        post_progress_to_discord=True,
        host_repos=(),
    )
    receipt = orch.run_task(
        TaskIntake(
            text="review invoices",
            channel_id="ch",
            workspace_id="ws",
            message_id="ask-no-dup",
        )
    )
    assert done_body in receipt.summary
    settles = [
        m
        for m in fake_discord.sent
        if (m.content or "").strip() and getattr(m, "thread_id", None)
    ]
    assert any(prior in (m.content or "") for m in settles)
    assert all(done_body not in (m.content or "") for m in settles)
    store.close()


_LONG_PUBLIC_ANSWER = (
    "The March invoice pack is closed and every line now matches the bank deposit "
    "we posted on Friday afternoon. Two vendor credits still need a second reviewer "
    "before we can archive the folder. Payroll is already reconciled through last "
    "Friday including the off-cycle bonus run. Legal flagged one late vendor credit "
    "that should land next week and asked us to keep the thread open. Nothing else "
    "is blocking close, so the extras can follow the Done card."
)


def _thread_answer_blobs(fake_discord, *needles: str) -> list[str]:
    blobs: list[str] = []
    for message in fake_discord.sent:
        if not getattr(message, "thread_id", None):
            continue
        blob = " ".join(
            [
                message.content or "",
                json.dumps(message.metadata or {}, default=str),
            ]
        )
        if any(needle in blob for needle in needles):
            blobs.append(blob)
    return blobs


def test_settle_bubbles_keeps_short_answers_one_message():
    short = "Open PRs: none. Issues: #12 docs drift."
    assert _settle_bubbles(short) == [short]
    assert _settle_bubbles("Done.") == ["Done."]


def test_settle_bubbles_splits_long_public_answer():
    bubbles = _settle_bubbles(_LONG_PUBLIC_ANSWER)
    assert 2 <= len(bubbles) <= 3
    joined = " ".join(bubbles)
    assert "March invoice pack" in joined
    assert "Nothing else is blocking close" in joined
    assert all(item.endswith((".", "!", "?")) for item in bubbles)


def test_long_public_answer_settles_as_two_or_three_thread_messages(
    tmp_path: Path, monkeypatch
):
    clock = _Clock()
    monkeypatch.setattr(
        "agent_discord.orchestration.orchestrator._monotonic",
        clock,
    )

    class _LongAnswerBackend(_TokenStreamBackend):
        def stream(self, request):
            self.last_request = request
            clock.advance(TOKEN_CARD_FLUSH_SECONDS + 0.05)
            yield DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage="thinking",
                    message=_LONG_PUBLIC_ANSWER,
                    details={
                        "token": True,
                        "stream_phase": "thinking",
                        "token_text": _LONG_PUBLIC_ANSWER,
                    },
                ),
            )
            yield DispatchEvent(
                kind=EventKind.RECEIPT,
                summary=ProgressSummary(
                    stage="done",
                    message=_LONG_PUBLIC_ANSWER,
                    percent=100.0,
                ),
            )

    store = SQLiteStore(tmp_path / "short-settle.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = _CountingFacade(
        DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    )
    orch = AgentOrchestrator(
        store=store,
        backend=_LongAnswerBackend(clock),
        discord=facade,
        post_progress_to_discord=True,
        host_repos=(),
    )
    receipt = orch.run_task(
        TaskIntake(
            text="review invoices",
            channel_id="ch",
            workspace_id="ws",
            message_id="ask-long-settle",
        )
    )
    assert "March invoice pack" in receipt.summary
    blobs = _thread_answer_blobs(
        fake_discord,
        "March invoice pack",
        "vendor credits",
        "Payroll is already reconciled",
        "Legal flagged",
        "Nothing else is blocking close",
    )
    assert 2 <= len(blobs) <= 3
    parents = _parent_headlines(fake_discord)
    assert len(parents) == 1
    assert "\n" not in parents[0].content.strip()
    store.close()


def test_short_public_answer_stays_one_thread_message(tmp_path: Path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(
        "agent_discord.orchestration.orchestrator._monotonic",
        clock,
    )
    short = "Open PRs: none. Issues: #12 docs drift."

    class _ShortAnswerBackend(_TokenStreamBackend):
        def stream(self, request):
            self.last_request = request
            clock.advance(TOKEN_CARD_FLUSH_SECONDS + 0.05)
            yield DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage="thinking",
                    message=short,
                    details={
                        "token": True,
                        "stream_phase": "thinking",
                        "token_text": short,
                    },
                ),
            )
            yield DispatchEvent(
                kind=EventKind.RECEIPT,
                summary=ProgressSummary(stage="done", message=short, percent=100.0),
            )

    store = SQLiteStore(tmp_path / "short-one.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = _CountingFacade(
        DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    )
    orch = AgentOrchestrator(
        store=store,
        backend=_ShortAnswerBackend(clock),
        discord=facade,
        post_progress_to_discord=True,
        host_repos=(),
    )
    receipt = orch.run_task(
        TaskIntake(
            text="check marionette prs",
            channel_id="ch",
            workspace_id="ws",
            message_id="ask-short-settle",
        )
    )
    assert short in receipt.summary
    extras = [
        m
        for m in fake_discord.sent
        if (m.content or "").strip() and getattr(m, "thread_id", None)
    ]
    assert extras == []
    blobs = _thread_answer_blobs(fake_discord, "Open PRs: none")
    assert len(blobs) == 1
    store.close()


def test_settle_followup_failure_keeps_first_and_does_not_fail_job(
    tmp_path: Path, monkeypatch
):
    clock = _Clock()
    monkeypatch.setattr(
        "agent_discord.orchestration.orchestrator._monotonic",
        clock,
    )

    class _BoomFollowup(_CountingFacade):
        def send_message(self, channel_id, content, *, thread_id=None, **kwargs):
            if thread_id and (content or "").strip() and "Legal flagged" in (content or ""):
                raise RuntimeError("discord follow-up failed")
            return super().send_message(
                channel_id, content, thread_id=thread_id, **kwargs
            )

    class _LongAnswerBackend(_TokenStreamBackend):
        def stream(self, request):
            self.last_request = request
            clock.advance(TOKEN_CARD_FLUSH_SECONDS + 0.05)
            yield DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage="thinking",
                    message=_LONG_PUBLIC_ANSWER,
                    details={
                        "token": True,
                        "stream_phase": "thinking",
                        "token_text": _LONG_PUBLIC_ANSWER,
                    },
                ),
            )
            yield DispatchEvent(
                kind=EventKind.RECEIPT,
                summary=ProgressSummary(
                    stage="done",
                    message=_LONG_PUBLIC_ANSWER,
                    percent=100.0,
                ),
            )

    store = SQLiteStore(tmp_path / "settle-boom.sqlite3")
    store.initialize()
    fake_discord = FakeDiscordMCPProvider()
    facade = _BoomFollowup(
        DiscordFacade(fake_discord, bot_token_fingerprint="fp", owner_id="test")
    )
    orch = AgentOrchestrator(
        store=store,
        backend=_LongAnswerBackend(clock),
        discord=facade,
        post_progress_to_discord=True,
        host_repos=(),
    )
    receipt = orch.run_task(
        TaskIntake(
            text="review invoices",
            channel_id="ch",
            workspace_id="ws",
            message_id="ask-settle-boom",
        )
    )
    assert receipt.status == TaskStatus.COMPLETED
    assert "March invoice pack" in receipt.summary
    store.close()


def test_run_task_records_start_and_done_reactions(tmp_path: Path):
    orch, store, fake_discord, _ = _orch(tmp_path)
    receipt = orch.run_task(
        TaskIntake(
            text="review invoices",
            channel_id="ch",
            workspace_id="ws",
            message_id="inbound-1",
        )
    )
    assert receipt.status == TaskStatus.COMPLETED
    pairs = [(r["message_id"], r["emoji"]) for r in fake_discord.reactions]
    assert ("inbound-1", "\U0001F440") in pairs
    assert ("inbound-1", "\u2705") in pairs
    assert all(r["channel_id"] == "ch" for r in fake_discord.reactions)
    store.close()


def _parent_headlines(fake_discord):
    return [
        m
        for m in fake_discord.sent
        if m.channel_id == "ch"
        and not getattr(m, "thread_id", None)
        and (m.content or "").strip()
    ]


def test_channel_tldr_posts_short_parent_headline(tmp_path: Path):
    orch, store, fake_discord, _ = _orch(tmp_path)
    receipt = orch.run_task(
        TaskIntake(
            text="review invoices",
            channel_id="ch",
            workspace_id="ws",
            message_id="ask-tldr",
        )
    )
    assert receipt.status == TaskStatus.COMPLETED
    assert fake_discord.threads
    parents = _parent_headlines(fake_discord)
    assert len(parents) == 1
    line = parents[0].content.strip()
    assert "\n" not in line
    assert len(line) <= 200
    rendered = render_receipt(receipt)
    assert line != rendered.strip()
    assert "**Receipt**" not in line
    assert "Progress:" not in line
    assert not (parents[0].metadata or {}).get("components")
    store.close()


def test_channel_tldr_one_per_job_when_settle_retried(tmp_path: Path):
    orch, store, fake_discord, _ = _orch(tmp_path)
    intake = TaskIntake(
        text="review invoices",
        channel_id="ch",
        workspace_id="ws",
        message_id="ask-tldr-dup",
    )
    receipt = orch.run_task(intake)
    thread_id = next(iter(fake_discord.threads))
    orch._post_channel_tldr(
        intake,
        run_id=receipt.run_id,
        task_id=receipt.task_id,
        status=receipt.status,
        summary=receipt.summary,
        thread_id=thread_id,
    )
    assert len(_parent_headlines(fake_discord)) == 1
    store.close()


def test_channel_tldr_skips_when_job_never_started_a_thread(tmp_path: Path):
    orch, store, fake_discord, _ = _orch(tmp_path)
    receipt = orch.run_task(
        TaskIntake(text="review invoices", channel_id="ch", workspace_id="ws")
    )
    assert receipt.status == TaskStatus.COMPLETED
    assert not fake_discord.threads
    assert _parent_headlines(fake_discord) == []
    store.close()


def test_channel_tldr_discord_error_does_not_fail_job(tmp_path: Path):
    orch, store, fake_discord, _ = _orch(tmp_path)
    inner = orch.discord.send_message

    def boom(channel_id, content, *, thread_id=None, **kwargs):
        if thread_id is None and (content or "").strip():
            raise RuntimeError("discord down")
        return inner(channel_id, content, thread_id=thread_id, **kwargs)

    orch.discord.send_message = boom
    receipt = orch.run_task(
        TaskIntake(
            text="review invoices",
            channel_id="ch",
            workspace_id="ws",
            message_id="ask-tldr-err",
        )
    )
    assert receipt.status == TaskStatus.COMPLETED
    store.close()


class _FakeSteerOrch:
    """Listen-path double: count steer vs new jobs without a live worker."""

    def __init__(self, store):
        self.store = store
        self.workspace = None
        self.steer_calls: list[tuple[str, str]] = []
        self.jobs: list[TaskIntake] = []
        self._live: dict[str, str] = {}
        self.steer_ok = True

    def running_run_for_thread(self, thread_id: str):
        return self._live.get((thread_id or "").strip())

    def steer(self, run_id: str, text: str) -> bool:
        if not self.steer_ok:
            raise RuntimeError("steer failed")
        self.steer_calls.append((run_id, text))
        return True

    def run_task(self, intake: TaskIntake) -> RunReceipt:
        self.jobs.append(intake)
        run_id = f"run-{len(self.jobs)}"
        return RunReceipt(
            task_id=f"task-{len(self.jobs)}",
            run_id=run_id,
            status=TaskStatus.COMPLETED,
            summary="ok",
        )


def _armed_store(tmp_path: Path, channel_id: str = "ch"):
    store = SQLiteStore(tmp_path / "steer.sqlite3")
    store.initialize()
    store.set_host_control(channel_id, armed=True)
    store.seed_owner_if_empty("human-1")
    return store


def test_running_thread_message_steers_without_sibling_job(tmp_path: Path):
    store = _armed_store(tmp_path)
    orch = _FakeSteerOrch(store)
    orch._live["job-thread"] = "run-1"
    orch.jobs.append(
        TaskIntake(text="first ask", channel_id="ch", workspace_id="ws", thread_id="job-thread")
    )
    fake = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="nudge it left",
            message_id="201",
            author_id="human-1",
            thread_id="job-thread",
        )
    )
    pool = JobPool()
    drain_inbound(
        orch,
        facade,
        channel_id="ch",
        workspace_id="ws",
        since_ms=0,
        job_pool=pool,
    )
    assert len(orch.steer_calls) == 1
    assert orch.steer_calls[0] == ("run-1", "nudge it left")
    assert len(orch.jobs) == 1
    assert pool.live_count() == 0
    store.close()


def test_idle_thread_followup_starts_new_job(tmp_path: Path):
    store = _armed_store(tmp_path)
    orch = _FakeSteerOrch(store)
    fake = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="first ask",
            message_id="301",
            author_id="human-1",
            thread_id="idle-thread",
        )
    )
    pool = JobPool()
    drain_inbound(
        orch,
        facade,
        channel_id="ch",
        workspace_id="ws",
        since_ms=0,
        job_pool=pool,
    )
    pool.wait(timeout=2.0)
    assert len(orch.jobs) == 1
    assert orch.steer_calls == []

    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="follow up later",
            message_id="302",
            author_id="human-1",
            thread_id="idle-thread",
        )
    )
    drain_inbound(
        orch,
        facade,
        channel_id="ch",
        workspace_id="ws",
        since_ms=0,
        job_pool=pool,
    )
    pool.wait(timeout=2.0)
    assert len(orch.jobs) == 2
    assert orch.jobs[1].text == "follow up later"
    assert orch.jobs[1].thread_id == "idle-thread"
    assert orch.steer_calls == []
    store.close()


def test_steer_failure_does_not_spawn_sibling(tmp_path: Path):
    store = _armed_store(tmp_path)
    orch = _FakeSteerOrch(store)
    orch._live["job-thread"] = "run-1"
    orch.steer_ok = False
    fake = FakeDiscordMCPProvider()
    facade = DiscordFacade(fake, bot_token_fingerprint="fp", owner_id="test")
    fake.inbox.append(
        DiscordMessage(
            channel_id="ch",
            content="please steer",
            message_id="401",
            author_id="human-1",
            thread_id="job-thread",
        )
    )
    pool = JobPool()
    drain_inbound(
        orch,
        facade,
        channel_id="ch",
        workspace_id="ws",
        since_ms=0,
        job_pool=pool,
    )
    assert orch.jobs == []
    assert pool.live_count() == 0
    assert any("Could not steer" in (m.content or "") for m in fake.sent)
    store.close()
