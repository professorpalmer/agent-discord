"""Puppetmaster agentic backend — OpenRouter/BYOK via subprocess env only."""

from __future__ import annotations

import os
import queue
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
from agent_discord.keys.vault import KeyVault
from agent_discord.puppetmaster.backend import (
    TokenStreamBuffer,
    _event_from_cli_line,
    _parse_safe_cli_completion,
    _safe_dispatch_prompt,
)
from agent_discord.puppetmaster.models import AGENTIC_MODEL_PIN


@dataclass
class AgenticPuppetmasterBackend:
    """Boundary for ``puppetmaster agentic`` with OpenRouter injected in env.

    The API key is never placed on argv and is never logged. Model resolution
    is exact-allowlist only (``openrouter/auto``); no silent fallback.
    """

    cli: str = "puppetmaster"
    pin: ModelPin = field(default_factory=lambda: AGENTIC_MODEL_PIN)
    cwd: Optional[str | Path] = None
    timeout_seconds: float = 3600.0
    vault: Optional[KeyVault] = None
    env: Optional[Mapping[str, str]] = None
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
        workdir = str(self.cwd) if self.cwd else None
        mode = str((request.metadata or {}).get("compute_mode") or "implement")
        if mode not in {"implement", "analyze"}:
            mode = "implement"
        is_git = bool(workdir) and (Path(workdir) / ".git").exists()
        command = [
            self.cli,
            "agentic",
            prompt,
            "--provider",
            "openrouter",
            "--model",
            pin.adapter_name,
            "--mode",
            mode,
            "--timeout-seconds",
            str(int(self.timeout_seconds)),
        ]
        if mode == "implement":
            command.append("--allow-dirty")
            if not is_git:
                command.append("--allow-non-worktree")
        elif not is_git:
            command.extend(["--allow-non-worktree", "--disable-codegraph"])
        if workdir:
            command.extend(["--cwd", workdir])

        child_env = dict(self.env) if self.env is not None else dict(os.environ)
        secret = self._resolve_secret()
        if secret:
            child_env["OPENROUTER_API_KEY"] = secret

        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                cwd=workdir,
                env=child_env,
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
                        message=f"dispatched via agentic with {pin.adapter_name}",
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
                    "backend": "agentic",
                    "cli": self.cli,
                    "cli_model": pin.adapter_name,
                    "job_id": safe_meta.get("job_id"),
                },
            ),
        )

    def cancel(self, run_id: str) -> bool:
        return False

    def status(self, run_id: str) -> TaskStatus:
        return self._statuses.get(run_id, TaskStatus.PENDING)

    def stream(self, request: DispatchRequest) -> Iterator[DispatchEvent]:
        """Stream agentic dispatch progress live instead of blocking until completion."""
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
        workdir = str(self.cwd) if self.cwd else None
        mode = str((request.metadata or {}).get("compute_mode") or "implement")
        if mode not in {"implement", "analyze"}:
            mode = "implement"
        is_git = bool(workdir) and (Path(workdir) / ".git").exists()
        command = [
            self.cli,
            "agentic",
            prompt,
            "--provider",
            "openrouter",
            "--model",
            pin.adapter_name,
            "--mode",
            mode,
            "--timeout-seconds",
            str(int(self.timeout_seconds)),
        ]
        if mode == "implement":
            command.append("--allow-dirty")
            if not is_git:
                command.append("--allow-non-worktree")
        elif not is_git:
            command.extend(["--allow-non-worktree", "--disable-codegraph"])
        if workdir:
            command.extend(["--cwd", workdir])
        command.append("--json-lines")

        child_env = dict(self.env) if self.env is not None else dict(os.environ)
        secret = self._resolve_secret()
        if secret:
            child_env["OPENROUTER_API_KEY"] = secret

        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=workdir,
                env=child_env,
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
                message=f"dispatched via agentic with {pin.adapter_name}",
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

    def _resolve_secret(self) -> str:
        if self.vault is not None:
            stored = self.vault.get("openrouter")
            if stored:
                return stored
        source = self.env if self.env is not None else os.environ
        return (source.get("OPENROUTER_API_KEY") or "").strip()
