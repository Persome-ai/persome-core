"""FastAPI routes for the Persome HTTP REST API.

Mounted at root ``/`` inside the MCP server's Starlette app.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .. import __version__, paths
from .. import events as events_mod
from ..capture import ax_capture, scheduler, screenshot_crypto
from ..config import Config
from ..config import load as load_config
from ..intent import recall as recall_mod
from ..intent import schema_prior as schema_prior_mod
from ..intent import store as intent_store
from ..logger import get
from ..mcp import captures as captures_mod
from ..mcp.server import (
    _get_schema,
    _list_memories,
    _read_memory,
    _recent_activity,
    _search,
)
from ..memory import task_outcome as task_outcome_mod
from ..store import book_pages as book_pages_store
from ..store import dream_runs as dream_runs_store
from ..store import entries as entries_mod
from ..store import fts, index_md
from ..store import outcomes as outcomes_store
from ..store import parser_ticks as parser_ticks_store
from ..timeline import aggregator as timeline_aggregator
from ..timeline import attention_trajectory as attention_traj
from ..timeline import store as timeline_store
from .models import (
    AgendaResponse,
    AgentNowResponse,
    ApiResponse,
    BookPageDetail,
    BookPageItem,
    CaptureIngestBody,
    CorrectWorkThreadBody,
    DataResponse,
    IntentsResponse,
    MemoryIngestBody,
    ModelPing,
    OutcomeBody,
    RecallPackItem,
    RecallPackResponse,
    ReviewBody,
    SetIntentStatusBody,
)

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


# ─── Memories ──────────────────────────────────────────────────────────────


@router.get("/memories", response_model=ApiResponse, tags=["memory"])
def list_memories(
    include_dormant: Annotated[
        bool, Query(description="是否包含休眠文件（长时间无更新的文件）")
    ] = False,
    include_archived: Annotated[
        bool, Query(description="是否包含归档文件（已被新版替代的过期文件）")
    ] = False,
) -> ApiResponse:
    """列出所有记忆文件及其元数据（描述、标签、状态、条目数等）。"""
    with fts.cursor() as conn:
        rows = _list_memories(
            conn, include_dormant=include_dormant, include_archived=include_archived
        )
    return ApiResponse(data=rows)


@router.get("/memories/{path:path}", response_model=ApiResponse, tags=["memory"])
def read_memory(
    path: Annotated[str, Path(description="记忆文件路径，如 user-profile.md")],
    since: Annotated[
        str | None,
        Query(description="起始时间 ISO8601，如 2026-05-01T00:00:00+08:00。传空值会被忽略"),
    ] = None,
    until: Annotated[
        str | None,
        Query(description="结束时间 ISO8601，如 2026-05-18T23:59:59+08:00。传空值会被忽略"),
    ] = None,
    tags: Annotated[
        list[str] | None, Query(description="按标签过滤，只返回包含任一指定标签的条目")
    ] = None,
    tail_n: Annotated[int | None, Query(description="只返回最近 N 条条目")] = None,
) -> ApiResponse:
    """读取指定记忆文件的内容和条目列表。"""
    with fts.cursor() as conn:
        result = _read_memory(conn, path=path, since=since, until=until, tags=tags, tail_n=tail_n)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return ApiResponse(data=result)


class MemoryAppendRequest(BaseModel):
    """Body for the Agent-Native memory write-back route (Phase 3)."""

    content: str
    tags: list[str] = []
    run_id: str = ""


@router.post("/memories/append", response_model=ApiResponse, tags=["memory"])
def append_memory(body: MemoryAppendRequest) -> ApiResponse:
    """Write a durable agent finding back into Persome memory (Agent-Native Persome, Phase 3).

    Funnels through the canonical ``entries.append_entry`` writer (same integrity gate /
    write-inversion as every other writer) and force-tags the entry ``source:agent-run`` (+
    ``run:<run_id>`` when provided). 422 on empty content. The HTTP twin of the MCP ``remember``
    tool. Spec: docs/superpowers/specs/2026-06-25-agent-native-persome-design.md §6.
    """
    from ..mcp import memory_write

    try:
        with fts.cursor() as conn:
            result = memory_write.remember(
                conn, content=body.content, tags=body.tags, run_id=body.run_id
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(data=result)


# ─── Search & Activity ─────────────────────────────────────────────────────


@router.get("/search", response_model=ApiResponse, tags=["memory"])
def search(
    query: Annotated[
        str,
        Query(
            description="搜索文本，支持自然语言/改写查询（语义匹配），也支持关键词，如 'Vanessa 什么时候开会'"
        ),
    ],
    paths: Annotated[
        list[str] | None,
        Query(
            description="限制搜索范围到指定文件路径，支持 glob 如 event-*.md、user-*.md。可传多个"
        ),
    ] = None,
    since: Annotated[
        str | None,
        Query(description="起始时间 ISO8601，如 2026-05-01T00:00:00+08:00。传空值会被忽略"),
    ] = None,
    until: Annotated[
        str | None,
        Query(description="结束时间 ISO8601，如 2026-05-18T23:59:59+08:00。传空值会被忽略"),
    ] = None,
    top_k: Annotated[int, Query(ge=1, le=50, description="返回结果数量上限，范围 1~50")] = 5,
    include_superseded: Annotated[bool, Query(description="是否包含已被替代的条目")] = False,
) -> ApiResponse:
    """对记忆条目进行混合语义检索（BM25 ⊕ dense 向量 → RRF 融合），按相关性排序返回。

    配了 embedding 端点（OPENAI_*）时按语义匹配——可用自然语言/改写查询，不必照抄原词；
    否则 fail-open 退化为 BM25 关键词检索（同一调用）。
    """
    with fts.cursor() as conn:
        result = _search(
            conn,
            query=query,
            paths=paths,
            since=since,
            until=until,
            top_k=top_k,
            include_superseded=include_superseded,
        )
    return ApiResponse(data=result)


@router.get("/activity", response_model=ApiResponse, tags=["memory"])
def recent_activity(
    since: Annotated[
        str | None,
        Query(description="起始时间 ISO8601，如 2026-05-01T00:00:00+08:00。传空值会被忽略"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="返回条目数量上限，范围 1~200")] = 20,
    prefix_filter: Annotated[
        list[str] | None, Query(description="按文件路径前缀过滤，如 ['event-', 'project-']")
    ] = None,
) -> ApiResponse:
    """按时间倒序获取最近的记忆条目，用于快速回顾近期活动。"""
    with fts.cursor() as conn:
        result = _recent_activity(conn, since=since, limit=limit, prefix_filter=prefix_filter)
    return ApiResponse(data=result)


# ─── Work threads ────────────────────────────────────────────────────────────


@router.get("/threads", response_model=ApiResponse, tags=["workthread"])
def work_threads(
    status: Annotated[
        str | None,
        Query(description="过滤状态 active/background/done/stale/superseded；空=全部"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="返回条目上限")] = 20,
) -> ApiResponse:
    """工作线列表（最近活跃优先）— 供菜单栏常驻消费。纯读 ``work_threads`` 表，零 LLM。

    只暴露 UI 需要的标量字段（不含内部 bindings/origin_evidence）。``active`` 标记
    当前活跃线；``active_id`` 便于消费端高亮。
    """
    from ..workthread import store as wt_store

    statuses = (status.strip(),) if status and status.strip() else None
    with fts.cursor() as conn:
        threads = wt_store.list_threads(conn, statuses=statuses, limit=limit)
        active = wt_store.active_thread(conn)
    active_id = active.id if active else None
    items = [
        {
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "total_active_minutes": t.total_active_minutes,
            "approximate": t.approximate,
            "confidence": t.confidence,
            "last_active": t.last_active,
            "pinned": t.pinned,
            "origin_actor": t.origin_actor,
            "active": t.id == active_id,
        }
        for t in threads
    ]
    return ApiResponse(data={"threads": items, "active_id": active_id})


# ─── Captures ──────────────────────────────────────────────────────────────


@router.post("/captures/ingest", response_model=ApiResponse, tags=["capture"])
def ingest_capture(body: CaptureIngestBody) -> ApiResponse:
    """接收 Swift "Persome" 主程序采集的一帧 capture，跑富化→落库→意图快路 hook。

    采集层（AX 树 + 焦点窗口截图）已搬进持有 Accessibility / Screen-Recording 的 Swift
    进程（``capture.source = "ingest"``）；daemon 自身不再 spawn watcher、不再抓屏，因而
    不需要任何系统权限。落库 / 去重 / hook 与 daemon 自采路径完全一致（共用同一 runner）。
    """
    result = scheduler.ingest_capture(_get_cfg(), body.model_dump())
    return ApiResponse(data=result)


@router.get("/captures/current", response_model=ApiResponse, tags=["capture"])
def current_context(
    app_filter: Annotated[
        str | None,
        Query(description="按应用名称过滤，如 Feishu、WeChat、Tabbit Browser。不填则返回所有应用"),
    ] = None,
    headline_limit: Annotated[int, Query(ge=1, le=20, description="摘要数量上限")] = 5,
    fulltext_limit: Annotated[int, Query(ge=1, le=10, description="全文数量上限")] = 3,
    timeline_limit: Annotated[int, Query(ge=1, le=50, description="时间线块数量上限")] = 8,
) -> ApiResponse:
    """获取当前屏幕捕获上下文，包括最近的捕获摘要、完整文本和时间线块。"""
    result = captures_mod.current_context(
        app_filter=app_filter,
        headline_limit=headline_limit,
        fulltext_limit=fulltext_limit,
        timeline_limit=timeline_limit,
    )
    return ApiResponse(data=result)


@router.get("/captures", response_model=ApiResponse, tags=["capture"])
def search_captures(
    query: Annotated[str, Query(description="搜索关键词，支持多词空格分隔，如 'Feishu 会议'")],
    since: Annotated[
        str | None,
        Query(description="起始时间 ISO8601，如 2026-05-01T00:00:00+08:00。传空值会被忽略"),
    ] = None,
    until: Annotated[
        str | None,
        Query(description="结束时间 ISO8601，如 2026-05-18T23:59:59+08:00。传空值会被忽略"),
    ] = None,
    app_name: Annotated[
        str | None, Query(description="按应用名称过滤，如 Feishu、WeChat、Tabbit Browser")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=50, description="返回结果数量上限，范围 1~50")] = 10,
) -> ApiResponse:
    """对原始屏幕捕获记录进行 BM25 全文搜索。"""
    results = captures_mod.search_captures(
        query=query, since=since, until=until, app_name=app_name, limit=limit
    )
    return ApiResponse(data={"query": query, "results": results})


@router.get("/captures/recent", response_model=ApiResponse, tags=["capture"])
def read_recent_capture(
    at: Annotated[
        str | None,
        Query(
            description="目标时间，ISO 格式如 2026-05-18T14:30:00+08:00，或简写如 14:30。不填则取最新"
        ),
    ] = None,
    file_stem: Annotated[
        str | None,
        Query(
            description="精确捕获 ID（headline/搜索结果里的 file_stem）。给定时按 ID 精确取，忽略 at/app_name 的就近匹配——避免同分钟/同目录的错配"
        ),
    ] = None,
    app_name: Annotated[
        str | None, Query(description="按应用名称过滤，如 Feishu、WeChat、Tabbit Browser")
    ] = None,
    window_title_substring: Annotated[
        str | None, Query(description="按窗口标题子串过滤，如 'Pull Request'、'CLAUDE.md'")
    ] = None,
    include_screenshot: Annotated[
        bool, Query(description="是否在响应中包含截图 base64。开启后响应体积会显著增大")
    ] = False,
    include_ax_tree: Annotated[
        bool,
        Query(
            description="渐进式披露『展开』：返回完整 ax_tree（含 visible_text 折叠掉的浏览器外壳——书签/标签/扩展）。体积大、按需开启；Agent 需要明细时才用"
        ),
    ] = False,
    max_age_minutes: Annotated[
        int,
        Query(
            ge=1,
            le=1440,
            description="当使用 at 参数时，允许的最大时间偏差（分钟）。如 at=14:30 且 max_age_minutes=15，则匹配 14:15~14:45 之间的捕获",
        ),
    ] = 15,
) -> ApiResponse:
    """读取最近一次的屏幕捕获详情。给定 file_stem 时按 ID 精确取，否则按时间/应用/窗口标题就近匹配。

    返回的 ``visible_text`` 是解析后的可见文本；``text_source`` 标明它来自
    ``ax``（AX 树）还是 ``ocr``（微信等 AX-poor 应用的屏幕 OCR），``ocr`` 块给出
    OCR 状态（``not_run`` / ``submitted_empty`` / ``recognized``），``ax`` 块给出
    AX 抓取状态（节点数、模式、是否仅抓到窗口标题帧）。浏览器的 ``visible_text``
    只含网页正文 + 一行外壳摘要（渐进式披露）；``include_ax_tree=1`` 取回完整结构。
    """
    if file_stem:
        result = captures_mod.read_capture_by_stem(
            file_stem, include_screenshot=include_screenshot, include_ax_tree=include_ax_tree
        )
    else:
        result = captures_mod.read_recent_capture(
            at=at,
            app_name=app_name,
            window_title_substring=window_title_substring,
            include_screenshot=include_screenshot,
            include_ax_tree=include_ax_tree,
            max_age_minutes=max_age_minutes,
        )
    if result is None:
        raise HTTPException(status_code=404, detail="no matching capture found")
    return ApiResponse(data=result)


# ─── Timeline ──────────────────────────────────────────────────────────────


@router.get("/timeline", response_model=ApiResponse, tags=["timeline"])
def list_timeline(
    since: Annotated[
        datetime | None,
        Query(description="开始时间（ISO 8601），如 2026-05-01T00:00:00+08:00"),
    ] = None,
    until: Annotated[
        datetime | None,
        Query(description="结束时间（ISO 8601），如 2026-05-20T23:59:59+08:00"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="最多返回条数，范围 1~200")] = 50,
) -> ApiResponse:
    """查询 timeline 原始块列表，支持时间范围过滤，按时间倒序返回。"""
    with fts.cursor() as conn:
        blocks = timeline_store.query_range(conn, since, until, limit)
    return ApiResponse(
        data=[
            {
                "id": b.id,
                "start_time": b.start_time.isoformat(),
                "end_time": b.end_time.isoformat(),
                "timezone": b.timezone,
                "entries": b.entries,
                "apps_used": b.apps_used,
                "capture_count": b.capture_count,
                "created_at": b.created_at.isoformat() if b.created_at else None,
                # Legacy-only (#544 保列停写): new blocks always carry [];
                # populated values exist only on pre-R4 historical rows. Do
                # NOT build new consumers on this field — read /intents (the
                # unified intent stream) instead.
                "helpful_intent_tags": b.helpful_intent_tags,
                # Attention-locus summary (Step 1) — the dominant focused
                # surface of the window + its rung/confidence.
                "attention_surface": b.attention_surface,
                "attention_rung": b.attention_rung,
                "attention_confidence": b.attention_confidence,
            }
            for b in blocks
        ]
    )


@router.get("/attention/trajectory", response_model=ApiResponse, tags=["timeline"])
def attention_trajectory(
    since: Annotated[
        datetime | None, Query(description="开始时间（ISO 8601）。不填则用最近 hours 小时")
    ] = None,
    until: Annotated[
        datetime | None, Query(description="结束时间（ISO 8601）。不填则到现在")
    ] = None,
    hours: Annotated[int, Query(ge=1, le=168, description="since 不填时回看的小时数")] = 24,
) -> ApiResponse:
    """注意力轨迹 + dwell：把每个 timeline 块的 dominant locus 聚合成
    ``by_dwell``（按总停留时长排序的 surface）+ ``trajectory``（时间顺序路径）。"""
    now = datetime.now().astimezone()
    start = since or (now - timedelta(hours=hours))
    with fts.cursor() as conn:
        spans = attention_traj.attention_trajectory(conn, start, until)
    payload = attention_traj.trajectory_summary(spans)
    payload["window"] = {"since": start.isoformat(), "until": (until or now).isoformat()}
    return ApiResponse(data=payload)


# ─── Rewind (截图回放) — spec E6/#9 ──────────────────────────────────────────


def _rewind_enabled() -> bool:
    """Gate for the Rewind read-only endpoints.

    Optional feature (spec E6/#9): off by default. Read via ``getattr`` so a
    ``Config`` without the field still resolves (the field is added later by the
    owner). When off, both Rewind endpoints 404 — indistinguishable from absent.
    """
    return bool(getattr(_get_cfg(), "rewind_enabled", False))


# Cheap `has_screenshot` detection for the day view: scan the raw capture bytes for the
# screenshot field marker instead of `json.loads`'ing the (now-large) capture. A present
# screenshot is `"image_base64": "<value>"`; a stripped/absent one has no non-empty value
# (the 24h hygiene pass removes it). A raw-bytes substring check is correct (unlike a size
# heuristic — big AX trees rival small screenshots) and ~140ms for a full day's captures.
_SHOT_MARK = b'"image_base64": "'
_EMPTY_SHOT = b'"image_base64": ""'


def _parse_day(date: str) -> tuple[datetime, datetime]:
    """Parse ``YYYY-MM-DD`` into the local-day ``[start, end)`` window.

    Raises ``HTTPException(404)`` on a malformed date so a bad ``date`` is a
    clean not-found, never a 500. The window is anchored in the local timezone
    so it lines up with capture stems (which carry a local offset).
    """
    try:
        day = datetime.strptime(date, "%Y-%m-%d")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail=f"invalid date '{date}'") from exc
    tz = datetime.now().astimezone().tzinfo
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    end = start + timedelta(days=1)
    return start, end


@router.get("/rewind/day", response_model=ApiResponse, include_in_schema=False, tags=["rewind"])
def rewind_day(
    date: Annotated[str, Query(description="目标日期 YYYY-MM-DD（本地时区）")],
) -> ApiResponse:
    """Rewind 日视图：当天的 timeline_block 列表 + 每块关联的 capture stems。

    每块给出"发生了什么"（已有的 entries / apps_used / focus + attention 摘要字段）
    加上经 ``aggregator.captures_in_window`` 按块时间窗解析出的 capture **标识**
    （stem + 是否有可用截图，**不内联图字节** — 截图走 ``/rewind/screenshot``）。

    可选 feature：``rewind_enabled`` 关（默认）→ 404。不存在的 date → 干净 404。
    """
    if not _rewind_enabled():
        raise HTTPException(status_code=404, detail="rewind disabled")

    start, end = _parse_day(date)

    with fts.cursor() as conn:
        blocks = timeline_store.query_range(conn, start, end, limit=200)

    # Resolve captures with ONE directory scan (not one per block). The old code called
    # captures_in_window per block (a full iterdir each, 200 blocks) which hangs for >30s
    # on a real buffer of ~9k captures. Pre-parse each stem to its datetime once, keep
    # only the day's captures, and detect has_screenshot from a raw-bytes marker scan (no
    # json.loads of the now-large capture). The per-block match below is then an in-memory
    # timestamp compare.
    day_caps: list[tuple[datetime, str, bool]] = []
    buf = paths.capture_buffer_dir()
    if buf.exists():
        for p in buf.iterdir():
            if p.suffix != ".json" or not p.is_file():
                continue
            ts = timeline_aggregator._stem_to_dt(p.stem)
            if ts is None or not (start <= ts < end):
                continue
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            has_shot = _SHOT_MARK in raw and _EMPTY_SHOT not in raw
            day_caps.append((ts, p.stem, has_shot))
    day_caps.sort(key=lambda c: c[0])

    items: list[dict[str, Any]] = []
    for b in blocks:
        captures: list[dict[str, Any]] = [
            {"stem": stem, "has_screenshot": has_shot}
            for (ts, stem, has_shot) in day_caps
            if b.start_time <= ts < b.end_time
        ]
        items.append(
            {
                "id": b.id,
                "start_time": b.start_time.isoformat(),
                "end_time": b.end_time.isoformat(),
                "timezone": b.timezone,
                "entries": b.entries,
                "apps_used": b.apps_used,
                "capture_count": b.capture_count,
                "attention_surface": b.attention_surface,
                "attention_rung": b.attention_rung,
                "attention_confidence": b.attention_confidence,
                "captures": captures,
            }
        )
    return ApiResponse(data={"date": date, "blocks": items})


@router.get("/rewind/screenshot", include_in_schema=False, tags=["rewind"])
def rewind_screenshot(
    stem: Annotated[str, Query(description="capture stem（/rewind/day 给出的标识）")],
) -> Response:
    """Rewind 截图字节：该 capture 的截图原样像素（经 ``read_screenshot`` 解密）。

    返回 ``image/jpeg`` 字节，或在缺图 / 无 key / 未知 stem 时 404（不崩、不 500）。
    与块视图语义对齐：块给"发生了什么"，截图给"原样像素"。

    可选 feature：``rewind_enabled`` 关（默认）→ 404。
    """
    if not _rewind_enabled():
        raise HTTPException(status_code=404, detail="rewind disabled")

    # Path-traversal guard (mirrors read_capture_by_stem): a stem is a bare
    # filename, never a path.
    if not stem or "/" in stem or "\\" in stem or ".." in stem:
        raise HTTPException(status_code=404, detail="no such capture")
    path = paths.capture_buffer_dir() / f"{stem}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such capture")
    try:
        data = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="capture unreadable") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=404, detail="capture unreadable")

    # read_screenshot is the one decode chokepoint: decrypts a sealed payload
    # when the key is present, returns plaintext bytes verbatim, and yields None
    # for a stripped / keyless / missing screenshot (never raises).
    img = screenshot_crypto.read_screenshot(data)
    if img is None:
        raise HTTPException(status_code=404, detail="no screenshot")
    return Response(content=img, media_type="image/jpeg")


# ─── Dev ops dashboard ───────────────────────────────────────────────────────


def _dev_enabled() -> bool:
    """Gate for the dev ops dashboard. The Persome app sets ``[dev] enabled`` true
    for a ``dev``-plan account; ``PERSOME_DEV=1`` forces it on locally. Off by
    default so a normal account never exposes it."""
    if (os.environ.get("PERSOME_DEV") or os.environ.get("MENS_DEV")):  # Mens is the legacy name
        return True
    try:
        return bool(load_config().dev.enabled)
    except Exception:  # noqa: BLE001
        return False


@router.get("/dev", include_in_schema=False, tags=["dev"])
def dev_dashboard() -> HTMLResponse:
    """Real-time ops dashboard (HTML, ECharts + SSE). 404 when dev mode is off
    so it is invisible / indistinguishable from absent on a normal account."""
    if not _dev_enabled():
        raise HTTPException(status_code=404, detail="not found")
    # Local override for rebuild-free iteration: drop an HTML file at
    # <root>/dev_dashboard.html and it is served instead of the baked-in page,
    # so dashboard tweaks don't need a full daemon rebuild.
    override = paths.root() / "dev_dashboard.html"
    try:
        if override.is_file():
            return HTMLResponse(override.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return HTMLResponse(_OPS_DASHBOARD_HTML)


@router.get("/dev/memory", include_in_schema=False, tags=["dev"])
def dev_memory_view() -> HTMLResponse:
    """The memory-rebuild §7-6 记忆图: the REAL relation graph + schema tower
    rendered as the ontology-three canvas (mockup
    2026-07-02-memory-ontology-three.html adapted to live data via
    /dev/memory-graph). Same dev gate + same rebuild-free override pattern
    (<root>/dev_memory.html) as the ops dashboard; embedded there as the
    记忆图 tab."""
    if not _dev_enabled():
        raise HTTPException(status_code=404, detail="not found")
    override = paths.root() / "dev_memory.html"
    try:
        if override.is_file():
            return HTMLResponse(override.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    from .dev_memory_view import MEMORY_VIEW_HTML

    return HTMLResponse(MEMORY_VIEW_HTML)


@router.get("/dev/memory-graph", include_in_schema=False, tags=["dev"])
def dev_memory_graph() -> dict[str, Any]:
    """Read-only JSON for the 记忆图 (§7-6): nodes (USER + roster identities +
    Activity endpoints), relation_edges (both statuses — the shadow/ACTIVE
    split IS the point), and the schema_faces tower, each carrying its
    bitemporal fields so the client can replay f(T) without refetching.
    Zero-LLM; fail-open per section (a store predating a table contributes
    an empty list)."""
    if not _dev_enabled():
        raise HTTPException(status_code=404, detail="not found")
    from ..evomem import identity as identity_mod
    from ..store import fts as fts_store

    edges: list[dict[str, Any]] = []
    faces: list[dict[str, Any]] = []
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
                }
                for r in conn.execute(
                    "SELECT src_identity, dst_identity, predicate, label, status, provenance,"
                    " confidence, observations, recall_count, valid_from, valid_to,"
                    " last_observed_at, src_kind, dst_kind, polarity FROM relation_edges"
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
    # absent/corrupt file → {} and the dashboard falls back to the entity force layout.
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
    }


@router.get("/dev/memory-node", include_in_schema=False, tags=["dev"])
def dev_memory_node(id: str) -> dict[str, Any]:
    """Raw receipts behind one graph node (§2.1 每个向量指回符号收据 — the
    click-through from the 记忆图 to the symbolic layer). Lazy per-node fetch
    so the graph payload stays lean. Zero-LLM, read-only, fail-open:

    - ``event:<intents.id>`` → the minting intent row (kind/status/rationale/
      participants/ts);
    - any other identity → the latest ACTIVE evo_nodes of its ``person-*.md``
      entity file (newest first, bounded, truncated) — the consolidation
      trail the point was distilled from.
    """
    if not _dev_enabled():
        raise HTTPException(status_code=404, detail="not found")
    from ..store import fts as fts_store

    raw: list[dict[str, Any]] = []
    source = ""
    try:
        with fts_store.cursor() as conn:
            conn.row_factory = sqlite3.Row
            if id.startswith("event:"):
                source = "intents"
                row = conn.execute(
                    "SELECT ts, kind, status, rationale, payload FROM intents WHERE id = ?",
                    (id.removeprefix("event:"),),
                ).fetchone()
                if row is not None:
                    try:
                        with_people = json.loads(row["payload"] or "{}").get("with") or []
                    except Exception:  # noqa: BLE001
                        with_people = []
                    text = f"[{row['kind']}·{row['status']}] {row['rationale'] or ''}"
                    if with_people:
                        text += f"（与：{'、'.join(str(p) for p in with_people)}）"
                    raw.append({"ts": row["ts"], "text": text[:300]})
            elif id != "self":
                source = f"person-{id}.md"
                for row in conn.execute(
                    "SELECT content, memory_at FROM evo_nodes"
                    " WHERE file_name = ? AND is_latest = 1 AND status = 'active'"
                    " ORDER BY memory_at DESC LIMIT 5",
                    (source,),
                ):
                    raw.append(
                        {"ts": row["memory_at"], "text": (row["content"] or "").strip()[:300]}
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


@router.get("/intents", response_model=DataResponse[IntentsResponse], tags=["intent"])
def list_intents(
    scope: Annotated[
        str | None, Query(description="按场景过滤，如 timeline / session-<id>")
    ] = None,
    status: Annotated[
        str | None, Query(description="按状态过滤：open / consumed / dismissed")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="返回结果数量上限（取最新）")] = 50,
) -> DataResponse[IntentsResponse]:
    """列出统一意图流（debug 视图 + R3 反馈的数据源），按 ts 倒序（最新在前）。"""
    with fts.cursor() as conn:
        intents = intent_store.recent_intents(conn, start="", end="￿", scope=scope, status=status)
    intents = intents[-limit:][::-1]  # newest first, capped
    items = [i.to_dict() for i in intents]  # pydantic coerces dict→IntentItem at build
    payload = IntentsResponse(intents=items, count=len(intents))  # type: ignore[arg-type]
    return DataResponse(data=payload)


def _recall_hints(text: str, *, limit: int = 8) -> list[str]:
    """Lightweight hint terms from free text for the recall-pack endpoint.

    Split on whitespace + common punctuation, keep tokens ≥2 chars, dedupe (order-
    preserving), cap at ``limit``. CJK text without spaces stays one phrase hint —
    matching how the recognizer feeds multi-word hints (FTS5-escaped downstream)."""
    raw = re.split(r"[\s,.;:!?，。；：！？、/\\()\[\]{}<>\"'`]+", text or "")
    seen: set[str] = set()
    out: list[str] = []
    for tok in raw:
        t = tok.strip()
        if len(t) < 2 or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out


@router.get("/recall/pack", response_model=DataResponse[RecallPackResponse], tags=["intent"])
def recall_pack(
    intent_id: Annotated[
        int | None, Query(description="按意图行 id 召回（解析 scope/hints/raw 句柄）")
    ] = None,
    scope: Annotated[
        str | None, Query(description="场景 id（无 intent_id 时必填），如 timeline / session-<id>")
    ] = None,
    text: Annotated[str | None, Query(description="自由文本，派生 hints + 稠密查询")] = None,
    hints: Annotated[
        list[str] | None, Query(description="显式 hint 词覆盖；省略则由 text 派生")
    ] = None,
    max_chars: Annotated[
        int | None, Query(ge=1, le=20000, description="字符预算；省略=recall_max_chars 配置")
    ] = None,
    per_layer_cap: Annotated[int, Query(ge=0, le=50, description="每层最多保留几条（0=不限）")] = 8,
    include_semantic: Annotated[
        bool | None, Query(description="是否跑稠密层；省略=配置开关；无 embedding 凭据自动空跑")
    ] = None,
    include_raw_handles: Annotated[
        bool, Query(description="是否回 capture_stem/timeline_block_id（外部 prompt 可置 false）")
    ] = True,
) -> DataResponse[RecallPackResponse]:
    """结构化、带引用、分层的召回包 —— 供主动任务 prompt 内联（识别即办）。

    与 ``intent.recall.assemble_background`` 跑同一套 per-layer helper、同序，但每条
    带 ``cite`` 句柄；scene 项再带 RAW 捕获句柄（截图 stem / timeline 块 id）。只读、
    不写遥测 tick。``intent_id`` 优先：按行解析 scope/hints/raw 句柄；否则用 scope+text。
    无 embedding 凭据时稠密层自动空跑（``dense.active=false``），全程确定性。"""
    if intent_id is None and not (scope and scope.strip()):
        raise HTTPException(status_code=422, detail="provide intent_id or scope")
    cfg = _get_cfg().intent_recognizer
    resolved_scope = scope or ""
    query_text = text or ""
    with fts.cursor() as conn:
        if intent_id is not None:
            it = intent_store.get_intent(conn, intent_id)
            if it is None:
                raise HTTPException(status_code=404, detail="no such intent")
            resolved_scope = it.scope
            if not query_text:
                query_text = str(
                    it.payload.get("text") or it.payload.get("task_text") or it.rationale or ""
                )
        hint_terms = [h for h in (hints or []) if h and h.strip()] or _recall_hints(query_text)
        schema_pairs = schema_prior_mod.active_schema_inferences_with_sources(conn)
        dense_on = cfg.recall_semantic_enabled if include_semantic is None else include_semantic
        items = recall_mod.assemble_background_structured(
            conn,
            scope=resolved_scope,
            hints=hint_terms,
            max_chars=max_chars or cfg.recall_max_chars,
            schema_pairs=schema_pairs,
            fold_superseded=cfg.recall_fold_superseded,
            chain_trail=cfg.recall_chain_trail,
            include_confidence=True,  # surface low-confidence/conflicted flags for the consumer
            recent_events_hours=cfg.recall_recent_events_hours,
            include_events=cfg.recall_recent_events_hours > 0,
            dense_query=(query_text if dense_on else None),
            dense_top_k=cfg.recall_semantic_top_k,
            include_raw_handles=include_raw_handles,
            per_layer_cap=per_layer_cap,
        )
    counts: dict[str, int] = {}
    for it_item in items:
        counts[it_item.layer] = counts.get(it_item.layer, 0) + 1
    used = sum(len(i.content) for i in items)
    payload = RecallPackResponse(
        scope=resolved_scope,
        intent_id=intent_id,
        items=[RecallPackItem(**vars(i)) for i in items],
        counts=counts,
        budget={"max_chars": max_chars or cfg.recall_max_chars, "used": used},
        dense={"enabled": dense_on, "active": any(i.layer == "semantic" for i in items)},
    )
    return DataResponse(data=payload)


@router.get("/parser/stats", response_model=ApiResponse, tags=["parser"])
def parser_hit_stats(
    since: Annotated[str | None, Query(description="ISO8601 起始时间（含），省略=不限")] = None,
    until: Annotated[str | None, Query(description="ISO8601 结束时间（不含），省略=不限")] = None,
) -> ApiResponse:
    """Per-app 解析器命中率埋点统计（通用可观测层）。

    timeline aggregator 每构建一个 block，就按 ``bundle_id`` 落一行 ``parser_ticks``：
    ``hit``（解析器渲染出非空会话）/ ``miss``（有解析器但 declined/空/抛错）/
    ``fallback``（窗口内无任何带解析器的 app）。返回 ``total`` / ``by_outcome`` /
    ``by_bundle``（按 bundle 分桶）/ ``hit_rate``（hit ÷ total）。用来证明解析器在
    生效，并在飞书改版导致语义类漂移时及早告警（同一 bundle 的 hit 衰减成 miss）。
    """
    with fts.cursor() as conn:
        data = parser_ticks_store.stats(conn, since=since or "", until=until or "￿")
    return ApiResponse(data=data)


@router.patch("/intents/{intent_id}", response_model=ApiResponse, tags=["intent"])
def set_intent_status(
    intent_id: Annotated[int, Path(description="意图行 ID")], body: SetIntentStatusBody
) -> ApiResponse:
    """回写意图状态（反馈闭环）：采纳=consumed / 忽略=dismissed / 做完=completed / 失败=failed。

    仅接受用户反馈态（``FEEDBACK_STATUSES``，#631 nit T）；``armed``/``expired`` 是引擎自有
    生命周期态，客户端不得设置，否则可造无 ``fire_config`` 的 armed 僵尸或把行回写成过期。
    ``completed``/``failed``（反向闭环 spec 2026-06-26 G2）由 app 的 ``maybeFinalize`` 对已采纳
    且执行完毕的 `.context` 任务回写，服务端盖 ``completed_at`` 戳，喂识别器的正向 ``_completed_prior``。
    """
    if body.status not in intent_store.FEEDBACK_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid status '{body.status}', expected one of {intent_store.FEEDBACK_STATUSES}"
            ),
        )
    with fts.cursor() as conn:
        ok = intent_store.update_intent_status(conn, intent_id=intent_id, new_status=body.status)
    return ApiResponse(data={"success": ok, "intent_id": intent_id, "new_status": body.status})


@router.post("/outcomes", response_model=ApiResponse, tags=["intent"])
def record_outcome(body: OutcomeBody) -> ApiResponse:
    """反向闭环 G4（spec 2026-06-26 §3.1.2）：记录一条执行结果（FollowUp / supervised）。

    **content-free**：只落枚举/布尔/计数/时长（``OutcomeBody`` 已钉死字段集）。供
    ``feedback-report`` 的 per-kind 成功率（≥N/桶才判定）+ 未来置信度校准读取——人在环，
    不在此自动改阈值。Pydantic 拒掉任何额外字段无从夹带屏幕文本/产物正文。
    """
    with fts.cursor() as conn:
        oid = outcomes_store.insert_outcome(
            conn,
            kind=body.kind,
            status=body.status,
            success=body.success,
            intent_id=body.intent_id,
            executor_tier=body.executor_tier,
            artifact_verified=body.artifact_verified,
            placed=body.placed,
            awaited_confirm=body.awaited_confirm,
            reschedule_suggested=body.reschedule_suggested,
            elapsed_ms=body.elapsed_ms,
        )
    return ApiResponse(data={"success": True, "outcome_id": oid})


@router.post("/memory/ingest", response_model=ApiResponse, tags=["intent"])
def ingest_memory(body: MemoryIngestBody) -> ApiResponse:
    """反向闭环 G1（spec 2026-06-26 §3.1.3）：**唯一**带内容的反向通道——把 app 蒸馏脱敏的
    task-outcome ``summary`` 灌成 ``task-outcome-*.md`` 记忆（evo_nodes 豁免，Q2）。

    隐私分级最严：``memory_ingest_enabled`` 一键禁用（**默认关**）；summary 已在 app 侧红线
    过滤，daemon 侧再过一道 ``privacy.scrub``，命中即整条**丢弃**（宁缺毋滥）；按 ``task_id``
    幂等。产物 URL **不落库**，只留类型。Best-effort：禁用/丢弃都返回 200（fail-open 语义）。
    """
    cfg = load_config()
    if not getattr(cfg, "memory_ingest_enabled", False):
        return ApiResponse(data={"status": "disabled"})
    artifact_types = [a.type for a in (body.artifacts or []) if a.type]
    with fts.cursor() as conn:
        res = task_outcome_mod.ingest_task_outcome(
            conn,
            task_id=body.task_id,
            kind=body.kind,
            title=body.title,
            summary=body.summary,
            intent_id=body.intent_id,
            artifact_types=artifact_types,
            ts=body.ts,
        )
    return ApiResponse(data={"status": res.status, "entry_id": res.entry_id})


# ─── WorkThread（工作线"现在进行时"层，spec 2026-06-12） ─────────────────────


@router.get("/work/context", response_model=ApiResponse, tags=["workthread"])
def get_work_context() -> ApiResponse:
    """当前工作线上下文（MCP `current_work_context` 的 REST 镜像，HUD chip 数据源）。

    返回 ``active_thread``（title/goal/origin 出生证明/since/确定性累计
    ``total_minutes`` + ``approximate`` 标记透传/最近进展/证据引用）+
    ``background_threads`` + churn/revive 遥测 ``stats``。
    """
    from ..workthread import review as workthread_review

    with fts.cursor() as conn:
        return ApiResponse(data=workthread_review.current_work_context(conn))


@router.patch("/work/threads/{thread_id}", response_model=ApiResponse, tags=["workthread"])
def correct_work_thread(
    thread_id: Annotated[str, Path(description="工作线 ID")], body: CorrectWorkThreadBody
) -> ApiResponse:
    """工作线纠错闭集（HUD chip 的零成本开关；每次调用同时铸一条真值标签）。

    动作：confirm / not_this / rename（配 ``rename``）/ merge（配 ``into_id``，
    pinned 源拒绝被吸收）/ pin。标签回流 confidence 校准（spec §十 10.4）。
    """
    from ..workthread import review as workthread_review

    with fts.cursor() as conn:
        result = workthread_review.apply_correction(
            conn,
            thread_id=thread_id,
            action=body.action,
            new_title=body.rename,
            into_id=body.into_id,
            source="hud",
        )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=str(result.get("error")))
    return ApiResponse(data=result)


# ─── Book pages ──────────────────────────────────────────────────────────────


def _paragraphs(body: str) -> list[str]:
    """Split a page body into non-empty paragraphs (blank-line separated)."""
    return [p.strip() for p in body.split("\n\n") if p.strip()]


@router.get("/book/pages", response_model=ApiResponse, tags=["book"])
def list_book_pages(
    limit: Annotated[int, Query(ge=1, le=200, description="返回结果数量上限")] = 20,
) -> ApiResponse:
    """列出书页（dream 旁挂的离线文学生成产物），按日期倒序（最新在前）。"""
    rows = book_pages_store.list_pages(limit=limit)
    items = [BookPageItem(**r).model_dump() for r in rows]
    return ApiResponse(data={"items": items, "count": len(items)})


@router.get("/book/pages/{page_id}", response_model=ApiResponse, tags=["book"])
def get_book_page(
    page_id: Annotated[str, Path(description="书页 ID（文件名 stem）")],
) -> ApiResponse:
    """获取单页书页详情；body 为段落数组（按空行切段）。不存在返回 404。"""
    page = book_pages_store.get_page(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=f"book page '{page_id}' not found")
    detail = BookPageDetail(
        id=page["id"],
        title=page["title"],
        date=page["date"],
        is_draft=page["is_draft"],
        body=_paragraphs(page["body"]),
    )
    return ApiResponse(data=detail.model_dump())


@router.patch("/book/pages/{page_id}", response_model=ApiResponse, tags=["book"])
def review_book_page(
    page_id: Annotated[str, Path(description="书页 ID（文件名 stem）")],
    body: ReviewBody,
) -> ApiResponse:
    """标记书页为已 Review（去草稿横幅）。Review 与 ✕ 后端语义相同。不存在返回 404。"""
    if not body.reviewed:
        # Only reviewed:true is meaningful — pages don't un-review.
        raise HTTPException(status_code=400, detail="reviewed must be true")
    ok = book_pages_store.mark_reviewed(page_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"book page '{page_id}' not found")
    return ApiResponse(data={"success": True, "id": page_id, "is_draft": False})


# ─── Reference ─────────────────────────────────────────────────────────────


@router.get("/schema", response_model=ApiResponse, tags=["reference"])
def get_schema() -> ApiResponse:
    """获取 MCP 服务器暴露的工具 schema（内存查询、搜索、捕获等接口说明）。"""
    return ApiResponse(data=_get_schema())


@router.get("/config", response_model=ApiResponse, tags=["reference"])
def get_config() -> ApiResponse:
    """获取当前运行配置，包括各阶段 LLM 模型、捕获参数、路径设置等。"""
    cfg = _get_cfg()
    # Serialize dataclasses recursively
    import dataclasses

    def _serialize(obj: Any) -> Any:
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return {k: _serialize(v) for k, v in dataclasses.asdict(obj).items()}
        if isinstance(obj, dict):
            return {k: _serialize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_serialize(v) for v in obj]
        return obj

    return ApiResponse(data=_serialize(cfg))


@router.get("/config/raw", response_model=ApiResponse, tags=["reference"])
def get_config_raw() -> ApiResponse:
    """返回 ~/.persome/config.toml 的原始文本，供 UI 编辑。"""
    cfg_path = paths.config_file()
    if not cfg_path.exists():
        return ApiResponse(data={"path": str(cfg_path), "content": ""})
    return ApiResponse(
        data={"path": str(cfg_path), "content": cfg_path.read_text(encoding="utf-8")}
    )


@router.get("/config/debug-hud", response_model=ApiResponse, tags=["reference"])
def get_debug_hud_config() -> ApiResponse:
    """Return the debug HUD's content allowlist (``[debug_hud] show``).

    Re-reads ``config.toml`` fresh each call (via ``load_config()`` rather than
    the cached cfg) so edits apply without a daemon restart — the HUD polls
    this endpoint and re-renders. See ``config.DebugHudConfig``.
    """
    return ApiResponse(data={"show": load_config().debug_hud.show})


class _DebugHudBody(BaseModel):
    show: list[str]


@router.put("/config/debug-hud", response_model=ApiResponse, tags=["reference"])
def put_debug_hud_config(body: _DebugHudBody) -> ApiResponse:
    """Persist the debug HUD allowlist (``[debug_hud] show``) to config.toml.

    Lets the app's in-HUD gear menu change what the panel shows with clicks —
    no hand-editing. Writes a targeted, formatting-preserving edit (see
    ``config.set_debug_hud_show``), filters to known keys, validates the result
    parses, then clears the cached cfg so reads reflect it immediately.
    """
    import tomllib as _toml

    from ..config import DEBUG_HUD_KEYS, set_debug_hud_show

    show = [k for k in body.show if k in DEBUG_HUD_KEYS]
    cfg_path = paths.config_file()
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    new_text = set_debug_hud_show(text, show)
    try:
        _toml.loads(new_text)
    except _toml.TOMLDecodeError as exc:  # defensive; our edit is well-formed
        raise HTTPException(status_code=500, detail=f"TOML write error: {exc}") from exc

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(new_text, encoding="utf-8")
    global _cfg
    _cfg = None
    return ApiResponse(data={"show": show})


class _UpdateConfigBody(BaseModel):
    content: str


@router.put("/config/raw", response_model=ApiResponse, tags=["reference"])
def put_config_raw(body: _UpdateConfigBody) -> ApiResponse:
    """把 UI 传上来的 TOML 文本写入 config.toml。

    - 写盘前 ``tomllib.loads`` 做语法校验；解析失败返回 400，原文件不动。
    - 写完后清掉 ``api/routes`` 与 ``api/chat_routes`` 里的模块级 _cfg 缓存，
      下一次 HTTP 调用会重新 ``config.load()``。
    - 已经在后台 loop 里持有 cfg 引用的任务（timeline / reducer / capture / …）
      **不会**自动 reload；那条路径由前端在 PUT 完成后调 daemon stop+start 解决。
    """
    import tomllib as _toml

    try:
        _toml.loads(body.content)
    except _toml.TOMLDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"TOML parse error: {exc}") from exc

    cfg_path = paths.config_file()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(body.content, encoding="utf-8")

    # 清两处缓存，让 HTTP 路由下次调用立刻读到新值（chat 也会受益）
    global _cfg
    _cfg = None
    try:
        from . import chat_routes as _chat_routes

        _chat_routes.set_config(None)
    except Exception:
        pass

    return ApiResponse(data={"path": str(cfg_path), "bytes": len(body.content)})


# ─── Daemon control ────────────────────────────────────────────────────────


@router.post("/daemon/pause", response_model=ApiResponse, tags=["control"])
def pause_capture() -> ApiResponse:
    """暂停屏幕捕获，写入暂停标志文件。"""
    paths.ensure_dirs()
    paths.paused_flag().write_text(datetime.now().isoformat())
    return ApiResponse(data={"capture": "paused"})


@router.post("/daemon/resume", response_model=ApiResponse, tags=["control"])
def resume_capture() -> ApiResponse:
    """恢复屏幕捕获，删除暂停标志文件。"""
    import contextlib

    with contextlib.suppress(FileNotFoundError):
        paths.paused_flag().unlink()
    return ApiResponse(data={"capture": "active"})


@router.post("/daemon/capture-once", response_model=ApiResponse, tags=["control"])
def capture_once() -> ApiResponse:
    """手动触发一次屏幕捕获，返回捕获文件路径。"""
    cfg = _get_cfg()
    provider = ax_capture.create_provider(
        depth=cfg.capture.ax_depth, timeout=cfg.capture.ax_timeout_seconds
    )
    path = scheduler.capture_once(cfg.capture, provider)
    if path:
        return ApiResponse(data={"path": str(path)})
    raise HTTPException(status_code=500, detail="capture skipped or failed")


# ─── Actuation confirm (side-effect approval round-trip) ────────────────────


@router.post("/actuation/confirm/{confirm_id}", response_model=ApiResponse, tags=["actuation"])
async def actuation_confirm(confirm_id: str, request: Request) -> ApiResponse:
    """Resolve a pending side-effect confirmation (the app answers a `confirm_request` SSE event).

    Body: ``{"approved": true|false}``. Returns ``{"matched": bool}`` — false if the id already
    timed out / was never pending. A gated actuation (send/delete/pay/Return) blocks on the daemon
    until this lands or its timeout fires (→ deny).
    """
    from starlette.requests import ClientDisconnect

    from ..actuation import confirm as _confirm

    try:
        body = await request.json()
    except (ValueError, TypeError, ClientDisconnect):
        # Malformed JSON, no body, or the app dropped the connection before the body finished
        # streaming (a racy/cancelled confirm POST). Treat as "no decision" → safe deny; never 500.
        body = {}
    approved = bool(body.get("approved", False)) if isinstance(body, dict) else False
    matched = _confirm.resolve(confirm_id, approved=approved)
    return ApiResponse(data={"matched": matched, "approved": approved})


@router.post("/actuation/takeover/begin", response_model=ApiResponse, tags=["actuation"])
async def actuation_takeover_begin(request: Request) -> ApiResponse:
    """Begin/keep-alive the RUN-lifecycle takeover glow (the app posts this at dispatch of a task
    with a take-over target, then every ~90s while the run lives).

    Body: ``{"task_id": "<uuid>", "app": "飞书", "bundle_id": "com.electron.lark", "pid": 123,
    "note": "<short title>"}`` — ``app``/``bundle_id``/``pid`` any-of. Idempotent; feeds the same
    glow pipeline as the ui_* chokepoints. Returns ``{"shown": bool}``.
    Spec: docs/superpowers/specs/2026-07-02-takeover-glow-overlay-design.md §4.0.
    """
    from starlette.requests import ClientDisconnect

    from ..actuation import takeover as _takeover
    from ..actuation.cursor_hud import hud as _hud

    if not getattr(_get_cfg(), "actuation_glow_enabled", True):
        return ApiResponse(data={"shown": False})
    try:
        body = await request.json()
    except (ValueError, TypeError, ClientDisconnect):
        body = {}
    if not isinstance(body, dict):
        body = {}
    payload = _takeover.tracker.begin_run(
        str(body.get("task_id", "") or "").strip(),
        app=str(body.get("app", "") or ""),
        bundle_id=str(body.get("bundle_id", "") or ""),
        pid=int(body.get("pid", 0) or 0),
        note=str(body.get("note", "") or ""),
    )
    if payload:
        _hud.glow(payload)
    return ApiResponse(data={"shown": payload is not None})


@router.post("/actuation/takeover/end", response_model=ApiResponse, tags=["actuation"])
async def actuation_takeover_end(request: Request) -> ApiResponse:
    """End the takeover glow for a finished run (the app posts this from `maybeFinalize`).

    Body: ``{"task_id": "<uuid>", "outcome": "done"|"failed"}``. Flashes the terminal glow
    (green/red) on every takeover session of that task, then forgets them. Idempotent and blind-
    postable: an unknown/absent task id (most runs never touch actuation) returns ``{"ended": 0}``.
    Spec: docs/superpowers/specs/2026-07-02-takeover-glow-overlay-design.md.
    """
    from starlette.requests import ClientDisconnect

    from ..actuation import takeover as _takeover
    from ..actuation.cursor_hud import hud as _hud

    try:
        body = await request.json()
    except (ValueError, TypeError, ClientDisconnect):
        body = {}
    if not isinstance(body, dict):
        body = {}
    task_id = str(body.get("task_id", "") or "").strip()
    outcome = str(body.get("outcome", "done") or "done").strip()
    payloads = _takeover.tracker.end_run(task_id, outcome=outcome)
    for p in payloads:
        _hud.glow(p)
    return ApiResponse(data={"ended": len(payloads)})


# ─── Index management ──────────────────────────────────────────────────────


@router.post("/indices/rebuild", response_model=ApiResponse, tags=["admin"])
def rebuild_index() -> ApiResponse:
    """重建记忆文件的 FTS5 索引和文件元数据索引。"""
    with fts.cursor() as conn:
        files_count, entry_count = entries_mod.rebuild_index(conn)
        index_md.rebuild(conn)
    return ApiResponse(data={"files": files_count, "entries": entry_count})


@router.post("/indices/rebuild-captures", response_model=ApiResponse, tags=["admin"])
def rebuild_captures_index() -> ApiResponse:
    """将 capture-buffer 目录下的所有 JSON 捕获文件重新索引到 captures_fts 表。"""
    import json

    buf = paths.capture_buffer_dir()
    if not buf.exists():
        raise HTTPException(status_code=404, detail="no capture-buffer directory")

    files = sorted(p for p in buf.iterdir() if p.is_file() and p.suffix == ".json")
    if not files:
        raise HTTPException(status_code=404, detail="capture-buffer is empty")

    indexed = 0
    skipped = 0
    with fts.cursor() as conn:
        for p in files:
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                skipped += 1
                continue
            meta = data.get("window_meta") or {}
            focused = data.get("focused_element") or {}
            try:
                fts.insert_capture(
                    conn,
                    id=p.stem,
                    timestamp=data.get("timestamp", ""),
                    app_name=meta.get("app_name") or "",
                    bundle_id=meta.get("bundle_id") or "",
                    window_title=meta.get("title") or "",
                    focused_role=focused.get("role") or "",
                    focused_value=focused.get("value") or "",
                    visible_text=data.get("visible_text") or "",
                    url=data.get("url") or "",
                )
                indexed += 1
            except Exception:
                skipped += 1

    return ApiResponse(data={"indexed": indexed, "skipped": skipped, "total": len(files)})


# ─── Dream ─────────────────────────────────────────────────────────────────


def _dream_run_row(run: dream_runs_store.DreamRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "started_at": run.started_at.isoformat(),
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "trigger": run.trigger,
        "status": run.status,
        "summary": run.summary,
        "written_count": run.written_count,
        "iterations": run.iterations,
        "error": run.error,
        "skipped_reason": run.skipped_reason,
        "written_ids": run.written_ids,
        "created_paths": run.created_paths,
    }


def _dream_event_row(ev: dream_runs_store.DreamEvent) -> dict[str, Any]:
    return {
        "id": ev.id,
        "run_id": ev.run_id,
        "ts": ev.ts.isoformat(),
        "type": ev.type,
        "payload": ev.payload,
    }


@router.post("/dream/run", response_model=ApiResponse, tags=["dream"])
async def trigger_dream() -> ApiResponse:
    """手动触发一次 dream slow-thinking 整理。

    Phase 1b: 走 enqueue_run → run-dispatcher。如已有 queued 的同 kind 行，去重返回
    已有 id（不重复烧 LLM），此时 ``deduped=true``。恒返 200 ``{status, run_id,
    deduped}``（不再回退 409）；run 通过 dispatcher 在后台线程跑完后 SSE 推
    ``agent_run`` 事件。客户端据 ``deduped`` 给「已在整理中」的轻量反馈（#396）。
    """
    from ..runs.recorder import enqueue_run

    cfg = _get_cfg()
    run_id, deduped = enqueue_run(cfg, kind="dream", trigger="manual", dispatch_source="api")
    return ApiResponse(data={"status": "queued", "run_id": run_id, "deduped": deduped})


@router.post("/bootstrap/run", response_model=ApiResponse, tags=["bootstrap"])
async def trigger_bootstrap(
    shallow: Annotated[bool, Query(description="只遍历目录结构、不读文件正文")] = False,
    exclude: Annotated[
        str,
        Query(
            description="逗号分隔的、用户在 onboarding 取消勾选的顶层文件夹名"
            "(如 Desktop,Documents)；这些文件夹不被扫描或读取。"
        ),
    ] = "",
) -> ApiResponse:
    """手动触发一次 day-0 冷启动画像。

    Phase 1b: 走 enqueue_run → run-dispatcher。``shallow`` + ``exclude`` 打包进
    payload 供 bootstrap executor 读取。去重是 **payload-aware**（#397）：仅当已有
    queued bootstrap 行的 payload 与本次**完全一致**才折叠（``deduped=true``）；
    用户改了勾选/shallow 则另开新行（``deduped=false``），不丢弃新选择。恒返 200
    ``{status, run_id, deduped}``。
    """
    from ..runs.recorder import enqueue_run

    excluded_list = [s.strip() for s in exclude.split(",") if s.strip()]
    payload = {"deep": not shallow, "exclude": excluded_list}
    cfg = _get_cfg()
    run_id, deduped = enqueue_run(
        cfg,
        kind="bootstrap",
        trigger="manual",
        dispatch_source="api",
        payload=payload,
    )
    return ApiResponse(data={"status": "queued", "run_id": run_id, "deduped": deduped})


# The macOS TCC-gated home folders the onboarding flow probes for read access.
_ACCESS_FOLDERS = ("Desktop", "Documents", "Downloads")


def _probe_folder(path: str) -> bool:
    """True iff this folder's contents are listable from the daemon process.

    A bare ``os.scandir`` is enough to trigger the macOS TCC prompt (first time)
    or return the prior decision. We consume one entry so a lazily-evaluated
    iterator actually touches the directory, then stop — no content is read."""
    try:
        with os.scandir(path) as it:
            for _ in it:
                break
        return True
    except (PermissionError, OSError):
        return False


@router.post("/bootstrap/access", response_model=ApiResponse, tags=["bootstrap"])
def probe_bootstrap_access() -> ApiResponse:
    """探测 Desktop / Documents / Downloads 三个 TCC 门禁文件夹的可读性。

    在 daemon 进程里逐个 ``os.scandir``：首次访问会拉起 macOS 隐私授权弹窗,
    先前拒过则立即返回已拒。只探测可达性、不读文件内容。前端 onboarding 的
    权限预检屏按本响应逐文件夹展示授权/拒绝。
    """
    from pathlib import Path

    home = Path.home()
    folders = []
    for name in _ACCESS_FOLDERS:
        folder = home / name
        folders.append({"name": name, "path": str(folder), "granted": _probe_folder(str(folder))})
    all_granted = all(f["granted"] for f in folders)
    return ApiResponse(data={"folders": folders, "all_granted": all_granted})


@router.get("/dream/runs", response_model=ApiResponse, tags=["dream"])
def list_dream_runs(
    limit: Annotated[int, Query(ge=1, le=100, description="最多返回多少条，默认 20")] = 20,
) -> ApiResponse:
    """列出最近的 dream 运行记录，按 started_at 倒序。"""
    with fts.cursor() as conn:
        rows = dream_runs_store.list_runs(conn, limit=limit)
    return ApiResponse(data={"runs": [_dream_run_row(r) for r in rows]})


@router.get("/dream/runs/{run_id}", response_model=ApiResponse, tags=["dream"])
def get_dream_run(
    run_id: Annotated[int, Path(ge=1, description="dream_runs.id")],
) -> ApiResponse:
    """读取单次 dream 运行的详情 + 完整事件列表。"""
    with fts.cursor() as conn:
        run = dream_runs_store.get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"dream run {run_id} not found")
        events = dream_runs_store.list_events(conn, run_id)
    return ApiResponse(
        data={
            "run": _dream_run_row(run),
            "events": [_dream_event_row(e) for e in events],
        }
    )


@router.get(
    "/events/stream",
    response_class=EventSourceResponse,
    responses={
        200: {
            "description": (
                "Pipeline 阶段事件的 SSE 流。每帧为 ``data: <json>\\n\\n``，"
                "其中 ``<json>`` 至少包含 ``stage`` 和 ``type`` 字段，附加 payload "
                "随 stage/type 组合不同（如 classifier/stage_start 携带 session_id）。"
            ),
            "content": {
                "text/event-stream": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": True,
                        "required": ["stage", "type"],
                        "properties": {
                            "stage": {
                                "type": "string",
                                "description": "事件来源阶段，如 classifier / dream / pattern_detector",
                            },
                            "type": {
                                "type": "string",
                                "description": "事件子类型，如 stage_start / stage_end / tool_call",
                            },
                        },
                    }
                }
            },
        }
    },
)
async def events_stream() -> EventSourceResponse:
    """Server-Sent Events stream of live agent activity.

    Clients receive newline-delimited ``data: <json>\\n\\n`` frames for every
    LLM tool call, text chunk, and stage lifecycle event.

    Uses ``EventSourceResponse`` (sse_starlette) rather than a plain
    ``StreamingResponse``: when the FastAPI app is mounted under the FastMCP
    Starlette server, a plain streaming response is buffered and never reaches
    the client, so subscribers saw zero frames even while events were
    published. ``EventSourceResponse`` flushes each event immediately and emits
    keepalive pings — the same path the working chat SSE uses.
    """

    async def _generate() -> AsyncIterator[dict[str, str]]:
        async with events_mod.subscribe() as sub:
            async for event in sub:
                yield {"data": json.dumps(event)}

    return EventSourceResponse(_generate())


# ─── Agent now / Agenda (home dashboard) ─────────────────────────────────────


def _capture_state() -> str:
    """active / paused / stopped — derived from the paused flag + live pid."""
    if not _read_pid():
        return "stopped"
    return "paused" if paths.paused_flag().exists() else "active"


def _agent_now_sub_status(
    run: dream_runs_store.DreamRun | None,
) -> list[dict[str, Any]]:
    """Up to 3 real sub-status lines.

    When a dream is running, surface its latest events (most recent
    ``tool_call`` / ``llm_text``). Otherwise fall back to recent memory
    activity. Every line is backed by a real source — when no source exists we
    return fewer lines rather than fabricate.
    """
    lines: list[dict[str, Any]] = []

    if run is not None and run.status == "running":
        # Only the latest few events matter; cap the read so a long run's full
        # event tape isn't loaded. 12 is a safe buffer above the 3 lines we
        # emit, since some tail events are stage_start/stage_end (skipped).
        with fts.cursor() as conn:
            events = dream_runs_store.list_events(conn, run.id, tail=12)
        for ev in reversed(events):
            if len(lines) >= 3:
                break
            ts = ev.ts.isoformat()
            if ev.type == "tool_call":
                name = ev.payload.get("name") or "tool"
                lines.append({"text": f"调用工具 {name}", "ts": ts})
            elif ev.type == "llm_text":
                text = (ev.payload.get("text") or "").strip()
                if text:
                    snippet = text[:80] + ("…" if len(text) > 80 else "")
                    lines.append({"text": snippet, "ts": ts})
        return lines

    # idle: recent memory activity as ambient context
    with fts.cursor() as conn:
        recent = _recent_activity(conn, since=None, limit=3, prefix_filter=None)
    for entry in recent.get("entries", []):
        if len(lines) >= 3:
            break
        content = (entry.get("content") or "").strip().splitlines()
        first = content[0] if content else ""
        if first:
            snippet = first[:80] + ("…" if len(first) > 80 else "")
            lines.append({"text": snippet, "ts": entry.get("timestamp")})
    return lines


@router.get("/agent/now", response_model=ApiResponse, tags=["dashboard"])
def agent_now() -> ApiResponse:
    """返回 agent「此刻在做什么」，全部由真实状态派生（不编造）。

    - 若有 dream（slow-thinking 整理）正在跑：``status='running'``，``title`` 取
      该 run 的 trigger 摘要，``started_at`` 为该 run 起始时间（app 据此自走计时器），
      ``elapsed_seconds`` 为服务端算一次的已运行秒数，``sub_status`` 取该 run 最近的
      工具调用 / LLM 文本事件。
    - 否则给出空闲快照：``status='idle'``，``title`` 取上一次 dream 的 summary（无则
      给中性占位），``sub_status`` 取最近的记忆活动条目。

    无论哪种情况都附 ``capture``（active/paused/stopped）和 ``last_activity_ts``
    （最近捕获时间戳）。``sub_status`` 行数 0~3，每行都有真实来源，缺来源即省略。

    数据来源：``dream_runs`` / ``dream_events`` 表、``capture-buffer`` 最新捕获、
    暂停标志文件、记忆 ``entries`` 表（idle 时）。
    """
    with fts.cursor() as conn:
        dream_runs_store.ensure_schema(conn)
        runs = dream_runs_store.list_runs(conn, limit=1)
    latest = runs[0] if runs else None

    last_ts, _last_app = _last_capture_info()
    capture = _capture_state()

    if latest is not None and latest.status == "running":
        elapsed = int((datetime.now(latest.started_at.tzinfo) - latest.started_at).total_seconds())
        trigger_label = "手动整理" if latest.trigger == "manual" else "每日整理"
        payload = AgentNowResponse(
            title=f"Dream 慢思考整理中（{trigger_label}）",
            status="running",
            started_at=latest.started_at.isoformat(),
            elapsed_seconds=max(0, elapsed),
            capture=capture,
            last_activity_ts=last_ts,
            sub_status=_agent_now_sub_status(latest),  # type: ignore[arg-type]
        )
        return ApiResponse(data=payload.model_dump(by_alias=True))

    # idle snapshot
    if latest is not None and latest.summary:
        title = f"上次整理：{latest.summary[:120]}"
    elif latest is not None:
        title = "空闲中（上次整理未产出新内容）"
    else:
        title = "空闲中（尚无整理记录）"
    payload = AgentNowResponse(
        title=title,
        status="idle",
        started_at=None,
        elapsed_seconds=None,
        capture=capture,
        last_activity_ts=last_ts,
        sub_status=_agent_now_sub_status(latest),  # type: ignore[arg-type]
    )
    return ApiResponse(data=payload.model_dump(by_alias=True))


def _agenda_items(range_: str) -> list[dict[str, Any]]:
    """Scheduled items derived from intents carrying a temporal anchor.

    The unified intent stream is the only structured source of
    "something is scheduled". We surface intents whose ``payload.when_text`` is
    non-empty (calendar / meeting / reminder kinds, or any kind a scene pack
    tagged with a time), recognized within the requested window. ``ts`` is the
    recognition time — we filter on it because intents don't carry a parsed
    absolute event datetime (``when_text`` is free-form natural language).

    Window filtering happens in Python on parsed, tz-aware datetimes — NOT via
    the SQLite string range. The unified sink writes ``ts`` as **naive local**
    (``datetime.now().isoformat(timespec="minutes")`` → ``2026-06-04T15:33``,
    no offset), while our window boundaries are tz-aware; comparing the two as
    ISO strings (SQLite ``ts >= ? AND ts < ?``) is lexicographic, so a naive ts
    can sort before a tz-aware ``start`` and get silently dropped. We instead
    pull all open intents and compare ``datetime.fromisoformat(ts)`` — treating
    naive timestamps as local — against the window.

    Returns an empty list when no temporally-anchored intent exists in-window —
    the app renders an empty state. We never fabricate meetings.
    """
    now = datetime.now().astimezone()
    local_tz = now.tzinfo
    if range_ == "week":
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    elif range_ == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # First day of next month (handles the December → January rollover).
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
    else:  # today / day → a single calendar day
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)

    with fts.cursor() as conn:
        intent_store.ensure_schema(conn)
        # "" .. "￿" spans every row; we window in Python below to dodge the
        # naive/aware string-comparison bug described above.
        intents = intent_store.recent_intents(conn, start="", end="￿", status="open")

    # Lifecycle filter (#546/#631 nit O): an ``open`` row whose ``valid_until``
    # already passed is stale even before the 23:55 harvest flips it to
    # ``expired`` — the same filter the recall scene layer and the active tick
    # apply. Without it the month window leaks overdue promises into the home
    # dashboard agenda.
    now_iso = datetime.now().isoformat(timespec="seconds")
    intents = [it for it in intents if not intent_store.is_expired(it, now=now_iso)]

    items: list[dict[str, Any]] = []
    for it in intents:
        when_text = str(it.payload.get("when_text") or "").strip()
        if not when_text:
            continue
        try:
            dt = datetime.fromisoformat(it.ts)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:  # naive sink ts → interpret as local
            dt = dt.replace(tzinfo=local_tz)
        if not (start <= dt < end):
            continue
        people = [str(p) for p in (it.payload.get("with") or [])]
        title = it.rationale.strip() or it.kind
        items.append(
            {
                "time_label": when_text,
                "title": title[:160],
                "kind": it.kind,
                "ts": it.ts,
                "source": "intent",
                "with": people,
                # The parsed tz-aware instant, kept only for sorting. Sorting on
                # the raw ``ts`` string is lexicographic and mixes naive sink ts
                # ("2026-06-04T15:33") with tz-aware ts ("...+08:00"); the offset
                # suffix flips the order vs the true instant (#321). Popped below
                # before the items leave this function.
                "_sort_dt": dt,
            }
        )
    # newest recognition first — sort on the parsed instant, not the raw string.
    items.sort(key=lambda d: d["_sort_dt"], reverse=True)
    for d in items:
        d.pop("_sort_dt", None)
    return items


@router.get("/agenda", response_model=DataResponse[AgendaResponse], tags=["dashboard"])
def agenda(
    range: Annotated[  # noqa: A002 — public query name; aliased to range_ internally
        str,
        Query(
            description="时间范围：today/day（今天）、week（本周，周一至周日）或 month（本月）。其他值回退为 today"
        ),
    ] = "today",
) -> DataResponse[AgendaResponse]:
    """返回 today/day（今天）、week（本周）或 month（本月）的日程项，全部源自真实意图数据（不编造会议）。

    数据来源：统一意图流 ``intents`` 表中带时间锚点（``payload.when_text`` 非空）的
    open 意图（kind 通常为 meeting / calendar / reminder），按识别时间 ``ts`` 落入所选
    窗口后返回。注意 ``ts`` 是「识别时间」而非「事件绝对时间」——意图不携带解析后的
    绝对时间（``when_text`` 是自然语言），故按识别时间过滤、用 ``when_text`` 作展示标签。

    无任何符合条件的意图时返回空 ``items``（app 显示空状态），不会编造日程。
    """
    range_ = range if range in ("today", "day", "week", "month") else "today"
    items = _agenda_items(range_)
    payload = AgendaResponse(range=range_, items=items, count=len(items))  # type: ignore[arg-type]
    return DataResponse(data=payload)


from .chat_routes import router as chat_router  # noqa: E402

router.include_router(chat_router)


# ─── Dev ops dashboard HTML (served by GET /dev when dev mode is on) ─────────
# Self-contained single page: ECharts (CDN) for charts + EventSource for the
# live event stream. Polls the existing JSON endpoints; no build step, no new
# Python deps. Same-origin (the daemon serves it), so all fetches hit 127.0.0.1.
_OPS_DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Persome · 运维看板</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
  :root{
    --bg:#F5F6F7;--card:#FFFFFF;--ink:#1F2329;--mut:#646A73;--faint:#8F959E;--line:#EFF0F1;
    --blue:#3370FF;--teal:#14C9C9;--gold:#FFC60A;--orange:#FF811A;--purple:#B37FEB;--green:#0FBF60;--red:#F76560;--cyan:#1FAEE3;
    --shadow:0 1px 2px rgba(31,35,41,.04),0 2px 10px rgba(31,35,41,.05);--r:16px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",Roboto,"Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
  .toolbar{display:flex;align-items:center;gap:8px;padding:0 26px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:9;flex-wrap:wrap}
  .toolbar .ttl{font-size:16px;font-weight:600;color:var(--ink);display:flex;align-items:center;gap:8px;padding:13px 8px 13px 0}
  .toolbar .ttl .dot{width:9px;height:9px;border-radius:50%;background:var(--blue)}
  .tabs{display:flex;gap:2px}
  .tabs a{padding:14px 14px 12px;font-size:14px;color:var(--mut);text-decoration:none;border-bottom:2px solid transparent;font-weight:500}
  .tabs a:hover{color:var(--ink)} .tabs a.active{color:var(--blue);border-bottom-color:var(--blue)}
  .chip{font-size:12px;padding:3px 11px;border-radius:8px;background:#F2F3F5;color:var(--mut);white-space:nowrap}
  .chip.ok{background:#E4FBE9;color:#0A9D4A}.chip.warn{background:#FFF4E6;color:#D9730D}.chip.bad{background:#FEEBEA;color:#D83931}
  .spacer{flex:1}
  .wrap{padding:18px 26px 34px;max-width:1680px;margin:0 auto}
  .page{display:none} .page.on{display:block}
  .kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:16px;margin-bottom:16px}
  @media(max-width:1000px){.kpis{grid-template-columns:repeat(3,1fr)}}
  .kpi{background:var(--card);border-radius:var(--r);padding:20px 22px;box-shadow:var(--shadow)}
  .kpi .l{font-size:13px;color:var(--faint);margin-bottom:10px}
  .kpi .v{font-size:40px;font-weight:700;line-height:1;color:var(--ink);letter-spacing:-1px;font-family:Roboto,-apple-system,"PingFang SC",sans-serif}
  .kpi .v.blue{color:var(--blue)}.kpi .v.green{color:var(--green)}.kpi .v.cyan{color:var(--cyan)}.kpi .v.orange{color:var(--orange)}
  .grid{display:grid;grid-template-columns:repeat(12,1fr);gap:16px}
  .card{background:var(--card);border-radius:var(--r);padding:18px 20px;box-shadow:var(--shadow);min-height:0}
  .card h2{margin:0 0 4px;font-size:15px;font-weight:600;color:var(--ink);display:flex;justify-content:space-between;align-items:baseline;gap:8px}
  .card h2 .sub{color:var(--faint);font-weight:400;font-size:12px}
  .c-3{grid-column:span 3}.c-4{grid-column:span 4}.c-5{grid-column:span 5}.c-6{grid-column:span 6}.c-7{grid-column:span 7}.c-8{grid-column:span 8}.c-12{grid-column:span 12}
  @media(max-width:1100px){.c-3,.c-4,.c-5,.c-6,.c-7,.c-8{grid-column:span 12}}
  .chart{width:100%;height:240px} .chart.sm{height:210px}
  .kv{display:grid;grid-template-columns:auto 1fr;gap:9px 14px;font-size:13px;margin-top:8px} .kv span:nth-child(odd){color:var(--faint)} .kv b{font-weight:500;text-align:right;color:var(--ink)}
  table{width:100%;border-collapse:collapse;font-size:12.5px} th{text-align:left;color:var(--faint);font-weight:400;padding:8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--card)}
  td{padding:8px;border-bottom:1px solid #F4F5F6;vertical-align:top;color:var(--mut)} tr:last-child td{border-bottom:0}
  .tag{display:inline-block;padding:2px 9px;border-radius:6px;font-size:11.5px;background:#EAF1FF;color:var(--blue)}
  .tag.open{background:#E4FBE9;color:#0A9D4A}.tag.dismissed{background:#FEEBEA;color:#D83931}.tag.armed{background:#FFF4E6;color:#D9730D}.tag.expired{background:#F2F3F5;color:#8F959E}.tag.consumed{background:#E5F8FE;color:#1690C7}
  .mut{color:var(--faint)} .scroll{overflow:auto}
  .feed{font-size:12.5px;margin-top:6px} .feed .it{padding:9px 0;border-bottom:1px solid #F4F5F6} .feed .it:last-child{border-bottom:0}
  .feed .k{color:var(--blue);font-size:12px} .feed .ts{color:var(--faint);font-size:11px;float:right} .feed .it b{font-weight:600}
  #log{height:216px;overflow:auto;font:11.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;background:#F7F8FA;border-radius:10px;padding:11px;color:var(--mut);margin-top:6px}
  #log .ln{white-space:pre-wrap;word-break:break-word;border-bottom:1px solid #EDEEF0;padding:2px 0} #log .t{color:var(--faint)} #log .e{color:var(--blue)}
  .split{display:grid;grid-template-columns:300px 1fr;gap:16px} @media(max-width:820px){.split{grid-template-columns:1fr}}
  .panel{background:var(--card);border-radius:var(--r);box-shadow:var(--shadow);padding:14px 16px}
  .ph{font-size:15px;font-weight:600;margin:0 0 10px;display:flex;justify-content:space-between;align-items:center;gap:10px}
  .ph .cnt{font-size:12px;color:var(--faint);font-weight:400}
  .mlist{max-height:74vh;overflow:auto} .mitem{padding:10px 12px;border-radius:10px;cursor:pointer;border:1px solid transparent}
  .mitem:hover{background:#F7F8FA} .mitem.sel{background:#EAF1FF;border-color:#D4E2FF}
  .mitem .n{font-weight:600;font-size:13px;color:var(--ink)} .mitem .d{font-size:11.5px;color:var(--faint);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .mitem .meta{font-size:11px;color:var(--faint);margin-top:4px}
  .badge{display:inline-block;background:#F2F3F5;color:var(--mut);border-radius:5px;padding:0 6px;font-size:10.5px;margin-right:4px}
  .entry{border-bottom:1px solid var(--line);padding:13px 0} .entry:last-child{border-bottom:0}
  .entry .et{font-size:11.5px;color:var(--faint);margin-bottom:5px} .entry .eb{white-space:pre-wrap;word-break:break-word;font-size:13px;color:#3a3f47}
  .mdetail{max-height:74vh;overflow:auto}
  .filters{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap} .fbtn{padding:4px 13px;border-radius:8px;background:var(--card);color:var(--mut);cursor:pointer;font-size:12.5px;box-shadow:var(--shadow);border:1px solid transparent}
  .fbtn.on{background:var(--blue);color:#fff} .fbtn:hover:not(.on){color:var(--ink)}
  .blk{background:var(--card);border-radius:14px;box-shadow:var(--shadow);padding:14px 16px;margin-bottom:12px}
  .blk .bh{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px} .blk .bt{font-weight:600;font-size:13.5px;font-family:Roboto,monospace}
  .blk .be{font-size:12.5px;color:#3a3f47;padding:6px 0;border-top:1px solid #F4F5F6;white-space:pre-wrap;word-break:break-word}
  pre.raw{white-space:pre-wrap;word-break:break-word;font:11.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;background:#F7F8FA;border-radius:10px;padding:12px;color:#3a3f47;max-height:60vh;overflow:auto;margin:0}
  .search{width:100%;padding:9px 13px;border:1px solid var(--line);border-radius:10px;font-size:13px;background:#FAFBFC;color:var(--ink);outline:none}
  .search:focus{border-color:var(--blue);background:#fff}
  .rawitem{padding:9px 11px;border-radius:9px;cursor:pointer;border:1px solid transparent} .rawitem:hover{background:#F7F8FA} .rawitem.sel{background:#EAF1FF}
  .chiprow{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
  .kv2{display:grid;grid-template-columns:auto 1fr;gap:7px 14px;font-size:12.5px;margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid var(--line)} .kv2 span{color:var(--faint)} .kv2 b{font-weight:500;color:var(--ink);word-break:break-all}
  .seclbl{font-size:12px;font-weight:600;color:var(--mut);margin:14px 0 5px;display:flex;gap:6px;align-items:baseline}
  ::-webkit-scrollbar{width:9px;height:9px}::-webkit-scrollbar-thumb{background:#DCDEE2;border-radius:6px}::-webkit-scrollbar-track{background:transparent}
</style></head>
<body>
<div class="toolbar">
  <div class="ttl"><span class="dot"></span>Persome 运维看板</div>
  <nav class="tabs">
    <a id="nav-overview" href="#overview">概览</a>
    <a id="nav-memory" href="#memory">记忆</a>
    <a id="nav-intents" href="#intents">意图</a>
    <a id="nav-timeline" href="#timeline">时间线</a>
    <a id="nav-raw" href="#raw">原始捕获</a>
    <a id="nav-feed" href="#feed">日程·动态</a>
    <a id="nav-book" href="#book">Book</a>
    <a id="nav-search" href="#search">搜索</a>
    <a id="nav-graph" href="#graph">记忆图</a>
  </nav>
  <div class="spacer"></div>
  <span class="chip" id="p-daemon">守护 —</span>
  <span class="chip" id="p-capture">抓取 —</span>
  <span class="chip" id="p-ax">AX —</span>
  <span class="chip" id="p-clock"></span>
  <span class="chip ok" id="p-live">● 实时</span>
</div>

<div class="wrap">
<div id="page-overview" class="page on">
<div class="kpis">
  <div class="kpi"><div class="l">意图识别命中率</div><div class="v blue" id="k-hit">—</div></div>
  <div class="kpi"><div class="l">已识别意图 · 累计</div><div class="v" id="k-persist">—</div></div>
  <div class="kpi"><div class="l">活跃 open 意图</div><div class="v green" id="k-open">—</div></div>
  <div class="kpi"><div class="l">记忆条目 / 文件</div><div class="v cyan" id="k-mem">—</div></div>
  <div class="kpi"><div class="l">解析器命中率</div><div class="v" id="k-parse">—</div></div>
  <div class="kpi"><div class="l">识别空跑率 whiteburn</div><div class="v orange" id="k-burn">—</div></div>
</div>
<div class="grid">
  <div class="card c-8"><h2>抓取速率 <span class="sub">次 / 分钟</span></h2><div class="chart" id="ch-cap"></div></div>
  <div class="card c-4"><h2>守护状态</h2><div class="kv" id="kv-status"></div></div>
  <div class="card c-6"><h2>注意力停留 <span class="sub">分钟 · 近 24h</span></h2><div class="chart" id="ch-dwell"></div></div>
  <div class="card c-3"><h2>应用占比 <span class="sub">近期</span></h2><div class="chart sm" id="ch-apps"></div></div>
  <div class="card c-3"><h2>意图状态分布</h2><div class="chart sm" id="ch-istatus"></div></div>
  <div class="card c-3"><h2>识别漏斗 <span class="sub">tick→命中→持久化</span></h2><div class="chart" id="ch-funnel-rec"></div></div>
  <div class="card c-3"><h2>意图类型 <span class="sub">by kind</span></h2><div class="chart" id="ch-kind"></div></div>
  <div class="card c-3"><h2>识别活跃趋势 <span class="sub">意图/小时</span></h2><div class="chart" id="ch-trend"></div></div>
  <div class="card c-3"><h2>意图来源分布</h2><div class="chart" id="ch-prov"></div></div>
  <div class="card c-4"><h2>快路闸漏斗 <span class="sub">fast-path gate</span></h2><div class="chart" id="ch-funnel"></div></div>
  <div class="card c-4"><h2>Pregate 效率 <span class="sub">跑 / 空跑 / 跳过</span></h2><div class="chart" id="ch-pregate"></div></div>
  <div class="card c-4"><h2>现在进行时 <span class="sub">/agent/now</span></h2><div class="scroll" style="max-height:240px"><div class="feed" id="now-feed"></div></div></div>
  <div class="card c-5"><h2>Pregate 趋势 <span class="sub">whiteburn / 命中率 · 按小时</span></h2><div class="chart" id="ch-pg-trend"></div></div>
  <div class="card c-5"><h2>软打扰下降 <span class="sub">surfaced-then-ignored 过度触发 · 逐日</span></h2><div class="chart" id="ch-softnag-trend"></div></div>
  <div class="card c-4"><h2>置信度分布 <span class="sub">confidence</span></h2><div class="chart" id="ch-conf"></div></div>
  <div class="card c-3"><h2>工作线 <span class="sub">workthread · 近7天</span></h2><div class="chart" id="ch-wt"></div></div>
  <div class="card c-7"><h2>解析器命中 <span class="sub">按应用 · hit / miss / fallback</span></h2><div class="chart" id="ch-parser"></div></div>
  <div class="card c-5"><h2>记忆条目 <span class="sub">按文件</span></h2><div class="chart" id="ch-mem"></div></div>
  <div class="card c-7"><h2>最近识别的意图 <span class="sub">/intents</span></h2><div class="scroll" style="max-height:270px"><table id="t-intents"><thead><tr><th>类型</th><th>置信</th><th>重要×紧急</th><th>状态</th><th>内容</th></tr></thead><tbody></tbody></table></div></div>
  <div class="card c-5"><h2>实时事件流 <span class="sub">SSE</span></h2><div id="log"></div></div>
</div>
</div>

<div id="page-memory" class="page"><div class="split">
  <div class="panel"><div class="ph">记忆文件 <span class="cnt" id="mem-cnt"></span></div><div class="mlist" id="mem-list"></div></div>
  <div class="panel"><div class="ph" id="mem-title">选择一个文件</div><div class="mdetail" id="mem-detail"><div class="mut">点左侧文件查看条目</div></div></div>
</div></div>

<div id="page-intents" class="page">
  <div class="filters" id="int-filters"></div>
  <div class="panel"><div class="ph">意图流 <span class="cnt" id="int-cnt"></span></div>
    <div class="scroll" style="max-height:78vh"><table id="int-tbl"><thead><tr><th>类型</th><th>置信</th><th>重要</th><th>紧急</th><th>状态</th><th>来源</th><th>时间</th><th>内容 / 依据</th></tr></thead><tbody></tbody></table></div>
  </div>
</div>

<div id="page-timeline" class="page">
  <div class="ph" style="padding:0 2px">时间线块 <span class="cnt" id="tl-cnt"></span></div>
  <div id="tl-list"></div>
</div>

<div id="page-raw" class="page">
  <div style="margin-bottom:12px"><input class="search" id="raw-q" placeholder="🔍 搜索原始捕获关键词(回车)…"></div>
  <div class="split">
    <div class="panel"><div class="ph">最近捕获 <span class="cnt" id="raw-cnt"></span></div><div class="mlist" id="raw-list"></div></div>
    <div class="panel"><div class="ph" id="raw-title">最新捕获</div><div id="raw-detail" class="mdetail"></div></div>
  </div>
</div>
<div id="page-feed" class="page"><div class="split" style="grid-template-columns:1fr 1fr">
  <div class="panel"><div class="ph">日程 · 即将到来 <span class="cnt" id="ag-cnt"></span></div><div id="ag-list" class="mdetail"></div></div>
  <div class="panel"><div class="ph">记忆动态 · 最近写入 <span class="cnt" id="act-cnt"></span></div><div id="act-list" class="mdetail"></div></div>
</div></div>
<div id="page-book" class="page"><div class="split">
  <div class="panel"><div class="ph">Book 篇章 <span class="cnt" id="bk-cnt"></span></div><div class="mlist" id="bk-list"></div></div>
  <div class="panel"><div class="ph" id="bk-title">选择一篇</div><div class="mdetail" id="bk-body"><div class="mut">点左侧篇章阅读</div></div></div>
</div></div>
<div id="page-search" class="page">
  <div style="margin-bottom:12px"><input class="search" id="g-q" placeholder="🔍 搜索记忆 + 原始捕获(回车)…"></div>
  <div class="split" style="grid-template-columns:1fr 1fr">
    <div class="panel"><div class="ph">记忆命中 <span class="cnt" id="sm-cnt"></span></div><div id="sm-list" class="mdetail"></div></div>
    <div class="panel"><div class="ph">捕获命中 <span class="cnt" id="sc-cnt"></span></div><div id="sc-list" class="mdetail"></div></div>
  </div>
</div>

<div id="page-graph" class="page">
  <!-- 记忆图（memory-rebuild §7-6）：/dev/memory 的 3D 画布，懒加载（首次切到本 tab 才拉 three.js） -->
  <div class="panel" style="padding:0;overflow:hidden">
    <iframe id="graph-frame" title="记忆图" style="display:block;width:100%;height:calc(100vh - 120px);border:0;background:#0c0e13"></iframe>
  </div>
</div>
</div>

<script>
const C={blue:'#3370FF',teal:'#14C9C9',gold:'#FFC60A',orange:'#FF811A',purple:'#B37FEB',green:'#0FBF60',red:'#F76560',cyan:'#1FAEE3',gray:'#DEE0E3',ink:'#1F2329',mut:'#646A73',faint:'#8F959E',grid:'#F2F3F5',line:'#EBECEF'};
const PIE=[C.blue,C.teal,C.gold,C.orange,C.purple,C.cyan,C.green,C.red];
const charts={};
function chart(id){const el=document.getElementById(id);if(!charts[id])charts[id]=echarts.init(el);return charts[id];}
async function j(p){const r=await fetch(p);if(!r.ok)throw new Error(p+' '+r.status);const d=await r.json();return (d&&'data'in d)?d.data:d;}
const hhmm=iso=>typeof iso==='string'&&iso.length>=16?iso.slice(11,16):'';
const pct=x=>x==null?'—':(x*100).toFixed(0)+'%';
const APP={'com.cmuxterm.app':'cmux','com.tab-browser.Tabbit':'Tabbit','com.electron.lark':'飞书','com.microsoft.VSCode':'VSCode','com.tencent.xinWeChat':'微信','com.anthropic.claudefordesktop':'Claude','com.apple.finder':'访达'};
const short=b=>APP[b]||String(b||'').split('.').pop();
const esc=s=>String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const AX={axisLabel:{color:C.faint,fontSize:11},splitLine:{lineStyle:{color:C.grid}},axisLine:{show:false},axisTick:{show:false}};
const TIP={backgroundColor:'#FFF',borderColor:C.line,borderWidth:1,textStyle:{color:C.ink,fontSize:12},extraCssText:'box-shadow:0 4px 16px rgba(31,35,41,.12);border-radius:8px;padding:8px 12px'};
const base=ex=>Object.assign({tooltip:Object.assign({trigger:'axis'},TIP)},ex||{});
const LEG={top:2,left:0,icon:'circle',itemWidth:8,itemHeight:8,itemGap:14,textStyle:{color:C.mut,fontSize:11.5},type:'scroll'};
function donut(id,data,colors){chart(id).setOption({tooltip:Object.assign({trigger:'item',formatter:'{b}: {c} ({d}%)'},TIP),legend:Object.assign({},LEG),color:colors||PIE,
  series:[{type:'pie',radius:['46%','72%'],center:['50%','58%'],avoidLabelOverlap:true,itemStyle:{borderColor:'#fff',borderWidth:2,borderRadius:3},label:{show:true,formatter:'{d}%',color:C.mut,fontSize:11},labelLine:{length:8,length2:8,lineStyle:{color:C.gray}},data:data}]});}
const setTxt=(id,t)=>{const e=document.getElementById(id);if(e)e.textContent=t;};
const setChip=(id,txt,cls)=>{const e=document.getElementById(id);if(e){e.textContent=txt;e.className='chip '+(cls||'');}};

async function refreshStatus(){
  try{const s=await j('/status');
    setChip('p-daemon','守护 '+(s.daemon||'—'),(String(s.daemon).includes('running')?'ok':'bad'));
    setChip('p-capture','抓取 '+(s.capture||'—'),(String(s.capture).includes('active')?'ok':'warn'));
    const rows=[['版本',s.version],['运行',s.uptime],['健康',s.health],['缓冲',s.buffer],['会话',s.sessions],['记忆',s.memory],['时间线',s.timeline]];
    document.getElementById('kv-status').innerHTML=rows.map(([k,v])=>'<span>'+k+'</span><b>'+(v==null?'—':(typeof v==='object'?JSON.stringify(v):esc(v)))+'</b>').join('');
  }catch(e){}
}
async function refreshCapture(){
  try{const b=[...(await j('/timeline?limit=120'))].reverse();
    chart('ch-cap').setOption(base({grid:{left:40,right:16,top:16,bottom:24},xAxis:Object.assign({type:'category',data:b.map(x=>hhmm(x.start_time)),boundaryGap:false},AX),yAxis:Object.assign({type:'value'},AX),
      series:[{type:'line',smooth:true,showSymbol:false,areaStyle:{color:new echarts.graphic.LinearGradient(0,0,0,1,[{offset:0,color:'rgba(51,112,255,.18)'},{offset:1,color:'rgba(51,112,255,0)'}])},lineStyle:{color:C.blue,width:2.5},itemStyle:{color:C.blue},data:b.map(x=>x.capture_count||0)}]}));
    const c={};for(const x of b)for(const a of (x.apps_used||[]))c[a]=(c[a]||0)+1;
    donut('ch-apps',Object.entries(c).sort((p,q)=>q[1]-p[1]).slice(0,8).map(([n,v])=>({name:short(n),value:v})));
  }catch(e){}
}
async function refreshDwell(){try{const rows=((await j('/attention/trajectory?hours=24')).by_dwell||[]).slice(0,12).reverse();
  chart('ch-dwell').setOption(base({grid:{left:150,right:24,top:10,bottom:20},xAxis:Object.assign({type:'value'},AX),yAxis:Object.assign({type:'category',data:rows.map(r=>(r.surface||'').slice(0,26))},AX),
    series:[{type:'bar',data:rows.map(r=>r.dwell_minutes),itemStyle:{color:C.teal,borderRadius:[0,5,5,0]},barWidth:'58%'}]}));}catch(e){}}
async function refreshIntentStats(){
  try{const s=await j('/intents/stats');
    setTxt('k-hit',pct(s.hit_rate));setTxt('k-persist',s.persisted_total??'—');
    const byStatus=(s.downstream&&s.downstream.intents_by_status)||{};
    setTxt('k-open',byStatus.open??'—');setTxt('k-burn',pct(s.pregate&&s.pregate.whiteburn_rate));
    chart('ch-funnel-rec').setOption({tooltip:Object.assign({trigger:'item'},TIP),series:[{type:'funnel',min:0,gap:3,top:10,bottom:10,label:{color:'#fff',fontSize:11,fontWeight:600},data:[{name:'tick '+(s.total_ticks||0),value:s.total_ticks||0},{name:'命中 '+(s.hit_ticks||0),value:s.hit_ticks||0},{name:'持久化 '+(s.persisted_total||0),value:s.persisted_total||0}],color:[C.purple,C.blue,C.green]}]});
    const bk=Object.entries(s.by_kind||{}).sort((a,b)=>b[1]-a[1]);
    chart('ch-kind').setOption(base({grid:{left:30,right:14,top:14,bottom:44},xAxis:Object.assign({type:'category',data:bk.map(x=>x[0])},AX,{axisLabel:{color:C.faint,rotate:30,fontSize:10}}),yAxis:Object.assign({type:'value'},AX),series:[{type:'bar',data:bk.map(x=>x[1]),itemStyle:{color:C.blue,borderRadius:[5,5,0,0]},barWidth:'52%'}]}));
    const scol={open:C.green,armed:C.orange,dismissed:C.red,expired:C.gray,consumed:C.cyan};
    donut('ch-istatus',Object.entries(byStatus).map(([n,v])=>({name:n,value:v})),Object.entries(byStatus).map(([n])=>scol[n]||C.blue));
    const pg=s.pregate||{};
    chart('ch-pregate').setOption(base({grid:{left:36,right:16,top:16,bottom:24},xAxis:Object.assign({type:'category',data:['跑LLM','空跑','跳过','空cap']},AX,{axisLabel:{color:C.faint,fontSize:11}}),yAxis:Object.assign({type:'value'},AX),
      series:[{type:'bar',barWidth:'46%',label:{show:true,position:'top',color:C.mut,fontSize:11,fontWeight:600},itemStyle:{borderRadius:[5,5,0,0]},data:[{value:pg.ran_ticks||0,itemStyle:{color:C.blue}},{value:pg.empty_ticks||0,itemStyle:{color:C.orange}},{value:pg.skipped_ticks||0,itemStyle:{color:C.gray}},{value:Math.round((pg.empty_capture_rate||0)*(pg.attempts||0)),itemStyle:{color:'#EBECEF'}}]}]}));
  }catch(e){}
}
async function refreshFunnel(){try{const s=await j('/intents/fast-path/stats');
  const order=['non_user','no_parser','not_conversation','not_allowed','no_unseen','no_anchor','throttled','recognized'];
  const o=s.by_outcome||{};const data=order.filter(k=>k in o).map(k=>[k,o[k]]);
  chart('ch-funnel').setOption(base({grid:{left:34,right:12,top:16,bottom:56},xAxis:Object.assign({type:'category',data:data.map(d=>d[0])},AX,{axisLabel:{color:C.faint,rotate:36,fontSize:9}}),yAxis:Object.assign({type:'value'},AX),
    series:[{type:'bar',barWidth:'52%',label:{show:true,position:'top',color:C.mut,fontSize:10},itemStyle:{borderRadius:[5,5,0,0]},data:data.map(d=>({value:d[1],itemStyle:{color:d[0]==='recognized'?C.green:C.blue}}))}]}));}catch(e){}}
async function refreshParser(){try{const s=await j('/parser/stats');
  const bb=Object.entries(s.by_bundle||{}).map(([b,o])=>[short(b),o.hit||0,o.miss||0,o.fallback||0]).sort((a,b)=>(b[1]+b[2]+b[3])-(a[1]+a[2]+a[3])).slice(0,9).reverse();
  chart('ch-parser').setOption(base({tooltip:Object.assign({trigger:'axis',axisPointer:{type:'shadow'}},TIP),legend:Object.assign({},LEG,{data:['hit','miss','fallback']}),grid:{left:96,right:18,top:30,bottom:18},xAxis:Object.assign({type:'value'},AX),yAxis:Object.assign({type:'category',data:bb.map(x=>x[0])},AX),
    series:[{name:'hit',type:'bar',stack:'p',data:bb.map(x=>x[1]),itemStyle:{color:C.green}},{name:'miss',type:'bar',stack:'p',data:bb.map(x=>x[2]),itemStyle:{color:C.red}},{name:'fallback',type:'bar',stack:'p',data:bb.map(x=>x[3]),itemStyle:{color:'#E4E6EA'}}]}));
  const tot=s.total||0,hit=(s.by_outcome&&s.by_outcome.hit)||0;setTxt('k-parse',tot?Math.round(hit/tot*100)+'%':'—');}catch(e){}}
async function refreshMem(){try{const m=await j('/memories');
  const files=(m.files||[]).filter(f=>f.entry_count).sort((a,b)=>b.entry_count-a.entry_count).slice(0,14).reverse();
  setTxt('k-mem',(m.files||[]).reduce((s,f)=>s+(f.entry_count||0),0)+' / '+(m.count||0));
  chart('ch-mem').setOption(base({grid:{left:156,right:24,top:10,bottom:18},xAxis:Object.assign({type:'value'},AX),yAxis:Object.assign({type:'category',data:files.map(f=>f.path.replace(/\.md$/,'').slice(0,24))},AX),series:[{type:'bar',data:files.map(f=>f.entry_count),itemStyle:{color:C.cyan,borderRadius:[0,5,5,0]},barWidth:'62%'}]}));}catch(e){}}
async function refreshIntentsChart(){try{const d=await j('/intents?limit=200');const its=d.intents||d||[];
  const byh={};for(const i of its){const k=(i.ts||'').slice(0,13);if(k)byh[k]=(byh[k]||0)+1;}const hrs=Object.keys(byh).sort();
  chart('ch-trend').setOption(base({grid:{left:30,right:14,top:16,bottom:24},xAxis:Object.assign({type:'category',data:hrs.map(h=>h.slice(11,13)+'h')},AX),yAxis:Object.assign({type:'value'},AX),series:[{type:'bar',data:hrs.map(h=>byh[h]),itemStyle:{color:C.blue,borderRadius:[4,4,0,0]},barWidth:'54%'}]}));
  const pv={};for(const i of its){const p=(i.payload&&i.payload.provenance)||'?';pv[p]=(pv[p]||0)+1;}
  donut('ch-prov',Object.entries(pv).map(([n,v])=>({name:n,value:v})),Object.entries(pv).map(([n])=>({user_committed:C.green,counterpart_proposed:C.orange}[n]||C.blue)));
  const cb={};for(const i of its){if(i.confidence==null)continue;const k=(Math.round(i.confidence*10)/10).toFixed(1);cb[k]=(cb[k]||0)+1;}const ck=Object.keys(cb).sort();
  chart('ch-conf').setOption(base({grid:{left:28,right:14,top:16,bottom:22},xAxis:Object.assign({type:'category',data:ck},AX),yAxis:Object.assign({type:'value'},AX),series:[{type:'bar',data:ck.map(k=>cb[k]),itemStyle:{color:C.cyan,borderRadius:[4,4,0,0]},barWidth:'56%'}]}));
  const body=document.querySelector('#t-intents tbody');
  body.innerHTML=its.slice(0,16).map(it=>{const p=it.payload||{};const iu=(p.importance!=null||p.urgency!=null)?((p.importance??'—')+'×'+(p.urgency??'—')):'—';const txt=(p.when_text?('['+p.when_text+'] '):'')+(it.rationale||'').slice(0,60);
    return '<tr><td><span class="tag">'+esc(it.kind||'?')+'</span></td><td>'+(it.confidence!=null?it.confidence.toFixed(2):'—')+'</td><td>'+esc(iu)+'</td><td><span class="tag '+esc(it.status||'')+'">'+esc(it.status||'?')+'</span></td><td class="mut">'+esc(txt)+'</td></tr>';}).join('')||'<tr><td colspan=5 class="mut">暂无</td></tr>';}catch(e){}}
async function refreshNow(){try{const n=await j('/agent/now');const items=(n.sub_status||[]).slice(0,8);
  const head='<div class="it"><b>'+esc(n.title||'')+'</b> <span class="tag '+esc(n.status||'')+'">'+esc(n.status||'')+'</span></div>';
  document.getElementById('now-feed').innerHTML=head+items.map(s=>{const m=/^\[(\w+)\]\s*(.*)$/.exec(s.text||'');const kind=m?m[1]:'';const rest=m?m[2]:(s.text||'');
    return '<div class="it"><span class="ts">'+esc(s.ts||'')+'</span>'+(kind?'<span class="k">['+esc(kind)+']</span> ':'')+esc(rest).slice(0,140)+'</div>';}).join('');}catch(e){}}
async function refreshWorkthread(){try{const s=(await j('/work/context')).stats||{};const ks=['opens','attaches','progress','completes','merges','revives'];const zh={opens:'开线',attaches:'挂入',progress:'进展',completes:'完成',merges:'合并',revives:'复活'};
  chart('ch-wt').setOption(base({grid:{left:26,right:14,top:16,bottom:24},xAxis:Object.assign({type:'category',data:ks.map(k=>zh[k])},AX,{axisLabel:{color:C.faint,fontSize:9,rotate:16}}),yAxis:Object.assign({type:'value',minInterval:1},AX),series:[{type:'bar',data:ks.map(k=>s[k]||0),itemStyle:{color:C.purple,borderRadius:[4,4,0,0]},barWidth:'54%'}]}));}catch(e){}}
const isoHour=d=>{const p=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+'T'+p(d.getHours())+':00:00';};
async function refreshPregateTrend(){try{const now=new Date();const wins=[];for(let i=7;i>=0;i--){const a=new Date(now.getTime()-i*3600000);wins.push([a,new Date(a.getTime()+3600000)]);}
  const res=await Promise.all(wins.map(([a,b])=>j('/intents/stats?since='+isoHour(a)+'&until='+isoHour(b)).catch(()=>null)));const xs=wins.map(([a])=>isoHour(a).slice(11,13)+'h');
  chart('ch-pg-trend').setOption(base({legend:Object.assign({},LEG,{data:['whiteburn%','命中率%']}),grid:{left:32,right:14,top:30,bottom:22},xAxis:Object.assign({type:'category',data:xs,boundaryGap:false},AX),yAxis:Object.assign({type:'value',max:100},AX),
    series:[{name:'whiteburn%',type:'line',smooth:true,connectNulls:true,symbol:'circle',symbolSize:5,label:{show:true,color:C.orange,fontSize:10},data:res.map(r=>r&&r.pregate?Math.round(r.pregate.whiteburn_rate*100):null),lineStyle:{color:C.orange,width:2.5},itemStyle:{color:C.orange}},{name:'命中率%',type:'line',smooth:true,connectNulls:true,symbol:'circle',symbolSize:5,label:{show:true,color:C.green,fontSize:10},data:res.map(r=>r?Math.round((r.hit_rate||0)*100):null),lineStyle:{color:C.green,width:2.5},itemStyle:{color:C.green}}]}));}catch(e){}}
function refreshOverview(){refreshStatus();refreshPerms();refreshCapture();refreshDwell();refreshIntentStats();refreshFunnel();refreshParser();refreshMem();refreshIntentsChart();refreshNow();refreshWorkthread();document.getElementById('p-clock').textContent=new Date().toLocaleTimeString();}

let memSel=null;
async function loadMemory(){
  try{const m=await j('/memories');const fs=m.files||[];setTxt('mem-cnt',fs.length+' 个文件');
    document.getElementById('mem-list').innerHTML=fs.map(f=>'<div class="mitem" data-p="'+esc(f.path)+'"><div class="n">'+esc(f.path.replace(/\.md$/,''))+' <span class="badge">'+(f.entry_count||0)+'</span></div><div class="d">'+esc(f.description||'')+'</div><div class="meta">'+(f.tags||[]).map(t=>'<span class="badge">'+esc(t)+'</span>').join('')+' 更新 '+esc(f.updated||'')+'</div></div>').join('');
    document.querySelectorAll('#mem-list .mitem').forEach(el=>el.onclick=()=>openMem(el.dataset.p));
    if(fs.length)openMem(memSel&&fs.some(f=>f.path===memSel)?memSel:fs[0].path);
  }catch(e){document.getElementById('mem-list').innerHTML='<div class="mut">加载失败</div>';}
}
async function openMem(path){memSel=path;
  document.querySelectorAll('#mem-list .mitem').forEach(el=>el.classList.toggle('sel',el.dataset.p===path));
  setTxt('mem-title',path);document.getElementById('mem-detail').innerHTML='<div class="mut">加载中…</div>';
  try{const d=await j('/memories/'+encodeURIComponent(path));const es=d.entries||[];
    document.getElementById('mem-detail').innerHTML='<div class="mut" style="margin-bottom:10px">'+esc(d.description||'')+'</div>'+es.map(e=>'<div class="entry"><div class="et">'+esc(e.timestamp||e.id||'')+'  '+(e.tags||[]).map(t=>'<span class="badge">'+esc(t)+'</span>').join('')+'</div><div class="eb">'+esc(e.body||'')+'</div></div>').join('')||'<div class="mut">无条目</div>';
  }catch(e){document.getElementById('mem-detail').innerHTML='<div class="mut">加载失败</div>';}
}

let intFilter='';
const INTF=[['','全部'],['open','open'],['armed','armed'],['dismissed','dismissed'],['consumed','consumed'],['expired','expired']];
function renderIntFilters(){document.getElementById('int-filters').innerHTML=INTF.map(([v,l])=>'<div class="fbtn'+(v===intFilter?' on':'')+'" data-v="'+v+'">'+l+'</div>').join('');
  document.querySelectorAll('#int-filters .fbtn').forEach(el=>el.onclick=()=>{intFilter=el.dataset.v;renderIntFilters();loadIntents();});}
async function loadIntents(){renderIntFilters();
  try{const d=await j('/intents?limit=200'+(intFilter?'&status='+intFilter:''));const its=d.intents||d||[];setTxt('int-cnt',its.length+' 条');
    document.querySelector('#int-tbl tbody').innerHTML=its.map(it=>{const p=it.payload||{};const ev=(it.evidence||[])[0];
      const body=(p.when_text?'<span class="badge">'+esc(p.when_text)+'</span> ':'')+esc(it.rationale||'')+(ev&&ev.quote?'<div class="mut" style="margin-top:4px">“'+esc(ev.quote)+'”</div>':'');
      return '<tr><td><span class="tag">'+esc(it.kind||'?')+'</span></td><td>'+(it.confidence!=null?it.confidence.toFixed(2):'—')+'</td><td>'+(p.importance??'—')+'</td><td>'+(p.urgency??'—')+'</td><td><span class="tag '+esc(it.status||'')+'">'+esc(it.status||'?')+'</span></td><td class="mut">'+esc(p.provenance||'')+(p.channel?' · '+esc(p.channel):'')+'</td><td class="mut" style="white-space:nowrap">'+esc((it.ts||'').slice(0,16).replace('T',' '))+'</td><td>'+body+'</td></tr>';}).join('')||'<tr><td colspan=8 class="mut">无</td></tr>';
  }catch(e){document.querySelector('#int-tbl tbody').innerHTML='<tr><td colspan=8 class="mut">加载失败</td></tr>';}
}

async function loadTimeline(){
  try{const bs=await j('/timeline?limit=60');setTxt('tl-cnt',bs.length+' 块');
    document.getElementById('tl-list').innerHTML=bs.map(b=>{const att=b.attention_surface?'<span class="tag">👁 '+esc(b.attention_surface)+(b.attention_rung?'·'+esc(b.attention_rung):'')+'</span>':'';
      return '<div class="blk"><div class="bh"><span class="bt">'+esc(hhmm(b.start_time))+'–'+esc(hhmm(b.end_time))+'</span>'+att+(b.apps_used||[]).map(a=>'<span class="badge">'+esc(short(a))+'</span>').join('')+'<span class="mut" style="margin-left:auto">'+(b.capture_count||0)+' 次捕获</span></div>'+(b.entries||[]).map(e=>'<div class="be">'+esc(e)+'</div>').join('')+'</div>';}).join('')||'<div class="mut">无</div>';
  }catch(e){document.getElementById('tl-list').innerHTML='<div class="mut">加载失败</div>';}
}

const SRCLBL={ax:['来源 AX 树','tag'],ocr:['来源 OCR','tag open'],none:['无文本','tag dismissed']};
const OCRLBL={recognized:['OCR 已识别','tag open'],submitted_empty:['OCR 已跑·空','tag armed'],not_run:['OCR 未运行','tag expired']};
function rawDetail(c){if(!c){document.getElementById('raw-detail').innerHTML='<div class="mut">无</div>';return;}
  const fe=c.focused_element||{},ax=c.ax||{},ocr=c.ocr||{},src=c.text_source||'';
  const sb=SRCLBL[src],ob=ocr.status?(OCRLBL[ocr.status]||['OCR '+ocr.status,'tag']):null;
  const chips=[
    sb?'<span class="'+sb[1]+'">'+esc(sb[0])+'</span>':'',
    ob?'<span class="'+ob[1]+'">'+esc(ob[0])+(ocr.chars?(' ·'+ocr.chars+'字'):'')+'</span>':'',
    ax.present?'<span class="tag">AX '+(ax.node_count||0)+' 节点'+(ax.has_content===false?' ·仅标题帧':'')+'</span>':(c.ax?'<span class="tag dismissed">无 AX</span>':''),
    c.has_screenshot?'<span class="tag">含截图</span>':'',
    c.cmux_text_injected?'<span class="tag">cmux 注入</span>':''
  ].filter(Boolean).join('');
  const kv=[['应用',short(c.bundle_id||c.app_name)],['窗口',c.window_title||'—'],['URL',c.url||'—'],
    ['聚焦',fe.role?(fe.role+(fe.value?' = '+String(fe.value).slice(0,100):'')+(fe.value_length?(' ['+fe.value_length+'字]'):'')):'—'],
    ['触发',c.trigger||'—'],['AX 模式',(ax.mode||'—')+(ax.depth!=null?(' · 深度'+ax.depth):'')],
    ['schema',c.schema_version!=null?('v'+c.schema_version):'—'],['时间',c.timestamp||''],['文件',c.file||c.file_stem||'—']];
  setTxt('raw-title',short(c.bundle_id||c.app_name)+'  '+(c.timestamp||'').slice(11,16));
  const axt=c.ax_text||'',ocrt=c.ocr_text||'';let body='';
  if(ocrt)body+='<div class="seclbl">OCR 识别内容'+(src==='ocr'?' <span class="mut">· 本条正文来源</span>':'')+'</div><pre class="raw">'+esc(ocrt)+'</pre>';
  if(axt)body+='<div class="seclbl">AX 可见文本'+(src==='ax'?' <span class="mut">· 本条正文来源</span>':'')+'</div><pre class="raw">'+esc(axt)+'</pre>';
  if(!ocrt&&!axt)body='<pre class="raw">'+esc(c.visible_text||'(无可见文本)')+'</pre>';
  document.getElementById('raw-detail').innerHTML=(chips?'<div class="chiprow">'+chips+'</div>':'')+'<div class="kv2">'+kv.map(([k,v])=>'<span>'+k+'</span><b>'+esc(v)+'</b>').join('')+'</div>'+body;}
async function fetchCap(el){const stem=el.dataset.stem;
  if(stem){try{const c=await j('/captures/recent?file_stem='+encodeURIComponent(stem));if(c&&c.file_stem===stem)return c;}catch(e){}}
  try{return await j('/captures/recent?at='+encodeURIComponent(el.dataset.at||'')+'&app_name='+encodeURIComponent(el.dataset.app||''));}catch(e){return null;}}
async function rawShow(el){document.querySelectorAll('#raw-list .rawitem').forEach(x=>x.classList.remove('sel'));el.classList.add('sel');
  rawDetail(await fetchCap(el));}
async function loadRaw(){
  try{const cur=await j('/captures/current?headline_limit=20');const hl=cur.recent_captures_headline||[];setTxt('raw-cnt',hl.length+' 条');
    document.getElementById('raw-list').innerHTML=hl.map((h,i)=>{const prev=h.preview||h.window_title||h.focused_role||'';const ch=h.text_chars!=null?(' · '+h.text_chars+'字'):'';
      return '<div class="rawitem'+(i===0?' sel':'')+'" data-stem="'+esc(h.file_stem||'')+'" data-at="'+esc(h.time)+'" data-app="'+esc(h.app_name)+'"><div class="n">'+esc(short(h.app_name||''))+' <span class="mut" style="float:right">'+esc(h.time)+ch+'</span></div><div class="d">'+esc(prev).slice(0,90)+'</div></div>';}).join('')||'<div class="mut">无</div>';
    document.querySelectorAll('#raw-list .rawitem').forEach(el=>el.onclick=()=>rawShow(el));
    const first=document.querySelector('#raw-list .rawitem');
    if(first)rawDetail(await fetchCap(first));
    else rawDetail(await j('/captures/recent').catch(()=>null));
  }catch(e){document.getElementById('raw-list').innerHTML='<div class="mut">加载失败</div>';}
}
document.getElementById('raw-q').addEventListener('keydown',async e=>{if(e.key!=='Enter')return;const q=e.target.value.trim();if(!q)return loadRaw();
  try{const r=await j('/captures?query='+encodeURIComponent(q));const arr=Array.isArray(r)?r:(r.results||r.captures||r.items||[]);setTxt('raw-cnt',arr.length+' 命中');
    document.getElementById('raw-list').innerHTML=arr.map(h=>{const app=h.app_name||'';const t=(h.timestamp||'').slice(11,16);
      return '<div class="rawitem" data-stem="'+esc(h.file_stem||'')+'" data-at="'+esc(t)+'" data-app="'+esc(app)+'"><div class="n">'+esc(short(app))+' <span class="mut" style="float:right">'+esc(t)+'</span></div><div class="d">'+esc((h.snippet||h.visible_text||h.window_title||'')).slice(0,80)+'</div></div>';}).join('')||'<div class="mut">无命中</div>';
    document.querySelectorAll('#raw-list .rawitem').forEach(el=>el.onclick=()=>rawShow(el));
  }catch(e){document.getElementById('raw-list').innerHTML='<div class="mut">搜索失败</div>';}
});


async function refreshPerms(){try{const p=await j('/permissions');const ax=p.accessibility||'?';setChip('p-ax','AX '+ax,(ax==='granted'||ax==='authorized'?'ok':'bad'));}catch(e){}}
async function loadFeed(){
  try{const a=await j('/agenda');const items=a.items||[];setTxt('ag-cnt',items.length+' 项');
    document.getElementById('ag-list').innerHTML=items.map(it=>'<div class="entry"><div class="et"><span class="tag">'+esc(it.kind||'?')+'</span> <b style="color:#1F2329">'+esc(it.time_label||'')+'</b>'+((it.with||[]).length?' · '+esc((it.with||[]).join('、')):'')+'  <span class="mut">'+esc((it.ts||'').slice(5,16).replace('T',' '))+'</span></div><div class="eb">'+esc(it.title||'')+'</div></div>').join('')||'<div class="mut">暂无日程</div>';}catch(e){document.getElementById('ag-list').innerHTML='<div class="mut">加载失败</div>';}
  try{const a=await j('/activity');const es=a.entries||[];setTxt('act-cnt',es.length+' 条');
    document.getElementById('act-list').innerHTML=es.map(e=>'<div class="entry"><div class="et"><span class="badge">'+esc((e.path||'').replace(/\.md$/,''))+'</span> '+esc(e.timestamp||'')+'</div><div class="eb">'+esc(e.content||'')+'</div></div>').join('')||'<div class="mut">无</div>';}catch(e){document.getElementById('act-list').innerHTML='<div class="mut">加载失败</div>';}
}
let bkSel=null;
async function loadBook(){
  try{const b=await j('/book/pages');const items=b.items||[];setTxt('bk-cnt',items.length+' 篇');
    document.getElementById('bk-list').innerHTML=items.map(p=>'<div class="mitem" data-id="'+esc(p.id)+'"><div class="n">'+esc(p.title||p.id)+(p.is_draft?' <span class="badge">草稿</span>':'')+'</div><div class="meta">'+esc(p.date||'')+'</div></div>').join('')||'<div class="mut">无篇章</div>';
    document.querySelectorAll('#bk-list .mitem').forEach(el=>el.onclick=()=>openBook(el.dataset.id));
    if(items.length)openBook(bkSel&&items.some(p=>p.id===bkSel)?bkSel:items[0].id);}catch(e){document.getElementById('bk-list').innerHTML='<div class="mut">加载失败</div>';}
}
async function openBook(id){bkSel=id;
  document.querySelectorAll('#bk-list .mitem').forEach(el=>el.classList.toggle('sel',el.dataset.id===id));
  document.getElementById('bk-body').innerHTML='<div class="mut">加载中…</div>';
  try{const d=await j('/book/pages/'+encodeURIComponent(id));const body=Array.isArray(d.body)?d.body:[d.body||''];
    setTxt('bk-title',(d.title||id)+(d.date?'  ·  '+d.date:''));
    document.getElementById('bk-body').innerHTML=body.map(p=>'<p style="margin:0 0 14px;line-height:1.85;color:#3a3f47;font-size:13.5px">'+esc(p)+'</p>').join('')||'<div class="mut">空</div>';}catch(e){document.getElementById('bk-body').innerHTML='<div class="mut">加载失败</div>';}
}
async function doSearch(q){if(!q)return;
  try{const r=await j('/search?query='+encodeURIComponent(q));const rs=r.results||[];setTxt('sm-cnt',rs.length+' 命中');
    document.getElementById('sm-list').innerHTML=rs.map(e=>'<div class="entry"><div class="et"><span class="badge">'+esc((e.path||'').replace(/\.md$/,''))+'</span> '+esc(e.timestamp||'')+'</div><div class="eb">'+esc(e.content||'')+'</div></div>').join('')||'<div class="mut">无命中</div>';}catch(e){document.getElementById('sm-list').innerHTML='<div class="mut">搜索失败</div>';}
  try{const r=await j('/captures?query='+encodeURIComponent(q));const arr=Array.isArray(r)?r:(r.results||r.captures||r.items||[]);setTxt('sc-cnt',arr.length+' 命中');
    document.getElementById('sc-list').innerHTML=arr.map(h=>'<div class="entry"><div class="et"><span class="badge">'+esc(h.app_name||'')+'</span> '+esc((h.timestamp||'').slice(0,16).replace('T',' '))+'</div><div class="eb">'+esc((h.snippet||h.visible_text||'')).slice(0,400)+'</div></div>').join('')||'<div class="mut">无命中</div>';}catch(e){document.getElementById('sc-list').innerHTML='<div class="mut">搜索失败</div>';}
}

const PAGES=['overview','memory','intents','timeline','raw','feed','book','search','graph'];
function show(pg){if(!PAGES.includes(pg))pg='overview';
  PAGES.forEach(p=>{document.getElementById('page-'+p).classList.toggle('on',p===pg);const n=document.getElementById('nav-'+p);if(n)n.classList.toggle('active',p===pg);});
  if(pg==='graph'){const f=document.getElementById('graph-frame');if(f&&!f.src)f.src='/dev/memory';}
  if(pg==='memory')loadMemory();else if(pg==='intents')loadIntents();else if(pg==='timeline')loadTimeline();else if(pg==='raw')loadRaw();else if(pg==='feed')loadFeed();else if(pg==='book')loadBook();
  else if(pg==='overview')setTimeout(()=>{for(const k in charts)charts[k].resize();},60);}
window.addEventListener('hashchange',()=>show(location.hash.slice(1)));
document.getElementById('g-q').addEventListener('keydown',e=>{if(e.key==='Enter')doSearch(e.target.value.trim());});
show(location.hash.slice(1)||'overview');
refreshOverview();setInterval(()=>{if(document.getElementById('page-overview').classList.contains('on'))refreshOverview();},5000);
refreshPregateTrend();setInterval(refreshPregateTrend,30000);
async function refreshSoftnagTrend(){try{const d=await j('/softnag/trend?limit=60');const pts=(d&&d.points)||[];const xs=pts.map(p=>String(p.day||'').slice(5));
  chart('ch-softnag-trend').setOption(base({legend:Object.assign({},LEG,{data:['软打扰条数','真实浮现采纳%']}),grid:{left:36,right:40,top:30,bottom:22},xAxis:Object.assign({type:'category',data:xs,boundaryGap:false},AX),
    yAxis:[Object.assign({type:'value',name:'软打扰',nameTextStyle:{color:C.faint,fontSize:10}},AX),Object.assign({type:'value',name:'采纳%',min:0,max:100,nameTextStyle:{color:C.faint,fontSize:10}},AX)],
    series:[{name:'软打扰条数',type:'line',smooth:true,connectNulls:true,symbol:'circle',symbolSize:5,yAxisIndex:0,label:{show:true,color:C.orange,fontSize:10},data:pts.map(p=>p.softnag),lineStyle:{color:C.orange,width:2.5},itemStyle:{color:C.orange},areaStyle:{color:'rgba(255,129,26,0.08)'}},
      {name:'真实浮现采纳%',type:'line',smooth:true,connectNulls:true,symbol:'circle',symbolSize:5,yAxisIndex:1,data:pts.map(p=>p.true_surface_accept_rate==null?null:Math.round(p.true_surface_accept_rate*100)),lineStyle:{color:C.green,width:2.5},itemStyle:{color:C.green}}]}));}catch(e){}}
refreshSoftnagTrend();setInterval(refreshSoftnagTrend,30000);

const log=document.getElementById('log');
function addLine(t){const d=document.createElement('div');d.className='ln';d.innerHTML='<span class="t">'+new Date().toLocaleTimeString()+'</span>  '+t;log.prepend(d);while(log.childNodes.length>200)log.removeChild(log.lastChild);}
try{const es=new EventSource('/events/stream');
  es.onmessage=e=>{let s=esc(e.data);try{const o=JSON.parse(e.data);s='<span class="e">'+esc(o.type||o.event||'event')+'</span>  '+esc(JSON.stringify(o)).slice(0,340);}catch(_){}addLine(s);};
  es.onerror=()=>setChip('p-live','● 已断开','bad');es.onopen=()=>setChip('p-live','● 实时','ok');
}catch(e){}
</script>
</body></html>
"""
