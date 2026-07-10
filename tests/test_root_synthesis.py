"Tests for test root synthesis."

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
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

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
        r1 = faces.upsert_root(
            conn, signature="\u7b2c\u4e00\u7248 apex", members=["b1"], anchors=["\u5f20\u4e09"]
        )
        r2 = faces.upsert_root(
            conn,
            signature="\u7b2c\u4e8c\u7248 apex",
            members=["b1", "b2"],
            anchors=["\u5f20\u4e09"],
        )
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
        _seed_body(
            conn,
            "\u8de8\u57df\u4f53\uff1a\u4ed6\u628a\u5de5\u7a0b\u4e25\u8c28\u5e26\u8fdb\u548c\u5f20\u4e09\u7684\u65b9\u6848\u5bf9\u9f50",
        )
        roster = Roster.build([("\u5f20\u4e09", [])])
        res = rs.synthesize_root(
            _cfg(),
            conn,
            llm_call=_llm(
                "\u8fd9\u4e2a\u4eba\u662f\u5de5\u7a0b\u5e08\uff0c\u6838\u5fc3\u5728\u610f\u4e25\u8c28\uff0c\u5e38\u548c\u5f20\u4e09\u5bf9\u9f50\u3002"
            ),
            roster=roster,
        )
        assert res.reason == "written" and res.face_id
        root = faces.resident_root(conn)
        assert root is not None and root["status"] == MemoryStatus.ACTIVE.value
        assert "\u5de5\u7a0b\u5e08" in root["signature"]
        assert json.loads(root["anchors"]) == ["\u5f20\u4e09"]  # scan_mentions → anchors


def test_empty_input_skips(ac_root):
    with fts.cursor() as conn:
        faces.ensure_schema(conn)
        res = rs.synthesize_root(
            _cfg(), conn, llm_call=_llm("\u4e0d\u8be5\u88ab\u8c03\u7528"), roster=Roster()
        )
        assert res.reason == "skip_empty_input"
        assert faces.resident_root(conn) is None


def test_empty_output_skips(ac_root):
    with fts.cursor() as conn:
        _seed_body(conn, "\u67d0\u4f53")
        res = rs.synthesize_root(_cfg(), conn, llm_call=_llm("   "), roster=Roster())
        assert res.reason == "skip_empty_output"
        assert faces.resident_root(conn) is None


def test_hallucination_keeps_prior_root(ac_root):
    with fts.cursor() as conn:
        # a prior good root exists
        faces.upsert_root(conn, signature="\u65e7 apex", members=[], anchors=[])
        _seed_body(conn, "\u5173\u4e8e\u5f20\u4e09\u7684\u4f53")

        roster = Roster.build([("\u5f20\u4e09", []), ("\u674e\u56db", [])])
        res = rs.synthesize_root(
            _cfg(),
            conn,
            llm_call=_llm(
                "\u8fd9\u4e2a\u4eba\u548c\u5f20\u4e09\u3001\u674e\u56db\u90fd\u5f88\u719f\u3002"
            ),
            roster=roster,
        )
        assert res.reason == "skip_hallucination"
        # prior root untouched — never regress to empty on a bad synthesis
        assert faces.resident_root(conn)["signature"] == "\u65e7 apex"
        assert _live_roots(conn) == 1


def test_token_budget_truncates(ac_root):
    with fts.cursor() as conn:
        _seed_body(conn, "\u4f53")
        long_apex = (
            "\u8fd9\u662f\u4e00\u53e5\u5f88\u957f\u7684\u8bdd\u3002" * 60
        )  # ~540 CJK chars ≫ budget 50
        res = rs.synthesize_root(_cfg(budget=50), conn, llm_call=_llm(long_apex), roster=Roster())
        assert res.reason == "written"
        root = faces.resident_root(conn)
        assert rs.estimate_tokens(root["signature"]) <= 50
        assert "⟨truncated⟩" in root["signature"]


def test_fit_budget_short_passthrough():
    assert rs.fit_budget("\u77ed\u53e5\u3002", 1500) == "\u77ed\u53e5\u3002"
    assert "⟨truncated⟩" not in rs.fit_budget("\u77ed\u53e5\u3002", 1500)


def test_run_root_synthesis_gated_off():
    cfg = SimpleNamespace(schema=SimpleNamespace(root_synthesis_enabled=False))
    # disabled → no DB touch, returns disabled
    res = rs.run_root_synthesis(cfg, conn=None)  # type: ignore[arg-type]
    assert res.reason == "disabled"
