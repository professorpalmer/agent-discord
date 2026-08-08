"""Model pin / no silent fallback."""

from __future__ import annotations

import pytest

from agent_discord.contracts import ModelNotAllowedError, ModelPin
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend
from agent_discord.puppetmaster.models import ADAPTER_NAME, CANONICAL_MODEL, DEFAULT_MODEL_PIN


def test_default_pin_constants():
    assert CANONICAL_MODEL == "cursor/grok-4-5"
    assert ADAPTER_NAME == "grok-4.5"
    assert DEFAULT_MODEL_PIN.allowlist == ("cursor/grok-4-5",)
    assert DEFAULT_MODEL_PIN.adapter_name == "grok-4.5"


def test_allowlist_rejects_other_models():
    pin = ModelPin()
    with pytest.raises(ModelNotAllowedError, match="no silent fallback"):
        pin.assert_allowed("cursor/gpt-5")


def test_fake_backend_no_fallback():
    backend = FakePuppetmasterBackend()
    with pytest.raises(ModelNotAllowedError):
        backend.resolve_model("claude-sonnet")
    pin = backend.resolve_model("cursor/grok-4-5")
    assert pin.canonical == "cursor/grok-4-5"
    assert pin.adapter_name == "grok-4.5"
