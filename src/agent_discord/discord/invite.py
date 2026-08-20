"""Bot invite URL. Scope is bot only — slash commands stay opt-in."""

from __future__ import annotations

# VIEW_CHANNEL | SEND_MESSAGES | MANAGE_MESSAGES | EMBED_LINKS |
# ATTACH_FILES | READ_MESSAGE_HISTORY | ADD_REACTIONS
DEFAULT_BOT_PERMISSIONS = 126016


class InviteError(ValueError):
    """Missing application id or bad invite inputs."""


def bot_invite_url(
    application_id: str,
    *,
    permissions: int = DEFAULT_BOT_PERMISSIONS,
) -> str:
    app = (application_id or "").strip()
    if not app.isdigit():
        raise InviteError("DISCORD_APPLICATION_ID must be the numeric application client id")
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={app}&permissions={int(permissions)}&scope=bot"
    )
