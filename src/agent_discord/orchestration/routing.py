"""Choose analyze vs implement before Puppetmaster dispatch."""

from __future__ import annotations


MODE_ANALYZE = "analyze"
MODE_IMPLEMENT = "implement"

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
    if any(marker in raw for marker in _IMPLEMENT_MARKERS):
        return MODE_IMPLEMENT
    tokens = raw.replace("\\", "/").split()
    looks_like_path = any("/" in token and "." in token for token in tokens)
    if looks_like_path and any(
        word in raw for word in ("edit", "fix", "add", "update", "change", "create", "write")
    ):
        return MODE_IMPLEMENT
    return MODE_ANALYZE
