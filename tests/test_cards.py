"""Discord V2 containers — dashboard panel, not a **Card** dump."""

from __future__ import annotations

from agent_discord.discord.layout import (
    ACTIVITY_NAME_MAX,
    CUSTOM_ID_MAX,
    FLAG_COMPONENTS_V2,
    TYPE_ACTION_ROW,
    TYPE_CONTAINER,
    TYPE_FILE,
    TYPE_MEDIA_GALLERY,
    TYPE_SECTION,
    TYPE_THUMBNAIL,
    iter_component_text,
    progress_bar,
    status_table,
    working_presence,
)
from agent_discord.host.panel import ASK_ID, OFF_ID, ON_ID
from agent_discord.orchestration.cards import (
    CARD_FOOTER,
    CODE_BODY_MAX,
    COLOR_IDLE,
    COLOR_LIVE,
    code_card,
    connect_card,
    diff_card,
    host_card,
    job_action_row,
    object_card,
    progress_card,
    working_card,
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
    assert "acl" in body
    assert "open" in body
    assert "Unpaired." in stopped.description
    assert "<t:" in body
    assert CARD_FOOTER in body
    running = host_card(armed=True)
    assert running.title == "Running"
    assert "Writes: Auto." in running.description
    realm = host_card(armed=True, realm="puppetmaster")
    assert "Realm: puppetmaster." in realm.description
    bank = host_card(armed=True, bank=True)
    assert "think-tank memory" in bank.description
    gated = host_card(armed=True, write_gate=True)
    assert "Writes: Gate." in gated.description
    paired = host_card(armed=True, paired=True, operator_count=1, role_count=2)
    assert "Paired. 1 operator. 2 roles." in paired.description
    assert "Last:" not in paired.description
    with_job = host_card(armed=True, paired=True, last_job="Last: failed · boom")
    assert "Last: failed · boom" in with_job.description
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


def _button_custom_ids(card) -> list[str]:
    ids: list[str] = []
    for child in card.v2_components()[0]["components"]:
        if child.get("type") != TYPE_ACTION_ROW:
            continue
        for item in child.get("components") or []:
            custom_id = item.get("custom_id")
            if custom_id:
                ids.append(str(custom_id))
    return ids


def test_code_card_uses_language_fence():
    card = code_card("python", "print(1)\n")
    assert card.title == "Code"
    assert card.description.startswith("```python\n")
    assert card.description.rstrip().endswith("```")
    assert "print(1)" in card.description
    hidden = code_card("python", "<thinking>secret</thinking>\nprint(1)")
    assert "secret" not in hidden.description
    assert "[redacted]" in hidden.description
    long_src = "x" * (CODE_BODY_MAX + 40)
    clipped = code_card("python", long_src)
    assert f"Truncated to {CODE_BODY_MAX} characters." in clipped.description
    assert ("x" * (CODE_BODY_MAX + 1)) not in clipped.description


def test_diff_card_uses_diff_fence():
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
    card = diff_card(patch, filename="app.py")
    assert card.title == "Diff"
    assert "```diff\n" in card.description
    assert "`app.py`" in card.description
    assert "+new" in card.description
    assert "-old" in card.description


def test_working_card_attaches_job_action_row():
    idle = working_card(task_label="wave 2", message="editing cards")
    assert idle.title == "Wave 2"
    assert _button_custom_ids(idle) == []
    live = working_card(task_label="wave 2", message="editing cards", run_id="run-22")
    ids = _button_custom_ids(live)
    assert ids == ["discord-os:job:cancel:run-22"]
    parked = working_card(
        task_label="Approve write",
        message="Waiting for Approve to write.",
        run_id="run-22",
        actions="parked",
    )
    assert _button_custom_ids(parked) == [
        "discord-os:job:approve:run-22",
        "discord-os:job:cancel:run-22",
    ]
    assert "run-22" not in live.text
    progress = progress_card(stage="work", message="card edited", run_id="live-card")
    assert _button_custom_ids(progress) == ["discord-os:job:cancel:live-card"]


def test_job_action_custom_ids_stay_under_discord_limit():
    row = job_action_row("r" * 200, actions="all")
    assert row["type"] == TYPE_ACTION_ROW
    ids = [item["custom_id"] for item in row["components"]]
    assert [item[: item.rfind(":") + 1] for item in ids] == [
        "discord-os:job:approve:",
        "discord-os:job:cancel:",
        "discord-os:job:retry:",
    ]
    assert all(len(item) <= CUSTOM_ID_MAX for item in ids)
    assert all(item not in {ON_ID, OFF_ID, ASK_ID} for item in ids)


def test_working_presence_name_and_status():
    payload = working_presence("wave 2")
    assert payload["op"] == 3
    assert payload["d"]["status"] == "dnd"
    assert payload["d"]["activities"][0]["name"] == "Working on wave 2"
    long_name = working_presence("x" * 200)["d"]["activities"][0]["name"]
    assert long_name.startswith("Working on ")
    assert len(long_name) <= ACTIVITY_NAME_MAX
