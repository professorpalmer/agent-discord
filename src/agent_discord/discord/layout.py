"""Discord Components V2 layout — the in-channel ceiling, not a TUI.

Container + Section + Text + Separator + File/Gallery + buttons.
Edit the same message to update it.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional, Sequence


FLAG_COMPONENTS_V2 = 1 << 15

TYPE_ACTION_ROW = 1
TYPE_BUTTON = 2
TYPE_STRING_SELECT = 3
TYPE_SECTION = 9
TYPE_TEXT = 10
TYPE_THUMBNAIL = 11
TYPE_MEDIA_GALLERY = 12
TYPE_FILE = 13
TYPE_SEPARATOR = 14
TYPE_CONTAINER = 17

STYLE_PRIMARY = 1
STYLE_SECONDARY = 2
STYLE_SUCCESS = 3
STYLE_DANGER = 4
STYLE_LINK = 5

ACTIVITY_WATCHING = 3
ACTIVITY_NAME_MAX = 128
CUSTOM_ID_MAX = 100
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def text_display(content: str) -> dict[str, Any]:
    return {"type": TYPE_TEXT, "content": content}


def separator(*, divider: bool = True, spacing: int = 1) -> dict[str, Any]:
    return {"type": TYPE_SEPARATOR, "divider": bool(divider), "spacing": int(spacing)}


def file_ref(filename: str) -> dict[str, Any]:
    name = _basename(filename)
    return {"type": TYPE_FILE, "file": {"url": f"attachment://{name}"}}


def media_gallery(filename: str) -> dict[str, Any]:
    name = _basename(filename)
    return {
        "type": TYPE_MEDIA_GALLERY,
        "items": [{"media": {"url": f"attachment://{name}"}}],
    }


def is_image_name(filename: str) -> bool:
    lower = _basename(filename).lower()
    return any(lower.endswith(suffix) for suffix in IMAGE_SUFFIXES)


def attachment_component(filename: str) -> dict[str, Any]:
    if is_image_name(filename):
        return media_gallery(filename)
    return file_ref(filename)


def section(texts: Sequence[str], accessory: dict[str, Any]) -> dict[str, Any]:
    children = [text_display(item) for item in texts if item][:3]
    return {"type": TYPE_SECTION, "components": children, "accessory": accessory}


def action_row(components: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {"type": TYPE_ACTION_ROW, "components": list(components)[:5]}


def button(
    label: str,
    custom_id: str,
    *,
    style: int = STYLE_SECONDARY,
    disabled: bool = False,
) -> dict[str, Any]:
    return {
        "type": TYPE_BUTTON,
        "style": int(style),
        "label": (label or "Button")[:80],
        "custom_id": (custom_id or "")[:CUSTOM_ID_MAX],
        "disabled": bool(disabled),
    }


def thumbnail(url: str, *, description: str = "Discord OS") -> dict[str, Any]:
    return {
        "type": TYPE_THUMBNAIL,
        "media": {"url": url},
        "description": (description or "Discord OS")[:256],
    }


def string_select(
    custom_id: str,
    options: Sequence[dict[str, str]],
    *,
    placeholder: str = "Recent jobs",
) -> dict[str, Any]:
    items = []
    for option in options[:25]:
        label = str(option.get("label") or "job")[:100]
        value = str(option.get("value") or "")[:100]
        if not value:
            continue
        item = {"label": label, "value": value}
        desc = str(option.get("description") or "").strip()
        if desc:
            item["description"] = desc[:100]
        items.append(item)
    return {
        "type": TYPE_STRING_SELECT,
        "custom_id": custom_id,
        "placeholder": (placeholder or "Recent jobs")[:150],
        "min_values": 1,
        "max_values": 1,
        "options": items,
    }


def link_button(label: str, url: str) -> dict[str, Any]:
    return {
        "type": TYPE_BUTTON,
        "style": STYLE_LINK,
        "label": (label or "Open")[:80],
        "url": url,
    }


def container(
    children: list[dict[str, Any]],
    *,
    color: int,
) -> dict[str, Any]:
    return {
        "type": TYPE_CONTAINER,
        "accent_color": int(color),
        "components": list(children),
    }


def status_table(rows: Sequence[tuple[str, str]]) -> str:
    pairs = [(str(key), str(value)) for key, value in rows if key]
    if not pairs:
        return ""
    width = max(len(key) for key, _value in pairs)
    lines = [f"{key.ljust(width)}  {value}" for key, value in pairs]
    return "```\n" + "\n".join(lines) + "\n```"


def discord_time(ts: Optional[int] = None) -> str:
    when = int(ts if ts is not None else time.time())
    return f"<t:{when}:R>"


def presence_update(*, status: str, name: str) -> dict[str, Any]:
    return {
        "op": 3,
        "d": {
            "since": None,
            "activities": [{"name": name, "type": ACTIVITY_WATCHING}],
            "status": status,
            "afk": False,
        },
    }


def working_presence(task_label: str) -> dict[str, Any]:
    """Gateway-ready presence payload. Does not send on the wire."""

    label = " ".join((task_label or "").split()) or "a task"
    name = f"Working on {label}"
    if len(name) > ACTIVITY_NAME_MAX:
        name = name[:ACTIVITY_NAME_MAX]
    return presence_update(status="dnd", name=name)


def _basename(filename: str) -> str:
    return (filename or "object.bin").replace("\\", "/").rsplit("/", 1)[-1]


def progress_bar(percent: float, *, width: int = 12) -> str:
    """TUI-adjacent meter. ASCII only — Discord markdown, not a terminal."""

    try:
        value = float(percent)
    except (TypeError, ValueError):
        value = 0.0
    value = max(0.0, min(100.0, value))
    filled = int(round(width * value / 100.0))
    filled = max(0, min(width, filled))
    return f"[{'=' * filled}{'.' * (width - filled)}] {value:.0f}%"


def iter_component_text(components: Optional[Iterable[Any]]) -> list[str]:
    found: list[str] = []
    for item in components or ():
        if not isinstance(item, dict):
            continue
        if item.get("type") == TYPE_TEXT:
            text = str(item.get("content") or "")
            if text:
                found.append(text)
        nested = item.get("components")
        if isinstance(nested, list):
            found.extend(iter_component_text(nested))
    return found
