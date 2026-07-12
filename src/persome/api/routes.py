"""FastAPI routes for the Persome HTTP REST API.

Mounted at root ``/`` inside the MCP server's Starlette app.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import __version__, paths, runtime_pid
from ..capture import ax_capture, ocr_health, scheduler, screen_recording
from ..capture.timestamps import newest_capture_path
from ..config import Config
from ..config import load as load_config
from ..logger import get
from ..model import ActivitySource, build_snapshot, normalize_activity_identity
from ..security.auth import (
    BROWSER_BOOTSTRAP_PATH,
    BROWSER_BOOTSTRAP_TTL_SECONDS,
    BROWSER_SESSION_COOKIE,
    BROWSER_SESSION_TTL_SECONDS,
    consume_browser_bootstrap_nonce,
    issue_browser_bootstrap_nonce,
)
from ..store import fts
from .models import ApiResponse, CaptureIngestBody, ModelPing

logger = get("persome.api")

_MODEL_PING_CACHE_TTL_SECONDS = 60.0
_MODEL_PING_CACHE_MAX_ENTRIES = 8
_model_ping_cache_lock = threading.Lock()
_model_ping_cache: dict[
    tuple[tuple[str, str, str, str, str], ...],
    tuple[float, dict[str, ModelPing]],
] = {}

# Re-export config so it can be overridden during tests
_cfg: Config | None = None


def set_config(cfg: Config | None) -> None:
    global _cfg
    _cfg = cfg


def _get_cfg() -> Config:
    return _cfg or load_config()


def _read_pid() -> int | None:
    process = runtime_pid.resolve_recorded_process()
    return process.pid if process is not None else None


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
    latest = newest_capture_path(p for p in buf.iterdir() if p.suffix == ".json")
    if latest is None:
        return None, None
    try:
        data = __import__("json").loads(latest.read_bytes())
        ts = data.get("timestamp")
        meta = data.get("window_meta") or {}
        app = meta.get("app_name")
        return ts, app
    except (OSError, ValueError):
        return latest.stem, None


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


def _status_model_pings(cfg: Config) -> tuple[dict[str, Any], dict[str, ModelPing]]:
    """Return the selected profile plus bounded, TTL-cached provider pings.

    ``/status`` is polled by local clients.  A health UI must not turn every
    refresh into paid provider traffic, and concurrent refreshes should collapse
    into one probe.  Holding the small process-local lock while probing also
    prevents a thundering herd after cache expiry.
    """
    from ..llm_setup import profile_dict
    from ..providers import resolve_profile
    from ..writer.llm import ping_stage

    stages = ("timeline", "reducer", "classifier", "compact")
    selected = resolve_profile(cfg.model_for("default"))
    profile = profile_dict(selected)

    resolved: dict[str, Any] = {}
    cache_parts: list[tuple[str, str, str, str, str]] = []
    for stage in stages:
        item = resolve_profile(cfg.model_for(stage))
        resolved[stage] = item
        key_fingerprint = sha256((item.api_key or "").encode("utf-8")).hexdigest()
        cache_parts.append((stage, item.protocol, item.model, item.base_url, key_fingerprint))
    cache_key = tuple(cache_parts)

    with _model_ping_cache_lock:
        now = time.monotonic()
        cached = _model_ping_cache.get(cache_key)
        if cached is not None and now - cached[0] < _MODEL_PING_CACHE_TTL_SECONDS:
            return profile, dict(cached[1])

        dedup: dict[tuple[str, str, str, str], list[str]] = {}
        for stage, item in resolved.items():
            provider_key = (
                item.protocol,
                item.model,
                item.base_url,
                sha256((item.api_key or "").encode("utf-8")).hexdigest(),
            )
            dedup.setdefault(provider_key, []).append(stage)

        results: dict[str, ModelPing] = {}
        if dedup:
            with ThreadPoolExecutor(max_workers=min(4, len(dedup))) as pool:
                future_to_stages = {
                    pool.submit(ping_stage, cfg, members[0]): members for members in dedup.values()
                }
                for future, members in future_to_stages.items():
                    try:
                        result = future.result(timeout=8.0)
                    except Exception:  # noqa: BLE001 - status degrades instead of failing
                        result = None
                    for stage in members:
                        if result is None:
                            results[stage] = ModelPing(
                                stage=stage,
                                model=cfg.model_for(stage).model,
                                ok=False,
                            )
                        else:
                            results[stage] = ModelPing(
                                stage=stage,
                                model=result.model,
                                ok=result.ok,
                                latency_ms=result.latency_ms,
                                error=result.error,
                            )

        if len(_model_ping_cache) >= _MODEL_PING_CACHE_MAX_ENTRIES:
            oldest = min(_model_ping_cache, key=lambda key: _model_ping_cache[key][0])
            _model_ping_cache.pop(oldest, None)
        _model_ping_cache[cache_key] = (now, dict(results))
        return profile, results


router = APIRouter()


@router.get("/health", response_model=ApiResponse, tags=["system"])
def health() -> ApiResponse:
    """Return liveness plus the configured local-OCR readiness state."""
    current = ocr_health.inspect(_get_cfg().capture)
    worker = ocr_health.worker_state()
    status = "degraded" if current.enabled and (not current.ready or worker != "ready") else "ok"
    return ApiResponse(
        data={
            "status": status,
            "ocr": current.state,
            "ocr_worker": worker,
            "ocr_enabled": current.enabled,
            "ocr_tier": current.tier,
        }
    )


@router.post(BROWSER_BOOTSTRAP_PATH, response_model=ApiResponse, tags=["system"])
def create_browser_bootstrap(response: Response) -> ApiResponse:
    """Issue a short-lived URL that opens the authenticated local model viewer.

    This POST itself requires the normal bearer token.  The returned URL holds
    only a one-minute, single-use nonce — never the long-lived daemon token.
    """
    nonce = issue_browser_bootstrap_nonce()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return ApiResponse(
        data={
            "bootstrap_url": f"{BROWSER_BOOTSTRAP_PATH}?{urlencode({'nonce': nonce})}",
            "expires_in_seconds": BROWSER_BOOTSTRAP_TTL_SECONDS,
        }
    )


@router.get(BROWSER_BOOTSTRAP_PATH, include_in_schema=False)
def consume_browser_bootstrap(
    nonce: str = Query(..., min_length=32, max_length=128),
) -> RedirectResponse:
    """Consume one browser nonce, set a model-only cookie, and redirect."""
    browser_session = consume_browser_bootstrap_nonce(nonce)
    if browser_session is None:
        raise HTTPException(status_code=410, detail="browser bootstrap expired or already used")
    session, path_token = browser_session
    viewer_path = f"/model/{path_token}"
    response = RedirectResponse(url=f"{viewer_path}/", status_code=303)
    response.set_cookie(
        BROWSER_SESSION_COOKIE,
        session,
        max_age=BROWSER_SESSION_TTL_SECONDS,
        httponly=True,
        secure=False,  # Runtime's loopback viewer is served over plain HTTP.
        samesite="strict",
        path=viewer_path,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/permissions", response_model=ApiResponse, tags=["system"])
def permissions() -> ApiResponse:
    """Return the daemon's current macOS permission state.

    Accessibility is self-probed by the configured bundled AX helper(s);
    Screen Recording is preflighted by the Runtime executable. A GUI onboarding
    flow should poll this aggregate instead of creating another TCC identity.
    Fields are ``granted``, ``denied``, or mode-aware ``not_applicable``.
    """
    cfg = _get_cfg()
    if cfg.capture.source == "ingest":
        return ApiResponse(
            data={
                "accessibility": "not_applicable",
                "screen_recording": "not_applicable",
            }
        )
    return ApiResponse(
        data={
            "accessibility": (
                "granted"
                if ax_capture.ax_trusted(include_watcher=cfg.capture.event_driven)
                else "denied"
            ),
            "screen_recording": (
                "granted" if screen_recording.has_screen_recording() else "denied"
            ),
        }
    )


@router.get("/status", response_model=ApiResponse, tags=["system"])
def status(check_models: bool = False) -> ApiResponse:
    """Return runtime status; provider probes run only when explicitly requested."""
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
        "ocr": ocr_health.inspect(cfg.capture).as_dict(),
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
        bufs = [p for p in buf.iterdir() if p.suffix == ".json"]
        latest = newest_capture_path(bufs)
        last = latest.name if latest else "(none)"
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
        tlb_row = conn.execute(
            "SELECT COUNT(*), "
            "(SELECT end_time FROM timeline_blocks "
            "ORDER BY persome_epoch(end_time) DESC LIMIT 1) "
            "FROM timeline_blocks"
        ).fetchone()
        tlb_count = tlb_row[0] if tlb_row else 0
        tlb_last = tlb_row[1] if tlb_row and tlb_row[1] else "(none)"
        data["timeline"] = f"{tlb_count} blocks, last end: {tlb_last}"

    # Resolving configuration is local.  Provider pings can be paid API calls,
    # so ordinary UI polling must never trigger them implicitly.
    ping_results: dict[str, ModelPing] = {}
    try:
        if check_models:
            data["llm_profile"], ping_results = _status_model_pings(cfg)
        else:
            from ..llm_setup import profile_dict
            from ..providers import resolve_profile

            data["llm_profile"] = profile_dict(resolve_profile(cfg.model_for("default")))
    except Exception as exc:
        logger.warning("model ping failed in status endpoint: %s", exc)

    data["models"] = ping_results
    data["models_checked"] = check_models
    return ApiResponse(data=data)


# ─── Capture ingest ───────────────────────────────────────────────────────


@router.post("/_onboarding/capture", response_model=ApiResponse, include_in_schema=False)
def onboarding_capture() -> ApiResponse:
    """Force one owner-authenticated capture inside the running daemon."""
    state = scheduler.active_runner_state(_get_cfg().capture)
    if state == "ingest-ready":
        return ApiResponse(data={"id": None, "mode": "ingest", "receipt": state})
    if state == "paused":
        raise HTTPException(status_code=409, detail="capture is paused by the owner")
    if state == "locked":
        raise HTTPException(status_code=423, detail="screen is locked or asleep")
    if state != "ready":
        raise HTTPException(status_code=503, detail="live capture runner is not ready")
    path = scheduler.capture_now()
    if path is None:
        raise HTTPException(status_code=503, detail="live capture runner is not ready")
    return ApiResponse(data={"id": path.stem, "mode": "daemon", "receipt": "fresh-capture"})


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
def model_view(request: Request) -> HTMLResponse:
    """Render the local Point/Line/Face/Volume/Root model explorer."""
    from .model_view import render_memory_view

    base_path = str(request.scope.get("persome.viewer_base_path") or "/model/")
    return HTMLResponse(render_memory_view(base_path))


_MODEL_ASSETS = {
    "three.module.js",
    "layout.mjs",
    "share.mjs",
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
def model_node(
    id: str = Query(..., min_length=1, max_length=512),
) -> dict[str, Any]:
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
