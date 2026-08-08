"""Safe receipt rendering for Discord — never includes hidden chain-of-thought."""

from __future__ import annotations

from agent_discord.contracts import RunReceipt, TaskStatus
from agent_discord.redaction import redact_text_markers, strip_forbidden_keys


def render_receipt(receipt: RunReceipt, *, max_progress: int = 5) -> str:
    status_icon = {
        TaskStatus.COMPLETED: "OK",
        TaskStatus.FAILED: "FAIL",
        TaskStatus.CANCELLED: "CANCELLED",
        TaskStatus.RUNNING: "RUNNING",
        TaskStatus.PROGRESS: "PROGRESS",
        TaskStatus.PENDING: "PENDING",
    }.get(receipt.status, receipt.status.value)

    summary = str(strip_forbidden_keys({"summary": receipt.summary}).get("summary", ""))
    lines = [
        f"**Receipt** `{receipt.run_id}`",
        f"Status: **{status_icon}**",
        f"Task: `{receipt.task_id}`",
        "",
        summary.strip() or "(no summary)",
    ]

    if receipt.progress:
        lines.append("")
        lines.append("Progress:")
        for item in list(receipt.progress)[-max_progress:]:
            pct = f" ({item.percent:.0f}%)" if item.percent is not None else ""
            # details may contain nested forbidden keys — never render them
            _ = strip_forbidden_keys(dict(item.details))
            lines.append(f"- [{item.stage}] {item.message}{pct}")

    if receipt.artifacts:
        lines.append("")
        lines.append("Artifacts:")
        for art in receipt.artifacts:
            safe_prov = strip_forbidden_keys(dict(art.provenance))
            _ = safe_prov  # provenance is never rendered into Discord text
            lines.append(f"- `{art.kind}` {art.path}")

    if receipt.usage:
        lines.append("")
        lines.append(
            f"Usage: model `{receipt.usage.model}` "
            f"(adapter `{receipt.usage.adapter_name}`)"
        )
        if receipt.usage.input_tokens is not None or receipt.usage.output_tokens is not None:
            lines.append(
                f"Tokens: in={receipt.usage.input_tokens!s} "
                f"out={receipt.usage.output_tokens!s}"
            )

    if receipt.error:
        lines.append("")
        lines.append(f"Error: {receipt.error}")

    return redact_text_markers("\n".join(lines))
