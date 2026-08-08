"""Explicit typed contracts shared across adapters and orchestration.

Tests inject fakes against these protocols — no Discord, Cursor, or network required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventKind(str, Enum):
    INTAKE = "intake"
    CONTEXT_SNAPSHOT = "context_snapshot"
    DISPATCH = "dispatch"
    PROGRESS = "progress"
    ARTIFACT = "artifact"
    RECEIPT = "receipt"
    ERROR = "error"
    CANCEL_REQUESTED = "cancel_requested"
    STATUS = "status"


@dataclass(frozen=True)
class ModelPin:
    """Pinned Cursor model — allowlist is exact-match only; no silent fallback."""

    canonical: str = "cursor/grok-4-5"
    adapter_name: str = "grok-4.5"
    allowlist: tuple[str, ...] = ("cursor/grok-4-5",)

    def assert_allowed(self, model: str) -> None:
        if model not in self.allowlist:
            raise ModelNotAllowedError(
                f"model {model!r} is not in allowlist {list(self.allowlist)}; "
                "no silent fallback"
            )


class ModelNotAllowedError(ValueError):
    """Raised when a requested model is outside the pinned allowlist."""


@dataclass(frozen=True)
class DiscordMessage:
    channel_id: str
    content: str
    message_id: str = ""
    thread_id: Optional[str] = None
    author_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolDescriptor:
    name: str
    description: str = ""
    input_schema: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolInvocationResult:
    name: str
    ok: bool
    content: Any = None
    error: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProgressSummary:
    """Safe, structured progress — never includes hidden chain-of-thought."""

    stage: str
    message: str
    percent: Optional[float] = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    kind: str
    path: str
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UsageReceipt:
    model: str
    adapter_name: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunReceipt:
    task_id: str
    run_id: str
    status: TaskStatus
    summary: str
    progress: Sequence[ProgressSummary] = field(default_factory=tuple)
    artifacts: Sequence[ArtifactRef] = field(default_factory=tuple)
    usage: Optional[UsageReceipt] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class TaskIntake:
    text: str
    channel_id: str
    workspace_id: str
    guild_id: Optional[str] = None
    thread_id: Optional[str] = None
    message_id: Optional[str] = None
    requester_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextSnapshot:
    task_id: str
    memories: Sequence[Mapping[str, Any]]
    bindings: Mapping[str, Any]
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchRequest:
    task_id: str
    run_id: str
    prompt: str
    model: str
    context: ContextSnapshot
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchEvent:
    kind: EventKind
    summary: ProgressSummary
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchResult:
    run_id: str
    status: TaskStatus
    events: Sequence[DispatchEvent]
    final_summary: str
    artifacts: Sequence[ArtifactRef] = field(default_factory=tuple)
    usage: Optional[UsageReceipt] = None
    error: Optional[str] = None


@runtime_checkable
class DiscordMCPProvider(Protocol):
    """Normalized Discord/MCP provider surface (SaseQ, BrainDAO, or fake)."""

    name: str

    def list_tools(self) -> Sequence[ToolDescriptor]: ...

    def invoke_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolInvocationResult: ...

    def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        thread_id: Optional[str] = None,
    ) -> DiscordMessage: ...

    def read_messages(
        self,
        channel_id: str,
        *,
        limit: int = 20,
        thread_id: Optional[str] = None,
    ) -> Sequence[DiscordMessage]: ...

    def post_thread_task(
        self,
        channel_id: str,
        title: str,
        content: str,
    ) -> DiscordMessage: ...


@runtime_checkable
class SamplingIngress(Protocol):
    """BrainDAO sampling-compatible ingress seam (no second Gateway required)."""

    def handle_sampling_request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


@runtime_checkable
class GatewayOwnerRegistry(Protocol):
    """One active Gateway owner per bot token."""

    def claim(self, bot_token_fingerprint: str, owner_id: str) -> None: ...

    def release(self, bot_token_fingerprint: str, owner_id: str) -> None: ...

    def current_owner(self, bot_token_fingerprint: str) -> Optional[str]: ...


@runtime_checkable
class PuppetmasterBackend(Protocol):
    """Backend boundary for installed Puppetmaster CLI/package or deterministic fakes."""

    def resolve_model(self, requested: str) -> ModelPin: ...

    def dispatch(self, request: DispatchRequest) -> DispatchResult: ...

    def cancel(self, run_id: str) -> bool: ...

    def status(self, run_id: str) -> TaskStatus: ...


@runtime_checkable
class EventStore(Protocol):
    def append_event(
        self,
        *,
        task_id: str,
        run_id: str,
        kind: EventKind,
        summary: str,
        payload: Mapping[str, Any],
        source: str,
        provenance: Mapping[str, Any],
    ) -> int: ...

    def list_events(self, run_id: str) -> Sequence[Mapping[str, Any]]: ...


@runtime_checkable
class MemoryStore(Protocol):
    def remember(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        content: str,
        source: str,
        provenance: Mapping[str, Any],
    ) -> str: ...

    def recall(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        query: str,
        limit: int = 8,
    ) -> Sequence[Mapping[str, Any]]: ...


class ClaimStatus(str, Enum):
    """Lifecycle of a research claim (distinct from ordinary memory recall)."""

    CANDIDATE = "candidate"
    VERIFIED = "verified"
    REJECTED = "rejected"
    NEGATIVE = "negative"


@dataclass(frozen=True)
class ResearchClaim:
    """Typed research claim with stable fingerprint and provenance/evidence."""

    claim_id: str
    fingerprint: str
    workspace_id: str
    scope: str
    claim_text: str
    status: ClaimStatus
    provenance: Mapping[str, Any] = field(default_factory=dict)
    evidence: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ResearchLease:
    """Exclusive short-lived lease so two workers cannot race one fingerprint."""

    fingerprint: str
    owner_id: str
    acquired_at: str
    expires_at: str


@runtime_checkable
class ResearchMemory(Protocol):
    """Optional research seam — claims, leases, negatives; not required for normal tasks."""

    def fingerprint_for(self, claim_text: str, scope: str) -> str: ...

    def upsert_claim(
        self,
        *,
        workspace_id: str,
        scope: str,
        claim_text: str,
        status: ClaimStatus,
        provenance: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]] = (),
        claim_id: Optional[str] = None,
    ) -> ResearchClaim: ...

    def get_claim(self, fingerprint: str) -> Optional[ResearchClaim]: ...

    def list_claims(
        self,
        *,
        workspace_id: str,
        status: Optional[ClaimStatus] = None,
        limit: int = 50,
    ) -> Sequence[ResearchClaim]: ...

    def list_negative_findings(
        self,
        *,
        workspace_id: str,
        scope: Optional[str] = None,
        limit: int = 50,
    ) -> Sequence[ResearchClaim]: ...

    def acquire_lease(
        self,
        fingerprint: str,
        owner_id: str,
        *,
        ttl_seconds: int = 300,
    ) -> bool: ...

    def release_lease(self, fingerprint: str, owner_id: str) -> bool: ...

    def get_lease(self, fingerprint: str) -> Optional[ResearchLease]: ...


@runtime_checkable
class Orchestrator(Protocol):
    def run_task(self, intake: TaskIntake) -> RunReceipt: ...

    def cancel(self, run_id: str) -> bool: ...

    def status(self, run_id: str) -> TaskStatus: ...
