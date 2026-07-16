"SQLite persistence for evolutionary memory nodes and chains."

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from ..logger import get
from ..store import fts
from . import backup, integrity
from .models import MemoryLayer, MemoryNode, MemoryStatus

_log = get("persome.evomem")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS evo_nodes (
    node_id       TEXT NOT NULL,
    user_id       TEXT NOT NULL DEFAULT 'default',
    agent_id      TEXT NOT NULL DEFAULT 'default',
    content       TEXT NOT NULL,
    layer         TEXT NOT NULL,
    supersedes    TEXT NOT NULL DEFAULT '[]',
    superseded_by TEXT NOT NULL DEFAULT '[]',
    is_latest     INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'active',
    memory_at     TEXT,
    gmt_created   TEXT,
    file_name     TEXT NOT NULL DEFAULT '',
    tags          TEXT NOT NULL DEFAULT '',
    refined_from  TEXT,
    abstracted_from TEXT NOT NULL DEFAULT '[]',
    confidence    TEXT,
    conflicted    INTEGER NOT NULL DEFAULT 0,
    occurred_at   TEXT,
    schema_summary TEXT,
    schema_inferences TEXT,
    schema_confidence REAL,
    valid_from    TEXT,
    valid_until   TEXT,
    PRIMARY KEY (node_id, user_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_evo_nodes_scope ON evo_nodes(user_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_evo_nodes_latest ON evo_nodes(user_id, agent_id, is_latest, status);
"""


_SSOT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("file_name", "TEXT NOT NULL DEFAULT ''"),
    ("tags", "TEXT NOT NULL DEFAULT ''"),
    ("refined_from", "TEXT"),
    ("abstracted_from", "TEXT NOT NULL DEFAULT '[]'"),
    ("confidence", "TEXT"),
    ("conflicted", "INTEGER NOT NULL DEFAULT 0"),
    ("occurred_at", "TEXT"),
    ("schema_summary", "TEXT"),
    ("schema_inferences", "TEXT"),
    ("schema_confidence", "REAL"),
    ("valid_from", "TEXT"),
    ("valid_until", "TEXT"),
)


class MigrationSnapshotError(RuntimeError):
    """Raised when a required pre-migration snapshot cannot be created."""


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(evo_nodes)")}
    missing = [(name, decl) for name, decl in _SSOT_COLUMNS if name not in cols]
    if not missing:
        return  # already current — no destructive DDL, nothing to snapshot

    snapshot = backup.create_snapshot(structural_only=True)
    if snapshot is None:
        raise MigrationSnapshotError(
            "pre-migration snapshot failed (VACUUM INTO / verification) — aborting"
            f" evo_nodes schema migration ({len(missing)} column(s) pending),"
            " table left untouched"
        )
    _log.info(
        "evo_nodes schema migration: pre-change snapshot %s taken, adding %d column(s)",
        snapshot.name,
        len(missing),
    )
    for name, decl in missing:
        conn.execute(f"ALTER TABLE evo_nodes ADD COLUMN {name} {decl}")


def schema_is_current(conn: sqlite3.Connection) -> bool:
    """Whether atomic node writes can proceed without schema-changing SQL."""
    have = {str(row[1]) for row in conn.execute("PRAGMA table_info(evo_nodes)")}
    required = {
        "node_id",
        "user_id",
        "agent_id",
        "content",
        "layer",
        "supersedes",
        "superseded_by",
        "is_latest",
        "status",
        "memory_at",
        "gmt_created",
        *{name for name, _decl in _SSOT_COLUMNS},
    }
    indexes = {str(row[1]) for row in conn.execute("PRAGMA index_list(evo_nodes)")}
    return required.issubset(have) and {
        "idx_evo_nodes_scope",
        "idx_evo_nodes_latest",
    }.issubset(indexes)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create/migrate the canonical node store in an owner process only."""
    if fts.is_client_process():
        return
    if schema_is_current(conn):
        return
    conn.executescript(_CREATE_SQL)
    _migrate(conn)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def upsert_node(conn: sqlite3.Connection, node: MemoryNode, *, user_id: str, agent_id: str) -> None:
    conn.execute(
        """
        INSERT INTO evo_nodes
            (node_id, user_id, agent_id, content, layer, supersedes,
             superseded_by, is_latest, status, memory_at, gmt_created,
             file_name, tags, refined_from, abstracted_from,
             confidence, conflicted, occurred_at,
             schema_summary, schema_inferences, schema_confidence,
             valid_from, valid_until)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(node_id, user_id, agent_id) DO UPDATE SET
            content=excluded.content,
            layer=excluded.layer,
            supersedes=excluded.supersedes,
            superseded_by=excluded.superseded_by,
            is_latest=excluded.is_latest,
            status=excluded.status,
            memory_at=excluded.memory_at,
            gmt_created=excluded.gmt_created,
            file_name=excluded.file_name,
            tags=excluded.tags,
            refined_from=excluded.refined_from,
            abstracted_from=excluded.abstracted_from,
            confidence=excluded.confidence,
            conflicted=excluded.conflicted,
            occurred_at=excluded.occurred_at,
            schema_summary=excluded.schema_summary,
            schema_inferences=excluded.schema_inferences,
            schema_confidence=excluded.schema_confidence,
            valid_from=excluded.valid_from,
            valid_until=excluded.valid_until
        """,
        (
            node.node_id,
            user_id,
            agent_id,
            node.content,
            str(node.layer),
            json.dumps(node.supersedes, ensure_ascii=False),
            json.dumps(node.superseded_by, ensure_ascii=False),
            1 if node.is_latest else 0,
            str(node.status),
            _iso(node.memory_at),
            _iso(node.gmt_created),
            node.file_name,
            node.tags,
            node.refined_from,
            json.dumps(node.abstracted_from, ensure_ascii=False),
            node.confidence,
            1 if node.conflicted else 0,
            node.occurred_at,
            node.schema_summary,
            (
                json.dumps(node.schema_inferences, ensure_ascii=False)
                if node.schema_inferences is not None
                else None
            ),
            node.schema_confidence,
            node.valid_from,
            node.valid_until,
        ),
    )


def _row_to_node(row: sqlite3.Row) -> MemoryNode:
    schema_inferences = row["schema_inferences"]
    return MemoryNode(
        node_id=row["node_id"],
        content=row["content"],
        layer=MemoryLayer(row["layer"]),
        supersedes=json.loads(row["supersedes"] or "[]"),
        superseded_by=json.loads(row["superseded_by"] or "[]"),
        is_latest=bool(row["is_latest"]),
        status=MemoryStatus(row["status"]),
        memory_at=_parse_iso(row["memory_at"]),
        gmt_created=_parse_iso(row["gmt_created"]),
        user_id=row["user_id"],
        agent_id=row["agent_id"],
        file_name=row["file_name"] or "",
        tags=row["tags"] or "",
        refined_from=row["refined_from"],
        abstracted_from=json.loads(row["abstracted_from"] or "[]"),
        confidence=row["confidence"],
        conflicted=bool(row["conflicted"]),
        occurred_at=row["occurred_at"],
        schema_summary=row["schema_summary"],
        schema_inferences=json.loads(schema_inferences) if schema_inferences else None,
        schema_confidence=row["schema_confidence"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
    )


class NodeStore:
    def __init__(self, user_id: str = "default", agent_id: str = "default") -> None:
        self.user_id = user_id
        self.agent_id = agent_id
        with fts.cursor() as conn:
            ensure_schema(conn)

    def save(self, node: MemoryNode) -> None:
        integrity.ensure_writes_allowed()
        with fts.cursor() as conn:
            self._upsert_node(conn, node)

    def _upsert_node(self, conn: sqlite3.Connection, node: MemoryNode) -> None:
        upsert_node(conn, node, user_id=self.user_id, agent_id=self.agent_id)

    def get(self, node_id: str) -> MemoryNode | None:
        with fts.cursor() as conn:
            row = conn.execute(
                "SELECT * FROM evo_nodes WHERE node_id=? AND user_id=? AND agent_id=?",
                (node_id, self.user_id, self.agent_id),
            ).fetchone()
        return _row_to_node(row) if row else None

    def get_by_ids(self, ids: list[str]) -> list[MemoryNode]:
        wanted = [i for i in ids if i]
        if not wanted:
            return []
        placeholders = ",".join("?" * len(wanted))
        with fts.cursor() as conn:
            rows = conn.execute(
                f"SELECT * FROM evo_nodes WHERE user_id=? AND agent_id=? "
                f"AND node_id IN ({placeholders})",
                (self.user_id, self.agent_id, *wanted),
            ).fetchall()
        by_id = {r["node_id"]: _row_to_node(r) for r in rows}
        return [by_id[i] for i in wanted if i in by_id]

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        with fts.cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM evo_nodes WHERE user_id=? AND agent_id=? "
                "AND is_latest=1 AND status='active' AND content LIKE ? "
                "ORDER BY gmt_created DESC, node_id",
                (self.user_id, self.agent_id, f"%{q}%"),
            ).fetchall()
        rows = rows[:top_k]
        n = len(rows)
        hits: list[dict] = []
        for i, row in enumerate(rows):
            node = _row_to_node(row)
            score = 1.0 - (i / n) if n else 0.0
            hits.append({"node_id": node.node_id, "score": score, "node": node})
        return hits

    def shadow(self, node_id: str, *, valid_until: str | None = None) -> None:
        integrity.ensure_writes_allowed()
        with fts.cursor() as conn:
            conn.execute(
                "UPDATE evo_nodes SET status=?, is_latest=0,"
                " valid_until=COALESCE(valid_until, ?)"
                " WHERE node_id=? AND user_id=? AND agent_id=?",
                (str(MemoryStatus.SHADOW), valid_until, node_id, self.user_id, self.agent_id),
            )

    def save_and_supersede(
        self, node: MemoryNode, *, old_id: str, old_valid_until: str | None = None
    ) -> None:
        integrity.ensure_writes_allowed()
        new_id = node.node_id
        with fts.cursor() as conn:
            # Claim the old head before reading it. A correction, session tick,
            # and CLI process may all target the same node; a read-before-BEGIN
            # lets both writers mint active successors. BEGIN IMMEDIATE plus the
            # in-transaction head check makes the transition a single CAS.
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = conn.execute(
                    "SELECT superseded_by, is_latest, status FROM evo_nodes "
                    "WHERE node_id=? AND user_id=? AND agent_id=?",
                    (old_id, self.user_id, self.agent_id),
                ).fetchone()
                if old is None:
                    raise KeyError(f"save_and_supersede: missing old node {old_id!r}")

                old_superseded_by = json.loads(old["superseded_by"] or "[]")
                already_this_transition = old_superseded_by == [new_id]
                if old_superseded_by and not already_this_transition:
                    raise sqlite3.IntegrityError(
                        f"save_and_supersede: old node {old_id!r} already has successor"
                    )
                if not already_this_transition and (
                    not bool(old["is_latest"]) or str(old["status"]) != "active"
                ):
                    raise sqlite3.IntegrityError(
                        f"save_and_supersede: old node {old_id!r} is not an active head"
                    )
                if already_this_transition:
                    existing_row = conn.execute(
                        "SELECT * FROM evo_nodes WHERE node_id=? AND user_id=? AND agent_id=?",
                        (new_id, self.user_id, self.agent_id),
                    ).fetchone()
                    existing = _row_to_node(existing_row) if existing_row is not None else None
                    if existing is None or old_id not in existing.supersedes:
                        raise sqlite3.IntegrityError(
                            f"save_and_supersede: broken existing transition to {new_id!r}"
                        )
                    if not existing.is_latest or existing.status is not MemoryStatus.ACTIVE:
                        # A delayed replay of a->b after b->c must not upsert b
                        # and resurrect it as a competing active head.
                        conn.execute("COMMIT")
                        return
                    if existing.superseded_by:
                        raise sqlite3.IntegrityError(
                            f"save_and_supersede: active successor {new_id!r} has descendants"
                        )
                if not old_superseded_by:
                    old_superseded_by = [new_id]

                if old_id not in node.supersedes:
                    node.supersedes.append(old_id)

                self._upsert_node(conn, node)
                updated = conn.execute(
                    "UPDATE evo_nodes SET superseded_by=?, status=?, is_latest=0,"
                    " valid_until=COALESCE(valid_until, ?)"
                    " WHERE node_id=? AND user_id=? AND agent_id=?",
                    (
                        json.dumps(old_superseded_by, ensure_ascii=False),
                        str(MemoryStatus.SHADOW),
                        old_valid_until,
                        old_id,
                        self.user_id,
                        self.agent_id,
                    ),
                ).rowcount
                if updated != 1:
                    raise sqlite3.IntegrityError(
                        f"save_and_supersede: lost old-node claim for {old_id!r}"
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def save_and_retire_sources(self, node: MemoryNode, *, source_ids: list[str]) -> None:
        integrity.ensure_writes_allowed()
        with fts.cursor() as conn:
            existing = [
                sid
                for sid in dict.fromkeys(s for s in source_ids if s)
                if conn.execute(
                    "SELECT 1 FROM evo_nodes WHERE node_id=? AND user_id=? AND agent_id=?",
                    (sid, self.user_id, self.agent_id),
                ).fetchone()
                is not None
            ]
            conn.execute("BEGIN")
            try:
                self._upsert_node(conn, node)
                for sid in existing:
                    conn.execute(
                        "UPDATE evo_nodes SET status=?, is_latest=0 "
                        "WHERE node_id=? AND user_id=? AND agent_id=?",
                        (str(MemoryStatus.SHADOW), sid, self.user_id, self.agent_id),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def save_and_shadow(self, node: MemoryNode, *, old_id: str) -> None:
        integrity.ensure_writes_allowed()
        with fts.cursor() as conn:
            conn.execute("BEGIN")
            try:
                self._upsert_node(conn, node)
                conn.execute(
                    "UPDATE evo_nodes SET status=?, is_latest=0 "
                    "WHERE node_id=? AND user_id=? AND agent_id=?",
                    (str(MemoryStatus.SHADOW), old_id, self.user_id, self.agent_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def all_latest(self) -> list[MemoryNode]:
        with fts.cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM evo_nodes WHERE user_id=? AND agent_id=? "
                "AND is_latest=1 AND status='active' ORDER BY gmt_created DESC, node_id",
                (self.user_id, self.agent_id),
            ).fetchall()
        return [_row_to_node(r) for r in rows]
