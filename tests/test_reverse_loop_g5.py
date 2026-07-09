"""Reverse-loop P4 / G5 micro-loops (spec 2026-06-26 §3.3) — three independent,
content-free closures that make a previously-silent signal measurable:

- **G5.1**: the sink FOLDS a re-recognized intent silently today; record one
  ``intent_fold_ticks`` row so "the same thing keeps getting re-recognized
  N×/session" (the content-fold tuning signal) is measurable.
- **G5.2**: a dormant ``armed`` event-intent TTL-reaped after 14 days never fired
  — flow its source schemas a false-positive negative signal (a schema that keeps
  predicting triggers that never happen should lose confidence) instead of a
  silent dismiss.
- **G5.3**: an intent that actually reached ``completed`` (P1's execution write-back)
  flows a POSITIVE backlink to its source schemas ("my schema helped a thing get
  DONE"); ``failed`` flows nothing (execution failure ≠ schema misprediction).

Deterministic, zero-LLM, zero-network — reuses the real schema_feedback seam.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from persome import config as config_mod
from persome.intent import audit, schema_prior, sink
from persome.intent import store as intent_store
from persome.intent.ontology import Intent, IntentEvidence
from persome.store import entries as entries_mod
from persome.store import files as files_mod
from persome.store import fts, intent_fold_ticks
from persome.writer import schema_miner_stage as stage


# ── shared helpers (mirror tests/test_schema_feedback.py) ────────────────────
def _seed_schema(conn, *, slug: str, status: str, inferences: list[str], confidence: float) -> str:
    name = f"schema-{slug}.md"
    entries_mod.create_file(
        conn, name=name, description=f"predictive schema: {slug}", tags=["schema", status]
    )
    body = stage.render_schema_body(
        central_proposition=f"用户在 {slug} 上有稳定倾向",
        supporting_summary="多条关联事实支撑",
        expected_inferences=inferences,
    )
    entries_mod.append_entry(
        conn, name=name, content=body, tags=["schema", status, f"confidence:{confidence:.2f}"]
    )
    return name


def _live_confidence(name: str) -> float:
    parsed = files_mod.read_file(files_mod.memory_path(name))
    live = [e for e in parsed.entries if not e.superseded_by]
    return schema_prior._confidence_of(" ".join(live[-1].tags))


def _intent(
    text: str,
    *,
    kind: str = "reminder",
    scope: str = "session-g5",
    sources: list[str] | None = None,
    status: str = "open",
) -> Intent:
    return Intent(
        kind=kind,
        scope=scope,
        status=status,
        rationale=text[:200],
        ts="2026-06-10T10:00",
        payload={"text": text},
        evidence=[IntentEvidence(source="session_trajectory", ref_id="b1")],
        schema_sources=list(sources or []),
    )


# ── G5.1: fold telemetry ─────────────────────────────────────────────────────
def test_fold_records_telemetry_row(ac_root):
    with fts.cursor() as conn:
        first = sink.persist_intent(conn, _intent("买牛奶", sources=[]))
        assert first is not None
        # Same content again → folds (returns None) AND records a fold tick.
        assert sink.persist_intent(conn, _intent("买牛奶", sources=[])) is None
        rows = conn.execute("SELECT scope, kind, outcome FROM intent_fold_ticks").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "session-g5" and rows[0][1] == "reminder"


def test_fold_heat_aggregates_per_kind(ac_root):
    with fts.cursor() as conn:
        for _ in range(3):
            sink.persist_intent(conn, _intent("交周报", sources=[]))  # 1 insert + 2 folds
        since = (datetime.now().astimezone() - timedelta(days=1)).isoformat()
        heat = intent_fold_ticks.fold_heat(conn, since=since)
    # 2 folds of the 'reminder' kind. These are EXACT dedup-key hits (identical body)
    # so target_id is NULL → distinct_targets=0; the fold COUNT (the §3.3 signal) is 2.
    assert heat == [("reminder", 2, 0)]


def test_fold_telemetry_gated_off(ac_root, monkeypatch):
    base = config_mod.load()
    base.intent_recognizer.intent_fold_telemetry_enabled = False
    monkeypatch.setattr(config_mod, "load", lambda: base)
    with fts.cursor() as conn:
        assert sink.persist_intent(conn, _intent("吃药", sources=[])) is not None
        assert sink.persist_intent(conn, _intent("吃药", sources=[])) is None  # still folds
        intent_fold_ticks.ensure_schema(conn)
        n = conn.execute("SELECT COUNT(*) FROM intent_fold_ticks").fetchone()[0]
    assert n == 0  # behaviour unchanged, just no telemetry


def test_fold_heat_surfaces_in_intent_audit(ac_root):
    with fts.cursor() as conn:
        for _ in range(2):
            sink.persist_intent(conn, _intent("改图标", sources=[]))
    from persome import paths

    text = audit.render_text(str(paths.index_db()))
    assert "fold heat (G5.1" in text


def _meeting(*, armed: bool, ts: str) -> Intent:
    """同一事实（周三 standup）的两种激活形式（#549 cross-form）——armed 事件式 vs
    immediate 立即式。base ``dedup_key`` 相同、只差触发后缀，故二者互为跨形式重复。"""
    return Intent(
        kind="meeting",
        scope="session-g5",
        status="armed" if armed else "open",
        rationale="用户确认每周三下午3点 standup",
        ts=ts,
        payload={"when_text": "周三下午3点", "with": ["团队"], "provenance": "user_committed"},
        evidence=[IntentEvidence(source="session_trajectory", ref_id="b1")],
        fire_on="app_opened" if armed else "",
        fire_config={"app": "飞书"} if armed else {},
    )


def test_cross_form_fold_records_telemetry_row(ac_root):
    """A cross-form fold (#549 armed↔immediate) records ONE ``intent_fold_ticks``
    row too — the third fold path was silently bypassing telemetry, so
    ``fold_heat`` systematically undercounted cross-form re-recognition."""
    now = datetime.now().astimezone()
    with fts.cursor() as conn:
        armed_id = sink.persist_intent(
            conn, _meeting(armed=True, ts=now.isoformat(timespec="minutes"))
        )
        assert armed_id is not None
        # Same fact re-recognized in the OTHER (immediate) form → cross-form fold → skip.
        later = (now + timedelta(minutes=5)).isoformat(timespec="minutes")
        assert sink.persist_intent(conn, _meeting(armed=False, ts=later)) is None
        rows = conn.execute(
            "SELECT scope, kind, target_id, outcome FROM intent_fold_ticks"
        ).fetchall()
        since = (now - timedelta(days=1)).isoformat()
        heat = intent_fold_ticks.fold_heat(conn, since=since)
    # Exactly one fold tick, attributed to the folded-onto armed row + the 'meeting' kind.
    assert len(rows) == 1
    assert rows[0][0] == "session-g5" and rows[0][1] == "meeting"
    assert rows[0][2] == armed_id and rows[0][3] == "skipped"
    # fold_heat counts it: 1 meeting fold onto 1 distinct target.
    assert heat == [("meeting", 1, 1)]


# ── G5.2: armed TTL reap → schema false-positive feedback ────────────────────
def _insert_armed(conn, *, name: str, days_old: int) -> int:
    iid = sink.persist_intent(
        conn, _intent("打开 Figma 时提醒", kind="reminder", sources=[name], status="armed")
    )
    assert iid is not None
    old = (datetime.now().astimezone() - timedelta(days=days_old)).isoformat(timespec="seconds")
    conn.execute("UPDATE intents SET status='armed', created_at=? WHERE id=?", (old, iid))
    conn.commit()
    return iid


def test_armed_reap_flows_negative_schema_feedback(ac_root):
    now = datetime.now().astimezone().isoformat()
    with fts.cursor() as conn:
        name = _seed_schema(conn, slug="fig", status="stable", inferences=["推论"], confidence=0.80)
        iid = _insert_armed(conn, name=name, days_old=20)
        reaped = intent_store.expire_stale_armed(conn, now=now)
        status = conn.execute("SELECT status FROM intents WHERE id=?", (iid,)).fetchone()[0]
    assert reaped == [iid]
    assert (
        status == "expired"
    )  # lifecycle reap happened (#532 MECE: dormant armed → system/staleness = expired, not dismissed)
    assert (
        _live_confidence(name) == 0.75
    )  # schema STILL took the −0.05 false-positive hit (G5.2 schema channel is orthogonal to the row's terminal label)


def test_armed_reap_feedback_gated_off(ac_root):
    now = datetime.now().astimezone().isoformat()
    cfg = config_mod.load()
    cfg.intent_recognizer.armed_reap_schema_feedback_enabled = False
    with fts.cursor() as conn:
        name = _seed_schema(conn, slug="fig", status="stable", inferences=["推论"], confidence=0.80)
        iid = _insert_armed(conn, name=name, days_old=20)
        reaped = intent_store.expire_stale_armed(conn, now=now, cfg=cfg)
    assert reaped == [iid]  # still reaped
    assert _live_confidence(name) == 0.80  # but schema untouched (gated off)


def test_armed_reap_without_sources_is_noop(ac_root):
    now = datetime.now().astimezone().isoformat()
    with fts.cursor() as conn:
        iid = _insert_armed(conn, name="", days_old=20)  # no schema_sources
        reaped = intent_store.expire_stale_armed(conn, now=now)
        status = conn.execute("SELECT status FROM intents WHERE id=?", (iid,)).fetchone()[0]
    assert (
        reaped == [iid] and status == "expired"
    )  # reaps fine (#532: → expired), nothing to feed back


# ── G5.3: completed → positive schema backlink; failed → no-op ───────────────
def test_completed_raises_schema_confidence(ac_root):
    with fts.cursor() as conn:
        name = _seed_schema(conn, slug="c", status="stable", inferences=["推论"], confidence=0.80)
        rid = sink.persist_intent(conn, _intent("产出文档", sources=[name]))
        assert intent_store.update_intent_status(conn, intent_id=rid, new_status="completed")
    assert _live_confidence(name) == 0.83  # +0.03, same positive delta as consumed


def test_failed_does_not_touch_schema(ac_root):
    with fts.cursor() as conn:
        name = _seed_schema(conn, slug="f", status="stable", inferences=["推论"], confidence=0.80)
        rid = sink.persist_intent(conn, _intent("产出文档", sources=[name]))
        assert intent_store.update_intent_status(conn, intent_id=rid, new_status="failed")
    assert _live_confidence(name) == 0.80  # execution failure ≠ schema misprediction


def test_consumed_then_completed_rewards_once_not_twice(ac_root):
    """Over-reward guard: open→consumed→completed must flow ONE +delta total, not
    two — else an accepted-and-done intent (+0.06) would outweigh a dismiss (−0.05),
    inverting the deliberate 罚>奖 asymmetry."""
    with fts.cursor() as conn:
        name = _seed_schema(conn, slug="cc", status="stable", inferences=["推论"], confidence=0.80)
        rid = sink.persist_intent(conn, _intent("产出文档", sources=[name]))
        assert intent_store.update_intent_status(conn, intent_id=rid, new_status="consumed")
        assert intent_store.update_intent_status(conn, intent_id=rid, new_status="completed")
    assert _live_confidence(name) == 0.83  # +0.03 once (consume), completed adds nothing
