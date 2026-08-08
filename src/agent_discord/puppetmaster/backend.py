"""Puppetmaster CLI/package backend adapter with pinned model enforcement."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from agent_discord.contracts import (
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
from agent_discord.redaction import redact_text_markers, strip_forbidden_keys


@dataclass
class PuppetmasterCliBackend:
    """Boundary usable with an installed Puppetmaster Cursor worker CLI.

    Invokes ``puppetmaster cursor`` (public CLI shape). Does not spend Cursor
    credits in tests — use FakePuppetmasterBackend instead.

    Model resolution is exact-allowlist only; never remaps to another model.
    The CLI receives the adapter model name (``grok-4.5``); receipts/audit keep
    the canonical pin (``cursor/grok-4-5``).
    """

    cli: str = "puppetmaster"
    pin: ModelPin = field(default_factory=lambda: DEFAULT_MODEL_PIN)
    cwd: Optional[str | Path] = None
    timeout_seconds: float = 3600.0
    _statuses: dict[str, TaskStatus] = field(default_factory=dict)

    def resolve_model(self, requested: str) -> ModelPin:
        self.pin.assert_allowed(requested)
        if requested != self.pin.canonical:
            raise ModelNotAllowedError(
                f"requested {requested!r} != pinned {self.pin.canonical!r}; "
                "no silent fallback"
            )
        return self.pin

    def available(self) -> bool:
        return shutil.which(self.cli) is not None

    def dispatch(self, request: DispatchRequest) -> DispatchResult:
        pin = self.resolve_model(request.model)
        self._statuses[request.run_id] = TaskStatus.RUNNING

        if not self.available():
            self._statuses[request.run_id] = TaskStatus.FAILED
            return DispatchResult(
                run_id=request.run_id,
                status=TaskStatus.FAILED,
                events=(
                    DispatchEvent(
                        kind=EventKind.ERROR,
                        summary=ProgressSummary(
                            stage="dispatch",
                            message=f"puppetmaster CLI not found: {self.cli}",
                        ),
                    ),
                ),
                final_summary="puppetmaster CLI unavailable",
                error=f"CLI not found: {self.cli}",
            )

        if not pin.adapter_name:
            self._statuses[request.run_id] = TaskStatus.FAILED
            return DispatchResult(
                run_id=request.run_id,
                status=TaskStatus.FAILED,
                events=(
                    DispatchEvent(
                        kind=EventKind.ERROR,
                        summary=ProgressSummary(
                            stage="dispatch",
                            message="model pin adapter_name is unavailable",
                        ),
                    ),
                ),
                final_summary="model pin unavailable",
                error="adapter_name missing on model pin",
            )

        prompt = _safe_dispatch_prompt(request)
        command = [
            self.cli,
            "cursor",
            "--implement",
            "--allow-dirty",
            "--model",
            pin.adapter_name,
            "--timeout-seconds",
            str(int(self.timeout_seconds)),
        ]
        workdir = str(self.cwd) if self.cwd else None
        if workdir:
            command.extend(["--cwd", workdir])
        command.append(prompt)

        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                cwd=workdir,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._statuses[request.run_id] = TaskStatus.FAILED
            return DispatchResult(
                run_id=request.run_id,
                status=TaskStatus.FAILED,
                events=(
                    DispatchEvent(
                        kind=EventKind.ERROR,
                        summary=ProgressSummary(stage="dispatch", message=str(exc)),
                    ),
                ),
                final_summary="dispatch failed",
                error=str(exc),
            )

        safe_meta = _parse_safe_cli_completion(proc.stdout, proc.stderr)
        if proc.returncode != 0:
            self._statuses[request.run_id] = TaskStatus.FAILED
            err = safe_meta.get("error") or proc.stderr.strip() or f"exit {proc.returncode}"
            return DispatchResult(
                run_id=request.run_id,
                status=TaskStatus.FAILED,
                events=(
                    DispatchEvent(
                        kind=EventKind.ERROR,
                        summary=ProgressSummary(stage="dispatch", message=str(err)),
                    ),
                ),
                final_summary="dispatch failed",
                error=str(err),
            )

        summary = str(safe_meta.get("summary") or "completed")
        self._statuses[request.run_id] = TaskStatus.COMPLETED
        return DispatchResult(
            run_id=request.run_id,
            status=TaskStatus.COMPLETED,
            events=(
                DispatchEvent(
                    kind=EventKind.DISPATCH,
                    summary=ProgressSummary(
                        stage="dispatch",
                        message=f"dispatched via CLI with {pin.adapter_name}",
                        details={"model": pin.canonical},
                    ),
                ),
                DispatchEvent(
                    kind=EventKind.RECEIPT,
                    summary=ProgressSummary(stage="done", message=summary, percent=100.0),
                    payload=safe_meta,
                ),
            ),
            final_summary=summary,
            usage=UsageReceipt(
                model=pin.canonical,
                adapter_name=pin.adapter_name,
                metadata={
                    "backend": "cli",
                    "cli": self.cli,
                    "cli_model": pin.adapter_name,
                    "job_id": safe_meta.get("job_id"),
                },
            ),
        )

    def cancel(self, run_id: str) -> bool:
        """Report unsupported cancellation instead of calling a fake CLI command."""
        return False

    def status(self, run_id: str) -> TaskStatus:
        return self._statuses.get(run_id, TaskStatus.PENDING)


def _safe_dispatch_prompt(request: DispatchRequest) -> str:
    """Build a plain-text prompt suitable for `puppetmaster cursor` (no hidden CoT)."""
    memory_bits = []
    for item in list(request.context.memories)[:8]:
        if isinstance(item, dict):
            content = str(item.get("content") or "").strip()
            if content:
                memory_bits.append(content[:400])
    lines = [
        request.prompt.strip(),
        "",
        f"task_id={request.task_id}",
        f"run_id={request.run_id}",
        f"model={request.model}",
    ]
    channel = request.metadata.get("channel_id") if request.metadata else None
    if channel:
        lines.append(f"channel_id={channel}")
    if memory_bits:
        lines.append("")
        lines.append("Context memories:")
        lines.extend(f"- {m}" for m in memory_bits)
    research = request.context.provenance.get("research")
    if isinstance(research, dict):
        research_claims = list(research.get("claims") or [])
        negative_findings = list(research.get("negative_findings") or [])
        if research_claims or negative_findings:
            lines.append("")
            lines.append("Research context:")
            for item in research_claims + negative_findings:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status") or "finding")
                scope = str(item.get("scope") or "unknown")
                claim_text = redact_text_markers(str(item.get("claim_text") or ""))[:400]
                if claim_text:
                    lines.append(f"- [{status}] ({scope}) {claim_text}")
    return "\n".join(lines).strip()


_SAFE_SUMMARY_KEYS = frozenset(
    {
        "summary",
        "result",
        "status",
        "job_id",
        "artifacts",
        "summary_path",
        "ok",
        "error",
        "message",
    }
)


def _parse_safe_cli_completion(stdout: str, stderr: str) -> dict[str, Any]:
    """Extract only safe structured completion fields; never relay hidden reasoning."""
    meta: dict[str, Any] = {}
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("job_id:"):
            meta["job_id"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("artifacts:"):
            value = stripped.split(":", 1)[1].strip()
            try:
                meta["artifacts"] = int(value)
            except ValueError:
                meta["artifacts"] = value
        elif stripped.startswith("summary:"):
            meta["summary_path"] = stripped.split(":", 1)[1].strip()

    parsed = _try_parse_json(stdout)
    if isinstance(parsed, dict):
        cleaned = strip_forbidden_keys(parsed)
        if isinstance(cleaned, dict):
            for key in _SAFE_SUMMARY_KEYS:
                if key in cleaned and key not in meta:
                    meta[key] = cleaned[key]
            if "summary" not in meta:
                for key in ("summary", "result", "message"):
                    if cleaned.get(key):
                        meta["summary"] = str(cleaned[key])
                        break

    summary_path = meta.get("summary_path")
    if summary_path and "summary" not in meta:
        path = Path(str(summary_path))
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            file_json = _try_parse_json(text)
            if isinstance(file_json, dict):
                cleaned = strip_forbidden_keys(file_json)
                if isinstance(cleaned, dict):
                    for key in ("summary", "result", "message", "status"):
                        if not cleaned.get(key):
                            continue
                        if key == "summary" or "summary" not in meta:
                            meta["summary"] = str(cleaned[key])
                            if key == "summary":
                                break
            elif text.strip():
                # Plain summary files: take a short preview only
                preview = text.strip().splitlines()[0][:500]
                meta["summary"] = preview

    if "summary" not in meta:
        if meta.get("job_id"):
            meta["summary"] = f"puppetmaster job {meta['job_id']} completed"
        else:
            meta["summary"] = "completed"

    err_text = (stderr or "").strip()
    if err_text and "error" not in meta:
        # Keep only the first line of stderr as a coarse error signal
        first = err_text.splitlines()[0][:500]
        if "error" in first.lower() or "failed" in first.lower() or "blocked" in first.lower():
            meta["error"] = first

    return strip_forbidden_keys(meta) if isinstance(meta, dict) else {}


def _try_parse_json(text: str) -> Optional[Any]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.rfind("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None
