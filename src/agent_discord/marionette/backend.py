"""Optional Marionette HTTP backend adapting to PuppetmasterBackend contracts.

Expected adapter contract (configurable paths — not a guaranteed upstream shape):

- POST  {base}{sessions_path}           create session → {session_id}
- POST  {base}{jobs_path}               dispatch job → {job_id, status, summary, ...}
- GET   {base}{jobs_path}/{id}          job record (artifacts/usage/events)
- GET   {base}{jobs_path}/{id}/events   SSE or JSON event stream
- GET   {base}{jobs_path}/{id}/status   {status}
- POST  {base}{jobs_path}/{id}/cancel   cancel → {cancelled: true}

When the base URL is unset or the transport cannot reach Marionette, this backend
fails closed with MarionetteConfigError / MarionetteTransportError — never
silently falls back to Puppetmaster.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

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
from agent_discord.marionette.transport import HttpTransport, UrllibTransport
from agent_discord.puppetmaster.models import DEFAULT_MODEL_PIN


class MarionetteConfigError(ValueError):
    """Raised when Marionette is selected but not configured."""


class MarionetteTransportError(RuntimeError):
    """Raised when the Marionette HTTP API is unavailable or returns an error."""


@dataclass(frozen=True)
class MarionetteEndpointConfig:
    """Configurable path suffixes relative to base_url."""

    sessions_path: str = "/v1/sessions"
    jobs_path: str = "/v1/jobs"

    def normalize(self) -> MarionetteEndpointConfig:
        return MarionetteEndpointConfig(
            sessions_path=_ensure_abs_path(self.sessions_path, "/v1/sessions"),
            jobs_path=_ensure_abs_path(self.jobs_path, "/v1/jobs"),
        )


@dataclass
class MarionetteBackend:
    """PuppetmasterBackend-compatible adapter over a configurable Marionette HTTP API."""

    base_url: str = ""
    pin: ModelPin = field(default_factory=lambda: DEFAULT_MODEL_PIN)
    endpoints: MarionetteEndpointConfig = field(default_factory=MarionetteEndpointConfig)
    transport: HttpTransport = field(default_factory=UrllibTransport)
    timeout_seconds: float = 60.0
    api_token: str = ""
    _statuses: dict[str, TaskStatus] = field(default_factory=dict)
    _job_ids: dict[str, str] = field(default_factory=dict)
    _session_id: Optional[str] = None

    def __post_init__(self) -> None:
        self.endpoints = self.endpoints.normalize()
        self.base_url = (self.base_url or "").rstrip("/")

    def resolve_model(self, requested: str) -> ModelPin:
        self.pin.assert_allowed(requested)
        if requested != self.pin.canonical:
            raise ModelNotAllowedError(
                f"requested {requested!r} != pinned {self.pin.canonical!r}; "
                "no silent fallback"
            )
        return self.pin

    def available(self) -> bool:
        return bool(self.base_url)

    def dispatch(self, request: DispatchRequest) -> DispatchResult:
        pin = self.resolve_model(request.model)
        self._statuses[request.run_id] = TaskStatus.RUNNING

        if not self.available():
            self._statuses[request.run_id] = TaskStatus.FAILED
            err = (
                "Marionette backend selected but MARIONETTE_BASE_URL is empty; "
                "configure an explicit endpoint or use AGENT_DISCORD_BACKEND=puppetmaster"
            )
            return _failed(request.run_id, err, stage="config")

        try:
            session_id = self._ensure_session(pin)
            job = self._create_job(session_id, request, pin)
            job_id = str(job.get("job_id") or job.get("id") or "")
            if not job_id:
                raise MarionetteTransportError("Marionette job response missing job_id")
            self._job_ids[request.run_id] = job_id

            events_raw = list(job.get("events") or [])
            if not events_raw:
                events_raw = self._fetch_events(job_id)
            if not job.get("summary") and not job.get("artifacts"):
                detail = self._get_job(job_id)
                job = {**detail, **job}

            status = _map_status(str(job.get("status") or "completed"))
            events = _normalize_events(events_raw, pin)
            artifacts = _normalize_artifacts(job.get("artifacts") or ())
            usage = _normalize_usage(job.get("usage"), pin)
            summary = str(job.get("summary") or job.get("final_summary") or "completed")
            error = job.get("error")
            if status == TaskStatus.FAILED and not error:
                error = "marionette job failed"
            self._statuses[request.run_id] = status
            return DispatchResult(
                run_id=request.run_id,
                status=status,
                events=tuple(events),
                final_summary=summary,
                artifacts=tuple(artifacts),
                usage=usage,
                error=str(error) if error else None,
            )
        except (MarionetteConfigError, MarionetteTransportError, OSError, ValueError, json.JSONDecodeError) as exc:
            self._statuses[request.run_id] = TaskStatus.FAILED
            return _failed(request.run_id, str(exc), stage="dispatch")

    def cancel(self, run_id: str) -> bool:
        job_id = self._job_ids.get(run_id)
        if not job_id or not self.available():
            return False
        url = self._url(f"{self.endpoints.jobs_path.rstrip('/')}/{job_id}/cancel")
        try:
            resp = self._request("POST", url, body={})
        except MarionetteTransportError:
            return False
        if resp.status >= 400:
            return False
        self._statuses[run_id] = TaskStatus.CANCELLED
        return True

    def status(self, run_id: str) -> TaskStatus:
        if run_id in self._statuses:
            cached = self._statuses[run_id]
            if cached in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                return cached
        job_id = self._job_ids.get(run_id)
        if not job_id or not self.available():
            return self._statuses.get(run_id, TaskStatus.PENDING)
        url = self._url(f"{self.endpoints.jobs_path.rstrip('/')}/{job_id}/status")
        try:
            resp = self._request("GET", url)
            data = resp.json() or {}
            status = _map_status(str(data.get("status") or "pending"))
            self._statuses[run_id] = status
            return status
        except (MarionetteTransportError, json.JSONDecodeError, TypeError, ValueError):
            return self._statuses.get(run_id, TaskStatus.PENDING)

    # --- HTTP helpers ---

    def _ensure_session(self, pin: ModelPin) -> str:
        if self._session_id:
            return self._session_id
        url = self._url(self.endpoints.sessions_path)
        resp = self._request(
            "POST",
            url,
            body={
                "model": pin.canonical,
                "adapter_name": pin.adapter_name,
                "backend": "marionette",
            },
        )
        data = resp.json() or {}
        session_id = str(data.get("session_id") or data.get("id") or "")
        if not session_id:
            raise MarionetteTransportError("Marionette session response missing session_id")
        self._session_id = session_id
        return session_id

    def _create_job(
        self,
        session_id: str,
        request: DispatchRequest,
        pin: ModelPin,
    ) -> dict[str, Any]:
        url = self._url(self.endpoints.jobs_path)
        memory_bits = []
        for item in list(request.context.memories)[:8]:
            if isinstance(item, dict):
                content = str(item.get("content") or "").strip()
                if content:
                    memory_bits.append(content[:400])
        body = {
            "session_id": session_id,
            "task_id": request.task_id,
            "run_id": request.run_id,
            "prompt": request.prompt,
            "model": pin.canonical,
            "adapter_name": pin.adapter_name,
            "context": {
                "memories": memory_bits,
                "bindings": dict(request.context.bindings),
                "provenance": dict(request.context.provenance),
            },
            "metadata": dict(request.metadata),
        }
        resp = self._request("POST", url, body=body)
        data = resp.json()
        if not isinstance(data, dict):
            raise MarionetteTransportError("Marionette job response was not a JSON object")
        return data

    def _get_job(self, job_id: str) -> dict[str, Any]:
        url = self._url(f"{self.endpoints.jobs_path.rstrip('/')}/{job_id}")
        resp = self._request("GET", url)
        data = resp.json()
        return data if isinstance(data, dict) else {}

    def _fetch_events(self, job_id: str) -> list[dict[str, Any]]:
        url = self._url(f"{self.endpoints.jobs_path.rstrip('/')}/{job_id}/events")
        resp = self._request("GET", url)
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" in ctype or resp.text().lstrip().startswith("event:"):
            return _parse_sse(resp.text())
        data = resp.json()
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return [x for x in data["events"] if isinstance(x, dict)]
        return []

    def _url(self, path: str) -> str:
        if not self.base_url:
            raise MarionetteConfigError("MARIONETTE_BASE_URL is required")
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        headers = {
            "accept": (
                "text/event-stream, application/json"
                if url.rstrip("/").endswith("/events")
                else "application/json"
            )
        }
        raw: Optional[bytes] = None
        if body is not None:
            headers["content-type"] = "application/json"
            raw = json.dumps(dict(body), sort_keys=True).encode("utf-8")
        if self.api_token:
            headers["authorization"] = f"Bearer {self.api_token}"
        try:
            resp = self.transport.request(
                method,
                url,
                headers=headers,
                body=raw,
                timeout=self.timeout_seconds,
            )
        except OSError as exc:
            raise MarionetteTransportError(f"Marionette transport error: {exc}") from exc
        if resp.status >= 400:
            snippet = resp.text()[:300]
            raise MarionetteTransportError(
                f"Marionette HTTP {resp.status} for {method} {url}: {snippet}"
            )
        return resp


def _ensure_abs_path(value: str, default: str) -> str:
    path = (value or default).strip() or default
    if not path.startswith("/"):
        path = "/" + path
    return path


def _failed(run_id: str, error: str, *, stage: str) -> DispatchResult:
    return DispatchResult(
        run_id=run_id,
        status=TaskStatus.FAILED,
        events=(
            DispatchEvent(
                kind=EventKind.ERROR,
                summary=ProgressSummary(stage=stage, message=error),
            ),
        ),
        final_summary="marionette dispatch failed",
        error=error,
    )


def _map_status(raw: str) -> TaskStatus:
    key = (raw or "").strip().lower()
    mapping = {
        "pending": TaskStatus.PENDING,
        "queued": TaskStatus.PENDING,
        "running": TaskStatus.RUNNING,
        "progress": TaskStatus.PROGRESS,
        "completed": TaskStatus.COMPLETED,
        "succeeded": TaskStatus.COMPLETED,
        "success": TaskStatus.COMPLETED,
        "failed": TaskStatus.FAILED,
        "error": TaskStatus.FAILED,
        "cancelled": TaskStatus.CANCELLED,
        "canceled": TaskStatus.CANCELLED,
    }
    return mapping.get(key, TaskStatus.PENDING)


def _normalize_events(raw_events: list[dict[str, Any]], pin: ModelPin) -> list[DispatchEvent]:
    if not raw_events:
        return [
            DispatchEvent(
                kind=EventKind.DISPATCH,
                summary=ProgressSummary(
                    stage="dispatch",
                    message=f"dispatched via marionette with {pin.adapter_name}",
                    details={"model": pin.canonical, "backend": "marionette"},
                ),
            )
        ]
    out: list[DispatchEvent] = []
    for item in raw_events:
        kind_raw = str(item.get("kind") or item.get("event") or "progress").lower()
        try:
            kind = EventKind(kind_raw)
        except ValueError:
            kind = EventKind.PROGRESS
        out.append(
            DispatchEvent(
                kind=kind,
                summary=ProgressSummary(
                    stage=str(item.get("stage") or kind.value),
                    message=str(item.get("message") or item.get("summary") or kind.value),
                    percent=item.get("percent"),
                    details={"backend": "marionette", "model": pin.canonical},
                ),
                payload={k: v for k, v in item.items() if k not in {"kind", "event"}},
            )
        )
    return out


def _normalize_artifacts(raw: Any) -> list[ArtifactRef]:
    out: list[ArtifactRef] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        artifact_id = str(item.get("artifact_id") or item.get("id") or "")
        path = str(item.get("path") or item.get("uri") or "")
        if not artifact_id or not path:
            continue
        prov = item.get("provenance") if isinstance(item.get("provenance"), Mapping) else {}
        out.append(
            ArtifactRef(
                artifact_id=artifact_id,
                kind=str(item.get("kind") or "artifact"),
                path=path,
                provenance=dict(prov),
            )
        )
    return out


def _normalize_usage(raw: Any, pin: ModelPin) -> UsageReceipt:
    meta: dict[str, Any] = {"backend": "marionette"}
    input_tokens = None
    output_tokens = None
    if isinstance(raw, Mapping):
        input_tokens = raw.get("input_tokens")
        output_tokens = raw.get("output_tokens")
        if isinstance(raw.get("memory"), Mapping):
            meta["memory"] = dict(raw["memory"])
        for key in ("metadata",):
            if isinstance(raw.get(key), Mapping):
                meta.update(dict(raw[key]))
    return UsageReceipt(
        model=pin.canonical,
        adapter_name=pin.adapter_name,
        input_tokens=input_tokens if isinstance(input_tokens, int) else None,
        output_tokens=output_tokens if isinstance(output_tokens, int) else None,
        metadata=meta,
    )


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current_kind = "progress"
    data_lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if data_lines:
                payload = "\n".join(data_lines)
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    parsed = {"message": payload}
                if isinstance(parsed, dict):
                    parsed.setdefault("kind", current_kind)
                    events.append(parsed)
                data_lines = []
                current_kind = "progress"
            continue
        if line.startswith("event:"):
            current_kind = line.split(":", 1)[1].strip() or "progress"
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
    if data_lines:
        payload = "\n".join(data_lines)
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"message": payload}
        if isinstance(parsed, dict):
            parsed.setdefault("kind", current_kind)
            events.append(parsed)
    return events
