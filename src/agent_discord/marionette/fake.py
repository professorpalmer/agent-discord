"""In-memory Marionette transport for deterministic unit tests (no network)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional
from uuid import uuid4

from agent_discord.marionette.transport import HttpResponse


@dataclass
class FakeMarionetteTransport:
    """Fake HTTP surface covering sessions, jobs, events, status, and cancel."""

    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail_next: bool = False
    unavailable: bool = False
    last_request: Optional[tuple[str, str, Optional[dict[str, Any]]]] = None
    request_log: list[tuple[str, str]] = field(default_factory=list)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        del headers, timeout  # unused in fake
        method_u = method.upper()
        self.request_log.append((method_u, url))
        payload: Optional[dict[str, Any]] = None
        if body:
            parsed = json.loads(body.decode("utf-8"))
            payload = parsed if isinstance(parsed, dict) else None
        self.last_request = (method_u, url, payload)

        if self.unavailable:
            return HttpResponse(status=503, headers={}, body=b'{"error":"unavailable"}', url=url)

        if self.fail_next:
            self.fail_next = False
            return HttpResponse(status=500, headers={}, body=b'{"error":"forced"}', url=url)

        path = url.split("?", 1)[0]
        # Match by trailing path segments so base URLs stay configurable in tests.
        if method_u == "POST" and path.rstrip("/").endswith("/sessions"):
            session_id = uuid4().hex
            record = {
                "session_id": session_id,
                "model": (payload or {}).get("model"),
                "status": "open",
            }
            self.sessions[session_id] = record
            return _json(201, record, url)

        if method_u == "POST" and path.rstrip("/").endswith("/jobs"):
            job_id = uuid4().hex
            session_id = str((payload or {}).get("session_id") or "")
            record = {
                "job_id": job_id,
                "session_id": session_id,
                "status": "completed",
                "prompt": (payload or {}).get("prompt"),
                "model": (payload or {}).get("model"),
                "summary": f"Completed: {str((payload or {}).get('prompt') or '')[:200]}",
                "artifacts": [
                    {
                        "artifact_id": f"art-{job_id[:8]}",
                        "kind": "text",
                        "path": f"memory://marionette/{job_id}",
                        "provenance": {"backend": "marionette-fake"},
                    }
                ],
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 22,
                    "memory": {"recalled": 0},
                },
                "events": [
                    {"kind": "dispatch", "stage": "dispatch", "message": "job accepted"},
                    {"kind": "progress", "stage": "work", "message": "working", "percent": 50.0},
                    {"kind": "receipt", "stage": "done", "message": "completed", "percent": 100.0},
                ],
            }
            self.jobs[job_id] = record
            return _json(201, record, url)

        if method_u == "GET" and "/jobs/" in path and path.rstrip("/").endswith("/events"):
            job_id = _segment_before(path, "events")
            job = self.jobs.get(job_id)
            if not job:
                return _json(404, {"error": "unknown job"}, url)
            # SSE-shaped body for stream parsers; also valid as JSON envelope.
            lines = []
            for ev in job.get("events") or []:
                lines.append(f"event: {ev.get('kind', 'progress')}")
                lines.append(f"data: {json.dumps(ev, sort_keys=True)}")
                lines.append("")
            body = ("\n".join(lines) + "\n").encode("utf-8")
            return HttpResponse(
                status=200,
                headers={"content-type": "text/event-stream"},
                body=body,
                url=url,
            )

        if method_u == "GET" and "/jobs/" in path and path.rstrip("/").endswith("/status"):
            job_id = _segment_before(path, "status")
            job = self.jobs.get(job_id)
            if not job:
                return _json(404, {"error": "unknown job"}, url)
            return _json(200, {"job_id": job_id, "status": job["status"]}, url)

        if method_u == "POST" and "/jobs/" in path and path.rstrip("/").endswith("/cancel"):
            job_id = _segment_before(path, "cancel")
            job = self.jobs.get(job_id)
            if not job:
                return _json(404, {"error": "unknown job"}, url)
            job["status"] = "cancelled"
            return _json(200, {"job_id": job_id, "status": "cancelled", "cancelled": True}, url)

        if method_u == "GET" and "/jobs/" in path:
            job_id = path.rstrip("/").rsplit("/", 1)[-1]
            job = self.jobs.get(job_id)
            if not job:
                return _json(404, {"error": "unknown job"}, url)
            return _json(200, job, url)

        return _json(404, {"error": f"no fake handler for {method_u} {path}"}, url)


def _segment_before(path: str, leaf: str) -> str:
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) >= 2 and parts[-1] == leaf:
        return parts[-2]
    return ""


def _json(status: int, payload: Mapping[str, Any], url: str) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={"content-type": "application/json"},
        body=json.dumps(dict(payload), sort_keys=True).encode("utf-8"),
        url=url,
    )
