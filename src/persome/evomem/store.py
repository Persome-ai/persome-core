"""SQLite-backed NodeStore for evomem 节点（teardown §2/§4/§5）。

复用 persome 的 ``store.fts.cursor()``（WAL、autocommit）。本模块自建
``evo_nodes`` 表，不碰既有 entries/captures 路径。所有读写按 ``(user_id, agent_id)``
作用域隔离。
"""

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

# SSOT 升格扩展列（切换设计稿 §1.2 + Q8，PR-2）。``CREATE TABLE IF NOT EXISTS``
# 对已存在的旧形态库不补列，所以迁移走 PRAGMA table_info 探测 + ``ALTER TABLE
# ADD COLUMN``（与 intent/store.py:_migrate 同款模式）。声明必须与 _CREATE_SQL
# 逐列一致，使「新建库」与「旧库迁移」两态收敛到同一 schema。
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
    """Raised when the forced pre-migration snapshot fails — the schema change
    is aborted and ``evo_nodes`` is left untouched (fail-safe, §3.2 变更前快照
    纪律: 丢快照不该静默，更不该在无救生艇状态下做破坏性 DDL)。"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Backfill SSOT columns onto a pre-existing old-shape evo_nodes table.

    §3.2 变更前快照（framework-layer 强制，issue #489）：``ALTER TABLE`` 是对
    SSOT 库（``evo_nodes`` 升格后 = 真相，损坏即丢数据，不再能从 markdown 零损失
    rebuild）的破坏性 DDL。由本迁移入口**强制**在改 schema 前先落一次验证式
    ``VACUUM INTO`` 变更前快照——框架层兜底，而非靠每个 caller 记得手动跑。这与
    另外两条一次性数据搬运（``backfill.run_backfill`` / ``restore.import_from_markdown``）
    已有的执行前快照纪律是同一道安全网，只是补齐了之前唯一漏掉的破坏性入口。

    失败策略 — fail-fast（设计取舍）：快照失败立即 ``raise MigrationSnapshotError``，
    schema 原样不动。变更前快照是安全网，丢了它再改真相库就是裸奔；宁可拒绝初始化
    （读侧仍可用旧 schema），也绝不留下「快照没拿到、schema 已破坏性变更」的中间态。

    成本克制：只有**确有缺列要补**时才打快照（新建库走 ``_CREATE_SQL`` 直接带全列，
    已是最新 schema 的库 reinit，都是 no-op、不付快照代价）。这点关键——
    ``NodeStore()`` 在 daemon 里被频繁实例化，每次都盲目打快照会把 23:55 每日快照
    纪律退化成「每次开连接都 VACUUM」。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(evo_nodes)")}
    missing = [(name, decl) for name, decl in _SSOT_COLUMNS if name not in cols]
    if not missing:
        return  # already current — no destructive DDL, nothing to snapshot

    # Forced pre-change snapshot BEFORE any ALTER TABLE. ``structural_only`` 复用
    # backfill/restore 同款语义：只把结构性违例当快照失败，alert-only 的投影对账类
    # （check 6）发现照常报警但不否决快照——schema 迁移不应被「投影侧待对账」否决。
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


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def upsert_node(conn: sqlite3.Connection, node: MemoryNode, *, user_id: str, agent_id: str) -> None:
    """在给定连接上 upsert 一个节点（不开/不提交事务，由调用方掌控）。

    模块级函数：``NodeStore._upsert_node`` 与增量影子写（``evomem/shadow.py``，
    PR-3）共用同一条 SQL——两条写路落库形态字节一致，是「增量影子 == 全量
    backfill」不变式在 SQL 层的承重点。节点落库的 scope 以入参为准。
    """
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
    """演化链节点的持久化层。

    ``search`` 只返回活跃链头（``is_latest=1 AND status='active'``）供 reconcile 候选；
    ``get_by_ids`` 不过滤 status，使链追溯能拿到已 shadow 的历史节点。
    """

    def __init__(self, user_id: str = "default", agent_id: str = "default") -> None:
        self.user_id = user_id
        self.agent_id = agent_id
        with fts.cursor() as conn:
            conn.executescript(_CREATE_SQL)
            _migrate(conn)

    def save(self, node: MemoryNode) -> None:
        """Upsert by (node_id, scope)。节点的 user/agent 以本 store 的 scope 为准。"""
        integrity.ensure_writes_allowed()
        with fts.cursor() as conn:
            self._upsert_node(conn, node)

    def _upsert_node(self, conn: sqlite3.Connection, node: MemoryNode) -> None:
        """在给定连接上 upsert 一个节点（不开/不提交事务，由调用方掌控）。"""
        upsert_node(conn, node, user_id=self.user_id, agent_id=self.agent_id)

    def get(self, node_id: str) -> MemoryNode | None:
        with fts.cursor() as conn:
            row = conn.execute(
                "SELECT * FROM evo_nodes WHERE node_id=? AND user_id=? AND agent_id=?",
                (node_id, self.user_id, self.agent_id),
            ).fetchone()
        return _row_to_node(row) if row else None

    def get_by_ids(self, ids: list[str]) -> list[MemoryNode]:
        """按 id 取节点（不过滤 status；链追溯要拿到 shadow 节点）。保持入参顺序。"""
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
        """子串匹配活跃链头，返回 ``[{node_id, score, node}]``（MVP，不引向量）。

        命中越靠前 score 越高（``1.0 - i/len``）。只匹配 ``is_latest=1`` 的活跃节点，
        因为 reconcile 候选只针对当前链头。
        """
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
        """逻辑删除/隐藏：status=shadow, is_latest=0。

        ``valid_until``（PR-6b 写权反转）：退役时刻，仅在节点尚未带 ``valid_until``
        时落（COALESCE，幂等）——镜像 markdown 写口对 ``entry_temporal`` 的
        ``WHERE valid_until IS NULL`` 口径。``None``（既有调用方）= 不碰 temporal，
        与旧行为逐字一致。
        """
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
        """原子地落盘新链头并 shadow 旧节点：INSERT(new) + UPDATE(old) 同一事务。

        新节点与旧节点在同一事务内更新，避免留下两个
        ``is_latest=1 status=active`` 节点（issue #427）。这里把：

        - 新节点带 ``supersedes=[old_id]`` 落盘（``is_latest=1 status=active``）
        - 旧节点 ``superseded_by`` 追加 new_id、``status=shadow``、``is_latest=0``
          （``old_valid_until`` 非空时一并 COALESCE 落退役时刻，PR-6b——与
          markdown 写口对 ``entry_temporal`` 的口径一致）

        合进一个 ``BEGIN...COMMIT``，崩在中间则整体回滚，不留半完成链头。
        """
        integrity.ensure_writes_allowed()
        new_id = node.node_id
        with fts.cursor() as conn:
            old = conn.execute(
                "SELECT superseded_by FROM evo_nodes WHERE node_id=? AND user_id=? AND agent_id=?",
                (old_id, self.user_id, self.agent_id),
            ).fetchone()
            if old is None:
                raise KeyError(f"save_and_supersede: missing old node {old_id!r}")

            old_superseded_by = json.loads(old["superseded_by"] or "[]")
            if new_id not in old_superseded_by:
                old_superseded_by.append(new_id)
            # 新节点必须指回 old（幂等）——调用方通常已带上，这里兜底保证链双向闭合。
            if old_id not in node.supersedes:
                node.supersedes.append(old_id)

            conn.execute("BEGIN")
            try:
                self._upsert_node(conn, node)
                conn.execute(
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
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def save_and_retire_sources(self, node: MemoryNode, *, source_ids: list[str]) -> None:
        """原子地落盘 N→1 合成链头并 retire(shadow) 所有源节点——**不写演化链指针**。

        ABSTRACT 链语义②（legacy 适配层 ``writer/reconcile_apply.py``（已于 PR-6b 删除）遗产移交，SSOT 切换设计
        §1.3，PR-6a）：多源出处是**正交 provenance 边**，记在合成节点的
        ``abstracted_from`` JSON 列（caller 负责填好），不是线性 supersede 链——
        N 个源指向同一个后继会撞链 back-map 的单前驱模型（反分叉铁律的语义口径）。
        所以源节点只走 retire（``status=shadow, is_latest=0``），**不**追加
        ``superseded_by`` 单指针；合成节点的 ``supersedes`` 保持为空。与
        markdown/backfill 侧的形态逐字一致：``#abstracted-from:{a,b,...}`` 多值
        tag + 逐源 strike（孤儿退役，不带 ``#superseded-by``）。

        原子性同 #416/#427：INSERT(new) + UPDATE×N 合进一个 ``BEGIN...COMMIT``，
        崩在中间整体回滚，不留「合成节点已活、部分源未退役」的 N+1 并存态。
        缺失的源 id 跳过（防御铁律外的异常形态），不阻断收敛。
        """
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
        """原子地落盘新链头并 shadow 旧节点（**不建演化链双向指针**）：INSERT(new) + UPDATE(old) 同一事务。

        UPDATE（同向精炼）产生一个新活跃链头 + 退役旧节点，但与 SUPERSEDE 不同——旧节点
        不进演化链（不写 ``supersedes`` / ``superseded_by`` 双向指针），所以不能复用
        ``save_and_supersede``（那会把旧节点 link 进链）。形态与之同构，故同样需要原子：
        否则「``save(new)`` 自动提交 → 另开 ``shadow`` 事务」崩在两步之间会留下新旧两个
        ``is_latest=1 status=active`` 链头，破坏「每条演化链唯一活跃链头」不变量
        （issue #448，同 #427 SUPERSEDE 根因）。缺失的 old_id → UPDATE 命中 0 行（no-op，
        与 ``shadow`` 一致），新节点仍照常落盘。
        """
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
        """is_latest=1 的活跃节点（链头），用于 System2 取语料 / 巡检。"""
        with fts.cursor() as conn:
            rows = conn.execute(
                "SELECT * FROM evo_nodes WHERE user_id=? AND agent_id=? "
                "AND is_latest=1 AND status='active' ORDER BY gmt_created DESC, node_id",
                (self.user_id, self.agent_id),
            ).fetchall()
        return [_row_to_node(r) for r in rows]
