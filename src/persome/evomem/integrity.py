"""evomem 链完整性自检 + 写口冻结 seam（SSOT 切换设计稿 §3.3，PR-1 生存性设施）。

The integrity contract is summarized in ``docs/config.md``.

与顶层 ``persome/integrity.py`` 的关系（刻意分开，不是重复）：顶层模块是
**物理层**启动自检——``PRAGMA integrity_check`` 失败 → 隔离坏文件 → 让正常路径从
markdown（现行 SSOT）自动重建，处置哲学是「隔离 + 自愈」。本模块是**链不变式层**
自检，服务于 evo_nodes 升格为 SSOT 之后的世界：真相层坏了没有 markdown 重放可以
兜底（§3.1——这套设施是对冲，不是等价替代，这句话不许粉饰），所以失败处置反过来
是「冻结写口 + 报警 + 等人裁决」，绝不自动恢复（纲领：不可知的核交还用户）。

七条检查（§3.3）：

1. ``PRAGMA quick_check``（物理层）。
2. 双向指针对称：``A.superseded_by ∋ B ⇔ B.supersedes ∋ A``，无悬空 id（evo_nodes，
   按 scope 隔离判定）。
3. 反分叉：每节点 ``superseded_by`` 长度 ≤ 1（链模型）。
4. 头一致性：``is_latest=1 ⇒ superseded_by=[] AND status='active'``；每条链 ≤1 个头。
5. 无环：沿 supersedes 边 DFS 不回到自身。
6. 投影对账（廉价抽查，**alert-only**）：evo_nodes 活跃头数 == ``entries.superseded=0``
   行数（排除 ``event-`` 前缀——Q2 裁定 event 条目豁免不进 evo_nodes；
   evo_nodes 为空 = backfill 前，跳过）。投影坏 = 可自愈路径（rebuild_index 从
   真相重放检索投影），只报警 + 记录，不自动重建。
   （entry_chain 版检查已随该表在 PR-7 退役。）
7. 失败处置：1–5 类（结构性）任一失败 → 报警 + （仅当
   ``[evomem] freeze_writes_on_failure`` 开启时）冻结写口；6 类只报警。

冻结是进程内全局标志（``freeze_writes`` / ``write_frozen`` / ``ensure_writes_allowed``），
由 markdown 写口（``store/entries.py``）与 evomem 写口（``evomem/store.py`` NodeStore）
共同检查——这是后续 PR（backfill / 双写 / 写口反转）复用的 seam。默认配置下该标志
永远不会置位，写路径行为与现状等价。注意标志是 per-process 的：daemon 内自检冻结的
是 daemon 进程自身的全部写路径（MCP/HTTP 写口同进程），独立 CLI 进程不受影响。

报警通路是结构化 ``logger.error``；daemon 日志是发布版的告警面。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..logger import get

_log = get("persome.evomem")

# Cap quick_check rows so a huge DB can't stall the tick; cap sample ids per
# violation so a badly damaged DB doesn't flood the structured error log.
_QUICK_CHECK_LIMIT = 100
_SAMPLE_LIMIT = 5


@dataclass(frozen=True)
class Violation:
    """One failed invariant. ``structural`` marks §3.3 class 1–5 (freeze-eligible);
    ``False`` marks class 6 projection-reconciliation findings (alert-only)."""

    check: str
    detail: str
    structural: bool


class WriteFrozenError(RuntimeError):
    """Raised by memory write paths while the integrity freeze flag is set."""


# ─── self-check audit ledger（原 §4.3 判据 2 数据源，PR-7 起为纯审计账） ──────
#
# ``integrity_check_runs``：每次 ``check_and_handle`` 落一行真实发现数。切主读
# 判据仪表盘（evomem-cutover-status）随双读对账在 PR-7 退役后，本账保留为
# daemon 健康审计 trail（自检历史可追溯），不再喂任何判据聚合。

_CHECK_RUNS_SCHEMA = """
CREATE TABLE IF NOT EXISTS integrity_check_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,                 -- ISO8601 check time
    day TEXT NOT NULL,                -- local YYYY-MM-DD
    source TEXT NOT NULL,             -- startup | daily-tick | ...
    violation_count INTEGER NOT NULL, -- REAL findings only (injected drills excluded)
    structural_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_integrity_check_runs_day ON integrity_check_runs(day DESC);
"""


def ensure_check_runs_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_CHECK_RUNS_SCHEMA)


def record_check_run(conn: sqlite3.Connection, *, source: str, violations: list) -> int:
    """One row per ``check_and_handle`` pass — REAL findings only.

    Called best-effort from the check itself (a recording failure degrades to a
    warning there, never affects the check). ``violations`` is the pre-injection
    list of :class:`Violation` — an injected alert-channel drill must not
    pollute the audit ledger.
    """
    from datetime import datetime

    ensure_check_runs_schema(conn)
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO integrity_check_runs"
        " (ts, day, source, violation_count, structural_count, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            now,
            now[:10],
            source,
            len(violations),
            sum(1 for v in violations if getattr(v, "structural", False)),
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def last_check_run(conn: sqlite3.Connection) -> dict | None:
    ensure_check_runs_schema(conn)
    r = conn.execute(
        "SELECT ts, day, source, violation_count, structural_count"
        " FROM integrity_check_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(r) if r else None


# ─── write-freeze seam (§3.3 #7) ─────────────────────────────────────────────

_freeze_lock = threading.Lock()
_frozen_reason: str | None = None


def freeze_writes(reason: str) -> None:
    """Set the process-wide write freeze. Reads stay available — only write
    paths that call ``ensure_writes_allowed`` start rejecting."""
    global _frozen_reason
    with _freeze_lock:
        _frozen_reason = reason
    _log.error("memory writes FROZEN: %s", reason)


def unfreeze_writes() -> None:
    """Clear the freeze — the explicit human "I have decided" button.
    Never called automatically (no auto-recovery, by design)."""
    global _frozen_reason
    with _freeze_lock:
        was = _frozen_reason
        _frozen_reason = None
    if was is not None:
        _log.warning("memory writes unfrozen (was: %s)", was)


def write_frozen() -> str | None:
    """Return the freeze reason when writes are frozen, else ``None``."""
    with _freeze_lock:
        return _frozen_reason


def ensure_writes_allowed() -> None:
    """Write-path guard: raise ``WriteFrozenError`` while frozen, else no-op.

    Called at the top of every memory write entry point (markdown writers in
    ``store/entries.py`` and NodeStore writers in ``evomem/store.py``). With the
    default config the flag is never set, so this is a pure flag check —
    behaviorally identical to before."""
    reason = write_frozen()
    if reason is not None:
        raise WriteFrozenError(f"memory writes are frozen by integrity check: {reason}")


# ─── alert channel (§4.3 判据 4: must be verifiable) ─────────────────────────


def emit_alert(
    check: str,
    detail: str,
    *,
    source: str,
    structural: bool = False,
    frozen: bool = False,
) -> None:
    """Log the shared alert for self-check and bad-snapshot findings."""
    _log.error(
        "integrity_alert [%s] %s: %s%s",
        source,
        check,
        detail,
        " (writes frozen)" if frozen else "",
    )


# ─── the checks ──────────────────────────────────────────────────────────────


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name=?", (name,)
    ).fetchone()
    return row is not None


def _sample(ids: list[str]) -> str:
    shown = ", ".join(ids[:_SAMPLE_LIMIT])
    more = len(ids) - _SAMPLE_LIMIT
    return shown + (f" (+{more} more)" if more > 0 else "")


def _check_quick_check(conn: sqlite3.Connection) -> list[Violation]:
    rows = conn.execute(f"PRAGMA quick_check({_QUICK_CHECK_LIMIT})").fetchall()
    results = [str(r[0]) for r in rows]
    if results == ["ok"]:
        return []
    return [Violation("quick_check", "; ".join(results[:_SAMPLE_LIMIT]), structural=True)]


@dataclass
class _EvoNode:
    supersedes: list[str]
    superseded_by: list[str]
    is_latest: bool
    status: str


def _load_evo_scopes(
    conn: sqlite3.Connection,
) -> tuple[dict[tuple[str, str], dict[str, _EvoNode]], list[Violation]]:
    """Load evo_nodes grouped by (user_id, agent_id) scope. Malformed pointer
    JSON is itself a structural violation (the pointer column IS the chain)."""
    scopes: dict[tuple[str, str], dict[str, _EvoNode]] = {}
    violations: list[Violation] = []
    rows = conn.execute(
        "SELECT node_id, user_id, agent_id, supersedes, superseded_by, is_latest, status"
        " FROM evo_nodes"
    ).fetchall()
    for r in rows:
        try:
            supersedes = json.loads(r["supersedes"] or "[]")
            superseded_by = json.loads(r["superseded_by"] or "[]")
            if not isinstance(supersedes, list) or not isinstance(superseded_by, list):
                raise ValueError("pointer column is not a JSON list")
        except (ValueError, TypeError) as e:
            violations.append(
                Violation(
                    "pointer_parse",
                    f"node {r['node_id']}: unparseable pointer column ({e})",
                    structural=True,
                )
            )
            continue
        scope = (r["user_id"], r["agent_id"])
        scopes.setdefault(scope, {})[r["node_id"]] = _EvoNode(
            supersedes=[str(x) for x in supersedes],
            superseded_by=[str(x) for x in superseded_by],
            is_latest=bool(r["is_latest"]),
            status=str(r["status"]),
        )
    return scopes, violations


def _chain_components(nodes: dict[str, _EvoNode]) -> dict[str, str]:
    """Union-find over (undirected) supersedes edges → node_id → component root."""
    parent: dict[str, str] = {nid: nid for nid in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for nid, n in nodes.items():
        for pred in n.supersedes:
            if pred in parent:
                parent[find(nid)] = find(pred)
    return {nid: find(nid) for nid in nodes}


def _check_evo_chain(conn: sqlite3.Connection) -> list[Violation]:
    """§3.3 checks 2–5 over evo_nodes (per scope). Trivially passes when the
    table is empty or absent — the pre-backfill state."""
    if not _table_exists(conn, "evo_nodes"):
        return []
    scopes, violations = _load_evo_scopes(conn)

    for scope, nodes in scopes.items():
        tag = f"scope={scope[0]}/{scope[1]}"
        dangling: list[str] = []
        asymmetric: list[str] = []
        forked: list[str] = []
        bad_heads: list[str] = []
        multi_heads: list[str] = []
        cyclic: list[str] = []

        # 2. bidirectional pointer symmetry + no dangling ids
        for nid, n in nodes.items():
            for succ in n.superseded_by:
                if succ not in nodes:
                    dangling.append(f"{nid}→{succ}")
                elif nid not in nodes[succ].supersedes:
                    asymmetric.append(f"{nid}↛{succ}")
            for pred in n.supersedes:
                if pred not in nodes:
                    dangling.append(f"{nid}→{pred}")
                elif nid not in nodes[pred].superseded_by:
                    asymmetric.append(f"{nid}↛{pred}")
            # 3. anti-fork
            if len(n.superseded_by) > 1:
                forked.append(nid)
            # 4a. head consistency: a head has no successor and is active
            if n.is_latest and (n.superseded_by or n.status != "active"):
                bad_heads.append(nid)

        # 4b. at most one head per chain (connected component over supersedes edges)
        component = _chain_components(nodes)
        heads_per_chain: dict[str, list[str]] = {}
        for nid, n in nodes.items():
            if n.is_latest:
                heads_per_chain.setdefault(component[nid], []).append(nid)
        for root, heads in heads_per_chain.items():
            if len(heads) > 1:
                multi_heads.append(f"chain[{root}]: {'+'.join(sorted(heads))}")

        # 5. acyclicity along supersedes edges (iterative coloring DFS)
        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(nodes, WHITE)
        for start in nodes:
            if color[start] != WHITE:
                continue
            stack: list[tuple[str, int]] = [(start, 0)]
            color[start] = GRAY
            while stack:
                cur, i = stack[-1]
                edges = [p for p in nodes[cur].supersedes if p in nodes]
                if i < len(edges):
                    stack[-1] = (cur, i + 1)
                    nxt = edges[i]
                    if color[nxt] == GRAY:
                        cyclic.append(f"{cur}→{nxt}")
                    elif color[nxt] == WHITE:
                        color[nxt] = GRAY
                        stack.append((nxt, 0))
                else:
                    color[cur] = BLACK
                    stack.pop()

        if dangling:
            violations.append(
                Violation(
                    "pointer_symmetry",
                    f"{tag}: dangling pointer(s): {_sample(dangling)}",
                    structural=True,
                )
            )
        if asymmetric:
            violations.append(
                Violation(
                    "pointer_symmetry",
                    f"{tag}: asymmetric pointer(s): {_sample(asymmetric)}",
                    structural=True,
                )
            )
        if forked:
            violations.append(
                Violation(
                    "anti_fork",
                    f"{tag}: node(s) with >1 successor: {_sample(forked)}",
                    structural=True,
                )
            )
        if bad_heads:
            violations.append(
                Violation(
                    "head_consistency",
                    f"{tag}: is_latest=1 node(s) with a successor or non-active "
                    f"status: {_sample(bad_heads)}",
                    structural=True,
                )
            )
        if multi_heads:
            violations.append(
                Violation(
                    "head_consistency",
                    f"{tag}: chain(s) with >1 head: {_sample(multi_heads)}",
                    structural=True,
                )
            )
        if cyclic:
            violations.append(
                Violation(
                    "acyclicity",
                    f"{tag}: cycle along supersedes edge(s): {_sample(cyclic)}",
                    structural=True,
                )
            )
    return violations


def _check_evo_projection(conn: sqlite3.Connection) -> list[Violation]:
    """§3.3 check 6 (evo edition, alert-only): active-head count vs the FTS
    retrieval projection's live-row count. Skipped while evo_nodes is empty —
    before the PR-2 backfill the comparison is meaningless by construction.

    Q2 裁定（PR-2）：``event-*`` 条目豁免不进 evo_nodes（量大、append-only、
    永不入链——backfill 跳过），所以对账侧的 live-entries 计数同样排除
    ``prefix='event'``，否则 backfill 后此检查必然恒红。"""
    if not _table_exists(conn, "evo_nodes") or not _table_exists(conn, "entries"):
        return []
    total = conn.execute("SELECT COUNT(*) FROM evo_nodes").fetchone()[0]
    if not total:
        return []
    evo_heads = conn.execute(
        "SELECT COUNT(*) FROM evo_nodes WHERE is_latest=1 AND status='active'"
    ).fetchone()[0]
    live_entries = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE superseded=0 AND prefix != 'event'"
    ).fetchone()[0]
    if evo_heads != live_entries:
        return [
            Violation(
                "projection_reconciliation",
                f"evo_nodes active heads ({evo_heads}) != non-event "
                f"entries.superseded=0 rows ({live_entries})",
                structural=False,
            )
        ]
    return []


def run_checks(conn: sqlite3.Connection) -> list[Violation]:
    """Run the full §3.3 check suite on an open connection. Read-only.

    Trivially passes on an empty / fresh database (a missing evo_nodes table is
    the clean pre-backfill state, not a failure)."""
    violations: list[Violation] = []
    violations += _check_quick_check(conn)
    violations += _check_evo_chain(conn)
    violations += _check_evo_projection(conn)
    return violations


def verify_snapshot(path: Path) -> list[Violation]:
    """Run the check suite against a snapshot FILE, read-only.

    Deliberately not ``fts.cursor`` — that path executes schema DDL and would
    WRITE to the snapshot. A snapshot that cannot even be opened counts as a
    structural quick_check failure."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return [Violation("quick_check", f"snapshot open failed: {e}", structural=True)]
    conn.row_factory = sqlite3.Row
    try:
        return run_checks(conn)
    except sqlite3.DatabaseError as e:
        return [Violation("quick_check", f"snapshot check failed: {e}", structural=True)]
    finally:
        conn.close()


# ─── orchestration ───────────────────────────────────────────────────────────


def check_and_handle(
    *,
    source: str,
    freeze_on_failure: bool = False,
    db_path: Path | None = None,
    inject_violation: Violation | None = None,
) -> list[Violation]:
    """Run the self-check on the live index, alert every violation, optionally freeze.

    Failure handling per §3.3 #7: STRUCTURAL violations (checks 1–5) freeze the
    write paths — but only when ``freeze_on_failure`` (config
    ``[evomem] freeze_writes_on_failure``, default off) is set; otherwise they
    alert only. Projection-reconciliation findings (check 6) never freeze:
    they are the self-healable side and PR-1 only alerts + records (projection
    rebuild is redefined in PR-7). No automatic recovery in any case.

    ``inject_violation`` is the test seam required by §4.3 判据 4: it lets a
    test (or a manual drill) push one fake violation through the FULL alert
    pipeline to verify the channel end-to-end. Never set in production calls.

    Never raises — a self-check that itself errors degrades to an alert.
    """
    try:
        from ..store import fts  # local import keeps module import light

        with fts.cursor(db_path) as conn:
            violations = run_checks(conn)
            # 自检审计账（原 §4.3 判据 2 数据源，PR-7 起为纯审计 trail）: persist
            # the REAL finding count per pass. Recorded BEFORE any injected drill
            # violation — an alert-channel drill must not pollute the ledger.
            # Best-effort: a recording failure degrades to a warning and never
            # affects the check itself.
            try:
                record_check_run(conn, source=source, violations=violations)
            except Exception:  # noqa: BLE001 — telemetry must never break the check
                _log.warning("integrity check-run recording failed (ignored)", exc_info=True)
    except sqlite3.DatabaseError as e:
        violations = [Violation("quick_check", f"DB open/check failed: {e}", structural=True)]
    except Exception as e:  # noqa: BLE001 — the check must never kill its caller
        violations = [Violation("self_check_error", f"integrity check errored: {e}", False)]
    if inject_violation is not None:
        violations = [*violations, inject_violation]

    structural = [v for v in violations if v.structural]
    froze = False
    if structural and freeze_on_failure:
        reason = "; ".join(f"{v.check}: {v.detail}" for v in structural[:3])
        freeze_writes(f"integrity check failed ({source}): {reason}")
        froze = True
    for v in violations:
        emit_alert(
            v.check,
            v.detail,
            source=source,
            structural=v.structural,
            frozen=froze and v.structural,
        )
    if not violations:
        _log.info("evomem integrity check ok (source=%s)", source)
    return violations


def startup_check(cfg: Config) -> list[Violation] | None:
    """Daemon-startup hook (§3.3 「daemon 启动时」). No-op when disabled."""
    if not cfg.evomem.integrity_check_enabled:
        return None
    return check_and_handle(
        source="startup",
        freeze_on_failure=cfg.evomem.freeze_writes_on_failure,
    )
