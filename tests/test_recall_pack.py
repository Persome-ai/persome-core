"""Structured, cited recall pack — `assemble_background_structured` + `GET /recall/pack`.

The structured sibling mirrors `assemble_background`'s string output (same per-layer
helpers, same admission/order) but emits `RecallItem`s carrying a `cite` handle and, for
scene items, a RAW capture handle (stem / timeline_block_id). The string path stays
byte-identical (the sink is a pure default-off side-channel) — pinned here.
"""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.intent import recall, sink
from persome.intent.ontology import Intent, IntentEvidence
from persome.store import entries as entries_mod
from persome.store import fts

_NOW = datetime.now().isoformat(timespec="seconds")


def _capture_intent(scope: str, text: str, stem: str) -> Intent:
    """Fast-path intent: evidence sourced from a capture stem (→ source_capture_stem)."""
    return Intent(
        kind="meeting_hint",
        scope=scope,
        rationale=text[:200],
        ts=_NOW,
        payload={"text": text},
        evidence=[IntentEvidence(source="capture", ref_id=stem)],
    )


def _block_intent(scope: str, text: str, block_id: str) -> Intent:
    """Slow-path intent: evidence refs a timeline_block id (no capture stem)."""
    return Intent(
        kind="meeting_hint",
        scope=scope,
        rationale=text[:200],
        ts=_NOW,
        payload={"text": text},
        evidence=[IntentEvidence(source="timeline_block", ref_id=block_id)],
    )


def _seed_facts(conn) -> None:
    entries_mod.create_file(conn, name="skill-deploy.md", description="deploy", tags=["x"])
    entries_mod.append_entry(
        conn, name="skill-deploy.md", content="run deploy for ProjectX", tags=["x"]
    )
    entries_mod.create_file(conn, name="project-x.md", description="project x", tags=["x"])
    entries_mod.append_entry(
        conn, name="project-x.md", content="ProjectX uses DeepSeek", tags=["x"]
    )


# ─── parity: structured mirrors the string pack, string stays byte-identical ──


def test_structured_mirrors_string_and_string_byte_identical(ac_root):
    with fts.cursor() as conn:
        sink.persist_intent(
            conn, _capture_intent("timeline", "确认下周预算", "2026-06-26T14-02-00p08-00")
        )
        _seed_facts(conn)
        # The string path is byte-identical whether or not the structured call runs
        # (the sink is a pure side-channel living in a different function).
        before = recall.assemble_background(conn, scope="timeline", hints=["ProjectX"])
        items = recall.assemble_background_structured(conn, scope="timeline", hints=["ProjectX"])
        after = recall.assemble_background(conn, scope="timeline", hints=["ProjectX"])
    assert before == after
    # every structured item's content appears in the string pack (faithful mirror)
    for it in items:
        assert it.content[:40].strip()[:20] in after or it.content in after
    # layer order matches the string section order: scene < behavior < fact
    layers = [it.layer for it in items]
    assert layers.index("scene_intent") < layers.index("behavior") < layers.index("fact")


# ─── citation handles ─────────────────────────────────────────────────────────


def test_citation_handles_mem_and_intent(ac_root):
    with fts.cursor() as conn:
        sink.persist_intent(conn, _capture_intent("timeline", "确认下周预算", "stem-abc"))
        _seed_facts(conn)
        items = recall.assemble_background_structured(conn, scope="timeline", hints=["ProjectX"])
    by_layer = {it.layer: it for it in items}
    assert by_layer["fact"].cite == "mem:project-x.md"
    assert by_layer["behavior"].cite == "mem:skill-deploy.md"
    assert by_layer["scene_intent"].cite.startswith("intent:")


def test_scene_fastpath_capture_stem(ac_root):
    with fts.cursor() as conn:
        sink.persist_intent(
            conn, _capture_intent("timeline", "预算评审", "2026-06-26T14-02-00p08-00")
        )
        items = recall.assemble_background_structured(conn, scope="timeline", hints=[])
    scene = [it for it in items if it.layer == "scene_intent"][0]
    assert scene.capture_stem == "2026-06-26T14-02-00p08-00"
    assert scene.timeline_block_id is None


def test_scene_slowpath_timeline_block_id(ac_root):
    with fts.cursor() as conn:
        sink.persist_intent(conn, _block_intent("timeline", "预算评审", "99213"))
        items = recall.assemble_background_structured(conn, scope="timeline", hints=[])
    scene = [it for it in items if it.layer == "scene_intent"][0]
    assert scene.capture_stem is None
    assert scene.timeline_block_id == 99213


def test_include_raw_handles_false_suppresses_stem(ac_root):
    with fts.cursor() as conn:
        sink.persist_intent(conn, _capture_intent("timeline", "预算评审", "stem-xyz"))
        items = recall.assemble_background_structured(
            conn, scope="timeline", hints=[], include_raw_handles=False
        )
    scene = [it for it in items if it.layer == "scene_intent"][0]
    assert scene.capture_stem is None and scene.timeline_block_id is None


def test_schema_pairs_emit_schema_layer(ac_root):
    with fts.cursor() as conn:
        items = recall.assemble_background_structured(
            conn,
            scope="timeline",
            hints=[],
            schema_pairs=[("用户偏好极简工具链", "schema-toolchain.md")],
        )
    schema = [it for it in items if it.layer == "schema"]
    assert schema and schema[0].cite == "schema:schema-toolchain.md"
    assert schema[0].content == "用户偏好极简工具链"


def test_per_layer_cap(ac_root):
    with fts.cursor() as conn:
        entries_mod.create_file(conn, name="project-big.md", description="big", tags=["x"])
        for i in range(6):
            entries_mod.append_entry(
                conn, name="project-big.md", content=f"DeepSeek note {i}", tags=["x"]
            )
        items = recall.assemble_background_structured(
            conn, scope="timeline", hints=["DeepSeek"], per_hint=20, per_layer_cap=2, max_chars=5000
        )
    facts = [it for it in items if it.layer == "fact"]
    assert len(facts) <= 2


# ─── route: GET /recall/pack ──────────────────────────────────────────────────


def test_recall_pack_route_by_intent_id(ac_root):
    with fts.cursor() as conn:
        rid = sink.persist_intent(
            conn, _capture_intent("timeline", "确认下周预算 ProjectX", "stem-1")
        )
        _seed_facts(conn)
    client = TestClient(build_api_app())
    resp = client.get("/recall/pack", params={"intent_id": rid})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["scope"] == "timeline"
    assert data["intent_id"] == rid
    layers = {it["layer"] for it in data["items"]}
    assert "scene_intent" in layers
    assert "counts" in data and "budget" in data and "dense" in data


def test_recall_pack_route_unknown_intent_404(ac_root):
    client = TestClient(build_api_app())
    assert client.get("/recall/pack", params={"intent_id": 99999999}).status_code == 404


def test_recall_pack_route_requires_id_or_scope_422(ac_root):
    client = TestClient(build_api_app())
    assert client.get("/recall/pack").status_code == 422


def test_recall_pack_route_dense_inactive_without_creds(ac_root):
    # No embedding creds in the test env → dense layer no-ops, deterministic.
    with fts.cursor() as conn:
        sink.persist_intent(conn, _capture_intent("timeline", "预算", "stem-2"))
    client = TestClient(build_api_app())
    resp = client.get("/recall/pack", params={"scope": "timeline", "text": "ProjectX 预算"})
    assert resp.status_code == 200
    assert resp.json()["data"]["dense"]["active"] is False
