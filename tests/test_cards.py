"""Discord V2 containers — dashboard panel, not a **Card** dump."""

from __future__ import annotations

from agent_discord.discord.layout import (
    FLAG_COMPONENTS_V2,
    TYPE_CONTAINER,
    TYPE_FILE,
    TYPE_MEDIA_GALLERY,
    TYPE_SECTION,
    TYPE_THUMBNAIL,
    iter_component_text,
    progress_bar,
    status_table,
)
from agent_discord.orchestration.cards import (
    CARD_FOOTER,
    COLOR_IDLE,
    COLOR_LIVE,
    connect_card,
    host_card,
    object_card,
    progress_card,
)


def _joined(card) -> str:
    return "\n".join(iter_component_text(card.v2_components()))


def test_host_card_is_a_v2_panel():
    stopped = host_card(armed=False, channel_id="1523512830907912363")
    assert stopped.title == "Stopped"
    assert "1523512830907912363" not in stopped.text
    assert "**Card**" not in stopped.text
    payload = stopped.v2_payload()
    assert payload["flags"] == FLAG_COMPONENTS_V2
    assert payload["components"][0]["type"] == TYPE_CONTAINER
    body = _joined(stopped)
    assert "### Stopped" in body
    assert "power" in body
    assert "off" in body
    assert "listen" in body
    assert "idle" in body
    assert "<t:" in body
    assert CARD_FOOTER in body
    running = host_card(armed=True)
    assert running.title == "Running"
    assert running.v2_components()[0]["accent_color"] == COLOR_LIVE
    assert COLOR_IDLE == stopped.v2_components()[0]["accent_color"]
    with_face = host_card(
        armed=True,
        avatar_url="https://cdn.discordapp.com/avatars/1/hash.png",
    )
    first = with_face.v2_components()[0]["components"][0]
    assert first["type"] == TYPE_SECTION
    assert first["accessory"]["type"] == TYPE_THUMBNAIL


def test_connect_card_hides_fingerprint():
    card = connect_card(provider="openrouter", fingerprint="aa57", source="env")
    assert card.title == "Connected"
    assert "aa57" not in card.text
    assert "Fingerprint" not in card.text


def test_object_card_is_filename_not_json():
    card = object_card(filename="agent-discord-os-probe.txt", size=29)
    assert card.title == "agent-discord-os-probe.txt"
    assert card.description == "29 B"
    assert "agent_discord_object" not in card.text
    kinds = [child["type"] for child in card.v2_components()[0]["components"]]
    assert TYPE_FILE in kinds


def test_progress_card_uses_a_meter():
    card = progress_card(stage="work", message="card edited", percent=100, run_id="live-card")
    assert card.title == "Work"
    assert card.percent == 100
    assert "live-card" not in card.text
    assert "[============] 100%" in _joined(card)
    assert progress_bar(50) == "[======......] 50%"
    assert "```" in status_table((("power", "off"),))


def test_image_object_uses_media_gallery():
    card = object_card(filename="shot.png", size=2048)
    kinds = [child["type"] for child in card.v2_components()[0]["components"]]
    assert TYPE_MEDIA_GALLERY in kinds
    assert TYPE_FILE not in kinds
