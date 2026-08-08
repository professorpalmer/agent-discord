"""Research memory: claims, fingerprints, leases, provenance, negative findings.

stdlib / SQLite only. Ordinary MemoryStore.recall remains unchanged — this is an
optional seam for research workflows.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union
from uuid import uuid4

from agent_discord.contracts import ClaimStatus, ResearchClaim, ResearchLease
from agent_discord.redaction import redact_text_markers, strip_forbidden_keys

RESEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_claims (
    claim_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    workspace_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    claim_text TEXT NOT NULL,
    status TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_research_claims_workspace_status
    ON research_claims(workspace_id, status);

CREATE INDEX IF NOT EXISTS idx_research_claims_workspace_scope
    ON research_claims(workspace_id, scope);

CREATE TABLE IF NOT EXISTS research_leases (
    fingerprint TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

_WS_RE = re.compile(r"\s+")


def normalize_claim_text(claim_text: str) -> str:
    """Normalize and redact claim text before fingerprinting."""
    safe_claim_text = redact_text_markers(claim_text or "")
    return _WS_RE.sub(" ", safe_claim_text.casefold().strip())


def normalize_scope(scope: str) -> str:
    return _WS_RE.sub(" ", (scope or "").casefold().strip())


def claim_fingerprint(claim_text: str, scope: str) -> str:
    """Stable SHA-256 of normalized claim + scope."""
    payload = f"{normalize_scope(scope)}\n{normalize_claim_text(claim_text)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class ResearchMemoryStore:
    """SQLite-backed research claims + exclusive fingerprint leases."""

    def __init__(self, path_or_store: Union[Path, str, Any]) -> None:
        # Accept Path/str or an object with `.path` / `._connection()` (SQLiteStore).
        if hasattr(path_or_store, "_connection") and hasattr(path_or_store, "path"):
            self._external = path_or_store
            self.path = Path(path_or_store.path)
            self._conn: Optional[sqlite3.Connection] = None
        else:
            self._external = None
            self.path = Path(path_or_store)
            self._conn = None

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connection()
        conn.executescript(RESEARCH_SCHEMA)
        if self._external is None:
            conn.commit()

    def close(self) -> None:
        if self._external is not None:
            return
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connection(self) -> sqlite3.Connection:
        if self._external is not None:
            return self._external._connection()
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.path), timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=30000")
        return self._conn

    def fingerprint_for(self, claim_text: str, scope: str) -> str:
        return claim_fingerprint(claim_text, scope)

    def upsert_claim(
        self,
        *,
        workspace_id: str,
        scope: str,
        claim_text: str,
        status: ClaimStatus,
        provenance: Mapping[str, Any],
        evidence: Sequence[Mapping[str, Any]] = (),
        claim_id: Optional[str] = None,
    ) -> ResearchClaim:
        if isinstance(status, ClaimStatus):
            status_value = status.value
        else:
            status_value = ClaimStatus(str(status)).value
        stored_scope = normalize_scope(scope)
        safe_claim_text = redact_text_markers(str(claim_text or ""))
        fingerprint = claim_fingerprint(safe_claim_text, stored_scope)
        safe_prov = strip_forbidden_keys(dict(provenance))
        if not isinstance(safe_prov, dict):
            safe_prov = {}
        safe_evidence = []
        for item in evidence:
            cleaned = strip_forbidden_keys(dict(item))
            if isinstance(cleaned, dict):
                safe_evidence.append(cleaned)

        conn = self._connection()
        existing = conn.execute(
            "SELECT claim_id FROM research_claims WHERE fingerprint=?",
            (fingerprint,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE research_claims SET
                    workspace_id=?,
                    scope=?,
                    claim_text=?,
                    status=?,
                    provenance_json=?,
                    evidence_json=?,
                    updated_at=datetime('now')
                WHERE fingerprint=?
                """,
                (
                    workspace_id,
                    stored_scope,
                    safe_claim_text,
                    status_value,
                    json.dumps(safe_prov, sort_keys=True),
                    json.dumps(safe_evidence, sort_keys=True),
                    fingerprint,
                ),
            )
        else:
            resolved_id = claim_id or uuid4().hex
            conn.execute(
                """
                INSERT INTO research_claims (
                    claim_id, fingerprint, workspace_id, scope, claim_text,
                    status, provenance_json, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_id,
                    fingerprint,
                    workspace_id,
                    stored_scope,
                    safe_claim_text,
                    status_value,
                    json.dumps(safe_prov, sort_keys=True),
                    json.dumps(safe_evidence, sort_keys=True),
                ),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM research_claims WHERE fingerprint=?",
            (fingerprint,),
        ).fetchone()
        assert row is not None
        return _claim_from_row(row)

    def get_claim(self, fingerprint: str) -> Optional[ResearchClaim]:
        row = self._connection().execute(
            "SELECT * FROM research_claims WHERE fingerprint=?",
            (fingerprint,),
        ).fetchone()
        return _claim_from_row(row) if row else None

    def list_claims(
        self,
        *,
        workspace_id: str,
        status: Optional[ClaimStatus] = None,
        limit: int = 50,
    ) -> Sequence[ResearchClaim]:
        conn = self._connection()
        if status is None:
            rows = conn.execute(
                """
                SELECT * FROM research_claims
                WHERE workspace_id=?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (workspace_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM research_claims
                WHERE workspace_id=? AND status=?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (workspace_id, status.value, limit),
            ).fetchall()
        return [_claim_from_row(r) for r in rows]

    def list_negative_findings(
        self,
        *,
        workspace_id: str,
        scope: Optional[str] = None,
        limit: int = 50,
    ) -> Sequence[ResearchClaim]:
        conn = self._connection()
        if scope is None:
            rows = conn.execute(
                """
                SELECT * FROM research_claims
                WHERE workspace_id=? AND status=?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (workspace_id, ClaimStatus.NEGATIVE.value, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM research_claims
                WHERE workspace_id=? AND status=? AND scope=?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (workspace_id, ClaimStatus.NEGATIVE.value, normalize_scope(scope), limit),
            ).fetchall()
        return [_claim_from_row(r) for r in rows]

    def acquire_lease(
        self,
        fingerprint: str,
        owner_id: str,
        *,
        ttl_seconds: int = 300,
    ) -> bool:
        """Atomically acquire a lease. Returns False if held by another non-expired owner."""
        if not fingerprint:
            raise ValueError("fingerprint is required")
        if not owner_id:
            raise ValueError("owner_id is required")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        now = _utc_now()
        expires = now + timedelta(seconds=ttl_seconds)
        now_s = _iso(now)
        expires_s = _iso(expires)

        conn = self._connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner_id, expires_at FROM research_leases WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if row is not None:
                current_owner = str(row["owner_id"])
                expires_at = _parse_iso(str(row["expires_at"]))
                if expires_at > now and current_owner != owner_id:
                    conn.rollback()
                    return False
            conn.execute(
                """
                INSERT INTO research_leases (fingerprint, owner_id, acquired_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    owner_id=excluded.owner_id,
                    acquired_at=excluded.acquired_at,
                    expires_at=excluded.expires_at
                """,
                (fingerprint, owner_id, now_s, expires_s),
            )
            conn.commit()
            return True
        except sqlite3.Error:
            conn.rollback()
            raise

    def release_lease(self, fingerprint: str, owner_id: str) -> bool:
        """Release a lease if owned by owner_id. Returns True when a row was removed."""
        if not fingerprint or not owner_id:
            return False
        conn = self._connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner_id FROM research_leases WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                conn.commit()
                return False
            if str(row["owner_id"]) != owner_id:
                conn.rollback()
                return False
            conn.execute(
                "DELETE FROM research_leases WHERE fingerprint=?",
                (fingerprint,),
            )
            conn.commit()
            return True
        except sqlite3.Error:
            conn.rollback()
            raise

    def get_lease(self, fingerprint: str) -> Optional[ResearchLease]:
        row = self._connection().execute(
            "SELECT * FROM research_leases WHERE fingerprint=?",
            (fingerprint,),
        ).fetchone()
        if row is None:
            return None
        lease = ResearchLease(
            fingerprint=str(row["fingerprint"]),
            owner_id=str(row["owner_id"]),
            acquired_at=str(row["acquired_at"]),
            expires_at=str(row["expires_at"]),
        )
        if _parse_iso(lease.expires_at) <= _utc_now():
            return None
        return lease


def _claim_from_row(row: sqlite3.Row) -> ResearchClaim:
    provenance = json.loads(row["provenance_json"] or "{}")
    evidence = json.loads(row["evidence_json"] or "[]")
    if not isinstance(provenance, dict):
        provenance = {}
    if not isinstance(evidence, list):
        evidence = []
    return ResearchClaim(
        claim_id=str(row["claim_id"]),
        fingerprint=str(row["fingerprint"]),
        workspace_id=str(row["workspace_id"]),
        scope=str(row["scope"]),
        claim_text=str(row["claim_text"]),
        status=ClaimStatus(str(row["status"])),
        provenance=provenance,
        evidence=tuple(evidence),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )
