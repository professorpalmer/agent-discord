"""Orchestration flow with DI-friendly seams for tests."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from uuid import uuid4

from agent_discord.contracts import (
    ArtifactRef,
    ContextSnapshot,
    DispatchRequest,
    DispatchResult,
    EventKind,
    ProgressSummary,
    RunReceipt,
    TaskIntake,
    TaskStatus,
    UsageReceipt,
)
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.object_store import DEFAULT_MAX_OBJECT_BYTES, DiscordObjectStore
from agent_discord.host.memory import memory_reach_block, recall_think_tank, settle_think_tank
from agent_discord.host.realms import realm_for_channel
from agent_discord.host.repos import HostRepo, host_reach_block, load_host_repos, resolve_host_repo
from agent_discord.host.tools import load_host_tools, tools_reach_block
from agent_discord.orchestration.cards import (
    edit_card,
    progress_card,
    receipt_card,
    send_card,
    working_card,
)
from agent_discord.orchestration.routing import (
    MODE_IMPLEMENT,
    compute_dispatch_mode,
    swarm_worker_count,
)
from agent_discord.persistence.research import ResearchMemoryStore
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.models import DEFAULT_MODEL_PIN
from agent_discord.redaction import redact_text_markers, strip_forbidden_keys

TOKEN_CARD_FLUSH_SECONDS = 0.35
TOKEN_TEXT_LIMIT = 1500
_STREAM_PHASES = frozenset({"thinking", "plan", "code", "dispatch", "done"})
_SWARM_ROLES = (
    "explore",
    "pipeline-mapper",
    "decision-explainer",
    "conflict-auditor",
    "test-coverage-reviewer",
)
_RATE_LIMIT_MARKERS = ("429", "rate limit", "rate_limited", "ratelimited")


def _monotonic() -> float:
    return time.monotonic()


def _is_token_stream(details: Mapping[str, Any]) -> bool:
    return bool(details.get("token")) or "stream_phase" in details


def _visible_card_text(text: str) -> str:
    from agent_discord.puppetmaster.backend import usable_worker_text

    raw = (text or "").strip()
    if raw[:1] in "{[":
        return ""
    return usable_worker_text(raw)


def _strip_prompt_section(block: str, heading: str) -> str:
    skip = False
    kept: list[str] = []
    marker = (heading or "").strip().lower()
    for line in (block or "").splitlines():
        stripped = line.strip().lower()
        if stripped == marker:
            skip = True
            continue
        if skip and stripped.startswith("[") and stripped.endswith("]") and stripped != marker:
            skip = False
        if skip:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


class AgentOrchestrator:
    """intake → context snapshot → pinned dispatch → events → Discord → receipt."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        backend: Any,
        discord: Optional[DiscordFacade] = None,
        model: str = DEFAULT_MODEL_PIN.canonical,
        post_progress_to_discord: bool = True,
        research: Optional[ResearchMemoryStore] = None,
        max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES,
        workspace: Optional[Path] = None,
        compute_cwd: Optional[Path] = None,
        host_repos: Optional[tuple[HostRepo, ...]] = None,
        retry_backoff_s: float = 0.0,
        presence: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.discord = discord
        self.model = model
        self.post_progress_to_discord = post_progress_to_discord
        # Optional research seam — None keeps normal tasks free of research metadata.
        self.research = research
        self.max_object_bytes = max_object_bytes
        self.workspace = Path(workspace) if workspace is not None else None
        self.compute_cwd = Path(compute_cwd) if compute_cwd is not None else None
        self.host_repos = host_repos
        self.retry_backoff_s = float(retry_backoff_s)
        self.presence = presence
        self._run_status: dict[str, TaskStatus] = {}
        self._checkpoints: dict[str, dict[str, Any]] = {}

    def run_task(self, intake: TaskIntake) -> RunReceipt:
        pin = self.backend.resolve_model(self.model)
        if intake.message_id:
            claimed = self.store.claim_inbound_message(
                intake.message_id, intake.channel_id
            )
            if not claimed:
                return self._duplicate_receipt(intake.message_id)

        from agent_discord.orchestration.service import is_spend_halted

        if is_spend_halted(self.store, intake.workspace_id):
            return self._halted_receipt(intake)

        task_id = uuid4().hex
        run_id = uuid4().hex

        self.store.merge_binding_metadata(
            intake.workspace_id,
            intake.channel_id,
            {"thread_id": intake.thread_id},
            guild_id=intake.guild_id,
        )
        self.store.create_task(
            task_id=task_id,
            workspace_id=intake.workspace_id,
            channel_id=intake.channel_id,
            intake_text=intake.text,
            thread_id=intake.thread_id,
            requester_id=intake.requester_id,
            metadata=dict(intake.metadata),
        )
        self.store.create_run(
            run_id=run_id,
            task_id=task_id,
            model=pin.canonical,
            adapter_name=pin.adapter_name,
            status=TaskStatus.RUNNING,
        )
        self._run_status[run_id] = TaskStatus.RUNNING
        self._set_presence("dnd", intake.text)

        if intake.message_id:
            self.store.bind_inbound_message(
                intake.message_id,
                task_id=task_id,
                run_id=run_id,
                channel_id=intake.channel_id,
            )
            if self.discord is not None:
                try:
                    self.discord.observe_message_id(intake.message_id)
                except Exception:
                    # Process-local facade dedupe is best-effort; SQLite is authoritative.
                    pass

        self._event(
            task_id,
            run_id,
            EventKind.INTAKE,
            "task intake accepted",
            {"text": intake.text, "channel_id": intake.channel_id},
            source="orchestrator",
        )

        memories = list(
            self.store.recall(
                workspace_id=intake.workspace_id,
                channel_id=intake.channel_id,
                query=intake.text,
                limit=8,
            )
        )
        tank = ""
        if self.discord is not None:
            try:
                tank = recall_think_tank(
                    self.discord,
                    self.store,
                    intake.text,
                    workspace_id=intake.workspace_id,
                )
            except Exception:
                tank = ""
        if tank:
            memories.insert(
                0,
                {
                    "memory_id": "think-tank",
                    "content": tank[:2000],
                    "source": "think-tank",
                },
            )
        pref_block = ""
        reader = getattr(self.store, "prompt_memory_block", None)
        if callable(reader):
            try:
                pref_block = reader(intake.workspace_id) or ""
            except Exception:
                pref_block = ""
        if pref_block:
            kept = _strip_prompt_section(pref_block, "[failures]")
            if kept:
                memories.insert(
                    0,
                    {
                        "memory_id": "preferences",
                        "content": kept,
                        "source": "preferences",
                    },
                )
        binding = self.store.get_binding(intake.workspace_id, intake.channel_id) or {}
        research_context = self._optional_research_context(intake)
        provenance: dict[str, Any] = {
            "source": "sqlite",
            "memory_count": len(memories),
        }
        if research_context:
            provenance["research"] = research_context
        snapshot = ContextSnapshot(
            task_id=task_id,
            memories=memories,
            bindings={
                "workspace_id": intake.workspace_id,
                "channel_id": intake.channel_id,
                "guild_id": intake.guild_id,
                "binding": binding,
            },
            provenance=provenance,
        )
        self._event(
            task_id,
            run_id,
            EventKind.CONTEXT_SNAPSHOT,
            f"context snapshot ({len(memories)} memories)",
            {
                "memory_ids": [m.get("memory_id") for m in memories],
                "provenance": dict(snapshot.provenance),
            },
            source="orchestrator",
        )

        progress_items: list[ProgressSummary] = []
        progress_message_id: Optional[str] = None
        job_thread_id = intake.thread_id
        requested_workers = None
        thread_history = ""
        if intake.metadata:
            requested_workers = intake.metadata.get("workers")
            bits = intake.metadata.get("thread_history") or []
            if bits:
                thread_history = "\n".join(str(item)[:200] for item in list(bits)[:6])
        workers = swarm_worker_count(intake.text, requested_workers)
        prompt = intake.text.strip()
        if thread_history:
            prompt = f"{prompt}\n\nThread history:\n{thread_history}"
        compute_mode = compute_dispatch_mode(intake.text)
        extra_meta = dict(intake.metadata) if intake.metadata else {}
        extra_meta.update(
            {
                "channel_id": intake.channel_id,
                "compute_mode": compute_mode,
                "workers": workers,
            }
        )
        repos = self.host_repos if self.host_repos is not None else load_host_repos()
        channel_realm = realm_for_channel(
            self.store,
            intake.channel_id,
            workspace_id=intake.workspace_id,
            repos=repos,
        )
        chosen = resolve_host_repo(
            intake.text,
            repos,
            default_cwd=self.compute_cwd,
        )
        if chosen is None:
            chosen = channel_realm
        run_cwd = chosen.path if chosen is not None else self.compute_cwd
        if run_cwd is not None:
            extra_meta["cwd"] = str(run_cwd)
        if chosen is not None:
            extra_meta["repo"] = chosen.name
        extra_meta["host_reach"] = "\n\n".join(
            item
            for item in (
                host_reach_block(repos, cwd=run_cwd),
                tools_reach_block(load_host_tools()),
                memory_reach_block(self.store, workspace_id=intake.workspace_id),
            )
            if item
        )
        approved = bool(extra_meta.get("approved"))
        if compute_mode == MODE_IMPLEMENT and not approved:
            from agent_discord.orchestration.service import writes_need_approval

            if writes_need_approval(self.store):
                return self._park_for_approval(
                    intake,
                    task_id=task_id,
                    run_id=run_id,
                )
        request = DispatchRequest(
            task_id=task_id,
            run_id=run_id,
            prompt=prompt,
            model=pin.canonical,
            context=snapshot,
            metadata=extra_meta,
        )
        if self.post_progress_to_discord and self.discord is not None:
            start_card = progress_card(
                stage="start",
                message="Starting.",
                percent=1,
                run_id=run_id,
            )
            progress_message_id = self._post_or_edit_progress(
                intake.channel_id,
                start_card,
                thread_id=job_thread_id,
                message_id=None,
            )
            if progress_message_id and not job_thread_id:
                job_thread_id = self._start_job_thread(
                    intake.channel_id,
                    progress_message_id,
                    intake.text,
                )
        if workers:
            return self.dispatch_swarm(
                intake,
                request,
                task_id=task_id,
                run_id=run_id,
                workers=workers,
                job_thread_id=job_thread_id,
                progress_message_id=progress_message_id,
            )
        stream = getattr(self.backend, "stream", None)
        if callable(stream):
            events_iter = stream(request)
            result = None
        else:
            result = self.backend.dispatch(request)
            events_iter = iter(result.events)

        token_text = ""
        token_dirty = False
        last_flush_at = _monotonic()
        last_percent: Optional[float] = None
        stream_stage = "start"
        stream_error: Optional[str] = None

        def flush_token_card(*, force: bool = False) -> None:
            nonlocal progress_message_id, token_dirty, last_flush_at
            visible = _visible_card_text(token_text)
            if not token_dirty or not visible:
                return
            if not force and (_monotonic() - last_flush_at) < TOKEN_CARD_FLUSH_SECONDS:
                return
            if self.post_progress_to_discord and self.discord is not None:
                card = progress_card(
                    stage=stream_stage,
                    message=visible,
                    percent=last_percent,
                    run_id=run_id,
                )
                progress_message_id = self._post_or_edit_progress(
                    intake.channel_id,
                    card,
                    thread_id=job_thread_id,
                    message_id=progress_message_id,
                )
            token_dirty = False
            last_flush_at = _monotonic()

        for event in events_iter:
            safe_details = strip_forbidden_keys(dict(event.summary.details))
            if not isinstance(safe_details, dict):
                safe_details = {}
            safe_payload = strip_forbidden_keys(dict(event.payload))
            if not isinstance(safe_payload, dict):
                safe_payload = {}
            summary = ProgressSummary(
                stage=event.summary.stage,
                message=redact_text_markers(event.summary.message),
                percent=event.summary.percent,
                details=safe_details,
            )
            progress_items.append(summary)
            self._event(
                task_id,
                run_id,
                event.kind,
                summary.message,
                {
                    "stage": summary.stage,
                    "percent": summary.percent,
                    "details": dict(summary.details),
                    **safe_payload,
                },
                source="backend",
            )
            if event.kind == EventKind.ERROR:
                stream_error = summary.message
            if (
                self.post_progress_to_discord
                and self.discord is not None
                and event.kind in {EventKind.PROGRESS, EventKind.DISPATCH}
            ):
                if summary.percent is not None:
                    last_percent = summary.percent
                if _is_token_stream(summary.details):
                    incoming = str(summary.details.get("token_text") or "")
                    if incoming:
                        token_text = incoming[-TOKEN_TEXT_LIMIT:]
                    elif summary.message:
                        token_text = (token_text + summary.message)[-TOKEN_TEXT_LIMIT:]
                    token_dirty = True
                    phase = str(
                        summary.details.get("stream_phase") or summary.stage or stream_stage
                    )
                    if phase in _STREAM_PHASES:
                        stream_stage = phase
                    flush_token_card()
                    continue
                visible = _visible_card_text(summary.message)
                if not visible:
                    continue
                if summary.stage:
                    stream_stage = summary.stage
                card = progress_card(
                    stage=summary.stage,
                    message=visible,
                    percent=summary.percent,
                    run_id=run_id,
                )
                progress_message_id = self._post_or_edit_progress(
                    intake.channel_id,
                    card,
                    thread_id=job_thread_id,
                    message_id=progress_message_id,
                )
                last_flush_at = _monotonic()

        flush_token_card(force=True)

        if result is None:
            streamed_status = self.backend.status(run_id)
            if streamed_status in {
                TaskStatus.PENDING,
                TaskStatus.RUNNING,
                TaskStatus.PROGRESS,
            }:
                streamed_status = (
                    TaskStatus.FAILED if stream_error else TaskStatus.COMPLETED
                )
            result = DispatchResult(
                run_id=run_id,
                status=streamed_status,
                events=tuple(progress_items),
                final_summary=progress_items[-1].message if progress_items else "completed",
                error=stream_error,
            )
            self._run_status[run_id] = streamed_status

        if (
            result.status == TaskStatus.FAILED
            and self._is_rate_limit(result.error)
        ):
            self._sleep_retry()
            retry_request = DispatchRequest(
                task_id=request.task_id,
                run_id=request.run_id,
                prompt=request.prompt,
                model=request.model,
                context=request.context,
                metadata={**dict(request.metadata), "resume": "rate_limit"},
            )
            result = self.backend.dispatch(retry_request)
            self._run_status[run_id] = result.status

        if result.status == TaskStatus.FAILED:
            writer = getattr(self.store, "record_failure", None)
            if callable(writer):
                try:
                    writer(
                        intake.workspace_id,
                        run_id,
                        result.error or result.final_summary or "failed",
                    )
                except Exception:
                    pass
            self._rollback_on_red(run_id)

        receipt_artifacts: list[ArtifactRef] = []
        for art in result.artifacts:
            persisted = self._persist_artifact(art, intake=intake, task_id=task_id, run_id=run_id)
            receipt_artifacts.append(persisted)

        usage_map = None
        if result.usage is not None:
            usage_map = {
                "model": result.usage.model,
                "adapter_name": result.usage.adapter_name,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "metadata": strip_forbidden_keys(dict(result.usage.metadata)),
            }
            self._record_usage_spend(intake.workspace_id, run_id, result.usage)

        from agent_discord.puppetmaster.backend import (
            _is_placeholder_summary,
            usable_worker_text,
        )

        spoken = usable_worker_text(redact_text_markers(result.final_summary))
        if not spoken or _is_placeholder_summary(spoken):
            spoken = usable_worker_text(token_text) or spoken
        spoken = _visible_card_text(spoken) or spoken
        safe_final_summary = spoken or "Worker finished without a written answer."
        safe_error = redact_text_markers(result.error) if result.error else None
        self.store.update_run(
            run_id,
            status=result.status,
            summary=safe_final_summary,
            error=safe_error,
            usage=usage_map,
        )
        self._run_status[run_id] = result.status

        self.store.remember(
            workspace_id=intake.workspace_id,
            channel_id=intake.channel_id,
            content=f"task:{intake.text[:240]} → {safe_final_summary[:240]}",
            source="orchestrator",
            provenance={"task_id": task_id, "run_id": run_id, "status": result.status.value},
        )
        if self.discord is not None and result.status == TaskStatus.COMPLETED:
            try:
                settle_think_tank(
                    self.discord,
                    self.store,
                    workspace_id=intake.workspace_id,
                    origin_channel=intake.channel_id,
                    summary=safe_final_summary[:400],
                )
            except Exception:
                pass

        receipt = RunReceipt(
            task_id=task_id,
            run_id=run_id,
            status=result.status,
            summary=safe_final_summary,
            progress=tuple(progress_items),
            artifacts=tuple(receipt_artifacts),
            usage=result.usage,
            error=safe_error,
        )
        card = receipt_card(receipt)
        rendered = card.text
        self._event(
            task_id,
            run_id,
            EventKind.RECEIPT,
            "final receipt",
            {"rendered": rendered, "status": result.status.value},
            source="orchestrator",
        )

        if self.post_progress_to_discord and self.discord is not None:
            if progress_message_id:
                try:
                    edit_card(
                        self.discord,
                        intake.channel_id,
                        progress_message_id,
                        card,
                    )
                except Exception:
                    send_card(self.discord, intake.channel_id, card)
            else:
                send_card(self.discord, intake.channel_id, card)
        self._set_presence("idle", "Discord OS")

        return receipt

    def dispatch_swarm(
        self,
        intake: TaskIntake,
        request: DispatchRequest,
        *,
        task_id: str,
        run_id: str,
        workers: int,
        job_thread_id: Optional[str],
        progress_message_id: Optional[str],
    ) -> RunReceipt:
        """Fan out one analyze worker per role, then optional implement handoff."""

        roles = list(_SWARM_ROLES[: max(2, min(int(workers), 5))])
        summaries: list[str] = []
        progress_items: list[ProgressSummary] = []
        for index, role in enumerate(roles):
            child_id = f"{run_id}-{role}"
            child = DispatchRequest(
                task_id=task_id,
                run_id=child_id,
                prompt=f"[{role}] {request.prompt}",
                model=request.model,
                context=request.context,
                metadata={**dict(request.metadata), "role": role, "parent_run_id": run_id},
            )
            result = self.backend.dispatch(child)
            bit = _visible_card_text(result.final_summary or role) or role
            summaries.append(f"{role}: {bit}")
            progress_items.append(
                ProgressSummary(
                    stage=role,
                    message=bit,
                    percent=round((index + 1) * 100.0 / (len(roles) + 1), 1),
                )
            )
            if self.post_progress_to_discord and self.discord is not None:
                progress_message_id = self._post_or_edit_progress(
                    intake.channel_id,
                    progress_card(
                        stage=role,
                        message=bit,
                        percent=progress_items[-1].percent,
                        run_id=run_id,
                    ),
                    thread_id=job_thread_id,
                    message_id=progress_message_id,
                )

        stitched = "\n".join(summaries)
        final_status = TaskStatus.COMPLETED
        handoff_error: Optional[str] = None
        if compute_dispatch_mode(intake.text) == MODE_IMPLEMENT or "implement" in intake.text.lower():
            handoff = DispatchRequest(
                task_id=task_id,
                run_id=f"{run_id}-implement",
                prompt=f"Implement from swarm findings:\n{stitched}\n\nTask:\n{intake.text}",
                model=request.model,
                context=request.context,
                metadata={**dict(request.metadata), "compute_mode": MODE_IMPLEMENT, "handoff": True},
            )
            handoff_result = self.backend.dispatch(handoff)
            stitched = f"{stitched}\nimplement: {handoff_result.final_summary}"
            final_status = handoff_result.status
            handoff_error = handoff_result.error

        self.store.update_run(
            run_id,
            status=final_status,
            summary=redact_text_markers(stitched),
            error=handoff_error,
        )
        self._run_status[run_id] = final_status
        receipt = RunReceipt(
            task_id=task_id,
            run_id=run_id,
            status=final_status,
            summary=redact_text_markers(stitched),
            progress=tuple(progress_items),
            error=handoff_error,
        )
        if self.post_progress_to_discord and self.discord is not None:
            card = receipt_card(receipt)
            if progress_message_id:
                try:
                    edit_card(self.discord, intake.channel_id, progress_message_id, card)
                except Exception:
                    send_card(self.discord, intake.channel_id, card)
            else:
                send_card(self.discord, intake.channel_id, card)
        self._event(
            task_id,
            run_id,
            EventKind.RECEIPT,
            "swarm receipt",
            {"workers": len(roles), "roles": roles},
            source="orchestrator",
        )
        self._set_presence("idle", "Discord OS")
        return receipt

    def apply_job_action(self, action: str, run_id: str) -> dict[str, Any]:
        """Approve / cancel / retry a stored run. Best-effort, no raises."""

        verb = (action or "").strip().lower()
        run = self.store.get_run(run_id) or {}
        if verb == "cancel":
            try:
                self.backend.cancel(run_id)
            except Exception:
                pass
            try:
                self.store.update_run(run_id, status=TaskStatus.CANCELLED, summary="cancelled")
            except Exception:
                pass
            return {"action": verb, "run_id": run_id, "status": "cancelled"}
        if verb == "retry":
            task_id = str(run.get("task_id") or "")
            task = self.store.get_task(task_id) if task_id else None
            text = ""
            if task:
                text = str(task.get("intake_text") or "")
            if text:
                return {
                    "action": verb,
                    "run_id": run_id,
                    "status": "queued",
                    "intake_text": text,
                }
            return {"action": verb, "run_id": run_id, "status": "missing"}
        if verb == "approve":
            return self._approve_parked_run(run_id)
        return {"action": verb, "run_id": run_id, "status": "ignored"}

    def _approve_parked_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id) or {}
        task_id = str(run.get("task_id") or "")
        reader = getattr(self.store, "task_metadata", None)
        meta = reader(task_id) if callable(reader) and task_id else {}
        if not isinstance(meta, dict) or not meta.get("awaiting_approval"):
            return {"action": "approve", "run_id": run_id, "status": "approved"}
        task = self.store.get_task(task_id) or {}
        intake_meta = dict(meta.get("intake_meta") or {})
        intake_meta["approved"] = True
        intake = TaskIntake(
            text=str(task.get("intake_text") or meta.get("text") or ""),
            channel_id=str(task.get("channel_id") or meta.get("channel_id") or ""),
            workspace_id=str(task.get("workspace_id") or meta.get("workspace_id") or "default"),
            guild_id=meta.get("guild_id"),
            thread_id=task.get("thread_id") or meta.get("thread_id"),
            requester_id=task.get("requester_id") or meta.get("requester_id"),
            metadata=intake_meta,
        )
        merger = getattr(self.store, "merge_task_metadata", None)
        if callable(merger):
            merger(task_id, {"awaiting_approval": False})
        try:
            self.store.update_run(
                run_id,
                status=TaskStatus.COMPLETED,
                summary="approved; write started",
            )
        except Exception:
            pass
        if not intake.text.strip() or not intake.channel_id:
            return {"action": "approve", "run_id": run_id, "status": "missing"}
        receipt = self.run_task(intake)
        return {
            "action": "approve",
            "run_id": receipt.run_id,
            "parked_run_id": run_id,
            "status": receipt.status.value,
            "receipt": receipt,
        }

    def _park_for_approval(
        self,
        intake: TaskIntake,
        *,
        task_id: str,
        run_id: str,
    ) -> RunReceipt:
        merger = getattr(self.store, "merge_task_metadata", None)
        if callable(merger):
            merger(
                task_id,
                {
                    "awaiting_approval": True,
                    "text": intake.text,
                    "channel_id": intake.channel_id,
                    "workspace_id": intake.workspace_id,
                    "guild_id": intake.guild_id,
                    "thread_id": intake.thread_id,
                    "requester_id": intake.requester_id,
                    "intake_meta": dict(intake.metadata or {}),
                },
            )
        summary = "Waiting for Approve to write."
        try:
            self.store.update_run(run_id, status=TaskStatus.PENDING, summary=summary)
        except Exception:
            pass
        self._run_status[run_id] = TaskStatus.PENDING
        receipt = RunReceipt(
            task_id=task_id,
            run_id=run_id,
            status=TaskStatus.PENDING,
            summary=summary,
        )
        if self.post_progress_to_discord and self.discord is not None:
            try:
                send_card(
                    self.discord,
                    intake.channel_id,
                    working_card(
                        task_label="Approve write",
                        message=summary,
                        run_id=run_id,
                        actions="parked",
                    ),
                    thread_id=intake.thread_id,
                )
            except Exception:
                pass
        self._set_presence("idle", "Discord OS")
        return receipt

    def _halted_receipt(self, intake: TaskIntake) -> RunReceipt:
        return RunReceipt(
            task_id="",
            run_id="",
            status=TaskStatus.FAILED,
            summary="spend halted",
            error="spend halted",
        )

    def _record_usage_spend(
        self,
        workspace_id: str,
        run_id: str,
        usage: UsageReceipt,
    ) -> None:
        from agent_discord.orchestration.service import (
            is_spend_halted,
            set_spend_halted,
            spend_usd_from_usage,
        )

        usd = spend_usd_from_usage(usage)
        writer = getattr(self.store, "record_spend", None)
        if callable(writer) and usd > 0:
            try:
                writer(workspace_id, run_id, usd)
            except Exception:
                return
        if is_spend_halted(self.store, workspace_id):
            set_spend_halted(self.store, True)

    def _set_presence(self, status: str, name: str) -> None:
        sender = self.presence
        if not callable(sender):
            return
        label = " ".join((name or "").split())[:80] or "Discord OS"
        if status == "dnd" and not label.startswith("Working"):
            label = f"Working on {label}"
        try:
            sender(status, label)
        except Exception:
            pass

    def _is_rate_limit(self, error: Optional[str]) -> bool:
        raw = (error or "").lower()
        return any(marker in raw for marker in _RATE_LIMIT_MARKERS)

    def _sleep_retry(self) -> None:
        delay = max(0.0, float(self.retry_backoff_s))
        if delay:
            time.sleep(delay)

    def _rollback_on_red(self, run_id: str) -> None:
        """Best-effort reverse of the uncommitted workspace diff after a failed run."""

        if self.workspace is None:
            return
        root = Path(self.workspace)
        if not (root / ".git").exists():
            return
        try:
            import subprocess

            snapped = subprocess.run(
                ["git", "diff", "--binary"],
                cwd=str(root),
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception:
            return
        diff = snapped.stdout or ""
        if not diff.strip():
            stored = self._checkpoints.get(run_id) or {}
            diff = str(stored.get("diff") or "")
        if not diff.strip():
            return
        path = root / ".agent-discord" / "checkpoints" / f"{run_id}.patch"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(diff, encoding="utf-8")
            self._checkpoints[run_id] = {"diff": diff}
            subprocess.run(
                ["git", "apply", "-R", "--whitespace=nowarn", str(path)],
                cwd=str(root),
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception:
            return

    def _duplicate_receipt(self, message_id: str) -> RunReceipt:
        """Return prior receipt or an explicit ignored-duplicate result (no re-dispatch)."""
        prior = self.store.get_inbound_message(message_id) or {}
        run_id = prior.get("run_id") or ""
        task_id = prior.get("task_id") or ""
        if run_id:
            run = self.store.get_run(str(run_id))
            if run:
                usage = None
                if run.get("usage_json"):
                    import json

                    try:
                        raw = json.loads(run["usage_json"])
                    except json.JSONDecodeError:
                        raw = None
                    if isinstance(raw, dict):
                        usage = UsageReceipt(
                            model=str(raw.get("model") or run.get("model") or ""),
                            adapter_name=str(
                                raw.get("adapter_name") or run.get("adapter_name") or ""
                            ),
                            input_tokens=raw.get("input_tokens"),
                            output_tokens=raw.get("output_tokens"),
                            metadata=strip_forbidden_keys(dict(raw.get("metadata") or {})),
                        )
                status = TaskStatus(run["status"])
                self._run_status[str(run_id)] = status
                return RunReceipt(
                    task_id=str(run["task_id"]),
                    run_id=str(run_id),
                    status=status,
                    summary=str(
                        run.get("summary")
                        or f"reused prior receipt for duplicate message_id={message_id}"
                    ),
                    usage=usage,
                    error=run.get("error"),
                )
        return RunReceipt(
            task_id=str(task_id or "duplicate"),
            run_id=str(run_id or "duplicate"),
            status=TaskStatus.COMPLETED,
            summary=f"ignored duplicate inbound message_id={message_id}",
            error=None,
        )

    def _persist_artifact(
        self,
        art: ArtifactRef,
        *,
        intake: TaskIntake,
        task_id: str,
        run_id: str,
    ) -> ArtifactRef:
        provenance = (
            strip_forbidden_keys(dict(art.provenance))
            if isinstance(art.provenance, Mapping)
            else {}
        )
        if not isinstance(provenance, dict):
            provenance = {}
        persisted = art
        if art.message_id and art.attachment_id:
            persisted = art
        elif self.discord is not None and art.path and Path(art.path).is_file():
            try:
                data = Path(art.path).read_bytes()
                store = DiscordObjectStore(
                    self.discord,
                    max_bytes=self.max_object_bytes,
                    workspace=self.workspace,
                )
                ref = store.put_or_overflow(
                    data,
                    channel_id=intake.channel_id,
                    filename=art.filename or Path(art.path).name,
                    kind=art.kind,
                    thread_id=intake.thread_id,
                    guild_id=intake.guild_id,
                    author_id=intake.requester_id,
                )
                if intake.guild_id:
                    provenance = {**provenance, "guild_id": intake.guild_id}
                if intake.thread_id:
                    provenance = {**provenance, "thread_id": intake.thread_id}
                persisted = ArtifactRef(
                    artifact_id=art.artifact_id,
                    kind=ref.kind,
                    path=art.path,
                    provenance=provenance,
                    channel_id=ref.channel_id,
                    message_id=ref.message_id,
                    attachment_id=ref.attachment_id,
                    sha256=ref.sha256,
                    size=ref.size,
                    filename=ref.filename,
                )
            except Exception as exc:
                provenance = {**provenance, "object_store_error": str(exc)}
                persisted = ArtifactRef(
                    artifact_id=art.artifact_id,
                    kind=art.kind,
                    path=art.path,
                    provenance=provenance,
                    filename=art.filename,
                    size=art.size,
                    sha256=art.sha256,
                )
        self.store.add_artifact(
            artifact_id=persisted.artifact_id,
            task_id=task_id,
            run_id=run_id,
            kind=persisted.kind,
            path=persisted.path or "",
            provenance=persisted.provenance
            if isinstance(persisted.provenance, Mapping)
            else provenance,
            channel_id=persisted.channel_id,
            message_id=persisted.message_id,
            attachment_id=persisted.attachment_id,
            filename=persisted.filename,
            sha256=persisted.sha256,
            size=persisted.size,
        )
        return persisted

    def _optional_research_context(self, intake: TaskIntake) -> Optional[dict[str, Any]]:
        """Attach research claims/negatives only when a research store is configured."""
        if self.research is None:
            return None
        claims = self.research.list_claims(workspace_id=intake.workspace_id, limit=8)
        negatives = self.research.list_negative_findings(
            workspace_id=intake.workspace_id, limit=8
        )
        if not claims and not negatives:
            return None
        return {
            "claim_count": len(claims),
            "negative_count": len(negatives),
            "claims": [
                {
                    "fingerprint": c.fingerprint,
                    "status": c.status.value,
                    "scope": c.scope,
                    "claim_text": c.claim_text[:400],
                }
                for c in claims
            ],
            "negative_findings": [
                {
                    "fingerprint": n.fingerprint,
                    "scope": n.scope,
                    "claim_text": n.claim_text[:400],
                }
                for n in negatives
            ],
        }

    def _start_job_thread(
        self,
        channel_id: str,
        message_id: str,
        text: str,
    ) -> Optional[str]:
        if self.discord is None:
            return None
        starter = getattr(self.discord, "start_thread_from_message", None)
        if not callable(starter):
            return None
        try:
            title = (text or "job").replace("\n", " ").strip() or "job"
            thread_id = starter(channel_id, message_id, title[:100])
        except Exception:
            return None
        return str(thread_id or "") or None

    def _post_or_edit_progress(
        self,
        channel_id: str,
        card: Any,
        *,
        thread_id: Optional[str],
        message_id: Optional[str],
    ) -> Optional[str]:
        if self.discord is None:
            return message_id
        if message_id:
            try:
                edited = edit_card(self.discord, channel_id, message_id, card)
                return edited.message_id or message_id
            except Exception:
                return message_id
        posted = send_card(self.discord, channel_id, card, thread_id=thread_id)
        if isinstance(posted, list) and posted:
            return posted[-1].message_id or message_id
        if posted is not None:
            return getattr(posted, "message_id", None) or message_id
        return message_id

    def cancel(self, run_id: str) -> bool:
        ok = bool(self.backend.cancel(run_id))
        if ok:
            self._run_status[run_id] = TaskStatus.CANCELLED
            run = self.store.get_run(run_id)
            if run:
                self.store.update_run(run_id, status=TaskStatus.CANCELLED, error="cancelled")
                self._event(
                    run["task_id"],
                    run_id,
                    EventKind.CANCEL_REQUESTED,
                    "cancel requested",
                    {},
                    source="orchestrator",
                )
        return ok

    def status(self, run_id: str) -> TaskStatus:
        if run_id in self._run_status:
            return self._run_status[run_id]
        backend_status = self.backend.status(run_id)
        run = self.store.get_run(run_id)
        if run:
            return TaskStatus(run["status"])
        return backend_status

    def _event(
        self,
        task_id: str,
        run_id: str,
        kind: EventKind,
        summary: str,
        payload: dict[str, Any],
        *,
        source: str,
    ) -> None:
        self.store.append_event(
            task_id=task_id,
            run_id=run_id,
            kind=kind,
            summary=redact_text_markers(summary),
            payload=strip_forbidden_keys(payload),
            source=source,
            provenance={"component": "AgentOrchestrator"},
        )
