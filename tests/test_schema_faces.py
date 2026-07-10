"""schema_faces — the §4.5 unified schema object (Memory-rebuild Phase 2).

Deterministic, zero-LLM. Covers: dual-extractor folding (signature +
footprint-Jaccard) → provenance escalation to ``both``; the resampling
stability gate (churning footprints stay shadow, stable ones promote);
signal-only contributions never corrupting footprint history; residency
selection/rendering; and the two production hooks (miner + sweeper) landing
faces without perturbing their own outputs.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from persome.store import schema_faces as faces


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    faces.ensure_schema(c)
    return c


def _row(conn: sqlite3.Connection, face_id: str) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM schema_faces WHERE face_id = ?", (face_id,)).fetchone()


MEMBERS = ["a1", "b2", "c3", "d4"]


class TestRecordFace:
    def test_new_face_born_shadow_with_source_provenance(self, conn):
        fid = faces.record_face(
            conn, source="mined", signature="每天早上先看邮件", members=MEMBERS, confidence=0.7
        )
        row = _row(conn, fid)
        assert row["status"] == "shadow"
        assert row["provenance"] == "mined"
        assert row["observations"] == 1
        assert json.loads(row["members"]) == sorted(MEMBERS)
        assert json.loads(row["footprints"]) == [sorted(MEMBERS)]

    def test_signature_match_folds_and_same_source_stays(self, conn):
        fid1 = faces.record_face(
            conn, source="mined", signature="每天早上先看邮件", members=MEMBERS
        )
        # NFKC/case/whitespace-normalized signature equality folds
        fid2 = faces.record_face(
            conn, source="mined", signature="  每天早上先看邮件 ", members=MEMBERS
        )
        assert fid1 == fid2
        row = _row(conn, fid1)
        assert row["observations"] == 2
        assert row["provenance"] == "mined"  # same source — no escalation

    def test_footprint_jaccard_folds_despite_different_signature(self, conn):
        fid1 = faces.record_face(conn, source="mined", signature="规律 A", members=MEMBERS)
        # 3/5 overlap = 0.6 ≥ MATCH_JACCARD → same face even with a new wording
        fid2 = faces.record_face(
            conn, source="emergent", signature="规律 A（改写）", members=["a1", "b2", "c3", "e5"]
        )
        assert fid1 == fid2

    def test_other_source_escalates_to_both(self, conn):
        fid = faces.record_face(conn, source="mined", signature="规律 A", members=MEMBERS)
        faces.record_face(conn, source="emergent", signature="规律 A", members=MEMBERS)
        assert _row(conn, fid)["provenance"] == "both"
        # further contributions never de-escalate
        faces.record_face(conn, source="mined", signature="规律 A", members=MEMBERS)
        assert _row(conn, fid)["provenance"] == "both"

    def test_disjoint_footprint_new_wording_births_new_face(self, conn):
        fid1 = faces.record_face(conn, source="mined", signature="规律 A", members=MEMBERS)
        fid2 = faces.record_face(
            conn, source="mined", signature="规律 B", members=["x1", "y2", "z3"]
        )
        assert fid1 != fid2

    def test_confidence_is_max_ratchet(self, conn):
        fid = faces.record_face(
            conn, source="mined", signature="规律 A", members=MEMBERS, confidence=0.8
        )
        faces.record_face(conn, source="mined", signature="规律 A", members=MEMBERS, confidence=0.3)
        assert _row(conn, fid)["confidence"] == pytest.approx(0.8)

    def test_signal_only_contribution_never_touches_footprints(self, conn):
        fid = faces.record_face(conn, source="mined", signature="规律 A", members=MEMBERS)
        faces.record_face(conn, source="emergent", signature="规律 A", members=[])
        row = _row(conn, fid)
        assert row["provenance"] == "both"  # it IS evidence
        assert row["observations"] == 2
        # but the mined footprint history is untouched — the resampling gate's
        # input is never corrupted by a member-less vouch
        assert json.loads(row["footprints"]) == [sorted(MEMBERS)]
        assert json.loads(row["members"]) == sorted(MEMBERS)

    def test_footprint_history_capped(self, conn):
        fid = faces.record_face(conn, source="mined", signature="规律 A", members=MEMBERS)
        for i in range(5):
            faces.record_face(
                conn, source="mined", signature="规律 A", members=[*MEMBERS[:3], f"n{i}"]
            )
        assert len(json.loads(_row(conn, fid)["footprints"])) == faces.FOOTPRINT_HISTORY_KEEP

    def test_levels_do_not_cross_fold(self, conn):
        fid1 = faces.record_face(conn, source="mined", signature="规律 A", members=MEMBERS)
        fid2 = faces.record_face(
            conn, source="emergent", signature="规律 A", members=MEMBERS, level=2
        )
        assert fid1 != fid2  # 面 and 体 with the same wording are distinct rows


class TestStabilityGateAndPromotion:
    def test_stability_arithmetic(self):
        assert faces.stability([["a", "b"], ["a", "b"]]) == 1.0
        assert faces.stability([["a", "b"], ["c", "d"]]) == 0.0
        assert faces.stability([["a", "b", "c"], ["a", "b", "d"]]) == pytest.approx(0.5)
        assert faces.stability([["a"]]) == 1.0  # <2 snapshots: vacuous, caller guards

    def test_single_signal_never_promotes(self, conn):
        fid = faces.record_face(conn, source="mined", signature="规律 A", members=MEMBERS)
        faces.record_face(conn, source="mined", signature="规律 A", members=MEMBERS)
        assert faces.maybe_promote(conn, fid) is False
        assert _row(conn, fid)["status"] == "shadow"

    def test_both_but_single_snapshot_never_promotes(self, conn):
        fid = faces.record_face(conn, source="mined", signature="规律 A", members=MEMBERS)
        faces.record_face(conn, source="emergent", signature="规律 A", members=[])  # signal-only
        # both-provenance, obs=2, but only ONE footprint snapshot — one sighting
        # can't self-certify stability
        assert faces.maybe_promote(conn, fid) is False

    def test_churning_footprint_stays_shadow(self, conn):
        fid = faces.record_face(conn, source="mined", signature="规律 A", members=["a", "b"])
        faces.record_face(conn, source="emergent", signature="规律 A", members=["c", "d"])
        # both + 2 snapshots, but the membership churned completely between
        # resamples — not a regularity yet
        assert faces.maybe_promote(conn, fid) is False
        assert _row(conn, fid)["status"] == "shadow"

    def test_stable_both_face_promotes_and_is_idempotent(self, conn):
        fid = faces.record_face(conn, source="mined", signature="规律 A", members=MEMBERS)
        faces.record_face(conn, source="emergent", signature="规律 A", members=MEMBERS)
        assert faces.maybe_promote(conn, fid) is True
        assert _row(conn, fid)["status"] == "active"
        assert faces.maybe_promote(conn, fid) is True  # idempotent

    def test_volume_promotes_after_two_stable_cross_domain_resamples(self, conn):
        fid = faces.record_face(
            conn,
            source="emergent",
            signature="跨域规律",
            members=["schema-a", "schema-b"],
            level=2,
        )
        assert faces.maybe_promote(conn, fid) is False
        faces.record_face(
            conn,
            source="emergent",
            signature="跨域规律",
            members=["schema-a", "schema-b"],
            level=2,
        )
        assert faces.maybe_promote(conn, fid) is True
        assert _row(conn, fid)["status"] == "active"

    def test_unknown_face_promote_is_false(self, conn):
        assert faces.maybe_promote(conn, "face-nope") is False


class TestResidency:
    def _promoted(self, conn, sig, members, obs_extra=0):
        fid = faces.record_face(conn, source="mined", signature=sig, members=members)
        faces.record_face(conn, source="emergent", signature=sig, members=members)
        for _ in range(obs_extra):
            faces.record_face(conn, source="mined", signature=sig, members=members)
        assert faces.maybe_promote(conn, fid)
        return fid

    def test_resident_faces_are_active_only_strongest_first(self, conn):
        weak = self._promoted(conn, "规律弱", ["w1", "w2"])
        strong = self._promoted(conn, "规律强", ["s1", "s2"], obs_extra=3)
        faces.record_face(conn, source="mined", signature="影子", members=["x1"])  # stays shadow
        rows = faces.resident_faces(conn, top_k=5)
        assert [r["face_id"] for r in rows] == [strong, weak]

    def test_render_residency(self, conn):
        self._promoted(conn, "每天早上先看邮件", MEMBERS)
        block = faces.render_residency(faces.resident_faces(conn))
        assert "每天早上先看邮件" in block
        assert "行为规律" in block
        assert faces.render_residency([]) == ""


class TestMemberKey:
    def test_stable_across_whitespace_and_case(self):
        assert faces.member_key(" Fact A ") == faces.member_key("fact a")
        assert faces.member_key("事实甲") != faces.member_key("事实乙")


# ── production hooks (miner + sweeper land faces without perturbing outputs) ──


class TestProductionHooks:
    def test_miner_records_mined_face(self, ac_root):
        import json as _json
        from types import SimpleNamespace

        from persome import config as config_mod
        from persome.store import entries as entries_mod
        from persome.store import fts
        from persome.writer import schema_miner_stage as stage

        payload = {
            "central_proposition": "用户在工具选型上偏好极简方案",
            "supporting_summary": "多次选择轻量工具",
            "expected_inferences": ["会拒绝重型框架"],
            "confidence": 0.85,
        }

        def fake_llm(_messages):
            msg = SimpleNamespace(
                content="```json\n" + _json.dumps(payload, ensure_ascii=False) + "\n```",
                tool_calls=[],
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                usage=SimpleNamespace(total_tokens=0),
            )

        cfg = config_mod.load(ac_root / "config.toml")
        cfg.memory_delta.apply_enabled = (
            False  # 测 entries 源挖掘；apply_enabled=True 下 mine 读 evo_nodes
        )
        facts = ["用 uv 而非 pip", "用 ruff 取代 black", "拒绝 litellm", "偏好 CLI 工具"]
        with fts.cursor() as c:
            entries_mod.create_file(c, name="project-tooling.md", description="d", tags=["t"])
            for f in facts:
                entries_mod.append_entry(c, name="project-tooling.md", content=f, tags=["fact"])
            result = stage.mine_schemas_for_user(cfg, c, llm_call=fake_llm)
            assert result.written_count == 1  # the mine itself is unperturbed
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT * FROM schema_faces").fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["provenance"] == "mined"
        assert row["status"] == "shadow"
        assert json.loads(row["members"]) == sorted(faces.member_key(f) for f in facts)

    def test_sweeper_records_body_and_vouches_parents_to_both(self, ac_root):
        from persome.store import fts
        from persome.writer import cross_domain_sweeper as sweeper

        a = sweeper._StableSchema(
            name="schema-project-a.md",
            source_path="project-a.md",
            central="规律 A",
            inferences=[],
            confidence=0.8,
        )
        b = sweeper._StableSchema(
            name="schema-project-b.md",
            source_path="project-b.md",
            central="规律 B",
            inferences=[],
            confidence=0.8,
        )
        collision = sweeper._Collision(
            detected=True,
            central_proposition="跨域融合规律",
            supporting_summary="s",
            expected_inferences=["i"],
            confidence=0.75,
        )
        with fts.cursor() as c:
            # pre-existing mined parent faces (as the miner would have left them)
            fa = faces.record_face(
                c, source="mined", signature="规律 A", members=["a1", "a2", "a3"]
            )
            fb = faces.record_face(
                c, source="mined", signature="规律 B", members=["b1", "b2", "b3"]
            )
            written = sweeper._persist_cross_schema(c, a, b, collision, stable_threshold=0.6)
            assert written is not None  # the fusion write is unperturbed
            c.row_factory = sqlite3.Row
            rows = {r["face_id"]: r for r in c.execute("SELECT * FROM schema_faces")}
        # level-2 体 born emergent, members = the two parent schema names
        bodies = [r for r in rows.values() if r["level"] == 2]
        assert len(bodies) == 1
        assert bodies[0]["provenance"] == "emergent"
        assert json.loads(bodies[0]["members"]) == sorted([a.name, b.name])
        # each parent escalated to both by the signal-only vouch, footprints intact
        for fid, sig_members in ((fa, ["a1", "a2", "a3"]), (fb, ["b1", "b2", "b3"])):
            assert rows[fid]["provenance"] == "both"
            assert json.loads(rows[fid]["footprints"]) == [sorted(sig_members)]


class TestAnchors:
    """§7-6 entity anchors — the honest hull vertices for the graph view."""

    def test_new_face_stores_sorted_anchors(self, conn):
        fid = faces.record_face(
            conn, source="mined", signature="规律 A", members=MEMBERS, anchors=["张伟", "Bob"]
        )
        assert json.loads(_row(conn, fid)["anchors"]) == ["Bob", "张伟"]

    def test_fold_unions_anchors(self, conn):
        fid = faces.record_face(
            conn, source="mined", signature="规律 A", members=MEMBERS, anchors=["张伟"]
        )
        faces.record_face(
            conn, source="emergent", signature="规律 A", members=MEMBERS, anchors=["Bob"]
        )
        assert json.loads(_row(conn, fid)["anchors"]) == ["Bob", "张伟"]

    def test_anchorless_contribution_keeps_existing_anchors(self, conn):
        fid = faces.record_face(
            conn, source="mined", signature="规律 A", members=MEMBERS, anchors=["张伟"]
        )
        faces.record_face(conn, source="mined", signature="规律 A", members=MEMBERS)
        assert json.loads(_row(conn, fid)["anchors"]) == ["张伟"]

    def test_ensure_schema_backfills_anchors_on_old_db(self):
        # a pre-anchors DB (base SCHEMA only) gains the column with a '[]' default
        c = sqlite3.connect(":memory:")
        c.executescript(faces.SCHEMA)
        c.execute(
            "INSERT INTO schema_faces (face_id, provenance, status, valid_from, created_at)"
            " VALUES ('f1', 'mined', 'shadow', 't0', 't0')"
        )
        faces.ensure_schema(c)
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT anchors FROM schema_faces WHERE face_id='f1'").fetchone()
        assert json.loads(row["anchors"]) == []

    def test_miner_anchor_derivation_from_source_entity(self, ac_root):
        from persome import config as config_mod
        from persome.writer import schema_miner_stage as stage

        cfg = config_mod.load(ac_root / "config.toml")
        # a face mined from person-张伟.md is ABOUT 张伟 even with an empty roster
        assert stage._face_anchors(cfg, "person-张伟.md", "他每周五整理周报") == ["张伟"]
        # project-/tool-/topic- 源也锚到源实体（spec 2026-07-04 §face-anchor：修 project 挖的
        # 行为 schema 0 锚飘空）——project-tooling.md 的 schema 锚 tooling 节点
        assert stage._face_anchors(cfg, "project-tooling.md", "偏好轻量工具") == ["tooling"]
        # user-* 源，或「用户…」行为 schema → 锚 self（§1.5-3 rollup vertex）
        assert stage._face_anchors(cfg, "user-preferences.md", "偏好中文交互") == ["self"]
        assert stage._face_anchors(cfg, "project-x.md", "用户偏好轻量工具") == ["self", "x"]

    def test_miner_anchors_scan_fact_bodies(self, ac_root, monkeypatch):

        from persome import config as config_mod
        from persome.evomem import identity as identity_mod
        from persome.writer import schema_miner_stage as stage

        cfg = config_mod.load(ac_root / "config.toml")
        roster = identity_mod.Roster.build([("张伟", []), ("Bob", [])])
        monkeypatch.setattr(identity_mod, "load_roster", lambda _cfg: roster)
        # the cluster emerged from facts naming 张伟/Bob — those ARE the hull
        # vertices, even though the signature names neither
        got = stage._face_anchors(
            cfg,
            "project-x.md",
            "协作节律稳定",
            ["和张伟对齐了接口", "Bob 提交了评审"],
        )
        # 事实里的 张伟/Bob 是凸包顶点，且 project-x 源本身也锚（spec 2026-07-04 §face-anchor）
        assert got == ["Bob", "x", "张伟"]
