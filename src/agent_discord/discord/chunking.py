"""Discord-safe message chunking (no external deps)."""

from __future__ import annotations

from typing import Sequence

# Discord message content limit
DEFAULT_LIMIT = 2000


def chunk_message(content: str, *, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Split content into Discord-safe chunks.

    Prefers paragraph, then newline, then whitespace breaks. Never returns
    empty chunks unless the input is empty (then a single empty string).
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if content == "":
        return [""]
    if len(content) <= limit:
        return [content]

    chunks: list[str] = []
    remaining = content
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        window = remaining[:limit]
        break_at = _best_break(window)
        if break_at <= 0:
            break_at = limit
        piece = remaining[:break_at].rstrip()
        if not piece:
            piece = remaining[:limit]
            break_at = len(piece)
        chunks.append(piece)
        remaining = remaining[break_at:].lstrip()
    return chunks


def _best_break(window: str) -> int:
    para = window.rfind("\n\n")
    if para > 0:
        return para + 2
    line = window.rfind("\n")
    if line > 0:
        return line + 1
    space = window.rfind(" ")
    if space > 0:
        return space + 1
    return 0


def join_chunks(chunks: Sequence[str]) -> str:
    return "\n".join(chunks)
