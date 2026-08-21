"""Puppetmaster CLI/package backend adapter with pinned model enforcement."""

from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

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
from agent_discord.redaction import (
    ALLOWED_REASONING_KEYS,
    redact_text_markers,
    strip_forbidden_keys,
)

TOKEN_TEXT_LIMIT = 1500
STREAM_PHASES = frozenset({"thinking", "plan", "code", "dispatch", "done"})
_TOKEN_EVENT_TYPES = frozenset({"token", "delta", "reasoning"})
_PHASE_ALIASES = {
    "think": "thinking",
    "thought": "thinking",
    "thoughts": "thinking",
    "planning": "plan",
    "coding": "code",
    "implement": "code",
    "implementation": "code",
    "working": "code",
    "complete": "done",
    "completed": "done",
}


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

    def stream(self, request: DispatchRequest) -> Iterator[DispatchEvent]:
        """Stream dispatch progress live instead of blocking until completion."""
        pin = self.resolve_model(request.model)
        self._statuses[request.run_id] = TaskStatus.RUNNING

        if not self.available():
            self._statuses[request.run_id] = TaskStatus.FAILED
            yield DispatchEvent(
                kind=EventKind.ERROR,
                summary=ProgressSummary(
                    stage="dispatch",
                    message=f"puppetmaster CLI not found: {self.cli}",
                ),
            )
            return

        if not pin.adapter_name:
            self._statuses[request.run_id] = TaskStatus.FAILED
            yield DispatchEvent(
                kind=EventKind.ERROR,
                summary=ProgressSummary(
                    stage="dispatch",
                    message="model pin adapter_name is unavailable",
                ),
            )
            return

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
        command.append("--json-lines")
        command.append(prompt)

        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=workdir,
            )
        except OSError as exc:
            self._statuses[request.run_id] = TaskStatus.FAILED
            yield DispatchEvent(
                kind=EventKind.ERROR,
                summary=ProgressSummary(stage="dispatch", message=str(exc)),
            )
            return

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        stdout_queue: queue.Queue[Optional[str]] = queue.Queue()
        stderr_queue: queue.Queue[Optional[str]] = queue.Queue()

        def _reader(pipe: Any, out: queue.Queue[Optional[str]], sink: list[str]) -> None:
            try:
                for line in iter(pipe.readline, ""):
                    sink.append(line)
                    out.put(line)
            finally:
                out.put(None)

        threads = [
            threading.Thread(target=_reader, args=(proc.stdout, stdout_queue, stdout_lines), daemon=True),
            threading.Thread(target=_reader, args=(proc.stderr, stderr_queue, stderr_lines), daemon=True),
        ]
        for t in threads:
            t.start()

        yield DispatchEvent(
            kind=EventKind.DISPATCH,
            summary=ProgressSummary(
                stage="dispatch",
                message=f"dispatched via CLI with {pin.adapter_name}",
                percent=2.0,
                details={"model": pin.canonical},
            ),
        )

        done_stdout = False
        done_stderr = False
        token_buffer = TokenStreamBuffer()
        try:
            while not (done_stdout and done_stderr):
                if not done_stdout:
                    try:
                        line = stdout_queue.get(timeout=0.25)
                        if line is None:
                            done_stdout = True
                        else:
                            event = _event_from_cli_line(line, pin.canonical, token_buffer)
                            if event is not None:
                                yield event
                    except queue.Empty:
                        pass
                if not done_stderr:
                    try:
                        line = stderr_queue.get(timeout=0.25)
                        if line is None:
                            done_stderr = True
                    except queue.Empty:
                        pass
                if proc.poll() is not None and done_stdout and done_stderr:
                    break
            proc.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self._statuses[request.run_id] = TaskStatus.FAILED
            yield DispatchEvent(
                kind=EventKind.ERROR,
                summary=ProgressSummary(stage="dispatch", message="timeout"),
            )
            return

        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        safe_meta = _parse_safe_cli_completion(stdout, stderr)
        if proc.returncode != 0:
            self._statuses[request.run_id] = TaskStatus.FAILED
            err = safe_meta.get("error") or stderr.strip() or f"exit {proc.returncode}"
            yield DispatchEvent(
                kind=EventKind.ERROR,
                summary=ProgressSummary(stage="dispatch", message=str(err)),
            )
            return

        summary = str(safe_meta.get("summary") or "completed")
        self._statuses[request.run_id] = TaskStatus.COMPLETED
        yield DispatchEvent(
            kind=EventKind.RECEIPT,
            summary=ProgressSummary(stage="done", message=summary, percent=100.0),
            payload=safe_meta,
        )


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


_SUMMARY_SKIP_PREFIXES = ("#", "---", "goal:", "role:", "status:")


def _first_visible_summary_line(text: str) -> str:
    for line in (text or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        lower = raw.lower()
        if any(lower.startswith(prefix) for prefix in _SUMMARY_SKIP_PREFIXES):
            continue
        return raw[:500]
    first = (text or "").strip().splitlines()
    return first[0][:500] if first else "completed"


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
                meta["summary"] = _first_visible_summary_line(text)

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


_PROGRESS_RE = re.compile(r"progress[:\s]+(\d+(?:\.\d+)?)%?", re.IGNORECASE)
_STAGE_RE = re.compile(r"stage[:\s]+([a-zA-Z0-9_\-]+)", re.IGNORECASE)


@dataclass
class TokenStreamBuffer:
    """Visible token window and current stream phase for one CLI run."""

    phase: str = "thinking"
    text: str = ""

    def extend(self, chunk: str) -> str:
        if chunk:
            self.text = (self.text + chunk)[-TOKEN_TEXT_LIMIT:]
        return self.text

    def set_phase(self, phase: str) -> str:
        self.phase = _normalize_stream_phase(phase, self.phase)
        return self.phase


def _normalize_stream_phase(raw: str, default: str = "thinking") -> str:
    stage = (raw or "").strip().lower()
    stage = _PHASE_ALIASES.get(stage, stage)
    if stage in STREAM_PHASES:
        return stage
    return default if default in STREAM_PHASES else "thinking"


def _extract_token_text(cleaned: dict[str, Any], event_type: str) -> str:
    if event_type == "delta":
        keys = ("text", "content", "delta")
    elif event_type == "token":
        keys = ("content", "text", "token")
    else:
        keys = (
            "summary",
            "reasoning_summary",
            "plan",
            "plan_summary",
            "approach",
            "findings",
            "text",
            "content",
        )
    for key in keys:
        value = cleaned.get(key)
        if value:
            return str(value)
    return ""


def _parse_token_line(
    line: str,
    model: str,
    buffer: Optional[TokenStreamBuffer] = None,
) -> Optional[DispatchEvent]:
    """Parse a token/reasoning/delta NDJSON line into a safe PROGRESS event.

    Raw chain-of-thought keys are stripped; only allowed reasoning summaries
    and visible token text are kept. Accumulated text is bounded.
    """
    raw = (line or "").strip()
    if not raw:
        return None
    parsed = _try_parse_json(raw)
    if not isinstance(parsed, dict):
        return None
    event_type = str(parsed.get("type") or "").strip().lower()
    if event_type not in _TOKEN_EVENT_TYPES:
        return None
    cleaned = strip_forbidden_keys(parsed)
    if not isinstance(cleaned, dict):
        return None
    event_type = str(cleaned.get("type") or event_type).strip().lower()
    if event_type not in _TOKEN_EVENT_TYPES:
        return None

    state = buffer if buffer is not None else TokenStreamBuffer()
    explicit = cleaned.get("stage") or cleaned.get("stream_phase") or cleaned.get("phase")
    if explicit:
        state.set_phase(str(explicit))
    elif event_type == "reasoning":
        if any(cleaned.get(key) for key in ("plan", "plan_summary")):
            state.set_phase("plan")
        else:
            state.set_phase("thinking")

    chunk = redact_text_markers(_extract_token_text(cleaned, event_type))
    if not chunk.strip():
        return None
    state.extend(chunk)

    percent: Optional[float] = None
    if "percent" in cleaned:
        try:
            percent = float(cleaned["percent"])
        except (TypeError, ValueError):
            percent = None

    details: dict[str, Any] = {
        "token": event_type in {"token", "delta"},
        "stream_phase": state.phase,
        "token_text": state.text,
    }
    if model:
        details["model"] = model
    for key in ALLOWED_REASONING_KEYS:
        value = cleaned.get(key)
        if value:
            details[key] = value

    return DispatchEvent(
        kind=EventKind.PROGRESS,
        summary=ProgressSummary(
            stage=state.phase,
            message=chunk[:500],
            percent=percent,
            details=details,
        ),
    )


def _event_from_cli_line(
    line: str,
    model: str,
    buffer: TokenStreamBuffer,
) -> Optional[DispatchEvent]:
    """Prefer token/reasoning NDJSON; fall back to existing progress lines."""
    token_event = _parse_token_line(line, model, buffer=buffer)
    if token_event is not None:
        return token_event
    progress = _parse_progress_line(line, model)
    if progress is not None and progress.summary.stage:
        stage = _PHASE_ALIASES.get(
            progress.summary.stage.strip().lower(),
            progress.summary.stage.strip().lower(),
        )
        if stage in STREAM_PHASES:
            buffer.set_phase(stage)
    return progress


def _parse_progress_line(line: str, model: str) -> Optional[DispatchEvent]:
    """Parse a single CLI output line into a safe PROGRESS event when possible."""
    raw = (line or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    if lower.startswith(("job_id:", "artifacts:", "summary:")):
        return None
    percent: Optional[float] = None
    stage = "working"
    message = raw[:500]

    match = _PROGRESS_RE.search(raw)
    if match:
        try:
            percent = float(match.group(1))
        except ValueError:
            percent = None
    stage_match = _STAGE_RE.search(raw)
    if stage_match:
        stage = stage_match.group(1)

    parsed = _try_parse_json(raw)
    if isinstance(parsed, dict):
        event_type = str(parsed.get("type") or "").strip().lower()
        if event_type in _TOKEN_EVENT_TYPES:
            return None
        cleaned = strip_forbidden_keys(parsed)
        if isinstance(cleaned, dict):
            if "percent" in cleaned:
                try:
                    percent = float(cleaned["percent"])
                except (TypeError, ValueError):
                    pass
            if "stage" in cleaned:
                stage = str(cleaned["stage"])
            if "message" in cleaned:
                message = str(cleaned["message"])[:500]
            elif "summary" in cleaned:
                message = str(cleaned["summary"])[:500]
            details = {k: v for k, v in cleaned.items() if k in ALLOWED_REASONING_KEYS}
            return DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage=stage,
                    message=redact_text_markers(message),
                    percent=percent,
                    details=details,
                ),
            )

    if percent is None and not any(word in lower for word in ("plan", "step", "tool", "finding", "reasoning", "working", "running")):
        return None

    return DispatchEvent(
        kind=EventKind.PROGRESS,
        summary=ProgressSummary(
            stage=stage,
            message=redact_text_markers(message),
            percent=percent,
            details={"model": model},
        ),
    )
