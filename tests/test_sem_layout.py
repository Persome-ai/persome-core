"""persome memory-viz — semantic fact-space layout precompute (src/persome/viz/sem_layout.py).

Deterministic, zero-LLM, zero-network; all data synthetic. Pins:

- end-to-end generate() off a small synthetic store (20 facts + vectors) →
  sem_facts.json with the exact schema the dashboard's renderSemSpace consumes
  (facts i/x/y/z/t, edges [i,j,w], faces id/sig/level/members, edge_source);
- the vectors path (cosine k-NN, k=6, sim ≥ 0.5) and the honest
  ``edge_source="vectors"`` label;
- the symbolic-graph fallback on stores without embeddings (schema_faces
  co-membership + ACTIVE relation_edges mention bridging, w=0.6,
  ``edge_source="graph"``);
- idempotency (rerun on an unchanged store → byte-identical output);
- /dev/memory-graph surfacing the file as ``sem_geo`` (fail-open when absent);
- the dashboard page's <script type="module"> block parsing under
  ``node --check`` — the JS lives inside a Python string, so py-level tests
  alone would stay green while the browser renders a blank page on a
  SyntaxError.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from persome import paths
from persome.evomem.models import MemoryLayer, MemoryNode
from persome.evomem.store import NodeStore
from persome.store import fts
from persome.store import relation_edges as edges_store
from persome.store import schema_faces as faces_store
from persome.store import vectors as vectors_store
from persome.viz import sem_layout

_BASE = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def _seed_facts(texts: list[str]) -> list[str]:
    """Save one live L2 fact per text (minute-spaced deposition), return node ids."""
    store = NodeStore()
    ids = []
    for i, text in enumerate(texts):
        node_id = f"20260105-{i:04d}-facade"
        store.save(
            MemoryNode(
                node_id=node_id,
                content=text,
                layer=MemoryLayer.L2_FACT,
                memory_at=_BASE + timedelta(minutes=i),
            )
        )
        ids.append(node_id)
    return ids


def _clustered_vectors(n: int, clusters: int = 3, dim: int = 8) -> list[np.ndarray]:
    """Synthetic unit vectors in ``clusters`` orthogonal groups: within-cluster
    cosine ≈ 1 (>> 0.5), cross-cluster ≈ 0 (< 0.5) — clean k-NN components."""
    rng = np.random.default_rng(7)
    out = []
    for i in range(n):
        base = np.zeros(dim)
        base[i % clusters] = 1.0
        vec = base + rng.normal(0, 0.03, dim)
        out.append(vec / np.linalg.norm(vec))
    return out


def _generate() -> dict:
    out = paths.root() / "sem_facts.json"
    sem_layout.generate(paths.index_db(), out)
    return json.loads(out.read_text(encoding="utf-8"))


class TestVectorsPath:
    def test_end_to_end_schema_and_knn_edges(self, ac_root):
        texts = [f"合成事实 {i}：项目 alpha 推进第 {i} 步" for i in range(20)]
        ids = _seed_facts(texts)
        vecs = _clustered_vectors(20)
        with fts.cursor() as conn:
            vectors_store.ensure_schema(conn)
            vectors_store.save_vectors(
                conn,
                list(zip(ids, vecs, strict=True)),
                embedded_at="2026-01-05T10:00:00+00:00",
            )
        payload = _generate()

        assert set(payload) == {"facts", "edges", "faces", "edge_source"}
        assert payload["edge_source"] == "vectors"

        facts = payload["facts"]
        assert len(facts) == 20
        for f in facts:
            assert isinstance(f["i"], int)
            assert -1.0 <= f["x"] <= 1.0 and -1.0 <= f["z"] <= 1.0
            assert 0.0 <= f["y"] <= 1.0  # normalized deposition time
            assert f["t"].startswith("合成事实")
            assert f["t2"]  # raw timestamp rides along for the hover detail
        # deposition normalization spans the full [0, 1] range
        ys = sorted(f["y"] for f in facts)
        assert ys[0] == 0.0 and ys[-1] == 1.0

        assert payload["edges"], "clustered vectors must produce k-NN edges"
        for i, j, w in payload["edges"]:
            assert 0 <= i < j < 20
            assert w >= sem_layout.KNN_MIN_SIM

        # three orthogonal vector clusters (~7 members each) → three components
        # → three Louvain faces, every one above the ≥5-member floor
        faces = payload["faces"]
        assert len(faces) == 3
        for face in faces:
            assert face["id"].startswith("sem-")
            assert face["sig"]
            assert len(face["sig"]) <= 40
            assert face["level"] == 1
            assert len(face["members"]) >= sem_layout.FACE_MIN_MEMBERS
            assert all(isinstance(m, int) and 0 <= m < 20 for m in face["members"])
        # faces partition disjointly (a fact belongs to one spatial cluster)
        all_members = [m for face in faces for m in face["members"]]
        assert len(all_members) == len(set(all_members))


class TestGraphFallback:
    def test_store_without_embeddings_uses_symbolic_graph(self, ac_root):
        texts = [
            "小明 提交了后端评审意见",
            "小红 更新了接口文档",
            "周会纪要：确定发布窗口",
            "数据看板新增留存曲线",
            "构建脚本迁移到新工具链",
            "小明 和 小红 对齐了联调时间",
            "发布检查单补充回滚步骤",
            "性能基线重新冻结",
        ]
        _seed_facts(texts)
        with fts.cursor() as conn:
            faces_store.ensure_schema(conn)
            edges_store.ensure_schema(conn)
            # a live face whose footprint covers five of the facts (member keys
            # are the fact-body hashes, exactly what the miner records)
            faces_store.record_face(
                conn,
                source="mined",
                signature="工作日循环：评审→文档→周会",
                members=[faces_store.member_key(t) for t in texts[:5]],
            )
            # an ACTIVE identity edge whose endpoints are mentioned in fact text
            edges_store.add_edge(
                conn,
                src_identity="小明",
                dst_identity="小红",
                predicate=edges_store.Predicate.KNOWS,
                src_kind=edges_store.EntityKind.PERSON,
                dst_kind=edges_store.EntityKind.PERSON,
                provenance="inferred",
                confidence=0.9,
                status="active",
            )
        payload = _generate()

        assert payload["edge_source"] == "graph"
        assert payload["edges"], "co-face members + relation mentions must connect"
        assert all(w == sem_layout.GRAPH_EDGE_W for _i, _j, w in payload["edges"])

        # the five co-face facts connect pairwise: C(5,2) = 10 edges minimum
        idx_by_text = {f["t"]: f["i"] for f in payload["facts"]}
        face_idxs = {idx_by_text[t] for t in texts[:5]}
        coface = [(i, j) for i, j, _w in payload["edges"] if i in face_idxs and j in face_idxs]
        assert len(coface) == 10
        # the relation edge bridges a 小明-mentioning fact to a 小红-mentioning one
        mention_pairs = [
            (i, j) for i, j, _w in payload["edges"] if not (i in face_idxs and j in face_idxs)
        ]
        assert mention_pairs

    def test_empty_store_writes_empty_payload(self, ac_root):
        with fts.cursor() as conn:  # create an index.db with no evomem tables
            conn.execute("SELECT 1")
        payload = _generate()
        assert payload == {
            "facts": [],
            "edges": [],
            "faces": [],
            "edge_source": "graph",
        }


class TestIdempotency:
    def test_rerun_on_unchanged_store_is_byte_identical(self, ac_root):
        ids = _seed_facts([f"事实 {i}" for i in range(12)])
        with fts.cursor() as conn:
            vectors_store.ensure_schema(conn)
            vectors_store.save_vectors(
                conn,
                list(zip(ids, _clustered_vectors(12), strict=True)),
                embedded_at="2026-01-05T10:00:00+00:00",
            )
        out = paths.root() / "sem_facts.json"
        sem_layout.generate(paths.index_db(), out)
        first = out.read_bytes()
        sem_layout.generate(paths.index_db(), out)
        assert out.read_bytes() == first

    def test_missing_db_raises(self, ac_root, tmp_path):
        with pytest.raises(FileNotFoundError):
            sem_layout.generate(tmp_path / "absent.db", tmp_path / "out.json")


class TestRouteSurfacesSemGeo:
    """/dev/memory-graph carries the precomputed file as ``sem_geo`` (fail-open)."""

    def test_sem_geo_rides_the_graph_payload(self, ac_root, monkeypatch):
        from persome.api import routes

        monkeypatch.setattr(routes, "_dev_enabled", lambda: True)
        g = routes.dev_memory_graph()
        assert g["sem_geo"] == {}  # absent file → empty, view falls back

        ids = _seed_facts([f"事实 {i}" for i in range(6)])
        with fts.cursor() as conn:
            vectors_store.ensure_schema(conn)
            vectors_store.save_vectors(
                conn,
                list(zip(ids, _clustered_vectors(6, clusters=2), strict=True)),
                embedded_at="2026-01-05T10:00:00+00:00",
            )
        sem_layout.generate(paths.index_db(), paths.root() / "sem_facts.json")
        g = routes.dev_memory_graph()
        assert len(g["sem_geo"]["facts"]) == 6
        assert g["sem_geo"]["edge_source"] == "vectors"

    def test_corrupt_file_is_fail_open(self, ac_root, monkeypatch):
        from persome.api import routes

        monkeypatch.setattr(routes, "_dev_enabled", lambda: True)
        (paths.root() / "sem_facts.json").write_text("{not json", encoding="utf-8")
        assert routes.dev_memory_graph()["sem_geo"] == {}


class TestPageJavaScriptParses:
    """The 记忆图 page's JS lives inside a Python string: py_compile / TestClient
    stay green on a JS SyntaxError while the browser renders a blank page. Extract
    every <script type="module"> block and let node parse it for real."""

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
    def test_module_scripts_pass_node_check(self, tmp_path):
        from persome.api.dev_memory_view import MEMORY_VIEW_HTML

        blocks = re.findall(r'<script type="module">(.*?)</script>', MEMORY_VIEW_HTML, re.S)
        assert blocks, "the page must carry at least one module script"
        for k, block in enumerate(blocks):
            path = tmp_path / f"block{k}.mjs"  # .mjs → node checks ESM syntax
            path.write_text(block, encoding="utf-8")
            proc = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
            assert proc.returncode == 0, f"module block {k} SyntaxError:\n{proc.stderr}"
