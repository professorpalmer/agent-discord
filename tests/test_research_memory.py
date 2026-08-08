"""Deterministic research memory: fingerprints, claims, leases, negatives."""

from __future__ import annotations

from pathlib import Path

from agent_discord.contracts import ClaimStatus, TaskIntake, TaskStatus
from agent_discord.discord.facade import DiscordFacade
from agent_discord.discord.providers.fake import FakeDiscordMCPProvider
from agent_discord.orchestration.orchestrator import AgentOrchestrator
from agent_discord.persistence.research import (
    ResearchMemoryStore,
    claim_fingerprint,
    normalize_claim_text,
)
from agent_discord.persistence.sqlite import SQLiteStore
from agent_discord.puppetmaster.fake import FakePuppetmasterBackend


def test_fingerprint_stability_and_normalization():
    a = claim_fingerprint("  Hello   World  ", "Scope/A")
    b = claim_fingerprint("hello world", "scope/a")
    c = claim_fingerprint("hello world", "scope/b")
    assert a == b
    assert a != c
    assert normalize_claim_text("  X   Y ") == "x y"


def test_claim_upsert_query_and_provenance(tmp_path: Path):
    store = SQLiteStore(tmp_path / "r.sqlite3")
    store.initialize()
    research = ResearchMemoryStore(store)

    first = research.upsert_claim(
        workspace_id="ws",
        scope="billing",
        claim_text="Invoice total is $12",
        status=ClaimStatus.CANDIDATE,
        provenance={"source_url": "https://example.test/a", "note": "seed"},
        evidence=[{"kind": "doc", "ref": "doc-1"}],
    )
    assert first.fingerprint == claim_fingerprint("Invoice total is $12", "billing")
    assert first.status == ClaimStatus.CANDIDATE
    assert first.provenance["source_url"] == "https://example.test/a"
    assert first.evidence[0]["ref"] == "doc-1"

    second = research.upsert_claim(
        workspace_id="ws",
        scope="billing",
        claim_text="invoice total is $12",
        status=ClaimStatus.VERIFIED,
        provenance={"source_url": "https://example.test/b", "chain_of_thought": "SECRET"},
        evidence=[{"kind": "doc", "ref": "doc-2", "hidden_cot": "nope"}],
    )
    assert second.claim_id == first.claim_id
    assert second.fingerprint == first.fingerprint
    assert second.status == ClaimStatus.VERIFIED
    assert "chain_of_thought" not in second.provenance
    assert "hidden_cot" not in second.evidence[0]

    hidden_claim = research.upsert_claim(
        workspace_id="ws",
        scope="redaction",
        claim_text="visible <thinking>SECRET</thinking>",
        status=ClaimStatus.CANDIDATE,
        provenance={},
    )
    assert hidden_claim.claim_text == "visible [redacted]"
    assert "SECRET" not in hidden_claim.claim_text

    loaded = research.get_claim(first.fingerprint)
    assert loaded is not None
    assert loaded.status == ClaimStatus.VERIFIED

    listed = research.list_claims(workspace_id="ws", status=ClaimStatus.VERIFIED)
    assert len(listed) == 1
    assert listed[0].fingerprint == first.fingerprint
    store.close()


def test_lease_exclusivity_and_expiry(tmp_path: Path):
    store = SQLiteStore(tmp_path / "lease.sqlite3")
    store.initialize()
    research = ResearchMemoryStore(store)
    fp = claim_fingerprint("shared claim", "scope")

    assert research.acquire_lease(fp, "worker-a", ttl_seconds=60) is True
    assert research.acquire_lease(fp, "worker-b", ttl_seconds=60) is False
    lease = research.get_lease(fp)
    assert lease is not None
    assert lease.owner_id == "worker-a"

    # Same owner may renew.
    assert research.acquire_lease(fp, "worker-a", ttl_seconds=60) is True
    assert research.release_lease(fp, "worker-b") is False
    assert research.release_lease(fp, "worker-a") is True
    assert research.get_lease(fp) is None
    assert research.acquire_lease(fp, "worker-b", ttl_seconds=60) is True

    # Expired lease can be taken by another owner (seed an already-expired row).
    conn = store._connection()
    conn.execute(
        """
        UPDATE research_leases
        SET owner_id=?, acquired_at=?, expires_at=?
        WHERE fingerprint=?
        """,
        ("worker-b", "2000-01-01T00:00:00Z", "2000-01-01T00:01:00Z", fp),
    )
    conn.commit()
    assert research.get_lease(fp) is None
    assert research.acquire_lease(fp, "worker-a", ttl_seconds=60) is True
    assert research.get_lease(fp) is not None
    assert research.get_lease(fp).owner_id == "worker-a"
    store.close()


def test_negative_findings_queryable_separately(tmp_path: Path):
    store = SQLiteStore(tmp_path / "neg.sqlite3")
    store.initialize()
    research = ResearchMemoryStore(store)
    research.upsert_claim(
        workspace_id="ws",
        scope="api",
        claim_text="endpoint exists",
        status=ClaimStatus.VERIFIED,
        provenance={"ok": True},
    )
    research.upsert_claim(
        workspace_id="ws",
        scope="api",
        claim_text="endpoint returns 404 for /missing",
        status=ClaimStatus.NEGATIVE,
        provenance={"checked": True},
        evidence=[{"status": 404}],
    )
    research.upsert_claim(
        workspace_id="ws",
        scope="docs",
        claim_text="no mention of feature X",
        status=ClaimStatus.NEGATIVE,
        provenance={},
    )

    negatives = research.list_negative_findings(workspace_id="ws")
    assert len(negatives) == 2
    assert all(n.status == ClaimStatus.NEGATIVE for n in negatives)
    scoped = research.list_negative_findings(workspace_id="ws", scope="api")
    assert len(scoped) == 1
    assert "404" in scoped[0].claim_text
    assert len(research.list_negative_findings(workspace_id="ws", scope="API")) == 1
    store.close()


def test_ordinary_memory_recall_unaffected(tmp_path: Path):
    store = SQLiteStore(tmp_path / "mem.sqlite3")
    store.initialize()
    research = ResearchMemoryStore(store)
    research.upsert_claim(
        workspace_id="ws",
        scope="x",
        claim_text="research-only claim about zebras",
        status=ClaimStatus.CANDIDATE,
        provenance={},
    )
    mid = store.remember(
        workspace_id="ws",
        channel_id="ch",
        content="ordinary memory about invoices",
        source="test",
        provenance={"kind": "memory"},
    )
    assert mid
    hits = store.recall(workspace_id="ws", channel_id="ch", query="invoices", limit=5)
    assert hits
    assert hits[0]["content"] == "ordinary memory about invoices"
    # Research claims are not mixed into ordinary recall.
    assert all("zebras" not in h["content"] for h in hits)
    store.close()


def test_orchestrator_optional_research_context(tmp_path: Path):
    store = SQLiteStore(tmp_path / "orch.sqlite3")
    store.initialize()
    research = ResearchMemoryStore(store)
    research.upsert_claim(
        workspace_id="ws",
        scope="topic",
        claim_text="useful prior finding",
        status=ClaimStatus.VERIFIED,
        provenance={"from": "test"},
    )
    research.upsert_claim(
        workspace_id="ws",
        scope="topic",
        claim_text="dead end path",
        status=ClaimStatus.NEGATIVE,
        provenance={},
    )

    facade = DiscordFacade(
        FakeDiscordMCPProvider(),
        bot_token_fingerprint="fp",
        owner_id="test",
    )
    backend = FakePuppetmasterBackend()
    orch = AgentOrchestrator(
        store=store,
        backend=backend,
        discord=facade,
        post_progress_to_discord=False,
        research=research,
    )
    receipt = orch.run_task(
        TaskIntake(text="continue research", channel_id="ch", workspace_id="ws")
    )
    assert receipt.status == TaskStatus.COMPLETED
    assert backend.last_request is not None
    prov = dict(backend.last_request.context.provenance)
    assert "research" in prov
    assert prov["research"]["claim_count"] >= 1
    assert prov["research"]["negative_count"] >= 1

    # Without research store, normal tasks stay free of research metadata.
    backend2 = FakePuppetmasterBackend()
    orch2 = AgentOrchestrator(
        store=store,
        backend=backend2,
        discord=facade,
        post_progress_to_discord=False,
        research=None,
    )
    orch2.run_task(TaskIntake(text="plain task", channel_id="ch", workspace_id="ws"))
    assert "research" not in dict(backend2.last_request.context.provenance)
    store.close()
