"""FastAPI routes for the Persome HTTP REST API.

Mounted at root ``/`` inside the MCP server's Starlette app.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response

from .. import __version__, paths
from ..capture import ax_capture, scheduler
from ..config import Config
from ..config import load as load_config
from ..logger import get
from ..model import ActivitySource, build_snapshot, normalize_activity_identity
from ..store import fts
from .chat_routes import router as chat_router
from .models import ApiResponse, CaptureIngestBody, ModelPing

logger = get("persome.api")

# Re-export config so it can be overridden during tests
_cfg: Config | None = None


def set_config(cfg: Config | None) -> None:
    global _cfg
    _cfg = cfg


def _get_cfg() -> Config:
    return _cfg or load_config()


def _read_pid() -> int | None:
    try:
        pid = int(paths.pid_file().read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def _daemon_uptime() -> str:
    pid = _read_pid()
    if not pid:
        return "stopped"
    try:
        mtime = paths.pid_file().stat().st_mtime
        now = datetime.now().astimezone()
        delta = now - datetime.fromtimestamp(mtime).astimezone()
        h, r = divmod(int(delta.total_seconds()), 3600)
        m = r // 60
        if h >= 24:
            return f"{h // 24}d {h % 24}h"
        if h:
            return f"{h}h {m}m"
        return f"{m}m"
    except OSError:
        return "unknown"


def _last_capture_info() -> tuple[str | None, str | None]:
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        return None, None
    json_files = sorted(p for p in buf.iterdir() if p.suffix == ".json")
    if not json_files:
        return None, None
    try:
        data = __import__("json").loads(json_files[-1].read_bytes())
        ts = data.get("timestamp")
        meta = data.get("window_meta") or {}
        app = meta.get("app_name")
        return ts, app
    except (OSError, ValueError):
        return json_files[-1].stem, None


def _health_status(pid: int | None, last_ts: str | None) -> str:
    if not pid:
        return "stopped"
    if not last_ts:
        return "running (no captures yet)"
    try:
        last = datetime.fromisoformat(last_ts)
        age = (datetime.now(last.tzinfo) - last).total_seconds()
    except (ValueError, TypeError):
        return "running"
    if age < 300:
        return "healthy"
    return "stale (no captures in >5m)"


router = APIRouter()


@router.get("/health", response_model=ApiResponse, tags=["system"])
def health() -> ApiResponse:
    """健康检查，返回服务存活状态。"""
    return ApiResponse(data={"status": "ok"})


@router.get("/permissions", response_model=ApiResponse, tags=["system"])
def permissions() -> ApiResponse:
    """返回 daemon 自身需要的 macOS 权限实时状态。

    辅助功能（Accessibility）由 **daemon 进程**申请——真正读 AX 树的
    ``mac-ax-helper`` / ``mac-ax-watcher`` 都由 daemon 派生，TCC 按 daemon 的
    身份记授权。GUI app 自己从不读 AX 树，所以引导页应轮询本端点反映 daemon
    的真实信任态，而不是在 app 进程里自查（那会多出一个冗余 TCC 主体、多弹一次框）。

    ``accessibility`` 取值 ``granted`` / ``denied``（非 macOS 主机恒为 ``denied``）。
    """
    return ApiResponse(data={"accessibility": "granted" if ax_capture.ax_trusted() else "denied"})


@router.get("/status", response_model=ApiResponse, tags=["system"])
def status() -> ApiResponse:
    """获取完整运行状态，包括版本、守护进程状态、运行时长、捕获状态、记忆统计、各阶段 LLM 连通性等。"""
    cfg = _get_cfg()
    pid = _read_pid()
    paused = paths.paused_flag().exists()
    uptime = _daemon_uptime()
    last_ts, last_app = _last_capture_info()
    health_label = _health_status(pid, last_ts)

    data: dict[str, Any] = {
        "version": __version__,
        "root": str(paths.root()),
        "daemon": f"running pid {pid}" if pid else "stopped",
        "uptime": uptime,
        "health": health_label,
        "capture": "paused" if paused else "active",
    }

    if last_ts:
        try:
            last_dt = datetime.fromisoformat(last_ts)
            age = (datetime.now(last_dt.tzinfo) - last_dt).total_seconds()
            if age < 60:
                ago = "just now"
            elif age < 3600:
                ago = f"{int(age // 60)}m ago"
            else:
                ago = f"{int(age // 3600)}h ago"
            data["last_capture"] = f"{ago} ({last_app})" if last_app else ago
        except (ValueError, TypeError):
            data["last_capture"] = last_ts
    else:
        data["last_capture"] = "(none)"

    buf = paths.capture_buffer_dir()
    if buf.exists():
        bufs = sorted(p for p in buf.iterdir() if p.suffix == ".json")
        last = bufs[-1].name if bufs else "(none)"
        data["buffer"] = f"{len(bufs)} files, last: {last}"

    with fts.cursor() as conn:
        sess_row = conn.execute(
            "SELECT COUNT(*), SUM(status='reduced'), SUM(status='ended'), SUM(status='failed')"
            " FROM sessions"
        ).fetchone()
        if sess_row and sess_row[0]:
            total, reduced, ended, failed = sess_row
            data["sessions"] = (
                f"{total} total ({reduced or 0} reduced, {ended or 0} ended, {failed or 0} failed)"
            )
        else:
            data["sessions"] = "(none)"
        active = fts.list_files(conn, include_dormant=False)
        dormant = [f for f in fts.list_files(conn, include_dormant=True) if f.status == "dormant"]
        total_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        data["memory"] = (
            f"{len(active)} active files, {len(dormant)} dormant, {total_entries} entries"
        )
        tlb_row = conn.execute("SELECT COUNT(*), MAX(end_time) FROM timeline_blocks").fetchone()
        tlb_count = tlb_row[0] if tlb_row else 0
        tlb_last = tlb_row[1] if tlb_row and tlb_row[1] else "(none)"
        data["timeline"] = f"{tlb_count} blocks, last end: {tlb_last}"

    # Model pings (best-effort; don't block on slow providers)
    stages = ("timeline", "reducer", "classifier", "compact")
    ping_results: dict[str, ModelPing] = {}
    try:
        from concurrent.futures import ThreadPoolExecutor

        from ..config import infer_provider, provider_api_key, provider_base_url
        from ..writer.llm import ping_stage

        dedup: dict[tuple[str, str, str], list[str]] = {}
        for stage in stages:
            m = cfg.model_for(stage)
            provider = infer_provider(m.model)
            base_url = m.base_url or (provider_base_url(provider) or "")
            api_key = provider_api_key(provider) or ""
            key = (m.model, base_url, api_key)
            dedup.setdefault(key, []).append(stage)

        if dedup:
            with ThreadPoolExecutor(max_workers=min(4, len(dedup))) as pool:
                future_to_stages = {
                    pool.submit(ping_stage, cfg, members[0]): members for members in dedup.values()
                }
                for future, members in future_to_stages.items():
                    try:
                        res = future.result(timeout=8.0)
                    except Exception:
                        res = None
                    for stage in members:
                        if res is None:
                            ping_results[stage] = ModelPing(
                                stage=stage, model=cfg.model_for(stage).model, ok=False
                            )
                        else:
                            ping_results[stage] = ModelPing(
                                stage=stage,
                                model=res.model,
                                ok=res.ok,
                                latency_ms=res.latency_ms,
                                error=res.error,
                            )
    except Exception as exc:
        logger.warning("model ping failed in status endpoint: %s", exc)

    data["models"] = ping_results
    return ApiResponse(data=data)


# ─── Capture ingest ───────────────────────────────────────────────────────


@router.post("/captures/ingest", response_model=ApiResponse, tags=["capture"])
def ingest_capture(body: CaptureIngestBody) -> ApiResponse:
    """接收可信本地生产者采集的一帧 capture，完成富化、去重和持久化。

    采集层（AX 树 + 焦点窗口截图）已搬进持有 Accessibility / Screen-Recording 的 Swift
    进程（``capture.source = "ingest"``）；daemon 自身不再 spawn watcher、不再抓屏，因而
    不需要任何系统权限。落库和去重与 daemon 自采路径共用同一 runner。
    """
    result = scheduler.ingest_capture(_get_cfg(), body.model_dump())
    return ApiResponse(data=result)


# ─── Paper model ──────────────────────────────────────────────────────────


@router.get("/model", response_class=HTMLResponse, tags=["model"])
def model_view() -> HTMLResponse:
    """Render the local Point/Line/Face/Volume/Root model explorer."""
    from .model_view import MEMORY_VIEW_HTML

    return HTMLResponse(MEMORY_VIEW_HTML)


_MODEL_ASSETS = {
    "three.module.js",
    "jsm/controls/OrbitControls.js",
    "jsm/geometries/ConvexGeometry.js",
    "jsm/math/ConvexHull.js",
    "jsm/renderers/CSS2DRenderer.js",
}


def _model_asset_path(asset_path: str) -> Path | None:
    if asset_path not in _MODEL_ASSETS:
        return None
    roots = (
        Path(__file__).resolve().parents[1] / "_bundled" / "model_assets",
        Path(__file__).resolve().parents[3] / "resources" / "model_assets",
    )
    for root in roots:
        candidate = root / asset_path
        if candidate.is_file():
            return candidate
    return None


@router.get("/model/assets/{asset_path:path}", include_in_schema=False, tags=["model"])
def model_asset(asset_path: str) -> Response:
    """Serve the pinned Three.js runtime bundled with the Python package."""
    path = _model_asset_path(asset_path)
    if path is None:
        raise HTTPException(status_code=404, detail="no such model asset")
    return Response(content=path.read_bytes(), media_type="text/javascript")


@router.get("/model/graph", tags=["model"])
def model_graph() -> dict[str, Any]:
    """Read-only model graph: nodes (USER + roster identities +
    Activity endpoints), relation_edges (both statuses — the shadow/ACTIVE
    split IS the point), and the schema_faces tower, each carrying its
    bitemporal fields so the client can replay f(T) without refetching.
    Zero-LLM; fail-open per section (a store predating a table contributes
    an empty list)."""
    from ..evomem import identity as identity_mod
    from ..store import fts as fts_store

    edges: list[dict[str, Any]] = []
    faces: list[dict[str, Any]] = []
    snapshot: dict[str, Any]
    with fts_store.cursor() as conn:
        conn.row_factory = sqlite3.Row
        try:
            from ..store import relation_edges as _edges_store

            _edges_store.ensure_schema(conn)  # backfills kind/polarity columns on old DBs
            edges = [
                {
                    "a": r["src_identity"],
                    "b": r["dst_identity"],
                    "predicate": r["predicate"],
                    "label": r["label"],
                    "status": r["status"],
                    "provenance": r["provenance"],
                    "confidence": r["confidence"],
                    "observations": r["observations"],
                    "recall_count": r["recall_count"],
                    "valid_from": r["valid_from"],
                    "valid_to": r["valid_to"],
                    "last_observed_at": r["last_observed_at"],
                    "src_kind": r["src_kind"],
                    "dst_kind": r["dst_kind"],
                    "polarity": r["polarity"] or "0",
                    "source_kind": r["source_kind"],
                    "source_id": r["source_id"],
                    "source_receipt": r["source_receipt"],
                }
                for r in conn.execute(
                    "SELECT src_identity, dst_identity, predicate, label, status, provenance,"
                    " confidence, observations, recall_count, valid_from, valid_to,"
                    " last_observed_at, src_kind, dst_kind, polarity, source_kind, source_id,"
                    " source_receipt FROM relation_edges"
                )
            ]
        except Exception:  # noqa: BLE001 — table may predate this build
            edges = []
        try:
            from ..store import schema_faces as _faces_store

            _faces_store.ensure_schema(conn)  # backfills the anchors column on old DBs
            # Optional cache of each face's member facts' REAL relative semantic
            # positions (embed → local PCA), written by the semantic-layout precompute.
            # When present, the renderer scatters a face's facts at these REAL positions
            # (a hull over them = the emergent cluster) instead of a fabricated sunflower.
            fact_pos: dict = {}
            try:
                from .. import paths as _paths

                _fp = _paths.root() / "fact_positions.json"
                if _fp.exists():
                    fact_pos = json.loads(_fp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                fact_pos = {}
            faces = [
                {
                    "id": r["face_id"],
                    "level": r["level"],
                    "signature": r["signature"],
                    "provenance": r["provenance"],
                    "status": r["status"],
                    "observations": r["observations"],
                    "confidence": r["confidence"],
                    "created_at": r["created_at"],
                    "valid_to": r["valid_to"],
                    "anchors": json.loads(r["anchors"] or "[]"),
                    # n_members = the fact cluster the face was mined from (点=fact,
                    # 维度判据: 面=fact-set≥3). The entity anchors are a lossy 1-2 point
                    # projection; the fact count is the face's TRUE dimension.
                    "n_members": len(json.loads(r["members"] or "[]")),
                    # fact_pts = the member facts' REAL relative semantic positions
                    # (embedding local-PCA, normalized). The face is the hull over THESE,
                    # so it emerges from where the facts actually sit in meaning-space.
                    "fact_pts": fact_pos.get(r["face_id"], []),
                }
                for r in conn.execute(
                    "SELECT face_id, level, signature, provenance, status, observations,"
                    " confidence, created_at, valid_to, anchors, members"
                    " FROM schema_faces WHERE valid_to IS NULL"
                )
            ]
        except Exception:  # noqa: BLE001
            faces = []
        snapshot = build_snapshot(conn)

    try:
        roster = identity_mod.load_roster(load_config())
        canonicals = set(roster.canonicals)
    except Exception:  # noqa: BLE001
        canonicals = set()
    ids = {"self"} | canonicals
    # typed non-person points (§7-6 kind-axis producer, 过渡腿): org-*/project-*
    # entity files enter the graph AS their kind. Edge-less ones land in the
    # orphan shell — honest per §1.5-2 (grow a substantive edge or be
    # forgotten), never fabricated edges.
    # 边端点存的是 canonical（"OpenAI Developers"），typed 点文件名是 slug
    # （"org-openai-developers.md"）——两者不统一会把点错判成孤儿（英文带空格名
    # slug≠canonical；CJK 名 slug=canonical 才碰巧没事）。边的 canonical 是身份权威：
    # 建 slug→canonical 映射，typed 点 id 取回它引用的 canonical，与边对齐。#bug
    from ..evomem.person_graph import _slug as _canon_slug

    slug_to_canon: dict[str, str] = {}
    for e in edges:
        for ident in (e["a"], e["b"]):
            if ident and ident != "self" and not ident.startswith("event:"):
                slug_to_canon.setdefault(_canon_slug(ident), ident)
    # 面锚与节点 id 同源对齐：anchors 存的是 slug（_face_anchors 的 stem.removeprefix），
    # 但 typed 点 id 走 slug→canonical（chronicleclient→ChronicleClient，大小写/空格名）。
    # 不归一 → 锚对不上节点 → 面飘空。用同一张映射把 anchors 也映成 canonical。#bug
    for f in faces:
        f["anchors"] = [slug_to_canon.get(_canon_slug(a), a) for a in (f.get("anchors") or [])]
    typed_kinds: dict[str, str] = {}
    try:
        with fts_store.cursor() as conn:
            for row in conn.execute(
                "SELECT DISTINCT file_name FROM evo_nodes"
                " WHERE (file_name LIKE 'org-%' OR file_name LIKE 'project-%'"
                "  OR file_name LIKE 'tool-%')"
                " AND is_latest = 1 AND status = 'active'"
            ):
                stem = row[0].removesuffix(".md")
                for prefix, kind in (
                    ("org-", "org"),
                    ("project-", "project"),
                    ("tool-", "artifact"),
                ):
                    if stem.startswith(prefix):
                        slug = stem.removeprefix(prefix)
                        # 点 id = 该 slug 对应的 canonical（与边对齐）；无边者回退 slug
                        nid = slug_to_canon.get(slug, slug)
                        if nid:
                            ids.add(nid)
                            typed_kinds[nid] = kind
    except Exception:  # noqa: BLE001 — typed points decorate, fail-open
        typed_kinds = {}
    # node 种类 axis (§1.2 / §7-6): recover each identity's EntityKind from the
    # persisted src_kind/dst_kind edge columns; heuristic fallback for rows
    # predating the columns (event:* prefix → event, roster → person).
    kind_by_id: dict[str, str] = {}
    for e in edges:
        ids.add(e["a"])
        ids.add(e["b"])
        for nid, k in ((e["a"], e.get("src_kind")), (e["b"], e.get("dst_kind"))):
            if k and kind_by_id.get(nid) in (None, "person"):
                kind_by_id[nid] = k
    nodes = []
    for nid in sorted(ids):
        if nid == "self":
            kind, label = "self", "USER"
        elif nid.startswith("event:"):
            kind, label = "event", nid
        else:
            kind = kind_by_id.get(nid) or typed_kinds.get(nid) or "person"
            label = nid
        nodes.append({"id": nid, "kind": kind, "label": label})
    # §7-8 检索权重状态（看板 stats 行）：当前融合配置 + 边的转正/喂食面
    from ..store import fts as _fts

    search_state = {
        "slot_pool_weight": _fts._POOL_WEIGHTS.get("slot"),  # noqa: SLF001
        "relation_pool_weight": _fts._POOL_WEIGHTS.get("relation"),  # noqa: SLF001
        "relation_include_shadow": bool(_fts._POOL_WEIGHTS.get("relation_shadow")),  # noqa: SLF001
        "contains_pool_rerank": bool(_fts._POOL_WEIGHTS.get("contains_rerank")),  # noqa: SLF001
        "active_edges": sum(1 for e in edges if e["status"] == "active"),
        "shadow_edges": sum(1 for e in edges if e["status"] == "shadow"),
    }
    # sem_geo = the unified semantic fact-space (fact point cloud + k-NN edges + emergent
    # face clusters), precomputed to <root>/sem_facts.json by `persome memory-viz`
    # (src/persome/viz/sem_layout.py). XZ = semantic layout, y = normalized deposition time
    # (driven by the frontend's as-of slider), brightness ∝ connection degree. Fail-open:
    # absent/corrupt file → {} and the explorer falls back to the entity force layout.
    sem_geo: dict = {}
    try:
        from .. import paths as _paths2

        _sf = _paths2.root() / "sem_facts.json"
        if _sf.exists():
            sem_geo = json.loads(_sf.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        sem_geo = {}
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "nodes": nodes,
        "edges": edges,
        "faces": faces,
        "sem_geo": sem_geo,
        "search": search_state,
        "model": snapshot,
    }


@router.get("/model/node", tags=["model"])
def model_node(id: str) -> dict[str, Any]:
    """Raw receipts behind one graph node (§2.1 每个向量指回符号收据 — the
    click-through from the 记忆图 to the symbolic layer). Lazy per-node fetch
    so the graph payload stays lean. Zero-LLM, read-only, fail-open:

    - ``event:<source-kind>:<source-id>`` → the canonical Activity source;
    - any other identity → the latest ACTIVE evo_nodes of its typed entity file
      entity file (newest first, bounded, truncated) — the consolidation
      trail the point was distilled from.
    """
    from ..store import fts as fts_store

    raw: list[dict[str, Any]] = []
    source = ""
    try:
        with fts_store.cursor() as conn:
            conn.row_factory = sqlite3.Row
            if id.startswith("event:"):
                normalized = normalize_activity_identity(id)
                event = next(
                    (
                        item
                        for item in ActivitySource(conn).events()
                        if item.stable_id == normalized
                    ),
                    None,
                )
                if event is not None:
                    source = event.source_receipt
                    raw.append(
                        {
                            "ts": event.occurred_at,
                            "text": event.summary[:300],
                            "receipt": event.source_receipt,
                        }
                    )
            elif id != "self":
                from ..evomem.person_graph import _slug

                slug = _slug(id)
                candidates = [f"{kind}-{slug}.md" for kind in ("person", "org", "project", "tool")]
                for row in conn.execute(
                    "SELECT content, memory_at, file_name, node_id FROM evo_nodes"
                    " WHERE file_name IN (?, ?, ?, ?) AND is_latest = 1 AND status = 'active'"
                    " ORDER BY memory_at DESC LIMIT 5",
                    candidates,
                ):
                    source = str(row["file_name"])
                    raw.append(
                        {
                            "ts": row["memory_at"],
                            "text": (row["content"] or "").strip()[:300],
                            "receipt": f"⟨{row['node_id']}:{row['file_name']}⟩",
                        }
                    )
    except Exception:  # noqa: BLE001 — receipts decorate the view, never 500 it
        raw = []
    tree = _node_tree(id)
    return {"id": id, "source": source, "raw": raw, "tree": tree}


_TREE_DEPTH = 2
_TREE_FANOUT = 8


def _node_tree(root: str) -> dict[str, Any]:
    """The relation tree rooted at ONE point (§1.2: 点开一个事物 → 以它为根的
    整棵树). Bounded BFS over relation_edges — both statuses (the dev view
    exists to show the shadow/active split), strongest-first per node
    (observations desc), fan-out ≤ 8, depth ≤ 2, cycle-guarded. Each hop
    carries predicate/direction/label/strength/status so the path reads as a
    narrative (§3.4 路径即叙事, rooted at the point instead of USER)."""
    from ..store import fts as fts_store

    def expand(conn, nid: str, seen: set[str], depth: int) -> dict[str, Any]:
        node: dict[str, Any] = {"id": nid, "edges": []}
        if depth >= _TREE_DEPTH:
            return node
        try:
            rows = conn.execute(
                "SELECT src_identity, dst_identity, predicate, label, status,"
                " observations, valid_to FROM relation_edges"
                " WHERE (src_identity = ? OR dst_identity = ?)"
                " ORDER BY observations DESC, edge_id LIMIT ?",
                (nid, nid, _TREE_FANOUT * 2),
            ).fetchall()
        except Exception:  # noqa: BLE001 — table may predate this build
            return node
        for r in rows:
            if len(node["edges"]) >= _TREE_FANOUT:
                break
            src, dst = str(r[0]), str(r[1])
            other = dst if src == nid else src
            if other in seen:
                continue
            seen.add(other)
            node["edges"].append(
                {
                    "predicate": str(r[2]),
                    "label": r[3],
                    "dir": "out" if src == nid else "in",
                    "status": str(r[4]),
                    "observations": int(r[5] or 1),
                    "historical": r[6] is not None,
                    "child": expand(conn, other, seen, depth + 1),
                }
            )
        return node

    try:
        with fts_store.cursor() as conn:
            return expand(conn, root, {root}, 0)
    except Exception:  # noqa: BLE001
        return {"id": root, "edges": []}


router.include_router(chat_router)
