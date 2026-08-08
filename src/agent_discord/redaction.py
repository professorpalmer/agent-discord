"""Recursive redaction of forbidden hidden-reasoning keys."""

from __future__ import annotations

import re
from typing import Any, Mapping

FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "chain_of_thought",
        "hidden_cot",
        "cot",
        "reasoning_content",
        "private_reasoning",
        "thinking",
        "reasoning",
    }
)
_HIDDEN_BLOCK_RE = re.compile(
    r"<(?:thinking|analysis|reasoning)>.*?</(?:thinking|analysis|reasoning)>",
    flags=re.IGNORECASE | re.DOTALL,
)
_HIDDEN_FIELD_RE = re.compile(
    r"""(?ix)
    (["']?(?:chain_of_thought|hidden_cot|cot|reasoning_content|
        private_reasoning|thinking|reasoning)["']?\s*[:=]\s*)
    (?:
        "(?:\\.|[^"])*"
        | '(?:\\.|[^'])*'
        | [^,\n}\]]+
    )
    """
)


def strip_forbidden_keys(value: Any) -> Any:
    """Recursively strip forbidden keys from mappings and walk lists/tuples."""
    if isinstance(value, Mapping):
        return {
            str(k): strip_forbidden_keys(v)
            for k, v in value.items()
            if str(k).lower() not in FORBIDDEN_PAYLOAD_KEYS
        }
    if isinstance(value, list):
        return [strip_forbidden_keys(item) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_forbidden_keys(item) for item in value)
    return value


def redact_text_markers(text: str) -> str:
    """Defense-in-depth string redaction for rendered receipts."""
    out = _HIDDEN_BLOCK_RE.sub("[redacted]", text)
    return _HIDDEN_FIELD_RE.sub("[redacted]", out)
