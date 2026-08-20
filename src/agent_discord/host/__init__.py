"""Host harness surfaces the listen machine can open. Discord is the remote."""

from __future__ import annotations

from agent_discord.host.actions import HostActionError, HostActionResult, run_host_action
from agent_discord.host.power import is_power_command, parse_power_command
from agent_discord.host.verbs import handle_open_message, is_open_command, parse_open_command

__all__ = [
    "HostActionError",
    "HostActionResult",
    "handle_open_message",
    "is_open_command",
    "is_power_command",
    "parse_open_command",
    "parse_power_command",
    "run_host_action",
]
