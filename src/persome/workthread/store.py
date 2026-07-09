"""DAO for the WorkThread layer (spec §五 Storage).

Tables (all self-ensuring, mirroring ``recall_budget_ticks``' pattern):

- ``work_threads``       — the state machine of record (bindings/evidence as
  JSON columns).
- ``workthread_queue``   — session summaries enqueued by the terminal-reduce
  callback, consumed by the hourly aggregation-window tracker (spec §四 触发).
- ``workthread_ticks``   — one row per tracker run: op counts + disagreement
  (H2) — the churn/revive telemetry denominators (spec §七 遥测).
- ``workthread_state``   — tiny kv (``frozen_open`` — churn 超阈冻结 open).
- ``workthread_labels``  — the label factory (H1 day-review verdicts + H2
  disagreement windows queued for annotation; spec §十).

The open-dedup gate (executor rule 2, F8 fix) searches **all history** via
:func:`similar_threads` — title 经语义归一（NFKC + 关键词集相似，#549 sink
折叠同款思路）+ origin_actor 匹配。
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta

from ..logger import get
from ..store.entries import make_id
from .model import OPEN_STATUSES, Binding, WorkThread

logger = get("persome.workthread.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    origin_type TEXT NOT NULL DEFAULT 'self_initiated',
    origin_actor TEXT NOT NULL DEFAULT '',
    origin_evidence TEXT NOT NULL DEFAULT '[]',
    origin_at TEXT NOT NULL DEFAULT '',
    origin_intent_id INTEGER,
    status TEXT NOT NULL DEFAULT 'background',
    first_seen TEXT NOT NULL DEFAULT '',
    last_active TEXT NOT NULL DEFAULT '',
    total_active_minutes INTEGER NOT NULL DEFAULT 0,
    approximate INTEGER NOT NULL DEFAULT 0,
    bindings TEXT NOT NULL DEFAULT '[]',
    progress_notes TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.5,
    pinned INTEGER NOT NULL DEFAULT 0,
    user_corrected INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_work_threads_status ON work_threads(status, last_active DESC);

CREATE TABLE IF NOT EXISTS workthread_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    sub_tasks TEXT NOT NULL DEFAULT '[]',
    start_time TEXT NOT NULL DEFAULT '',
    end_time TEXT NOT NULL DEFAULT '',
    enqueued_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_workthread_queue_pending
    ON workthread_queue(consumed_at, enqueued_at);

CREATE TABLE IF NOT EXISTS workthread_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    window_id TEXT NOT NULL DEFAULT '',
    sessions INTEGER NOT NULL DEFAULT 0,
    opens INTEGER NOT NULL DEFAULT 0,
    attaches INTEGER NOT NULL DEFAULT 0,
    revives INTEGER NOT NULL DEFAULT 0,
    completes INTEGER NOT NULL DEFAULT 0,
    merges INTEGER NOT NULL DEFAULT 0,
    disagreement INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_workthread_ticks_ts ON workthread_ticks(ts DESC);

CREATE TABLE IF NOT EXISTS workthread_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workthread_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_workthread_labels_day ON workthread_labels(day, id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


# ─── row ↔ dataclass ─────────────────────────────────────────────────────────


def _row_to_thread(row: sqlite3.Row) -> WorkThread:
    return WorkThread(
        id=row["id"],
        title=row["title"],
        goal=row["goal"] or "",
        origin_type=row["origin_type"] or "self_initiated",
        origin_actor=row["origin_actor"] or "",
        origin_evidence=json.loads(row["origin_evidence"] or "[]"),
        origin_at=row["origin_at"] or "",
        origin_intent_id=row["origin_intent_id"],
        status=row["status"] or "background",
        first_seen=row["first_seen"] or "",
        last_active=row["last_active"] or "",
        total_active_minutes=int(row["total_active_minutes"] or 0),
        approximate=bool(row["approximate"]),
        bindings=[Binding.from_dict(b) for b in json.loads(row["bindings"] or "[]")],
        progress_notes=json.loads(row["progress_notes"] or "[]"),
        confidence=float(row["confidence"] or 0.5),
        pinned=bool(row["pinned"]),
        user_corrected=int(row["user_corrected"] or 0),
    )


def insert_thread(conn: sqlite3.Connection, thread: WorkThread) -> str:
    ensure_schema(conn)
    if not thread.id:
        thread.id = make_id(thread.first_seen or datetime.now().isoformat(timespec="minutes"))
    blobs = thread.to_row_json()
    conn.execute(
        """
        INSERT INTO work_threads
            (id, title, goal, origin_type, origin_actor, origin_evidence, origin_at,
             origin_intent_id, status, first_seen, last_active, total_active_minutes,
             approximate, bindings, progress_notes, confidence, pinned, user_corrected)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            thread.id,
            thread.title,
            thread.goal,
            thread.origin_type,
            thread.origin_actor,
            blobs["origin_evidence"],
            thread.origin_at,
            thread.origin_intent_id,
            thread.status,
            thread.first_seen,
            thread.last_active,
            thread.total_active_minutes,
            1 if thread.approximate else 0,
            blobs["bindings"],
            blobs["progress_notes"],
            thread.confidence,
            1 if thread.pinned else 0,
            thread.user_corrected,
        ),
    )
    conn.commit()
    return thread.id


def save_thread(conn: sqlite3.Connection, thread: WorkThread) -> None:
    """Full-row UPDATE (the executor mutates the dataclass then saves)."""
    ensure_schema(conn)
    blobs = thread.to_row_json()
    conn.execute(
        """
        UPDATE work_threads SET
            title = ?, goal = ?, origin_type = ?, origin_actor = ?,
            origin_evidence = ?, origin_at = ?, origin_intent_id = ?, status = ?,
            first_seen = ?, last_active = ?, total_active_minutes = ?, approximate = ?,
            bindings = ?, progress_notes = ?, confidence = ?, pinned = ?, user_corrected = ?
        WHERE id = ?
        """,
        (
            thread.title,
            thread.goal,
            thread.origin_type,
            thread.origin_actor,
            blobs["origin_evidence"],
            thread.origin_at,
            thread.origin_intent_id,
            thread.status,
            thread.first_seen,
            thread.last_active,
            thread.total_active_minutes,
            1 if thread.approximate else 0,
            blobs["bindings"],
            blobs["progress_notes"],
            thread.confidence,
            1 if thread.pinned else 0,
            thread.user_corrected,
            thread.id,
        ),
    )
    conn.commit()


def get_thread(conn: sqlite3.Connection, thread_id: str) -> WorkThread | None:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM work_threads WHERE id = ?", (thread_id,)).fetchone()
    return _row_to_thread(row) if row else None


def list_threads(
    conn: sqlite3.Connection, *, statuses: tuple[str, ...] | None = None, limit: int = 200
) -> list[WorkThread]:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT * FROM work_threads WHERE status IN ({placeholders}) "
            "ORDER BY last_active DESC LIMIT ?",
            (*statuses, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM work_threads ORDER BY last_active DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_thread(r) for r in rows]


def open_threads(conn: sqlite3.Connection) -> list[WorkThread]:
    return list_threads(conn, statuses=OPEN_STATUSES)


def active_thread(conn: sqlite3.Connection) -> WorkThread | None:
    rows = list_threads(conn, statuses=("active",), limit=1)
    return rows[0] if rows else None


def non_open_index(conn: sqlite3.Connection, *, days: int = 90) -> list[WorkThread]:
    """近 N 天 done/stale/superseded 线 — tracker 输入③，复活/recurring 的接球区."""
    ensure_schema(conn)
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="minutes")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM work_threads WHERE status IN ('done','stale','superseded') "
        "AND last_active >= ? ORDER BY last_active DESC LIMIT 100",
        (since,),
    ).fetchall()
    return [_row_to_thread(r) for r in rows]


# ─── open-dedup gate: semantic title normalization (F8 fix) ──────────────────

_LATIN_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")
# Title boilerplate that carries no identity signal ("Kevin 交办：X" vs "X").
_STOP_TOKENS = frozenset(
    {"交办", "任务", "工作", "处理", "继续", "完成", "the", "and", "for", "task", "work"}
)


def normalize_title_tokens(title: str) -> frozenset[str]:
    """Title → keyword set（语义归一：NFKC + casefold + latin tokens + CJK bigrams）.

    复用 #549 sink 折叠的 normalize 思路：不追求 NLP 级实体抽取，只要同一件事
    的两种表述（"Kevin 交办：意图识别优化" / "意图识别链路优化"）落到可比的
    关键词集上。Deterministic, dependency-free.
    """
    s = unicodedata.normalize("NFKC", title or "").casefold()
    tokens: set[str] = set(_LATIN_TOKEN_RE.findall(s))
    # Bigram WITHIN each contiguous CJK run — joining runs across punctuation
    # ("交办：意图…" → "办意") would mint spurious cross-word bigrams.
    for run in _CJK_RUN_RE.findall(s):
        if len(run) == 1:
            tokens.add(run)
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return frozenset(t for t in tokens if t not in _STOP_TOKENS)


def title_similarity(a: str, b: str) -> float:
    """Overlap coefficient of the normalized keyword sets (0.0 when either empty).

    Overlap (|∩| / min size) — not Jaccard — because the common false-twin shape
    is "decorated retelling of the same title" ("Kevin 交办：意图识别优化" vs
    "意图识别链路优化"): the shorter side is nearly contained in the longer one,
    which Jaccard punishes for the decoration's extra tokens.
    """
    ta, tb = normalize_title_tokens(a), normalize_title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


# Overlap threshold for "same undertaking". Tuned permissive-ish: a false fold
# costs one attach to a near-twin (recoverable via the correction port); a
# missed fold births the twin thread F1/F8 exist to prevent.
SIMILARITY_THRESHOLD = 0.5


def find_duplicate(
    conn: sqlite3.Connection, *, title: str, origin_actor: str = ""
) -> WorkThread | None:
    """查重闸（executor rule 2）：对全历史线找同一 undertaking 的既有线.

    Match = title 关键词集 Jaccard ≥ threshold AND origin_actor 一致（任一侧为空
    视作通配——self_initiated 线没有 actor，不能因此漏掉复活）。Open 线优先于
    非 open（复活是兜底，不是首选），同档内取相似度最高者。
    """
    ensure_schema(conn)
    actor_norm = unicodedata.normalize("NFKC", origin_actor or "").casefold().strip()
    best: tuple[int, float, WorkThread] | None = None  # (open_rank, similarity, thread)
    for thread in list_threads(conn, limit=1000):
        # superseded 是 merge 单向吸收后的终态——把它选作复活候选等于悄悄撤销那次
        # merge（src 工作量在 dst 与复活 src 上双计、孪生线重生）。终态不进复活接球区
        # （done/stale 才是合法的复活兜底）(#573)。
        if thread.status == "superseded":
            continue
        sim = title_similarity(title, thread.title)
        if sim < SIMILARITY_THRESHOLD:
            continue
        cand_actor = unicodedata.normalize("NFKC", thread.origin_actor or "").casefold().strip()
        if actor_norm and cand_actor and actor_norm != cand_actor:
            continue
        # docstring 真值：Open 线优先于非 open（复活是兜底，不是首选），同档内才比
        # 相似度——主键 open_rank、次键 sim（此前 (sim, open_rank) 让一条相似度略高的
        # done/stale 线盖过 open 线、误触复活）(#569)。
        open_rank = 1 if thread.status in OPEN_STATUSES else 0
        key = (open_rank, sim, thread)
        if best is None or (key[0], key[1]) > (best[0], best[1]):
            best = key
    return best[2] if best else None


# ─── queue (S2 aggregation window) ───────────────────────────────────────────


def enqueue_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    summary: str,
    sub_tasks: list[str],
    start_time: str,
    end_time: str,
    enqueued_at: str | None = None,
) -> int:
    ensure_schema(conn)
    cur = conn.execute(
        "INSERT INTO workthread_queue "
        "(session_id, summary, sub_tasks, start_time, end_time, enqueued_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            session_id,
            summary,
            json.dumps(sub_tasks, ensure_ascii=False),
            start_time,
            end_time,
            enqueued_at or datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def pending_queue(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM workthread_queue WHERE consumed_at IS NULL ORDER BY enqueued_at ASC"
    ).fetchall()


def mark_consumed(conn: sqlite3.Connection, ids: list[int], *, ts: str | None = None) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE workthread_queue SET consumed_at = ? WHERE id IN ({placeholders})",
        (ts or datetime.now().isoformat(timespec="seconds"), *ids),
    )
    conn.commit()


# ─── telemetry ticks + churn (spec §七) ──────────────────────────────────────


def record_tick(
    conn: sqlite3.Connection,
    *,
    ts: str,
    window_id: str,
    sessions: int,
    opens: int,
    attaches: int,
    revives: int,
    completes: int,
    merges: int,
    disagreement: bool = False,
    outcome: str = "ok",
) -> int:
    ensure_schema(conn)
    cur = conn.execute(
        "INSERT INTO workthread_ticks "
        "(ts, window_id, sessions, opens, attaches, revives, completes, merges, "
        " disagreement, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ts,
            window_id,
            sessions,
            opens,
            attaches,
            revives,
            completes,
            merges,
            1 if disagreement else 0,
            outcome,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def stats(conn: sqlite3.Connection, *, days: int = 7) -> dict:
    """Churn / revive-rate telemetry over the trailing window (spec §七).

    ``thread_churn`` = opens / attaches（周新开线 / 周 attach）；attaches==0 时
    有 opens 记为 1.0（全是新开 = 最碎），否则 0.0。``revive_rate`` = revives /
    attaches. 这些是辅助形状指标（§十 10.5 降级宣言）——运行时质量主张以 H1
    每日真值为准。
    """
    ensure_schema(conn)
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="minutes")
    row = conn.execute(
        "SELECT COUNT(*) AS ticks, COALESCE(SUM(opens),0) AS opens, "
        "COALESCE(SUM(attaches),0) AS attaches, COALESCE(SUM(revives),0) AS revives, "
        "COALESCE(SUM(completes),0) AS completes, COALESCE(SUM(merges),0) AS merges, "
        "COALESCE(SUM(disagreement),0) AS disagreements "
        "FROM workthread_ticks WHERE ts >= ?",
        (since,),
    ).fetchone()
    ticks, opens, attaches = int(row[0]), int(row[1]), int(row[2])
    revives, completes, merges, disagreements = (
        int(row[3]),
        int(row[4]),
        int(row[5]),
        int(row[6]),
    )
    churn = round(opens / attaches, 4) if attaches else (1.0 if opens else 0.0)
    return {
        "days": days,
        "ticks": ticks,
        "opens": opens,
        "attaches": attaches,
        "revives": revives,
        "completes": completes,
        "merges": merges,
        "disagreement_ticks": disagreements,
        "disagreement_rate": round(disagreements / ticks, 4) if ticks else 0.0,
        "thread_churn": churn,
        "revive_rate": round(revives / attaches, 4) if attaches else 0.0,
        "frozen_open": is_open_frozen(conn),
    }


# Churn threshold above which ``open`` is frozen (only attach until a human
# unfreezes — spec §七 遥测动作).
CHURN_FREEZE_THRESHOLD = 0.3
# Minimum attach denominator before churn is trusted enough to freeze — a
# single (1 open / 2 attach) day must not lock the state machine.
CHURN_MIN_ATTACHES = 10


def maybe_freeze_on_churn(
    conn: sqlite3.Connection,
    *,
    freeze_on_churn: bool = True,
    threshold: float = CHURN_FREEZE_THRESHOLD,
    min_attaches: int = CHURN_MIN_ATTACHES,
) -> bool:
    """Freeze ``open`` when 7-day churn exceeds the threshold. Returns frozen?

    ``freeze_on_churn=False`` is the kill-switch: it never freezes AND clears any
    stale freeze left by an earlier over-aggressive threshold, so the tracker
    recovers (mints new threads again) without a manual ``persome thread unfreeze``.
    """
    if not freeze_on_churn:
        if is_open_frozen(conn):
            unfreeze_open(conn)
            logger.info(
                "workthread churn guard disabled (freeze_on_churn=false) — "
                "clearing a stale freeze so the tracker resumes minting threads"
            )
        return False
    s = stats(conn, days=7)
    if s["attaches"] >= min_attaches and s["thread_churn"] > threshold:
        if not is_open_frozen(conn):
            set_state(conn, "frozen_open", "1")
            logger.warning(
                "workthread churn %.2f > %.2f over 7d (%d opens / %d attaches) — "
                "freezing `open` (only attach until `persome thread unfreeze`)",
                s["thread_churn"],
                threshold,
                s["opens"],
                s["attaches"],
            )
        return True
    return is_open_frozen(conn)


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO workthread_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    ensure_schema(conn)
    row = conn.execute("SELECT value FROM workthread_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def is_open_frozen(conn: sqlite3.Connection) -> bool:
    return get_state(conn, "frozen_open") == "1"


def unfreeze_open(conn: sqlite3.Connection) -> None:
    set_state(conn, "frozen_open", "0")


# ─── label factory (H1/H2, spec §十) ─────────────────────────────────────────


def add_label(
    conn: sqlite3.Connection,
    *,
    day: str,
    thread_id: str,
    action: str,
    payload: dict | None = None,
    source: str,
) -> int:
    ensure_schema(conn)
    cur = conn.execute(
        "INSERT INTO workthread_labels (ts, day, thread_id, action, payload, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            datetime.now().isoformat(timespec="seconds"),
            day,
            thread_id,
            action,
            json.dumps(payload or {}, ensure_ascii=False),
            source,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def labels_for_day(conn: sqlite3.Connection, day: str) -> list[sqlite3.Row]:
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM workthread_labels WHERE day = ? ORDER BY id", (day,)
    ).fetchall()


def pending_label_queue(conn: sqlite3.Connection, *, limit: int = 50) -> list[sqlite3.Row]:
    """H2 分歧窗口的待标注队列（action='needs_label'，高不确定样本优先消耗注意力）."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM workthread_labels WHERE action = 'needs_label' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
