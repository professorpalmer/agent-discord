"""Orchestration flow with DI-friendly seams for tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional
from uuid import uuid4

from agent_discord.contracts import (
    ArtifactRef,
    ContextSnapshot,
    DispatchRequest,
    EventKind,
    ProgressSummary,
    RunReceipt,
    TaskIntake,
    TaskStatus,
    UsageReceipt,
)
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.object_store import DEFAULT_MAX_OBJECT_BYTES, DiscordObjectStore
from agent_discord.orchestration.cards import (
    edit_card,
    progress_card,
    receipt_card,
    send_card,
)
from agent_discord.orchestration.routing import compute_dispatch_mode
from agent_discord.persistence.research import ResearchMemoryStore
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.models import DEFAULT_MODEL_PIN
from agent_discord.redaction import redact_text_markers, strip_forbidden_keys


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
        self._run_status: dict[str, TaskStatus] = {}

    def run_task(self, intake: TaskIntake) -> RunReceipt:
        pin = self.backend.resolve_model(self.model)
        if intake.message_id:
            claimed = self.store.claim_inbound_message(
                intake.message_id, intake.channel_id
            )
            if not claimed:
                return self._duplicate_receipt(intake.message_id)

        task_id = uuid4().hex
        run_id = uuid4().hex

        self.store.upsert_binding(
            workspace_id=intake.workspace_id,
            channel_id=intake.channel_id,
            guild_id=intake.guild_id,
            metadata={"thread_id": intake.thread_id},
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
        request = DispatchRequest(
            task_id=task_id,
            run_id=run_id,
            prompt=intake.text,
            model=pin.canonical,
            context=snapshot,
            metadata={
                "channel_id": intake.channel_id,
                "compute_mode": compute_dispatch_mode(intake.text),
            },
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
        result = self.backend.dispatch(request)

        for event in result.events:
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
            if (
                self.post_progress_to_discord
                and self.discord is not None
                and event.kind == EventKind.PROGRESS
            ):
                card = progress_card(
                    stage=summary.stage,
                    message=summary.message,
                    percent=summary.percent,
                    run_id=run_id,
                )
                progress_message_id = self._post_or_edit_progress(
                    intake.channel_id,
                    card,
                    thread_id=job_thread_id,
                    message_id=progress_message_id,
                )

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

        safe_final_summary = redact_text_markers(result.final_summary)
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
            send_card(
                self.discord,
                intake.channel_id,
                card,
                thread_id=job_thread_id,
            )

        return receipt

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
                pass
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
