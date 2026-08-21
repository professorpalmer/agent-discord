"""Choose analyze vs implement before Puppetmaster dispatch."""

from __future__ import annotations

from typing import Optional


MODE_ANALYZE = "analyze"
MODE_IMPLEMENT = "implement"
MODE_SWARM = "swarm"

_SWARM_MARKERS = (
    "swarm",
    "audit this",
    "audit the",
    "fan out",
    "multi-worker",
    "multi worker",
    "several workers",
)

_IMPLEMENT_MARKERS = (
    "implement",
    "fix the",
    "fix this",
    "patch ",
    "edit ",
    "write ",
    "create file",
    "add test",
    "add tests",
    "refactor",
    "rename ",
    "delete file",
    "apply ",
    "ship ",
    "commit ",
)


def compute_dispatch_mode(text: str) -> str:
    """Questions stay read-only. File/code work gets a write worker."""

    raw = (text or "").strip().lower()
    if not raw:
        return MODE_ANALYZE
    if swarm_worker_count(text):
        return MODE_SWARM
    if any(marker in raw for marker in _IMPLEMENT_MARKERS):
        return MODE_IMPLEMENT
    tokens = raw.replace("\\", "/").split()
    looks_like_path = any("/" in token and "." in token for token in tokens)
    if looks_like_path and any(
        word in raw for word in ("edit", "fix", "add", "update", "change", "create", "write")
    ):
        return MODE_IMPLEMENT
    return MODE_ANALYZE


def swarm_worker_count(text: str, requested: Optional[int] = None) -> int:
    """Return worker count for a swarm ask, else 0.

    Explicit ``requested`` wins when greater than 1. Bare "swarm" / "audit this"
    defaults to 3, capped at 5.
    """

    if requested is not None:
        try:
            n = int(requested)
        except (TypeError, ValueError):
            n = 0
        if n > 1:
            return max(2, min(n, 5))
    raw = (text or "").strip().lower()
    if not raw:
        return 0
    if not any(marker in raw for marker in _SWARM_MARKERS):
        return 0
    for token in raw.replace(",", " ").split():
        if token.isdigit():
            n = int(token)
            if 2 <= n <= 8:
                return max(2, min(n, 5))
    return 3
