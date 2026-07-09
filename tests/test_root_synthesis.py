"""root_synthesis — the level-3 apex (Memory Root Apex, 2026-07-04 spec).

Deterministic, mock-LLM. Covers the singleton chain-supersede of upsert_root, the four
synthesis gates (empty-input skip / empty-output skip / 提及子集反幻觉 / token-budget
truncation), born-active default-ON, and the cold-start residency fallback contract.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from persome.evomem.identity import Roster
from persome.evomem.models import MemoryStatus
from persome.store import fts
from persome.store import schema_faces as faces
from persome.writer import root_synthesis as rs


def _cfg(budget: int = 1500):
    return SimpleNamespace(
        schema=SimpleNamespace(root_synthesis_enabled=True, root_token_budget=budget)
    )


def _llm(apex: str):
    """A fake call_llm returning ``{"apex": apex}`` in the OpenAI-shaped envelope."""

    def _call(_messages):
        content = json.dumps({"apex": apex}, ensure_ascii=False)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    return _call


def _seed_body(conn, sig, members=("m1", "m2")):
    fid = faces.record_face(conn, source="mined", signature=sig, members=list(members), level=2)
    conn.execute("UPDATE schema_faces SET status = 'active' WHERE face_id = ?", (fid,))
    return fid


def _live_roots(conn):
    conn.row_factory = None
    return conn.execute(
        "SELECT COUNT(*) FROM schema_faces WHERE level = 3 AND valid_to IS NULL"
    ).fetchone()[0]


# ── upsert_root: the singleton invariant ─────────────────────────────────────


def test_upsert_root_singleton_chain_supersede(ac_root):
    with fts.cursor() as conn:
        r1 = faces.upsert_root(conn, signature="第一版 apex", members=["b1"], anchors=["张三"])
        r2 = faces.upsert_root(conn, signature="第二版 apex", members=["b1", "b2"], anchors=["张三"])
        assert r1 != r2
        assert _live_roots(conn) == 1  # singleton: exactly one live level-3
        live = faces.resident_root(conn)
        assert live["face_id"] == r2
        assert live["status"] == MemoryStatus.ACTIVE.value
        assert live["observations"] == 2  # obs carries forward (+1 per nightly resample)
        old = conn.execute(
            "SELECT status, valid_to FROM schema_faces WHERE face_id = ?", (r1,)
        ).fetchone()
        assert old[0] == MemoryStatus.SUPERSEDED.value and old[1] is not None


def test_resident_root_none_and_render_empty(ac_root):
    with fts.cursor() as conn:
        faces.ensure_schema(conn)
        assert faces.resident_root(conn) is None  # cold start
    assert faces.render_root(None) == ""


# ── synthesize_root: the four gates ──────────────────────────────────────────


def test_synthesize_writes_root_born_active(ac_root):
    with fts.cursor() as conn:
        # 张三 appears in the input material (a 体 about working with them), so the apex
        # may name 张三 — grounded, not a hallucination.
        _seed_body(conn, "跨域体：他把工程严谨带进和张三的方案对齐")
        roster = Roster.build([("张三", [])])
        res = rs.synthesize_root(
            _cfg(), conn, llm_call=_llm("这个人是工程师，核心在意严谨，常和张三对齐。"), roster=roster
        )
        assert res.reason == "written" and res.face_id
        root = faces.resident_root(conn)
        assert root is not None and root["status"] == MemoryStatus.ACTIVE.value
        assert "工程师" in root["signature"]
        assert json.loads(root["anchors"]) == ["张三"]  # scan_mentions → anchors


def test_empty_input_skips(ac_root):
    with fts.cursor() as conn:
        faces.ensure_schema(conn)  # no active 体, no profile (tmp memory dir empty)
        res = rs.synthesize_root(_cfg(), conn, llm_call=_llm("不该被调用"), roster=Roster())
        assert res.reason == "skip_empty_input"
        assert faces.resident_root(conn) is None


def test_empty_output_skips(ac_root):
    with fts.cursor() as conn:
        _seed_body(conn, "某体")
        res = rs.synthesize_root(_cfg(), conn, llm_call=_llm("   "), roster=Roster())
        assert res.reason == "skip_empty_output"
        assert faces.resident_root(conn) is None


def test_hallucination_keeps_prior_root(ac_root):
    with fts.cursor() as conn:
        # a prior good root exists
        faces.upsert_root(conn, signature="旧 apex", members=[], anchors=[])
        _seed_body(conn, "关于张三的体")
        # roster knows 张三 AND 李四; input mentions only 张三; apex invents 李四 → hallucination
        roster = Roster.build([("张三", []), ("李四", [])])
        res = rs.synthesize_root(
            _cfg(), conn, llm_call=_llm("这个人和张三、李四都很熟。"), roster=roster
        )
        assert res.reason == "skip_hallucination"
        # prior root untouched — never regress to empty on a bad synthesis
        assert faces.resident_root(conn)["signature"] == "旧 apex"
        assert _live_roots(conn) == 1


def test_token_budget_truncates(ac_root):
    with fts.cursor() as conn:
        _seed_body(conn, "体")
        long_apex = "这是一句很长的话。" * 60  # ~540 CJK chars ≫ budget 50
        res = rs.synthesize_root(
            _cfg(budget=50), conn, llm_call=_llm(long_apex), roster=Roster()
        )
        assert res.reason == "written"
        root = faces.resident_root(conn)
        assert rs.estimate_tokens(root["signature"]) <= 50
        assert "⟨truncated⟩" in root["signature"]


def test_fit_budget_short_passthrough():
    assert rs.fit_budget("短句。", 1500) == "短句。"
    assert "⟨truncated⟩" not in rs.fit_budget("短句。", 1500)


def test_run_root_synthesis_gated_off():
    cfg = SimpleNamespace(schema=SimpleNamespace(root_synthesis_enabled=False))
    # disabled → no DB touch, returns disabled
    res = rs.run_root_synthesis(cfg, conn=None)  # type: ignore[arg-type]
    assert res.reason == "disabled"
