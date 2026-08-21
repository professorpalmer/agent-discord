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
from typing import Any, Iterator, Mapping, Optional

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
RECEIPT_TEXT_LIMIT = 1800
STREAM_PHASES = frozenset({"thinking", "plan", "code", "dispatch", "done"})
_CLI_FLAG_CACHE: dict[tuple[str, str, str], bool] = {}
_SUMMARY_SKIP_PREFIXES = (
    "#",
    "---",
    "goal:",
    "role:",
    "status:",
    "task_id=",
    "run_id=",
    "model=",
    "channel_id=",
    "job_id:",
    "artifacts:",
    "summary:",
    "usage:",
    "puppetmaster:",
    "dispatched via",
    "context memories:",
    "research context:",
    "write the answer",
    "do not repeat",
    "internal:",
    "task:",
    "[failures]",
    "[preferences]",
    "[style]",
    "[swarm.",
    "decision:",
    "finding:",
    "gist:",
    "verification:",
    "confidence=",
    "outcome:",
    "full report:",
    "host reach",
)


def cli_supports_flag(cli: str, subcommand: str, flag: str) -> bool:
    """Probe ``cli subcommand --help`` once. Live Puppetmaster may lack --json-lines."""

    key = (cli, subcommand, flag)
    cached = _CLI_FLAG_CACHE.get(key)
    if cached is not None:
        return cached
    supported = False
    try:
        proc = subprocess.run(
            [cli, subcommand, "--help"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        blob = f"{proc.stdout}\n{proc.stderr}"
        supported = flag in blob
    except Exception:
        supported = False
    _CLI_FLAG_CACHE[key] = supported
    return supported
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
        workdir = request_workdir(request, self.cwd)
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
        command = prepend_early_job_id(
            [
                self.cli,
                "cursor",
                "--implement",
                "--allow-dirty",
                "--model",
                pin.adapter_name,
                "--timeout-seconds",
                str(int(self.timeout_seconds)),
            ]
        )
        workdir = request_workdir(request, self.cwd)
        if workdir:
            command.extend(["--cwd", workdir])
        if cli_supports_flag(self.cli, "cursor", "--json-lines"):
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

        yield DispatchEvent(
            kind=EventKind.DISPATCH,
            summary=ProgressSummary(
                stage="dispatch",
                message=f"dispatched via CLI with {pin.adapter_name}",
                percent=2.0,
                details={"model": pin.canonical},
            ),
        )
        for event in iter_cli_process_events(
            proc,
            model=pin.canonical,
            cli=self.cli,
            timeout_seconds=self.timeout_seconds,
        ):
            if event.kind == EventKind.ERROR:
                self._statuses[request.run_id] = TaskStatus.FAILED
            elif event.kind == EventKind.RECEIPT:
                self._statuses[request.run_id] = TaskStatus.COMPLETED
            yield event
        if self._statuses.get(request.run_id) == TaskStatus.RUNNING:
            self._statuses[request.run_id] = TaskStatus.COMPLETED


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
        "Write the answer as visible prose a person can read in Discord.",
        "Do not repeat task_id, run_id, or model lines.",
        "",
        "Internal:",
        f"task_id={request.task_id}",
        f"run_id={request.run_id}",
        f"model={request.model}",
    ]
    channel = request.metadata.get("channel_id") if request.metadata else None
    if channel:
        lines.append(f"channel_id={channel}")
    reach = ""
    if request.metadata:
        reach = str(request.metadata.get("host_reach") or "").strip()
    if reach:
        lines.append("")
        lines.append(reach)
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


def prepend_early_job_id(command: list[str]) -> list[str]:
    if len(command) < 2 or command[1] == "--emit-job-id-early":
        return list(command)
    return [command[0], "--emit-job-id-early", *command[1:]]


def request_workdir(
    request: DispatchRequest,
    fallback: Optional[str | Path] = None,
) -> Optional[str]:
    """Honor a per-run checkout from metadata, else the backend default."""

    meta = request.metadata or {}
    raw = str(meta.get("cwd") or "").strip()
    if raw:
        return str(Path(raw).expanduser())
    if fallback:
        return str(Path(fallback))
    return None


def usable_worker_text(text: str, *, limit: int = RECEIPT_TEXT_LIMIT) -> str:
    """Keep human dialogue. Drop prompt echoes and CLI metadata."""

    parts: list[str] = []
    for line in (text or "").splitlines():
        raw = line.strip()
        if not raw:
            if parts and parts[-1] != "":
                parts.append("")
            continue
        if _is_skipped_worker_line(raw):
            continue
        parts.append(raw)
    body = "\n".join(parts).strip()
    if len(body) > limit:
        return body[: max(0, limit - 3)].rstrip() + "..."
    return body


def _is_skipped_worker_line(raw: str) -> bool:
    lower = (raw or "").strip().lower()
    if not lower:
        return True
    if "usage: puppetmaster" in lower:
        return True
    return any(lower.startswith(prefix) for prefix in _SUMMARY_SKIP_PREFIXES)


def _first_visible_summary_line(text: str) -> str:
    return usable_worker_text(text, limit=500) or "completed"


def job_show_text(cli: str, job_id: str) -> str:
    ident = (job_id or "").strip()
    if not cli or not ident:
        return ""
    try:
        proc = subprocess.run(
            [cli, "show", ident],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return ""
    return usable_worker_text(proc.stdout or "")


def _is_placeholder_summary(text: str) -> bool:
    raw = (text or "").strip().lower()
    if not raw or raw in {"completed", "ok", "done"}:
        return True
    if raw.startswith("puppetmaster job ") and raw.endswith(" completed"):
        return True
    if raw.startswith("worker finished without"):
        return True
    return False


def _completion_summary(
    safe_meta: Mapping[str, Any],
    buffer: "TokenStreamBuffer",
    cli: str,
) -> str:
    spoken_meta = usable_worker_text(str(safe_meta.get("summary") or ""))
    if spoken_meta and not _is_placeholder_summary(spoken_meta):
        return spoken_meta
    spoken_buf = usable_worker_text(buffer.text)
    if spoken_buf:
        return spoken_buf
    shown = job_show_text(cli, str(safe_meta.get("job_id") or ""))
    if shown:
        return shown
    if spoken_meta:
        return spoken_meta
    return "Worker finished without a written answer."


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
                        spoken = usable_worker_text(str(cleaned[key]))
                        if spoken:
                            meta["summary"] = spoken
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
                spoken = usable_worker_text(text)
                if spoken:
                    meta["summary"] = spoken

    raw_summary = meta.get("summary")
    if raw_summary:
        spoken = usable_worker_text(str(raw_summary))
        if spoken:
            meta["summary"] = spoken
        else:
            meta.pop("summary", None)

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


def _normalize_token_event_type(parsed: Mapping[str, Any]) -> str:
    event_type = str(parsed.get("type") or "").strip().lower()
    if event_type == "text":
        return "delta"
    if event_type in _TOKEN_EVENT_TYPES:
        return event_type
    kind = str(parsed.get("kind") or "").strip().lower()
    if kind in {"text", "token", "delta"}:
        return "delta"
    if kind in _TOKEN_EVENT_TYPES:
        return kind
    if not event_type and parsed.get("text"):
        return "delta"
    return event_type


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
    event_type = _normalize_token_event_type(parsed)
    if event_type not in _TOKEN_EVENT_TYPES:
        return None
    cleaned = strip_forbidden_keys(parsed)
    if not isinstance(cleaned, dict):
        return None
    event_type = _normalize_token_event_type(cleaned) or event_type
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
    if percent is None:
        percent = min(92.0, 10.0 + (len(state.text) * 0.04))

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
    """Prefer token/reasoning NDJSON; then progress; then visible prose."""
    token_event = _parse_token_line(line, model, buffer=buffer)
    if token_event is not None:
        return token_event
    raw = (line or "").strip()
    if not raw or _is_skipped_worker_line(raw):
        return None
    progress = _parse_progress_line(line, model)
    if progress is not None and progress.summary.stage:
        stage = _PHASE_ALIASES.get(
            progress.summary.stage.strip().lower(),
            progress.summary.stage.strip().lower(),
        )
        if stage in STREAM_PHASES:
            buffer.set_phase(stage)
        return progress
    return _prose_token_event(line, model, buffer)


def _prose_token_event(
    line: str,
    model: str,
    buffer: TokenStreamBuffer,
) -> Optional[DispatchEvent]:
    raw = (line or "").strip()
    if not raw or _is_skipped_worker_line(raw):
        return None
    if raw[:1] in "{[":
        return None
    chunk = redact_text_markers(raw)
    if not chunk.strip():
        return None
    buffer.extend(chunk + "\n")
    return DispatchEvent(
        kind=EventKind.PROGRESS,
        summary=ProgressSummary(
            stage=buffer.phase,
            message=chunk[:500],
            percent=min(92.0, 10.0 + (len(buffer.text) * 0.04)),
            details={
                "token": True,
                "stream_phase": buffer.phase,
                "token_text": buffer.text,
                "model": model,
            },
        ),
    )


def _start_delta_follower(cli: str, job_id: str, timeout_seconds: float) -> Optional[Any]:
    ident = (job_id or "").strip()
    if not cli or not ident:
        return None
    try:
        return subprocess.Popen(
            [
                cli,
                "deltas",
                ident,
                "--follow",
                "--json",
                "--follow-timeout-seconds",
                str(max(8, int(timeout_seconds))),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return None


def iter_cli_process_events(
    proc: Any,
    *,
    model: str,
    cli: str = "",
    timeout_seconds: float = 3600.0,
) -> Iterator[DispatchEvent]:
    """Read CLI stdout/stderr plus optional `deltas --follow` into live events."""

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    line_queue: queue.Queue[Any] = queue.Queue()
    main_done = 0

    def _main_reader(pipe: Any, sink: list[str]) -> None:
        try:
            if pipe is None:
                return
            for line in iter(pipe.readline, ""):
                sink.append(line)
                line_queue.put(line)
        finally:
            line_queue.put("__main_done__")

    def _follow_reader(pipe: Any) -> None:
        if pipe is None:
            return
        for line in iter(pipe.readline, ""):
            line_queue.put(line)

    for pipe, sink in ((proc.stdout, stdout_lines), (proc.stderr, stderr_lines)):
        threading.Thread(target=_main_reader, args=(pipe, sink), daemon=True).start()

    token_buffer = TokenStreamBuffer()
    follower = None
    seen_job_id = ""
    try:
        while True:
            try:
                item = line_queue.get(timeout=0.25)
            except queue.Empty:
                if proc.poll() is not None and main_done >= 2:
                    break
                continue
            if item == "__main_done__":
                main_done += 1
                if proc.poll() is not None and main_done >= 2:
                    break
                continue
            line = str(item)
            stripped = line.strip()
            if stripped.lower().startswith("job_id:") and not seen_job_id:
                seen_job_id = stripped.split(":", 1)[1].strip()
                if follower is None:
                    follower = _start_delta_follower(cli, seen_job_id, timeout_seconds)
                    if follower is not None:
                        threading.Thread(
                            target=_follow_reader,
                            args=(follower.stdout,),
                            daemon=True,
                        ).start()
            event = _event_from_cli_line(line, model, token_buffer)
            if event is not None:
                yield event
        proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        yield DispatchEvent(
            kind=EventKind.ERROR,
            summary=ProgressSummary(stage="dispatch", message="timeout"),
        )
        return
    finally:
        if follower is not None:
            try:
                follower.kill()
                follower.wait(timeout=2)
            except Exception:
                pass

    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)
    safe_meta = _parse_safe_cli_completion(stdout, stderr)
    if proc.returncode not in {0, None}:
        err = safe_meta.get("error") or stderr.strip() or f"exit {proc.returncode}"
        yield DispatchEvent(
            kind=EventKind.ERROR,
            summary=ProgressSummary(stage="dispatch", message=str(err)),
        )
        return
    summary = _completion_summary(safe_meta, token_buffer, cli)
    if isinstance(safe_meta, dict):
        safe_meta["summary"] = summary
    yield DispatchEvent(
        kind=EventKind.RECEIPT,
        summary=ProgressSummary(stage="done", message=summary, percent=100.0),
        payload=safe_meta,
    )


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
        event_type = str(parsed.get("type") or parsed.get("kind") or "").strip().lower()
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
            human = ""
            if cleaned.get("message"):
                human = str(cleaned["message"])
            elif cleaned.get("summary"):
                human = str(cleaned["summary"])
            spoken = usable_worker_text(human)
            if not spoken:
                return None
            details = {k: v for k, v in cleaned.items() if k in ALLOWED_REASONING_KEYS}
            return DispatchEvent(
                kind=EventKind.PROGRESS,
                summary=ProgressSummary(
                    stage=stage,
                    message=redact_text_markers(spoken)[:500],
                    percent=percent,
                    details=details,
                ),
            )
        return None

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
