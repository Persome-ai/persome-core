"""Canonical model snapshot HTTP routes and the offline viewer shell."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from persome import paths
from persome.api import routes
from persome.api.model_view import render_memory_view
from persome.evomem.models import MemoryLayer, MemoryNode
from persome.evomem.store import NodeStore
from persome.model import ModelBuildCoordinator, create_build_manifest
from persome.store import fts
from persome.store import relation_edges as edges_store

BUILD_KEYS = {
    "build_id",
    "completed_at",
    "config_hash",
    "core_commit",
    "degraded_stages",
    "duration_ms",
    "input_window",
    "mode",
    "models",
    "prompt_hashes",
    "started_at",
    "status",
    "trigger",
}


@pytest.fixture(autouse=True)
def _reset_model_graph_cache():
    routes._clear_model_graph_cache()
    yield
    routes._clear_model_graph_cache()


def _save_point(
    *,
    node_id: str,
    content: str,
    file_name: str = "project-persome.md",
) -> None:
    NodeStore().save(
        MemoryNode(
            node_id=node_id,
            content=content,
            layer=MemoryLayer.L2_FACT,
            file_name=file_name,
            memory_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        )
    )


class TestGraphJson:
    def test_graph_uses_transactionally_stable_live_reader(self, ac_root, monkeypatch):
        sentinel = {"schema_version": 1, "source": "live-reader"}
        calls = []

        def fake_live_snapshot(conn, *, redact=True):  # type: ignore[no-untyped-def]
            calls.append(conn)
            assert redact is False
            return sentinel

        monkeypatch.setattr(routes, "build_live_snapshot", fake_live_snapshot)

        graph = routes.model_graph()

        assert graph["model"] is sentinel
        assert len(calls) == 1

    def test_graph_reuses_recent_owner_local_snapshot(self, ac_root, monkeypatch):
        calls = []

        def fake_live_snapshot(conn, *, redact=True):  # type: ignore[no-untyped-def]
            calls.append(conn)
            assert redact is False
            return {"schema_version": 1, "call": len(calls)}

        monkeypatch.setattr(routes, "build_live_snapshot", fake_live_snapshot)

        first = routes.model_graph()
        second = routes.model_graph()

        assert first is second
        assert first["model"]["call"] == 1
        assert len(calls) == 1

    def test_graph_cache_expires_after_bounded_ttl(self, ac_root, monkeypatch):
        calls = []
        clock = [100.0]

        def fake_live_snapshot(conn, *, redact=True):  # type: ignore[no-untyped-def]
            calls.append(conn)
            return {"schema_version": 1, "call": len(calls)}

        monkeypatch.setattr(routes, "build_live_snapshot", fake_live_snapshot)
        monkeypatch.setattr(routes.time, "monotonic", lambda: clock[0])

        first = routes.model_graph()
        clock[0] += routes._MODEL_GRAPH_CACHE_TTL_SECONDS + 0.1
        second = routes.model_graph()

        assert first["model"]["call"] == 1
        assert second["model"]["call"] == 2
        assert len(calls) == 2

    def test_graph_refresh_is_single_flight(self, ac_root, monkeypatch):
        calls = []
        started = threading.Event()
        release = threading.Event()

        def fake_live_snapshot(conn, *, redact=True):  # type: ignore[no-untyped-def]
            calls.append(conn)
            started.set()
            assert release.wait(timeout=2)
            return {"schema_version": 1, "call": len(calls)}

        monkeypatch.setattr(routes, "build_live_snapshot", fake_live_snapshot)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(routes.model_graph)
            assert started.wait(timeout=2)
            second = pool.submit(routes.model_graph)
            release.set()
            first_payload = first.result(timeout=2)
            second_payload = second.result(timeout=2)

        assert first_payload is second_payload
        assert len(calls) == 1

    def test_graph_preserves_raw_owner_local_content(self, ac_root):
        content = "/" + "Users" + "/synthetic-owner/private-note"
        _save_point(node_id="point-private", content=content)

        graph = routes.model_graph()

        assert graph["model"]["points"][0]["content"] == content
        assert graph["model"]["stats"]["redactions"] == {}

    def test_graph_is_the_canonical_snapshot(self, ac_root):
        _save_point(node_id="point-runtime", content="The runtime stores local context.")

        graph = routes.model_graph()

        assert set(graph) == {"generated_at", "model"}
        assert graph["model"]["schema_version"] == 1
        assert [point["id"] for point in graph["model"]["points"]] == ["point-runtime"]
        assert set(graph["model"]) >= {
            "points",
            "lines",
            "faces",
            "volumes",
            "root",
            "receipts",
        }

    def test_empty_store_returns_an_empty_snapshot(self, ac_root):
        graph = routes.model_graph()
        assert graph["model"]["points"] == []
        assert graph["model"]["stats"]["points"] == 0
        build = graph["model"]["build"]
        assert set(build) == BUILD_KEYS
        assert build["status"] == "not_built"
        assert build["trigger"] == "no_completed_build"
        assert build["build_id"] is None
        assert build["started_at"] is None
        assert build["completed_at"] is None

    def test_incomplete_saved_manifest_does_not_fabricate_completed_build(self, ac_root):
        paths.atomic_write_private_text(
            paths.model_build_manifest(),
            json.dumps({"status": "complete", "build_id": "incomplete"}),
        )

        build = routes.model_graph()["model"]["build"]

        assert build["status"] == "not_built"
        assert build["build_id"] is None

    def test_active_build_is_exposed_as_building(self, ac_root):
        coordinator = ModelBuildCoordinator()
        with coordinator.acquire(wait_seconds=0):
            paths.atomic_write_private_text(
                paths.model_build_manifest(),
                json.dumps(
                    {
                        "build_id": None,
                        "status": "building",
                        "trigger": "test-route",
                        "started_at": "2026-07-12T08:00:00+00:00",
                        "completed_at": None,
                        "duration_ms": 0,
                        "degraded_stages": [],
                    }
                ),
            )

            build = routes.model_graph()["model"]["build"]

        assert build["status"] == "building"
        assert set(build) == BUILD_KEYS
        assert build["trigger"] == "test-route"
        assert build["build_id"] is None

    @pytest.mark.parametrize("degraded_stages", [[], ["root_synthesis"]])
    def test_saved_build_manifest_is_preserved(self, ac_root, degraded_stages):
        manifest = create_build_manifest(
            core_commit="0123456789abcdef",
            models={"timeline": "fixture-model"},
            config={"fixture": True},
            input_window={"start": "2026-07-01T00:00:00+00:00", "end": None},
            degraded_stages=degraded_stages,
            started_at="2026-07-12T08:00:00+00:00",
            completed_at="2026-07-12T08:01:00+00:00",
            duration_ms=60_000,
            trigger="test-fixture",
            mode="mock",
        )
        paths.atomic_write_private_text(
            paths.model_build_manifest(),
            json.dumps(manifest, ensure_ascii=False),
        )

        assert routes.model_graph()["model"]["build"] == manifest


class TestViewPage:
    def test_page_uses_snapshot_native_offline_assets(self, ac_root):
        body = render_memory_view()
        assert '<base href="/model/">' in body
        assert 'href="assets/viewer.css"' in body
        assert 'src="assets/viewer.js"' in body
        assert '"./assets/three.module.js"' in body
        assert "cdn.jsdelivr.net" not in body
        assert "Personal Model" in body
        assert "Points" in body and "Volumes" in body and "Root" in body
        assert "The shape" in body and "of you." in body
        assert "Local only" in body
        assert 'id="share-x"' in body
        assert 'title="Share your constellation to X" disabled>' in body
        assert 'id="share-notice"' in body
        assert 'aria-label="Zoom controls"' in body
        assert 'id="zoom-out"' in body
        assert 'id="zoom-reset"' in body
        assert 'id="zoom-in"' in body
        assert "Scroll or pinch to zoom" in body
        assert 'role="tablist"' in body
        assert 'data-detail-tab="overview"' in body
        assert 'data-detail-tab="evidence"' in body
        assert 'data-detail-tab="history"' in body
        assert 'id="evidence-breadcrumbs"' in body

    def test_bundled_viewer_assets_are_served(self, ac_root):
        three = routes.model_asset("three.module.js")
        layout = routes.model_asset("layout.mjs")
        evidence = routes.model_asset("evidence.mjs")
        share = routes.model_asset("share.mjs")
        viewer = routes.model_asset("viewer.js")
        css = routes.model_asset("viewer.css")

        assert len(three.body) > 1_000_000
        assert b"class WebGLRenderer" in three.body
        assert b"computeClusterLayout" in layout.body
        assert b"nodeEvidenceCards" in evidence.body
        assert b"buildXIntentUrl" in share.body
        assert b"drawShareCard" in share.body
        assert b'from "./layout.mjs"' in viewer.body
        assert b'from "./share.mjs"' in viewer.body
        assert b"model.points" in viewer.body
        assert b"model.lines" in viewer.body
        assert b"model.faces" in viewer.body
        assert b"model.volumes" in viewer.body
        assert b"model.root" in viewer.body
        assert b'fetch("./graph"' in viewer.body
        assert b"MODEL_GRAPH_TIMEOUT_MS = 45_000" in viewer.body
        assert b"modelLoadPromise" in viewer.body
        assert b"controller.abort()" in viewer.body
        assert b'retry.textContent = "Retry"' in viewer.body
        assert b"fetch(`./node" in viewer.body
        assert b"fetch(`./evidence?ref=" in viewer.body
        assert b"Direct evidence" in viewer.body
        assert b"Nearby context" in viewer.body
        assert b"Technical details" in viewer.body
        assert b"evidenceTrail" in viewer.body
        assert b'fetch("/model' not in viewer.body
        assert b"ACESFilmicToneMapping" in viewer.body
        assert b"model.root?.signature" in viewer.body
        assert b'buildStatus === "not_built"' in viewer.body
        assert b'buildStatus === "building"' in viewer.body
        assert "Building…".encode() in viewer.body
        assert b'not_built: "not-built"' in viewer.body
        assert b'building: "building"' in viewer.body
        assert b'degraded: "degraded"' in viewer.body
        assert b'complete: "complete"' in viewer.body
        assert b"build-state--${buildStateClass}" in viewer.body
        assert b".build-state--not-built" in css.body
        assert b".build-state--building" in css.body
        assert b".build-state--degraded" in css.body
        assert b".build-state--complete" in css.body
        assert b"--build-color: #8d8799" in css.body
        assert b"--build-color: var(--line)" in css.body
        assert b"--build-color: var(--root)" in css.body
        assert b"--build-color: var(--point)" in css.body
        assert b"controls.zoomToCursor = true" in viewer.body
        assert b"downloadShareCard" in viewer.body
        assert b"shareReady = Boolean" in viewer.body
        assert b"window.open" in viewer.body
        assert b"my-persome-constellation.png" in share.body
        assert b"window.__persomeZoomState" in viewer.body
        assert b"if (!REDUCED_MOTION)" in viewer.body
        assert b'event.key === "+"' in viewer.body
        assert b'event.key === "-"' in viewer.body
        assert b'event.key === "0"' in viewer.body
        assert b".zoom-controls" in css.body
        assert b".share-button" in css.body
        assert b".share-notice" in css.body
        assert b".evidence-link" in css.body
        assert b".evidence-breadcrumbs" in css.body
        assert b".error button" in css.body
        assert b"(min-width: 1181px) and (max-width: 1360px)" in css.body
        assert b"top: 116px" in css.body
        assert b"prefers-reduced-motion" in css.body
        assert viewer.media_type == "text/javascript"
        assert layout.media_type == "text/javascript"
        assert share.media_type == "text/javascript"
        assert css.media_type == "text/css"

    def test_viewer_interaction_contract_keeps_nodes_ahead_of_clickable_lines(self, ac_root):
        page = render_memory_view()
        viewer = routes.model_asset("viewer.js").body.decode()
        css = routes.model_asset("viewer.css").body.decode()

        assert 'id="canvas" aria-hidden="true"' not in page
        assert 'document.createElement("button")' in viewer
        assert 'element.setAttribute("aria-controls", "detail")' in viewer
        assert 'element.setAttribute("aria-expanded", "false")' in viewer
        assert 'event.key === "Escape"' in viewer
        assert "raycaster.params.Line" not in viewer
        assert "pickables.push(line)" not in viewer
        assert "screenLinePickables.push(lineObject)" in viewer
        assert "MIN_NODE_HIT_RADIUS_PX = 12" in viewer
        assert "MIN_LINE_HIT_RADIUS_PX = 8" in viewer
        assert 'registerSelectionTarget("line", item.id, lineObject)' in viewer
        assert 'id="line-select"' in page
        assert 'lineSelectEl.addEventListener("change"' in viewer
        assert "placeholder.disabled = lines.length > 0" in viewer
        assert "linePresentation(item, model)" in viewer
        assert 'appendMeta("Predicate", lineDetail?.predicate)' in viewer
        assert 'appendMeta("From", lineDetail?.source)' in viewer
        assert "item.source ? `Source ID: ${item.source}`" in viewer
        assert "pointer-events: auto" in css
        assert ".line-explorer:focus-within" in css
        assert '.model-label[aria-expanded="true"]' in css
        assert '.detail[data-kind="line"]' in css


class TestEvidenceResolverRoute:
    def test_point_receipt_resolves_through_unified_endpoint(self, ac_root):
        _save_point(
            node_id="point-runtime",
            content="The runtime stores auditable local context.",
            file_name="synthetic/runtime.md",
        )

        detail = routes.model_evidence(ref="⟨point-runtime:synthetic/runtime.md⟩")

        assert detail["kind"] == "point"
        assert detail["id"] == "point-runtime"
        assert detail["path"] == "synthetic/runtime.md"
        assert detail["summary"] == "The runtime stores auditable local context."
        assert detail["status"] == "active"

    def test_unknown_receipt_remains_an_inspectable_missing_result(self, ac_root):
        receipt = "⟨expired:project-old.md⟩"

        detail = routes.model_evidence(ref=receipt)

        assert detail["canonical_reference"] == receipt
        assert detail["status"] == "missing"
        assert detail["metadata"]["receipt_preserved"] is True


class TestNodeReceipts:
    def test_snapshot_point_returns_its_exact_receipt(self, ac_root):
        _save_point(
            node_id="point-runtime",
            content="The runtime stores auditable local context.",
            file_name="synthetic/runtime.md",
        )

        detail = routes.model_node(id="point-runtime")

        assert detail["source"] == "synthetic/runtime.md"
        assert detail["raw"][0]["text"] == "The runtime stores auditable local context."
        assert detail["raw"][0]["receipt"] == "⟨point-runtime:synthetic/runtime.md⟩"

    def test_historical_snapshot_point_keeps_its_receipt(self, ac_root):
        _save_point(
            node_id="point-runtime-v1",
            content="The runtime used an earlier model contract.",
            file_name="synthetic/runtime.md",
        )
        with fts.cursor() as conn:
            conn.execute(
                "UPDATE evo_nodes SET is_latest = 0, status = 'superseded' WHERE node_id = ?",
                ("point-runtime-v1",),
            )

        detail = routes.model_node(id="point-runtime-v1")

        assert detail["source"] == "synthetic/runtime.md"
        assert detail["raw"][0]["receipt"] == "⟨point-runtime-v1:synthetic/runtime.md⟩"

    def test_person_node_returns_entity_trail(self, ac_root):
        NodeStore().save(
            MemoryNode(
                node_id="n-zw-1",
                content="\u5f20\u4f1f\u8d1f\u8d23\u540e\u7aef\u8bc4\u5ba1",
                layer=MemoryLayer.L4_IDENTITY,
                file_name="person-\u5f20\u4f1f.md",
                memory_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
            )
        )
        detail = routes.model_node(id="\u5f20\u4f1f")
        assert detail["source"] == "person-\u5f20\u4f1f.md"
        assert (
            detail["raw"]
            and "\u5f20\u4f1f\u8d1f\u8d23\u540e\u7aef\u8bc4\u5ba1" in detail["raw"][0]["text"]
        )
        assert detail["raw"][0]["receipt"] == "⟨n-zw-1:person-\u5f20\u4f1f.md⟩"

    def test_legacy_event_node_uses_activity_adapter(self, ac_root):
        import json

        with fts.cursor() as conn:
            conn.execute(
                """
                CREATE TABLE intents (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    dedup_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolution_outcome TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO intents (id, ts, scope, kind, confidence, status, rationale,"
                " payload, evidence, dedup_key, created_at, resolution_outcome)"
                " VALUES (77, '2026-07-01T09:00:00+00:00', 'timeline', 'meeting', 0.9,"
                " 'resolved', '\u548c\u5f20\u4f1f\u5bf9\u9f50\u63a5\u53e3', ?, '[]', 'k77',"
                " '2026-07-01T09:00:00+00:00', 'done')",
                (json.dumps({"with": ["\u5f20\u4f1f"]}, ensure_ascii=False),),
            )
        detail = routes.model_node(id="event:77")
        assert detail["source"] == "⟨77:intents⟩"
        assert (
            detail["raw"]
            and "\u548c\u5f20\u4f1f\u5bf9\u9f50\u63a5\u53e3" in detail["raw"][0]["text"]
        )
        assert detail["raw"][0]["receipt"] == "⟨77:intents⟩"

    def test_unknown_id_is_empty_fail_open(self, ac_root):
        detail = routes.model_node(id="\u4e0d\u5b58\u5728\u7684\u4eba")
        assert detail["raw"] == []


class TestNodeTree:
    """A node drill-down includes a bounded strongest-first relation tree."""

    def _seed_chain(self, conn):
        for src, dst, src_kind, dst_kind, observations in (
            ("self", "\u5f20\u4f1f", "self", "person", 5),
            ("\u5f20\u4f1f", "Bob", "person", "person", 2),
            ("self", "\u674e\u56db", "self", "person", 1),
        ):
            edges_store.add_edge(
                conn,
                src_identity=src,
                dst_identity=dst,
                predicate="knows",
                src_kind=src_kind,
                dst_kind=dst_kind,
                provenance="inferred",
                confidence=0.9,
                observations=observations,
            )

    def test_tree_rooted_at_point_walks_both_directions(self, ac_root):
        with fts.cursor() as conn:
            edges_store.ensure_schema(conn)
            self._seed_chain(conn)
        tree = routes.model_node(id="\u5f20\u4f1f")["tree"]
        firsts = [
            (edge["dir"], edge["child"]["id"], edge["observations"]) for edge in tree["edges"]
        ]
        assert ("in", "self", 5) in firsts and ("out", "Bob", 2) in firsts
        self_node = next(edge["child"] for edge in tree["edges"] if edge["child"]["id"] == "self")
        second_level = {edge["child"]["id"] for edge in self_node["edges"]}
        assert "\u674e\u56db" in second_level and "\u5f20\u4f1f" not in second_level

    def test_tree_isolated_point_is_bare_root(self, ac_root):
        detail = routes.model_node(id="\u5b64\u70b9")
        assert detail["tree"] == {"id": "\u5b64\u70b9", "edges": []}
