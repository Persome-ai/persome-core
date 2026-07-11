"""FastAPI routes for the Persome HTTP REST API.

Mounted at root ``/`` inside the MCP server's Starlette app.
"""

from __future__ import annotations

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
    """Return the service liveness status."""
    return ApiResponse(data={"status": "ok"})


@router.get("/permissions", response_model=ApiResponse, tags=["system"])
def permissions() -> ApiResponse:
    """Return the daemon's current macOS permission state.

    Accessibility belongs to the daemon process because it launches the AX
    helpers that read the tree. A GUI onboarding flow should poll this endpoint
    instead of creating a second TCC identity. ``accessibility`` is ``granted``
    or ``denied`` and is always ``denied`` on non-macOS hosts.
    """
    return ApiResponse(data={"accessibility": "granted" if ax_capture.ax_trusted() else "denied"})


@router.get("/status", response_model=ApiResponse, tags=["system"])
def status() -> ApiResponse:
    """Return version, daemon, capture, memory, and LLM connectivity status."""
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
    """Ingest one frame from a trusted local producer.

    This replay/embedding path shares enrichment, deduplication, and persistence
    with the daemon's native macOS AX watcher.
    """
    result = scheduler.ingest_capture(_get_cfg(), body.model_dump())
    return ApiResponse(data=result)


# ─── Personal model ───────────────────────────────────────────────────────


@router.get("/model", response_class=HTMLResponse, tags=["model"])
def model_view() -> HTMLResponse:
    """Render the local Point/Line/Face/Volume/Root model explorer."""
    from .model_view import MEMORY_VIEW_HTML

    return HTMLResponse(MEMORY_VIEW_HTML)


_MODEL_ASSETS = {
    "three.module.js",
    "layout.mjs",
    "viewer.css",
    "viewer.js",
    "jsm/controls/OrbitControls.js",
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
    """Serve the offline viewer and pinned Three.js runtime bundled in the wheel."""
    path = _model_asset_path(asset_path)
    if path is None:
        raise HTTPException(status_code=404, detail="no such model asset")
    media_type = "text/css" if path.suffix == ".css" else "text/javascript"
    return Response(content=path.read_bytes(), media_type=media_type)


@router.get("/model/graph", tags=["model"])
def model_graph() -> dict[str, Any]:
    """Return the canonical versioned Point/Line/Face/Volume/Root snapshot."""
    from ..store import fts as fts_store

    with fts_store.cursor() as conn:
        snapshot = build_snapshot(conn)
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "model": snapshot,
    }


@router.get("/model/node", tags=["model"])
def model_node(id: str) -> dict[str, Any]:
    """Return the symbolic receipts behind one graph node.

    This is the click-through from the visual graph to the symbolic layer. Lazy per-node fetch
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
            if id != "self":
                try:
                    row = conn.execute(
                        "SELECT content, memory_at, file_name, node_id FROM evo_nodes"
                        " WHERE node_id = ? LIMIT 1",
                        (id,),
                    ).fetchone()
                except sqlite3.Error:
                    row = None
                if row is not None:
                    source = str(row["file_name"])
                    raw.append(
                        {
                            "ts": row["memory_at"],
                            "text": (row["content"] or "").strip()[:300],
                            "receipt": f"⟨{row['node_id']}:{row['file_name']}⟩",
                        }
                    )
            if not raw and id.startswith("event:"):
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
            elif not raw and id != "self":
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
    """Return the relation tree rooted at one Point.

    Uses bounded BFS over relation_edges with both statuses (the dev view
    exists to show the shadow/active split), strongest-first per node
    (observations desc), fan-out ≤ 8, depth ≤ 2, cycle-guarded. Each hop
    carries predicate/direction/label/strength/status so the path reads as a
    narrative rooted at the Point instead of the memory owner."""
    from ..store import fts as fts_store

    def expand(
        conn: sqlite3.Connection,
        nid: str,
        seen: set[str],
        depth: int,
    ) -> dict[str, Any]:
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
