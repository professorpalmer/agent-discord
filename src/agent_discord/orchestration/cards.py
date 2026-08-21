"""Discord-native harness cards. Skip marker is embed footer ``Discord OS``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from agent_discord import PRODUCT_NAME
from agent_discord.contracts import RunReceipt, TaskStatus
from agent_discord.discord.layout import (
    FLAG_COMPONENTS_V2,
    STYLE_DANGER,
    STYLE_PRIMARY,
    STYLE_SUCCESS,
    action_row,
    attachment_component,
    button,
    container,
    discord_time,
    iter_component_text,
    link_button,
    progress_bar,
    section,
    status_table,
    text_display,
    thumbnail,
)
from agent_discord.host.actions import job_custom_id
from agent_discord.redaction import redact_text_markers, strip_forbidden_keys


CARD_PREFIX = "**Card**"
CARD_FOOTER = PRODUCT_NAME

COLOR_IDLE = 0x4E5058
COLOR_LIVE = 0x248046
COLOR_WORK = 0xC27C0E
COLOR_FAIL = 0xDA373C
COLOR_FILE = 0x5865F2
CODE_BODY_MAX = 1800
_TRUNCATION_NOTE = f"Truncated to {CODE_BODY_MAX} characters."

_PROVIDER_LABELS = {"openrouter": "OpenRouter"}
_SURFACE_LABELS = {
    "files": "Files",
    "terminal": "Terminal",
    "browser": "Browser",
}
_RECEIPT_TITLES = {
    TaskStatus.COMPLETED: ("Done", COLOR_LIVE),
    TaskStatus.FAILED: ("Failed", COLOR_FAIL),
    TaskStatus.CANCELLED: ("Cancelled", COLOR_IDLE),
    TaskStatus.RUNNING: ("Working", COLOR_WORK),
    TaskStatus.PROGRESS: ("Working", COLOR_WORK),
    TaskStatus.PENDING: ("Queued", COLOR_IDLE),
}


@dataclass(frozen=True)
class CardMessage:
    """One Discord V2 container. ``text`` is the MCP fallback when V2 is dropped."""

    kind: str
    title: str
    description: str = ""
    color: int = COLOR_IDLE
    fields: tuple[tuple[str, str, bool], ...] = ()
    percent: Optional[float] = None
    file_name: str = ""
    link_url: str = ""
    updated_ts: Optional[int] = None
    avatar_url: str = ""
    rows: tuple[dict[str, Any], ...] = ()

    @property
    def text(self) -> str:
        lines = [self.title]
        if self.description:
            lines.append(self.description)
        for name, value, _inline in self.fields:
            lines.append(f"{name}: {value}")
        return redact_text_markers("\n".join(lines))

    def embeds(self) -> list[dict[str, Any]]:
        embed: dict[str, Any] = {
            "title": self.title,
            "color": int(self.color),
            "footer": {"text": CARD_FOOTER},
        }
        if self.description:
            embed["description"] = self.description
        if self.fields:
            embed["fields"] = [
                {"name": name, "value": value, "inline": bool(inline)}
                for name, value, inline in self.fields
            ]
        return [embed]

    def v2_components(
        self,
        *,
        rows: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        """One Container: heading, status table, body, file, buttons."""

        heading = f"### {self.title}"
        if self.avatar_url:
            children: list[dict[str, Any]] = [
                section([heading], thumbnail(self.avatar_url))
            ]
        else:
            children = [text_display(heading)]
        if self.kind == "HOST":
            live = self.title == "Running"
            table = [
                ("power", "on" if live else "off"),
                ("listen", "live" if live else "idle"),
            ]
            table.extend((name, value) for name, value, _inline in self.fields)
            children.append(text_display(status_table(table)))
        body_parts: list[str] = []
        if self.description:
            body_parts.append(self.description)
        if self.percent is not None:
            body_parts.append(f"`{progress_bar(self.percent)}`")
        if self.fields and self.kind != "HOST":
            body_parts.append(
                "\n".join(f"`{name}`  {value}" for name, value, _inline in self.fields)
            )
        stamp = discord_time(self.updated_ts)
        body_parts.append(f"-# {CARD_FOOTER}  ·  {stamp}")
        children.append(text_display(redact_text_markers("\n\n".join(body_parts))))
        if self.file_name:
            children.append(attachment_component(self.file_name))
        extra = list(self.rows)
        extra.extend(rows or [])
        if self.link_url:
            extra.append(action_row([link_button("Open", self.link_url)]))
        if extra:
            children.extend(extra)
        return [container(children, color=self.color)]

    def v2_payload(
        self,
        *,
        rows: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        return {
            "flags": FLAG_COMPONENTS_V2,
            "components": self.v2_components(rows=rows),
        }


def is_harness_card(content: str) -> bool:
    text = (content or "").strip()
    return text.startswith(CARD_PREFIX) or text.startswith("**Receipt**")


def is_harness_message(
    content: str,
    embeds: Optional[Sequence[MappingLike]] = None,
    components: Optional[Sequence[MappingLike]] = None,
) -> bool:
    if is_harness_card(content):
        return True
    for embed in embeds or ():
        if not isinstance(embed, dict):
            continue
        footer = embed.get("footer")
        text = ""
        if isinstance(footer, dict):
            text = str(footer.get("text") or "")
        if text == CARD_FOOTER or text.startswith(f"{CARD_FOOTER} "):
            return True
    for text in iter_component_text(components):
        if CARD_FOOTER in text or text.startswith(CARD_PREFIX):
            return True
    return False


def render_connect_card(
    *,
    provider: str,
    fingerprint: str,
    source: str,
    ticket: str = "",
    error: str = "",
) -> str:
    return connect_card(
        provider=provider,
        fingerprint=fingerprint,
        source=source,
        ticket=ticket,
        error=error,
    ).text


def connect_card(
    *,
    provider: str,
    fingerprint: str = "",
    source: str = "",
    ticket: str = "",
    error: str = "",
) -> CardMessage:
    _ = fingerprint
    label = _PROVIDER_LABELS.get(provider, provider or "OpenRouter")
    if error:
        return CardMessage(
            kind="CONNECT",
            title="Connect failed",
            description=redact_text_markers(error),
            color=COLOR_FAIL,
        )
    if ticket:
        return CardMessage(
            kind="CONNECT",
            title="Finish on the host",
            description=(
                f"Run this on the machine, then paste the {label} key on stdin.\n"
                f"discord-os connect --ticket {ticket} --provider {provider}\n"
                "Expires in 15 minutes."
            ),
            color=COLOR_IDLE,
        )
    detail = "from this host" if source == "env" else "on this host"
    return CardMessage(
        kind="CONNECT",
        title="Connected",
        description=f"{label} is ready {detail}.",
        color=COLOR_LIVE,
    )


def render_progress_card(
    *,
    stage: str,
    message: str,
    percent: Optional[float] = None,
    run_id: str = "",
) -> str:
    return progress_card(
        stage=stage, message=message, percent=percent, run_id=run_id
    ).text


def progress_card(
    *,
    stage: str,
    message: str,
    percent: Optional[float] = None,
    run_id: str = "",
    actions: str = "running",
) -> CardMessage:
    title = _title_case(stage) or "Working"
    body = redact_text_markers(message or "")
    return CardMessage(
        kind="PROGRESS",
        title=title,
        description=body,
        color=COLOR_WORK,
        percent=percent,
        rows=_job_rows(run_id, actions),
    )


def working_card(
    *,
    task_label: str = "",
    message: str = "",
    percent: Optional[float] = None,
    run_id: str = "",
    actions: str = "running",
) -> CardMessage:
    title = _title_case(task_label) or "Working"
    return CardMessage(
        kind="WORKING",
        title=title,
        description=redact_text_markers(message or ""),
        color=COLOR_WORK,
        percent=percent,
        rows=_job_rows(run_id, actions),
    )


def code_card(language: str, code: str, title: str = "Code") -> CardMessage:
    return CardMessage(
        kind="CODE",
        title=title or "Code",
        description=_bounded_fence(language, code),
        color=COLOR_FILE,
    )


def diff_card(
    diff_text: str,
    filename: str = "patch.diff",
    title: str = "Diff",
) -> CardMessage:
    heading = f"`{filename}`" if filename else ""
    body = _bounded_fence("diff", diff_text)
    description = f"{heading}\n{body}" if heading else body
    return CardMessage(
        kind="DIFF",
        title=title or "Diff",
        description=description,
        color=COLOR_FILE,
    )


def job_action_row(run_id: str, *, actions: str = "parked") -> dict[str, Any]:
    rid = (run_id or "").strip()
    mode = (actions or "parked").strip().lower()
    if mode == "running":
        items = [button("Cancel", job_custom_id("cancel", rid), style=STYLE_DANGER)]
    elif mode == "done":
        items = [button("Retry", job_custom_id("retry", rid), style=STYLE_PRIMARY)]
    elif mode == "all":
        items = [
            button("Approve", job_custom_id("approve", rid), style=STYLE_SUCCESS),
            button("Cancel", job_custom_id("cancel", rid), style=STYLE_DANGER),
            button("Retry", job_custom_id("retry", rid), style=STYLE_PRIMARY),
        ]
    else:
        items = [
            button("Approve", job_custom_id("approve", rid), style=STYLE_SUCCESS),
            button("Cancel", job_custom_id("cancel", rid), style=STYLE_DANGER),
        ]
    return action_row(items)


def render_receipt_card(receipt: RunReceipt, *, max_progress: int = 5) -> str:
    return receipt_card(receipt, max_progress=max_progress).text


def receipt_card(receipt: RunReceipt, *, max_progress: int = 5) -> CardMessage:
    title, color = _RECEIPT_TITLES.get(receipt.status, ("Receipt", COLOR_IDLE))
    summary = str(strip_forbidden_keys({"summary": receipt.summary}).get("summary", ""))
    description = redact_text_markers(summary.strip() or title)
    fields: list[tuple[str, str, bool]] = []
    jump = ""
    if receipt.artifacts:
        lines = []
        for art in receipt.artifacts:
            _ = strip_forbidden_keys(dict(art.provenance))
            obj = art.as_object_ref()
            if obj is not None:
                from agent_discord.contracts import discord_jump_url

                if not jump:
                    jump = discord_jump_url(
                        obj.guild_id, obj.channel_id, obj.message_id
                    )
                lines.append(obj.filename or art.kind)
            elif art.path:
                lines.append(art.path)
            else:
                lines.append(art.kind)
        if lines:
            fields.append(("Files", "\n".join(lines[:8]), False))
    if receipt.usage and receipt.usage.model:
        fields.append(("Model", receipt.usage.model, True))
    if receipt.error:
        fields.append(("Error", redact_text_markers(receipt.error)[:1024], False))
    _ = max_progress
    return CardMessage(
        kind="RECEIPT",
        title=title,
        description=description,
        color=color,
        fields=tuple(fields),
        link_url=jump,
        rows=_job_rows(receipt.run_id, "done"),
    )


def render_open_card(
    *,
    surface: str,
    target: str,
    error: str = "",
) -> str:
    return open_card(surface=surface, target=target, error=error).text


def open_card(*, surface: str, target: str, error: str = "") -> CardMessage:
    label = _SURFACE_LABELS.get(surface, surface or "Host")
    if error:
        return CardMessage(
            kind="OPEN",
            title="Could not open",
            description=redact_text_markers(error),
            color=COLOR_FAIL,
        )
    detail = target if surface == "browser" and target else label
    return CardMessage(
        kind="OPEN",
        title="Opened",
        description=detail,
        color=COLOR_LIVE,
    )


def render_host_card(
    *,
    armed: bool,
    channel_id: str = "",
) -> str:
    return host_card(armed=armed, channel_id=channel_id).text


def host_card(
    *,
    armed: bool,
    channel_id: str = "",
    confirm_off: bool = False,
    avatar_url: str = "",
    spend_usd: float = 0.0,
    cap_usd: Optional[float] = None,
    halted: bool = False,
    paired: bool = False,
    operator_count: int = 0,
    role_count: int = 0,
    last_job: str = "",
    write_gate: bool = False,
    realm: str = "",
    bank: bool = False,
    github: str = "",
) -> CardMessage:
    _ = channel_id
    fields = _host_status_fields(
        paired=paired,
        operator_count=operator_count,
        role_count=role_count,
        write_gate=write_gate,
        spend_usd=spend_usd,
        cap_usd=cap_usd,
        halted=halted,
        realm=realm,
        bank=bank,
        github=github,
    )
    if confirm_off:
        return CardMessage(
            kind="HOST",
            title="Stop?",
            description="Confirm to stop. Cancel keeps it running.",
            color=COLOR_WORK,
            avatar_url=avatar_url,
            fields=fields,
        )
    return CardMessage(
        kind="HOST",
        title="Halted" if halted and armed else ("Running" if armed else "Stopped"),
        description=last_job.strip(),
        color=COLOR_FAIL if halted and armed else (COLOR_LIVE if armed else COLOR_IDLE),
        avatar_url=avatar_url,
        fields=fields,
    )


def _host_status_fields(
    *,
    paired: bool,
    operator_count: int,
    role_count: int,
    write_gate: bool,
    spend_usd: float,
    cap_usd: Optional[float],
    halted: bool,
    realm: str,
    bank: bool,
    github: str = "",
) -> tuple[tuple[str, str, bool], ...]:
    from agent_discord.orchestration.service import format_usd

    acl = "open"
    if paired:
        ops = max(0, int(operator_count))
        roles = max(0, int(role_count))
        op_word = "op" if ops == 1 else "ops"
        role_word = "role" if roles == 1 else "roles"
        acl = f"paired · {ops} {op_word} · {roles} {role_word}"
    rows: list[tuple[str, str, bool]] = [
        ("acl", acl, True),
        ("writes", "gate" if write_gate else "auto", True),
    ]
    spend = format_usd(spend_usd)
    if cap_usd is not None:
        spend = f"{spend} / {format_usd(cap_usd)}"
    if halted:
        spend = f"{spend} halt"
    rows.append(("spend", spend, True))
    name = (realm or "").strip()
    if name:
        rows.append(("realm", name, True))
    if bank:
        rows.append(("bank", "yes", True))
    if github:
        rows.append(("github", github, True))
    return tuple(rows)


def note_card(text: str, *, source_channel: str = "") -> CardMessage:
    body = (text or "").strip()
    if source_channel:
        body = f"From #{source_channel}\n{body}"
    return CardMessage(
        kind="NOTE",
        title="Note",
        description=body or "Empty note.",
        color=COLOR_IDLE,
    )


def render_overflow_card(
    *,
    filename: str,
    sha256: str,
    size: int,
    jump_url: str,
    local_stash: str = "",
) -> str:
    return overflow_card(
        filename=filename,
        sha256=sha256,
        size=size,
        jump_url=jump_url,
        local_stash=local_stash,
    ).text


def overflow_card(
    *,
    filename: str,
    sha256: str,
    size: int,
    jump_url: str,
    local_stash: str = "",
) -> CardMessage:
    _ = sha256
    fields: list[tuple[str, str, bool]] = [("Size", format_size(size), True)]
    if local_stash:
        fields.append(("Host copy", local_stash, False))
    return CardMessage(
        kind="OVERFLOW",
        title="Too large for Discord",
        description=filename,
        color=COLOR_FAIL,
        fields=tuple(fields),
        link_url=jump_url,
    )


def object_card(*, filename: str, size: int, kind: str = "blob") -> CardMessage:
    label = "Overflow pointer" if kind == "overflow" else filename
    return CardMessage(
        kind="OBJECT",
        title=label,
        description=format_size(size),
        color=COLOR_FILE,
        file_name="" if kind == "overflow" else filename,
    )


def format_size(size: int) -> str:
    n = max(0, int(size))
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        value = n / 1024
        return f"{value:.1f} KB".replace(".0 KB", " KB")
    value = n / (1024 * 1024)
    return f"{value:.1f} MB".replace(".0 MB", " MB")


def send_card(
    discord: Any,
    channel_id: str,
    card: CardMessage,
    *,
    thread_id: Optional[str] = None,
    components: Optional[list[dict[str, Any]]] = None,
) -> Any:
    poster = getattr(discord, "send_message", None)
    if not callable(poster):
        return None
    payload = card.v2_payload(rows=components)
    try:
        return poster(
            channel_id,
            "",
            thread_id=thread_id,
            components=payload["components"],
            flags=payload["flags"],
        )
    except TypeError:
        try:
            return poster(
                channel_id,
                "",
                thread_id=thread_id,
                embeds=card.embeds(),
                components=components,
            )
        except TypeError:
            try:
                return poster(
                    channel_id, card.text, thread_id=thread_id, components=components
                )
            except TypeError:
                return poster(channel_id, card.text, thread_id=thread_id)


def edit_card(
    discord: Any,
    channel_id: str,
    message_id: str,
    card: CardMessage,
    *,
    components: Optional[list[dict[str, Any]]] = None,
) -> Any:
    editor = getattr(discord, "edit_message", None)
    if not callable(editor):
        raise TypeError("edit_message is not available")
    payload = card.v2_payload(rows=components)
    try:
        return editor(
            channel_id,
            message_id,
            "",
            components=payload["components"],
            flags=payload["flags"],
        )
    except TypeError:
        try:
            return editor(
                channel_id,
                message_id,
                "",
                embeds=card.embeds(),
                components=components,
            )
        except TypeError:
            if components is not None:
                try:
                    return editor(
                        channel_id, message_id, card.text, components=components
                    )
                except TypeError:
                    pass
            return editor(channel_id, message_id, card.text)


def _title_case(stage: str) -> str:
    raw = (stage or "").replace("_", " ").strip()
    return raw[:1].upper() + raw[1:] if raw else ""


def _job_rows(run_id: str, actions: str = "running") -> tuple[dict[str, Any], ...]:
    if not (run_id or "").strip():
        return ()
    return (job_action_row(run_id, actions=actions),)


def _fence_language(language: str) -> str:
    return "".join((language or "").split()).replace("`", "")[:32]


def _bounded_fence(language: str, source: str) -> str:
    cleaned = redact_text_markers(source or "")
    truncated = len(cleaned) > CODE_BODY_MAX
    if truncated:
        cleaned = cleaned[:CODE_BODY_MAX]
    fence = "```"
    while fence in cleaned:
        fence += "`"
    lang = _fence_language(language)
    opener = f"{fence}{lang}" if lang else fence
    block = f"{opener}\n{cleaned}\n{fence}"
    if truncated:
        return f"{block}\n{_TRUNCATION_NOTE}"
    return block


MappingLike = dict[str, Any]
