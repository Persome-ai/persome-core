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

from fastapi import APIRouter, HTTPException, Path, Query
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
from ..store import entries as entries_mod
from ..store import fts, index_md
from ..store import parser_ticks as parser_ticks_store
from ..timeline import aggregator as timeline_aggregator
from ..timeline import attention_trajectory as attention_traj
from ..timeline import store as timeline_store
from .models import (
    ApiResponse,
    CaptureIngestBody,
    DataResponse,
    IntentsResponse,
    ModelPing,
    RecallPackItem,
    RecallPackResponse,
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
    if os.environ.get("PERSOME_DEV") or os.environ.get("MENS_DEV"):  # Mens is the legacy name
        return True
    try:
        return bool(load_config().dev.enabled)
    except Exception:  # noqa: BLE001
        return False


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
                                "description": "事件来源阶段，如 classifier / pattern_detector",
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


from .chat_routes import router as chat_router  # noqa: E402

router.include_router(chat_router)
