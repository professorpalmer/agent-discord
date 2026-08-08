"""Optional Marionette backend — fake transport, pin, config failures."""

from __future__ import annotations

import pytest

from agent_discord.contracts import (
    ContextSnapshot,
    DispatchRequest,
    ModelNotAllowedError,
    TaskStatus,
)
from agent_discord.marionette.backend import (
    MarionetteBackend,
    MarionetteEndpointConfig,
    _map_status,
)
from agent_discord.marionette.fake import FakeMarionetteTransport
from agent_discord.puppetmaster.models import DEFAULT_MODEL_PIN


def _request(run_id: str = "run-1") -> DispatchRequest:
    return DispatchRequest(
        task_id="task-1",
        run_id=run_id,
        prompt="investigate widgets",
        model="cursor/grok-4-5",
        context=ContextSnapshot(
            task_id="task-1",
            memories=[{"content": "prior note"}],
            bindings={"channel_id": "ch"},
            provenance={"source": "test"},
        ),
        metadata={"channel_id": "ch"},
    )


def test_marionette_fake_dispatch_normalizes_contracts():
    transport = FakeMarionetteTransport()
    backend = MarionetteBackend(
        base_url="http://marionette.test",
        pin=DEFAULT_MODEL_PIN,
        transport=transport,
        endpoints=MarionetteEndpointConfig(
            sessions_path="/v1/sessions",
            jobs_path="/v1/jobs",
        ),
    )
    pin = backend.resolve_model("cursor/grok-4-5")
    assert pin.canonical == "cursor/grok-4-5"
    assert pin.adapter_name == "grok-4.5"

    result = backend.dispatch(_request())
    assert result.status == TaskStatus.COMPLETED
    assert result.usage is not None
    assert result.usage.model == "cursor/grok-4-5"
    assert result.usage.adapter_name == "grok-4.5"
    assert result.usage.metadata.get("backend") == "marionette"
    assert result.artifacts
    assert any(e.kind.value == "dispatch" or e.summary.stage == "dispatch" for e in result.events)
    assert transport.sessions
    assert transport.jobs


def test_marionette_no_silent_model_fallback():
    backend = MarionetteBackend(
        base_url="http://marionette.test",
        transport=FakeMarionetteTransport(),
    )
    with pytest.raises(ModelNotAllowedError, match="no silent fallback"):
        backend.resolve_model("cursor/other")


def test_marionette_missing_base_url_fails_closed():
    backend = MarionetteBackend(base_url="", transport=FakeMarionetteTransport())
    result = backend.dispatch(_request("run-cfg"))
    assert result.status == TaskStatus.FAILED
    assert result.error
    assert "MARIONETTE_BASE_URL" in result.error


def test_marionette_transport_unavailable_fails_closed():
    transport = FakeMarionetteTransport(unavailable=True)
    backend = MarionetteBackend(base_url="http://marionette.test", transport=transport)
    result = backend.dispatch(_request("run-down"))
    assert result.status == TaskStatus.FAILED
    assert result.error
    assert "503" in result.error or "unavailable" in result.error.lower()


def test_marionette_cancel_and_status():
    transport = FakeMarionetteTransport()
    backend = MarionetteBackend(base_url="http://marionette.test", transport=transport)
    result = backend.dispatch(_request("run-cancel"))
    assert result.status == TaskStatus.COMPLETED
    assert backend.status("run-cancel") == TaskStatus.COMPLETED
    assert backend.cancel("run-cancel") is True
    assert backend.status("run-cancel") == TaskStatus.CANCELLED


def test_marionette_sse_events_parsed():
    from agent_discord.marionette.backend import _parse_sse

    transport = FakeMarionetteTransport()
    backend = MarionetteBackend(base_url="http://marionette.test", transport=transport)
    result = backend.dispatch(_request("run-sse"))
    assert result.events
    job_id = next(iter(transport.jobs))
    # Fake returns SSE for /events; parse the body the same way the backend does.
    resp = transport.request("GET", f"http://marionette.test/v1/jobs/{job_id}/events")
    assert "text/event-stream" in resp.headers.get("content-type", "")
    events = _parse_sse(resp.text())
    assert events
    assert events[0].get("kind") in {"dispatch", "progress", "receipt"}


def test_marionette_unknown_status_does_not_claim_completion():
    assert _map_status("future-server-state") == TaskStatus.PENDING
