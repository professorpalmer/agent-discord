"""Background job pool: one OS thread per ask.

Hermes Bot Mode's useful trick here is persist-then-settle — accept the
message, keep the listen loop moving, paint the receipt on the origin
channel when the worker finishes. Same machine, not isolated VMs.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from agent_discord.contracts import RunReceipt, TaskIntake
from agent_discord.orchestration.routing import MODE_IMPLEMENT, compute_dispatch_mode


DEFAULT_MAX_LIVE = 8


class JobPool:
    """Run ``run_task`` off the listen loop. Implement writes serialize per realm."""

    def __init__(self, *, max_live: int = DEFAULT_MAX_LIVE) -> None:
        self.max_live = max(1, int(max_live))
        self._lock = threading.Lock()
        self._live: dict[str, threading.Thread] = {}
        self._write_locks: dict[str, threading.Lock] = {}
        self._done: list[RunReceipt] = []
        self._seq = 0

    def submit(
        self,
        runner: Callable[[TaskIntake], RunReceipt],
        intake: TaskIntake,
        *,
        write_key: str = "",
    ) -> str:
        with self._lock:
            self._seq += 1
            job_id = f"job-{self._seq}"
        lock_name = write_key if _needs_write_lock(intake) else ""

        def run() -> None:
            try:
                if lock_name:
                    with self._write_lock(lock_name):
                        receipt = runner(intake)
                else:
                    receipt = runner(intake)
                with self._lock:
                    self._done.append(receipt)
            finally:
                with self._lock:
                    self._live.pop(job_id, None)

        thread = threading.Thread(
            target=run,
            name=f"discord-os-{job_id}",
            daemon=True,
        )
        self._wait_for_slot()
        with self._lock:
            self._live[job_id] = thread
        thread.start()
        return job_id

    def reap(self) -> list[RunReceipt]:
        with self._lock:
            done = list(self._done)
            self._done.clear()
        return done

    def live_count(self) -> int:
        with self._lock:
            return len(self._live)

    def wait(self, timeout: Optional[float] = None) -> list[RunReceipt]:
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while True:
            with self._lock:
                threads = list(self._live.values())
            if not threads:
                return self.reap()
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self.reap()
            for thread in threads:
                thread.join(timeout=0.05 if remaining is None else min(0.05, remaining))
            if deadline is not None and time.monotonic() >= deadline:
                return self.reap()

    def _wait_for_slot(self) -> None:
        while True:
            with self._lock:
                if len(self._live) < self.max_live:
                    return
            time.sleep(0.05)

    def _write_lock(self, key: str) -> threading.Lock:
        with self._lock:
            held = self._write_locks.get(key)
            if held is None:
                held = threading.Lock()
                self._write_locks[key] = held
            return held


def _needs_write_lock(intake: TaskIntake) -> bool:
    return compute_dispatch_mode(intake.text) == MODE_IMPLEMENT


def realm_write_key(intake: TaskIntake, cwd: str = "") -> str:
    path = (cwd or "").strip()
    if path:
        return path
    return str(intake.channel_id or "channel")
