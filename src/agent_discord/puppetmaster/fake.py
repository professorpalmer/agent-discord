"""Deterministic fake Puppetmaster backend for tests (no Cursor credits)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from agent_discord.contracts import (
    ArtifactRef,
    DispatchEvent,
    DispatchRequest,
    DispatchResult,
    EventKind,
    ModelNotAllowedError,
    ModelPin,
    ProgressSummary,
    TaskStatus,
    UsageReceipt,
)
from agent_discord.puppetmaster.models import DEFAULT_MODEL_PIN


@dataclass
class FakePuppetmasterBackend:
    pin: ModelPin = field(default_factory=lambda: DEFAULT_MODEL_PIN)
    runs: dict[str, TaskStatus] = field(default_factory=dict)
    cancelled: set[str] = field(default_factory=set)
    fail_next: bool = False
    last_request: Optional[DispatchRequest] = None
    dispatch_count: int = 0
    artifact_files: list[str] = field(default_factory=list)

    def resolve_model(self, requested: str) -> ModelPin:
        self.pin.assert_allowed(requested)
        if requested != self.pin.canonical:
            # Explicit failure — never remap silently
            raise ModelNotAllowedError(
                f"requested {requested!r} != pinned {self.pin.canonical!r}; "
                "no silent fallback"
            )
        return self.pin

    def dispatch(self, request: DispatchRequest) -> DispatchResult:
        self.last_request = request
        self.dispatch_count += 1
        pin = self.resolve_model(request.model)
        self.runs[request.run_id] = TaskStatus.RUNNING
        if request.run_id in self.cancelled:
            self.runs[request.run_id] = TaskStatus.CANCELLED
            return DispatchResult(
                run_id=request.run_id,
                status=TaskStatus.CANCELLED,
                events=(
                    DispatchEvent(
                        kind=EventKind.CANCEL_REQUESTED,
                        summary=ProgressSummary(
                            stage="cancelled", message="run cancelled before work"
                        ),
                    ),
                ),
                final_summary="cancelled",
                error="cancelled",
            )
        if self.fail_next:
            self.fail_next = False
            self.runs[request.run_id] = TaskStatus.FAILED
            return DispatchResult(
                run_id=request.run_id,
                status=TaskStatus.FAILED,
                events=(
                    DispatchEvent(
                        kind=EventKind.ERROR,
                        summary=ProgressSummary(stage="error", message="forced failure"),
                    ),
                ),
                final_summary="failed",
                error="forced failure",
            )

        events = (
            DispatchEvent(
                kind=EventKind.DISPATCH,
                summary=ProgressSummary(
                    stage="dispatch",
                    message=f"dispatched with {pin.adapter_name}",
                    details={"model": pin.canonical},
                ),
                payload={"model": pin.canonical, "adapter": pin.adapter_name},
            ),
            DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage="work",
                    message="working",
                    percent=50.0,
                ),
            ),
            DispatchEvent(
                kind=EventKind.RECEIPT,
                summary=ProgressSummary(
                    stage="done",
                    message="completed",
                    percent=100.0,
                ),
            ),
        )
        self.runs[request.run_id] = TaskStatus.COMPLETED
        return DispatchResult(
            run_id=request.run_id,
            status=TaskStatus.COMPLETED,
            events=events,
            final_summary=f"Completed: {request.prompt[:200]}",
            artifacts=tuple(self._artifact_refs()),
            usage=UsageReceipt(
                model=pin.canonical,
                adapter_name=pin.adapter_name,
                input_tokens=10,
                output_tokens=20,
                metadata={"backend": "fake"},
            ),
        )

    def _artifact_refs(self) -> list[ArtifactRef]:
        out: list[ArtifactRef] = []
        for index, raw in enumerate(self.artifact_files):
            path = Path(raw)
            out.append(
                ArtifactRef(
                    artifact_id=f"fake-artifact-{index}",
                    kind="file",
                    path=str(path),
                    filename=path.name,
                )
            )
        return out

    def cancel(self, run_id: str) -> bool:
        self.cancelled.add(run_id)
        self.runs[run_id] = TaskStatus.CANCELLED
        return True

    def status(self, run_id: str) -> TaskStatus:
        return self.runs.get(run_id, TaskStatus.PENDING)
