"""Headless host process: pidfile + detach. Discord /on /off is power, not this."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


PID_NAME = "host.pid"
META_NAME = "host.json"


def host_pid_path(workspace: Path) -> Path:
    return Path(workspace) / PID_NAME


def host_meta_path(workspace: Path) -> Path:
    return Path(workspace) / META_NAME


def host_log_path(workspace: Path) -> Path:
    return Path(workspace) / "logs" / "host.log"


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_host_meta(workspace: Path) -> dict[str, Any]:
    path = host_meta_path(workspace)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_host_meta(workspace: Path, *, pid: int, channel_id: str) -> None:
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    payload = {"pid": int(pid), "channel_id": str(channel_id)}
    host_meta_path(workspace).write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    host_pid_path(workspace).write_text(f"{int(pid)}\n", encoding="utf-8")


def clear_host_meta(workspace: Path) -> None:
    for path in (host_pid_path(workspace), host_meta_path(workspace)):
        try:
            path.unlink()
        except OSError:
            pass


def running_host_pid(workspace: Path) -> Optional[int]:
    meta = read_host_meta(workspace)
    raw = meta.get("pid")
    if raw is None:
        text = ""
        try:
            text = host_pid_path(workspace).read_text(encoding="utf-8").strip()
        except OSError:
            return None
        raw = text.splitlines()[0] if text else ""
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    if not pid_is_alive(pid):
        return None
    return pid


def stop_host(workspace: Path) -> Optional[int]:
    pid = running_host_pid(workspace)
    if pid is None:
        clear_host_meta(workspace)
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        clear_host_meta(workspace)
        return None
    clear_host_meta(workspace)
    return pid


def start_detached(
    argv: Sequence[str],
    *,
    workspace: Path,
    channel_id: str,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    """Spawn a detached child. Caller must refuse when a live host already exists."""

    workspace = Path(workspace)
    (workspace / "logs").mkdir(parents=True, exist_ok=True)
    log_path = host_log_path(workspace)
    handle = open(log_path, "ab")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": handle,
        "stderr": handle,
        "close_fds": True,
    }
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    if env is not None:
        kwargs["env"] = dict(env)
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(list(argv), **kwargs)
    finally:
        handle.close()
    write_host_meta(workspace, pid=int(proc.pid), channel_id=channel_id)
    return int(proc.pid)


def host_run_argv(channel_id: str, *, extra: Optional[Sequence[str]] = None) -> list[str]:
    argv = [sys.executable, "-m", "agent_discord", "host", "run", "--channel-id", channel_id]
    if extra:
        argv.extend(extra)
    return argv
