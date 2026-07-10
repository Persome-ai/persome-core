"""DAO for ``recall_budget_ticks`` — per-call telemetry of the recall budget.

Every ``recall.assemble_background`` call records exactly one row here
describing how the shared ``max_chars`` budget was spent: how many candidate
texts each layer (schema_prior / scene / behavior / fact / keyword / trail)
admitted and — crucially — how many it **rejected** because the budget was
full. A call with any rejection is ``squeezed``.

This is the measurement the 2026-06-10 recall-budget ablation
(``docs/research/2026-06-10-recall-budget-ablation.md``) called for: the
ablation proved that *when* key memories are squeezed out of the 1200-char
budget, slow-path recognition quality collapses (negative-suppression 6/6
misfires) — but it could not show how often squeezing actually happens in
production. This table provides that denominator, so the "raise max_chars to
2400?" decision can be made on real squeeze rates instead of an adversarial
synthetic fixture.

Mirrors the ``recognition_ticks`` / ``parser_ticks`` audit-table pattern:
canonical output is still the assembled background string handed to the
recognizer — this table is telemetry only, written best-effort (a failed
write never affects recall).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from ..logger import get

logger = get("persome.store.recall_budget_ticks")

# Canonical layer names, in assembly-priority order. Kept as a module constant
# so the recorder (``intent.recall._Budget``) and ``stats`` share one source of
# truth and stats can zero-fill every bucket deterministically.
#
# ``events`` is lowest priority and shares the main budget last, so its rejected
# count shows whether recent activity is being squeezed out.
LAYERS: tuple[str, ...] = (
    "schema_prior",
    "scene",
    "behavior",
    "fact",
    "keyword",
    "semantic",
    "trail",
    "events",
)

# Per-layer counter keys inside the ``layers`` JSON blob.
COUNTERS: tuple[str, ...] = ("admitted", "admitted_chars", "rejected", "rejected_chars")

SCHEMA = """
CREATE TABLE IF NOT EXISTS recall_budget_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,              -- ISO8601 assembly time
    scope TEXT NOT NULL,           -- recall scope (session-*, meeting-*, ...)
    max_chars INTEGER NOT NULL,    -- shared assembly ceiling
    used INTEGER NOT NULL,         -- chars admitted across all layers
    layers TEXT NOT NULL DEFAULT '{}',  -- JSON {layer: {admitted, admitted_chars, rejected, rejected_chars}}
    squeezed INTEGER NOT NULL DEFAULT 0, -- 1 when ANY layer rejected at least one text
    hints TEXT NOT NULL DEFAULT '[]',   -- JSON list of the call's hint terms (debugging telemetry)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recall_budget_ticks_ts ON recall_budget_ticks(ts DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # ``CREATE TABLE IF NOT EXISTS`` does not add columns to a pre-existing table,
    # so the PR-4 ``hints`` column is backfilled via the same PRAGMA-probe + ALTER
    # pattern intent/store.py and evomem/store.py use.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(recall_budget_ticks)")}
    if "hints" not in cols:
        conn.execute("ALTER TABLE recall_budget_ticks ADD COLUMN hints TEXT NOT NULL DEFAULT '[]'")


def _zero_layers() -> dict[str, dict[str, int]]:
    return {layer: {c: 0 for c in COUNTERS} for layer in LAYERS}


def record_tick(
    conn: sqlite3.Connection,
    *,
    ts: str,
    scope: str,
    max_chars: int,
    used: int,
    layers: dict[str, dict[str, int]],
    hints: list[str] | None = None,
) -> int:
    """Insert one recall-budget telemetry row. Returns the row id.

    ``layers`` is the per-layer counter dict produced by the budget recorder;
    unknown layer names are stored verbatim but ``stats`` only zero-fills the
    canonical :data:`LAYERS` buckets. ``squeezed`` is derived here (any layer
    with ``rejected > 0``) so every writer shares one definition. ``hints``
    records the call's hint terms（最初服务 PR-4 双读重放，对账机器在 PR-7 随
    entry_chain 退役后保留为「这次 recall 由什么驱动」的调试遥测）。
    """
    ensure_schema(conn)
    squeezed = any(int(b.get("rejected", 0)) > 0 for b in layers.values())
    cur = conn.execute(
        """
        INSERT INTO recall_budget_ticks
            (ts, scope, max_chars, used, layers, squeezed, hints, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts,
            str(scope or ""),
            int(max_chars),
            int(used),
            json.dumps(layers, ensure_ascii=False),
            1 if squeezed else 0,
            json.dumps([str(h) for h in (hints or []) if str(h).strip()], ensure_ascii=False),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def stats(conn: sqlite3.Connection, *, since: str = "", until: str = "￿") -> dict:
    """Squeeze-rate telemetry over ``[since, until)`` by ts.

    Returns:
        - ``total_ticks``    — number of ``assemble_background`` calls recorded.
        - ``squeezed_ticks`` — calls where any layer rejected at least one text.
        - ``squeeze_rate``   — ``squeezed_ticks / total_ticks`` (4 decimals;
                               ``0.0`` when there are no ticks).
        - ``by_layer``       — per-layer sums of admitted/rejected counts+chars,
                               plus ``squeezed_ticks`` (calls where THIS layer
                               rejected something) — the per-layer squeeze rate
                               the ablation report keys the 2400 decision on.
        - ``rejected_share`` — each layer's share of all rejection events
                               (``rejected / total_rejected``; ``{}`` when
                               nothing was rejected).
        - ``avg_used`` / ``avg_max_chars`` — mean budget usage and cap.
        - ``since`` / ``until`` — echoed bounds (``None`` when unbounded).
    """
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT max_chars, used, layers, squeezed FROM recall_budget_ticks "
        "WHERE ts >= ? AND ts < ?",
        (since, until),
    ).fetchall()
    total = len(rows)
    squeezed_ticks = sum(1 for r in rows if int(r["squeezed"] or 0))
    by_layer: dict[str, dict[str, int]] = _zero_layers()
    for layer in by_layer:
        by_layer[layer]["squeezed_ticks"] = 0
    used_sum = 0
    max_sum = 0
    for r in rows:
        used_sum += int(r["used"] or 0)
        max_sum += int(r["max_chars"] or 0)
        try:
            layers = json.loads(r["layers"] or "{}")
        except (TypeError, ValueError):
            continue
        for layer, counters in layers.items():
            bucket = by_layer.get(layer)
            if bucket is None or not isinstance(counters, dict):
                continue
            for c in COUNTERS:
                bucket[c] += int(counters.get(c, 0) or 0)
            if int(counters.get("rejected", 0) or 0) > 0:
                bucket["squeezed_ticks"] += 1
    total_rejected = sum(b["rejected"] for b in by_layer.values())
    rejected_share = (
        {
            layer: round(b["rejected"] / total_rejected, 4)
            for layer, b in by_layer.items()
            if b["rejected"]
        }
        if total_rejected
        else {}
    )
    return {
        "total_ticks": total,
        "squeezed_ticks": squeezed_ticks,
        "squeeze_rate": round(squeezed_ticks / total, 4) if total else 0.0,
        "by_layer": by_layer,
        "rejected_share": rejected_share,
        "avg_used": round(used_sum / total, 1) if total else 0.0,
        "avg_max_chars": round(max_sum / total, 1) if total else 0.0,
        "since": since or None,
        "until": until if until != "￿" else None,
    }


def prune(conn: sqlite3.Connection, *, keep: int = 50000) -> int:
    """Keep only the most recent ``keep`` rows (bounded telemetry). Returns the
    number of rows deleted."""
    ensure_schema(conn)
    cur = conn.execute(
        "DELETE FROM recall_budget_ticks WHERE id NOT IN "
        "(SELECT id FROM recall_budget_ticks ORDER BY id DESC LIMIT ?)",
        (keep,),
    )
    conn.commit()
    return cur.rowcount
