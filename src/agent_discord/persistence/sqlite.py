"""Small SQLite store — stdlib only, optional FTS5 when available."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from uuid import uuid4

from agent_discord import CLI_OWNER_PREFIX, LEGACY_CLI_OWNER_PREFIX
from agent_discord.contracts import EventKind, TaskStatus
from agent_discord.discord.errors import GatewayOwnershipError
from agent_discord.persistence.research import RESEARCH_SCHEMA
from agent_discord.redaction import redact_text_markers, strip_forbidden_keys


SCHEMA = """
CREATE TABLE IF NOT EXISTS workspace_bindings (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    guild_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(workspace_id, channel_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    thread_id TEXT,
    intake_text TEXT NOT NULL,
    status TEXT NOT NULL,
    requester_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    model TEXT NOT NULL,
    adapter_name TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT,
    error TEXT,
    usage_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memory_entries (
    memory_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    channel_id TEXT,
    message_id TEXT,
    attachment_id TEXT,
    filename TEXT,
    sha256 TEXT,
    size INTEGER,
    content_type TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS seen_messages (
    message_id TEXT PRIMARY KEY,
    channel_id TEXT,
    task_id TEXT,
    run_id TEXT,
    seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gateway_owners (
    bot_token_fingerprint TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    claimed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS listen_watermarks (
    channel_id TEXT PRIMARY KEY,
    last_created_ms INTEGER,
    last_message_id TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class SQLiteStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None
        self._fts_enabled = False

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connection()
        conn.executescript(SCHEMA)
        conn.executescript(RESEARCH_SCHEMA)
        self._migrate_seen_messages(conn)
        self._migrate_artifacts(conn)
        self._fts_enabled = self._try_enable_fts(conn)
        conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.path), timeout=30.0)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=30000")
        return self._conn

    def _migrate_seen_messages(self, conn: sqlite3.Connection) -> None:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(seen_messages)").fetchall()
        }
        if "task_id" not in cols:
            conn.execute("ALTER TABLE seen_messages ADD COLUMN task_id TEXT")
        if "run_id" not in cols:
            conn.execute("ALTER TABLE seen_messages ADD COLUMN run_id TEXT")

    def _migrate_artifacts(self, conn: sqlite3.Connection) -> None:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()
        }
        for name, decl in (
            ("channel_id", "TEXT"),
            ("message_id", "TEXT"),
            ("attachment_id", "TEXT"),
            ("filename", "TEXT"),
            ("sha256", "TEXT"),
            ("size", "INTEGER"),
            ("content_type", "TEXT"),
        ):
            if name not in cols:
                conn.execute(f"ALTER TABLE artifacts ADD COLUMN {name} {decl}")

    def _try_enable_fts(self, conn: sqlite3.Connection) -> bool:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    memory_id UNINDEXED,
                    content,
                    workspace_id UNINDEXED,
                    channel_id UNINDEXED
                )
                """
            )
            return True
        except sqlite3.OperationalError:
            return False

    # --- bindings ---

    def upsert_binding(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        guild_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> str:
        conn = self._connection()
        row = conn.execute(
            "SELECT id FROM workspace_bindings WHERE workspace_id=? AND channel_id=?",
            (workspace_id, channel_id),
        ).fetchone()
        binding_id = row["id"] if row else uuid4().hex
        conn.execute(
            """
            INSERT INTO workspace_bindings (id, workspace_id, channel_id, guild_id, metadata_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id, channel_id) DO UPDATE SET
                guild_id=excluded.guild_id,
                metadata_json=excluded.metadata_json
            """,
            (
                binding_id,
                workspace_id,
                channel_id,
                guild_id,
                json.dumps(dict(metadata or {}), sort_keys=True),
            ),
        )
        conn.commit()
        return binding_id

    def get_binding(self, workspace_id: str, channel_id: str) -> Optional[dict[str, Any]]:
        row = self._connection().execute(
            "SELECT * FROM workspace_bindings WHERE workspace_id=? AND channel_id=?",
            (workspace_id, channel_id),
        ).fetchone()
        return dict(row) if row else None

    # --- tasks / runs ---

    def create_task(
        self,
        *,
        task_id: str,
        workspace_id: str,
        channel_id: str,
        intake_text: str,
        thread_id: Optional[str] = None,
        requester_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        conn = self._connection()
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, workspace_id, channel_id, thread_id, intake_text,
                status, requester_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                workspace_id,
                channel_id,
                thread_id,
                intake_text,
                TaskStatus.PENDING.value,
                requester_id,
                json.dumps(dict(metadata or {}), sort_keys=True),
            ),
        )
        conn.commit()

    def create_run(
        self,
        *,
        run_id: str,
        task_id: str,
        model: str,
        adapter_name: str,
        status: TaskStatus = TaskStatus.PENDING,
    ) -> None:
        conn = self._connection()
        conn.execute(
            """
            INSERT INTO runs (run_id, task_id, model, adapter_name, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, task_id, model, adapter_name, status.value),
        )
        conn.execute(
            "UPDATE tasks SET status=?, updated_at=datetime('now') WHERE task_id=?",
            (status.value, task_id),
        )
        conn.commit()

    def update_run(
        self,
        run_id: str,
        *,
        status: TaskStatus,
        summary: Optional[str] = None,
        error: Optional[str] = None,
        usage: Optional[Mapping[str, Any]] = None,
    ) -> None:
        conn = self._connection()
        conn.execute(
            """
            UPDATE runs SET status=?, summary=COALESCE(?, summary),
                error=?, usage_json=?, updated_at=datetime('now')
            WHERE run_id=?
            """,
            (
                status.value,
                summary,
                error,
                json.dumps(dict(usage), sort_keys=True) if usage else None,
                run_id,
            ),
        )
        row = conn.execute("SELECT task_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row:
            conn.execute(
                "UPDATE tasks SET status=?, updated_at=datetime('now') WHERE task_id=?",
                (status.value, row["task_id"]),
            )
        conn.commit()

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        row = self._connection().execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        row = self._connection().execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    # --- events ---

    def append_event(
        self,
        *,
        task_id: str,
        run_id: str,
        kind: EventKind,
        summary: str,
        payload: Mapping[str, Any],
        source: str,
        provenance: Mapping[str, Any],
    ) -> int:
        safe_payload = strip_forbidden_keys(dict(payload))
        if not isinstance(safe_payload, dict):
            safe_payload = {}
        safe_provenance = strip_forbidden_keys(dict(provenance))
        if not isinstance(safe_provenance, dict):
            safe_provenance = {}
        conn = self._connection()
        cur = conn.execute(
            """
            INSERT INTO events (
                task_id, run_id, kind, summary, payload_json, source, provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                run_id,
                kind.value if isinstance(kind, EventKind) else str(kind),
                redact_text_markers(summary),
                json.dumps(safe_payload, sort_keys=True),
                source,
                json.dumps(safe_provenance, sort_keys=True),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def list_events(self, run_id: str) -> Sequence[Mapping[str, Any]]:
        rows = self._connection().execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY id ASC", (run_id,)
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            item["provenance"] = json.loads(item.pop("provenance_json") or "{}")
            out.append(item)
        return out

    # --- memory ---

    def remember(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        content: str,
        source: str,
        provenance: Mapping[str, Any],
    ) -> str:
        memory_id = uuid4().hex
        conn = self._connection()
        safe_prov = strip_forbidden_keys(dict(provenance))
        if not isinstance(safe_prov, dict):
            safe_prov = {}
        conn.execute(
            """
            INSERT INTO memory_entries (
                memory_id, workspace_id, channel_id, content, source, provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                workspace_id,
                channel_id,
                content,
                source,
                json.dumps(safe_prov, sort_keys=True),
            ),
        )
        if self._fts_enabled:
            conn.execute(
                """
                INSERT INTO memory_fts (memory_id, content, workspace_id, channel_id)
                VALUES (?, ?, ?, ?)
                """,
                (memory_id, content, workspace_id, channel_id),
            )
        conn.commit()
        return memory_id

    def recall(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        query: str,
        limit: int = 8,
    ) -> Sequence[Mapping[str, Any]]:
        conn = self._connection()
        if self._fts_enabled and query.strip():
            try:
                rows = conn.execute(
                    """
                    SELECT m.* FROM memory_fts f
                    JOIN memory_entries m ON m.memory_id = f.memory_id
                    WHERE f.workspace_id=? AND f.channel_id=?
                      AND memory_fts MATCH ?
                    ORDER BY m.created_at DESC
                    LIMIT ?
                    """,
                    (workspace_id, channel_id, _fts_query(query), limit),
                ).fetchall()
                return [_memory_row(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        like = f"%{query.strip()}%" if query.strip() else "%"
        rows = conn.execute(
            """
            SELECT * FROM memory_entries
            WHERE workspace_id=? AND channel_id=? AND content LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (workspace_id, channel_id, like, limit),
        ).fetchall()
        return [_memory_row(r) for r in rows]

    # --- artifacts ---

    def add_artifact(
        self,
        *,
        artifact_id: str,
        task_id: str,
        run_id: str,
        kind: str,
        path: str = "",
        provenance: Mapping[str, Any] | None = None,
        channel_id: str = "",
        message_id: str = "",
        attachment_id: str = "",
        filename: str = "",
        sha256: str = "",
        size: int = 0,
        content_type: str = "",
    ) -> None:
        conn = self._connection()
        safe_prov = strip_forbidden_keys(dict(provenance or {}))
        if not isinstance(safe_prov, dict):
            safe_prov = {}
        conn.execute(
            """
            INSERT INTO artifacts (
                artifact_id, task_id, run_id, kind, path,
                channel_id, message_id, attachment_id, filename,
                sha256, size, content_type, provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                task_id,
                run_id,
                kind,
                path,
                channel_id,
                message_id,
                attachment_id,
                filename,
                sha256,
                size,
                content_type,
                json.dumps(safe_prov, sort_keys=True),
            ),
        )
        conn.commit()

    def list_artifacts(self, run_id: str) -> Sequence[Mapping[str, Any]]:
        rows = self._connection().execute(
            "SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at ASC", (run_id,)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["provenance"] = json.loads(item.pop("provenance_json") or "{}")
            out.append(item)
        return out

    def list_objects(
        self,
        channel_id: str,
        *,
        run_id: Optional[str] = None,
        limit: int = 50,
    ) -> Sequence[Mapping[str, Any]]:
        """Pointer index for CLI ls — rows with a Discord message/attachment id."""

        conn = self._connection()
        if run_id:
            rows = conn.execute(
                """
                SELECT * FROM artifacts
                WHERE channel_id=? AND run_id=?
                  AND message_id IS NOT NULL AND message_id != ''
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (channel_id, run_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM artifacts
                WHERE channel_id=?
                  AND message_id IS NOT NULL AND message_id != ''
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (channel_id, limit),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["provenance"] = json.loads(item.pop("provenance_json") or "{}")
            out.append(item)
        return out

    # --- inbound message dedupe (durable / idempotent) ---

    def claim_inbound_message(
        self,
        message_id: str,
        channel_id: Optional[str] = None,
    ) -> bool:
        """Atomically claim a Discord message id. True if newly claimed."""
        if not message_id:
            return True
        conn = self._connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO seen_messages (message_id, channel_id) VALUES (?, ?)",
                (message_id, channel_id),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False

    def bind_inbound_message(
        self,
        message_id: str,
        *,
        task_id: str,
        run_id: str,
        channel_id: Optional[str] = None,
    ) -> None:
        if not message_id:
            return
        conn = self._connection()
        conn.execute(
            """
            UPDATE seen_messages
            SET task_id=?, run_id=?, channel_id=COALESCE(?, channel_id)
            WHERE message_id=?
            """,
            (task_id, run_id, channel_id, message_id),
        )
        conn.commit()

    def get_inbound_message(self, message_id: str) -> Optional[dict[str, Any]]:
        if not message_id:
            return None
        row = self._connection().execute(
            "SELECT * FROM seen_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    def mark_message_seen(self, message_id: str, channel_id: Optional[str] = None) -> bool:
        """Return True if newly seen, False if duplicate. Compatibility helper."""
        return self.claim_inbound_message(message_id, channel_id)

    # --- listen watermark (durable per-channel high-water) ---

    def get_listen_watermark(self, channel_id: str) -> Optional[dict[str, Any]]:
        row = self._connection().execute(
            """
            SELECT channel_id, last_created_ms, last_message_id
            FROM listen_watermarks
            WHERE channel_id=?
            """,
            (channel_id,),
        ).fetchone()
        if row is None:
            return None
        raw_ms = row["last_created_ms"]
        return {
            "channel_id": str(row["channel_id"]),
            "last_created_ms": int(raw_ms) if raw_ms is not None else None,
            "last_message_id": str(row["last_message_id"] or ""),
        }

    def seed_listen_watermark(self, channel_id: str, created_ms: int) -> dict[str, Any]:
        """Insert first-listen high-water if absent. Never overwrite a later mark."""

        conn = self._connection()
        conn.execute(
            """
            INSERT OR IGNORE INTO listen_watermarks (channel_id, last_created_ms, last_message_id)
            VALUES (?, ?, '')
            """,
            (channel_id, created_ms),
        )
        conn.commit()
        existing = self.get_listen_watermark(channel_id)
        if existing is not None:
            return existing
        return {
            "channel_id": channel_id,
            "last_created_ms": created_ms,
            "last_message_id": "",
        }

    def set_listen_watermark(
        self,
        channel_id: str,
        *,
        created_ms: Optional[int],
        message_id: str = "",
    ) -> None:
        conn = self._connection()
        conn.execute(
            """
            INSERT INTO listen_watermarks (
                channel_id, last_created_ms, last_message_id, updated_at
            ) VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(channel_id) DO UPDATE SET
                last_created_ms=excluded.last_created_ms,
                last_message_id=excluded.last_message_id,
                updated_at=datetime('now')
            """,
            (channel_id, created_ms, message_id),
        )
        conn.commit()

    # --- gateway ownership ---

    def claim_gateway(self, bot_token_fingerprint: str, owner_id: str) -> None:
        if not bot_token_fingerprint:
            raise GatewayOwnershipError("bot_token_fingerprint is required")
        if not owner_id:
            raise GatewayOwnershipError("owner_id is required")
        conn = self._connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner_id FROM gateway_owners WHERE bot_token_fingerprint=?",
                (bot_token_fingerprint,),
            ).fetchone()
            if row is not None and row["owner_id"] != owner_id:
                if not _cli_owner_is_dead(str(row["owner_id"])):
                    conn.rollback()
                    raise GatewayOwnershipError(
                        f"token {bot_token_fingerprint} already owned by {row['owner_id']!r}; "
                        f"refusing claim by {owner_id!r}"
                    )
            conn.execute(
                """
                INSERT INTO gateway_owners (bot_token_fingerprint, owner_id, claimed_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(bot_token_fingerprint) DO UPDATE SET
                    owner_id=excluded.owner_id,
                    claimed_at=datetime('now')
                """,
                (bot_token_fingerprint, owner_id),
            )
            conn.commit()
        except GatewayOwnershipError:
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise GatewayOwnershipError(f"gateway claim failed: {exc}") from exc

    def release_gateway(self, bot_token_fingerprint: str, owner_id: str) -> None:
        conn = self._connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner_id FROM gateway_owners WHERE bot_token_fingerprint=?",
                (bot_token_fingerprint,),
            ).fetchone()
            if row is None:
                conn.commit()
                return
            if row["owner_id"] != owner_id:
                conn.rollback()
                raise GatewayOwnershipError(
                    f"token {bot_token_fingerprint} owned by {row['owner_id']!r}; "
                    f"cannot release as {owner_id!r}"
                )
            conn.execute(
                "DELETE FROM gateway_owners WHERE bot_token_fingerprint=?",
                (bot_token_fingerprint,),
            )
            conn.commit()
        except GatewayOwnershipError:
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise GatewayOwnershipError(f"gateway release failed: {exc}") from exc

    def gateway_owner(self, bot_token_fingerprint: str) -> Optional[str]:
        row = self._connection().execute(
            "SELECT owner_id FROM gateway_owners WHERE bot_token_fingerprint=?",
            (bot_token_fingerprint,),
        ).fetchone()
        return str(row["owner_id"]) if row else None


def _cli_owner_is_dead(owner_id: str) -> bool:
    """True when owner looks like discord-os-cli-<pid>-<hex> and pid is gone."""

    prefix = ""
    for candidate in (CLI_OWNER_PREFIX, LEGACY_CLI_OWNER_PREFIX):
        if owner_id.startswith(candidate):
            prefix = candidate
            break
    if not prefix:
        return False
    pid_text, sep, _ = owner_id[len(prefix) :].partition("-")
    if not sep:
        return False
    try:
        pid = int(pid_text)
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    return False


def _memory_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["provenance"] = json.loads(item.pop("provenance_json") or "{}")
    return item


def _fts_query(query: str) -> str:
    tokens = [t for t in query.replace('"', " ").split() if t]
    if not tokens:
        return '""'
    return " OR ".join(f'"{t}"' for t in tokens)
