"""Host tool catalog — CLI and HTTP, not MCP.

GrokBot's useful trick is a remote tool client on the host. Discord OS
workers already have a shell. Named binaries and HTTP recipes are the
MCP-equivalent. Custom MCPs become whatever CLI the owner already runs.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence


@dataclass(frozen=True)
class HostTool:
    name: str
    kind: str
    bin: str = ""
    url: str = ""
    hint: str = ""
    ready: bool = False


def load_host_tools(
    *,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[HostTool, ...]:
    source = dict(os.environ if env is None else env)
    found: list[HostTool] = []
    seen = set()

    def add(tool: HostTool) -> None:
        key = tool.name.lower()
        if key in seen:
            return
        seen.add(key)
        found.append(tool)

    for tool in _parse_tools_env(source.get("DISCORD_OS_TOOLS") or ""):
        add(tool)
    add(_wiki_tool(source))
    add(_bin_tool("aws", "AWS CLI. Example: aws sts get-caller-identity"))
    add(_bin_tool("gh", "GitHub CLI. Example: gh pr list --state open"))
    return tuple(item for item in found if item.name)


def tools_reach_block(tools: Sequence[HostTool] = ()) -> str:
    catalog = tuple(tools) if tools else load_host_tools()
    lines = [
        "Host tools (CLI or HTTP — not MCP inside Discord):",
        "- discord-os wiki query / recall / note from the shell.",
    ]
    ready = [item for item in catalog if item.ready]
    if not ready:
        return "\n".join(lines)
    lines.append("- Ready on this Mac:")
    for tool in ready:
        detail = tool.hint or tool.bin or tool.url
        lines.append(f"  - {tool.name} ({tool.kind}): {detail}")
    return "\n".join(lines)


def _wiki_tool(env: Mapping[str, str]) -> HostTool:
    from agent_discord.host.wiki import wiki_configured, wiki_hint

    ready = wiki_configured(env)
    return HostTool(
        name="wiki",
        kind="http",
        hint=wiki_hint(env) if ready else "Set WIKI_BASE_URL and WIKI_OWNER_TOKEN.",
        ready=ready,
    )


def _bin_tool(name: str, hint: str) -> HostTool:
    from agent_discord.host.repos import which_on_host

    path = which_on_host(name) or shutil.which(name) or ""
    return HostTool(
        name=name,
        kind="cli",
        bin=path,
        hint=hint if path else f"{name} is not on PATH.",
        ready=bool(path),
    )


def _parse_tools_env(raw: str) -> tuple[HostTool, ...]:
    text = (raw or "").strip()
    if not text:
        return ()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return ()
        if not isinstance(parsed, dict):
            return ()
        tools = []
        for name, spec in parsed.items():
            tool = _tool_from_spec(str(name), spec)
            if tool is not None:
                tools.append(tool)
        return tuple(tools)
    tools = []
    for item in text.split(","):
        name, sep, rest = item.partition(":")
        if not sep:
            continue
        tool = _tool_from_spec(name.strip(), rest.strip())
        if tool is not None:
            tools.append(tool)
    return tuple(tools)


def _tool_from_spec(name: str, spec: object) -> Optional[HostTool]:
    key = (name or "").strip().lower()
    if not key:
        return None
    if isinstance(spec, str):
        value = spec.strip()
        if value.startswith("http://") or value.startswith("https://"):
            return HostTool(name=key, kind="http", url=value, hint=value, ready=True)
        path = shutil.which(value) or value
        return HostTool(
            name=key,
            kind="cli",
            bin=path,
            hint=value,
            ready=bool(shutil.which(value) or os.path.isfile(value)),
        )
    if not isinstance(spec, dict):
        return None
    kind = str(spec.get("kind") or "").strip().lower()
    url = str(spec.get("url") or "").strip()
    binary = str(spec.get("bin") or spec.get("command") or "").strip()
    hint = str(spec.get("hint") or "").strip()
    if not kind:
        kind = "http" if url else "cli"
    ready = bool(url) or bool(shutil.which(binary) or (binary and os.path.isfile(binary)))
    return HostTool(
        name=key,
        kind=kind,
        bin=binary,
        url=url,
        hint=hint or url or binary,
        ready=ready,
    )
