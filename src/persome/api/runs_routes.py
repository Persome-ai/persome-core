"""GET /runs — Calendar work-board read endpoint.

Returns RunCards in a day/week/month window, UNIONing the canonical
``agent_runs`` ledger with legacy ``dream_runs`` rows mapped into the same
shape (read-only; no migration). Anchor time = started_at if set else
enqueued_at. Empty window → empty items (honest empty state, never fabricated).
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from ..config import Config
from ..store import agent_runs as ar_store
from ..store import dream_runs as dr_store
from ..store import fts
from .models import (
    ApiResponse,
    CreateRunRequest,
    DataResponse,
    PatchRunRequest,
    RunDetailResponse,
    RunEventItem,
    RunsResponse,
)

_cfg: Config | None = None


def set_config(cfg: Config | None) -> None:
    global _cfg  # noqa: PLW0603
    _cfg = cfg


def _get_cfg() -> Config:
    from ..config import load as _load

    return _cfg or _load()


router = APIRouter()


def _window(range_: str) -> tuple[datetime, datetime]:
    now = datetime.now().astimezone()
    if range_ == "week":
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=7)
    if range_ == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (
            start.replace(year=start.year + 1, month=1)
            if start.month == 12
            else start.replace(month=start.month + 1)
        )
        return start, end
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _agent_card(r: ar_store.AgentRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "source": "agent_run",
        "kind": r.kind,
        "title": r.title,
        "status": r.status,
        "trigger": r.trigger,
        "enqueued_at": r.enqueued_at.isoformat(),
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "progress": r.progress,
        "progress_label": r.progress_label,
        "summary": r.summary,
    }


def _dream_card(r: dr_store.DreamRun) -> dict[str, Any]:
    """Map a legacy dream_runs row into the RunCard shape. dream has no queue,
    so enqueued_at == started_at; status values are a subset of the enum."""
    started = r.started_at.isoformat()
    return {
        "id": r.id,
        "source": "dream",
        "kind": "dream",
        "title": r.summary or "每日整理",
        "status": r.status,
        "trigger": r.trigger,
        "enqueued_at": started,
        "started_at": started,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "progress": None,
        "progress_label": "",
        "summary": r.summary,
    }


def _dream_in_window(r: dr_store.DreamRun, start: datetime, end: datetime) -> bool:
    anchor = r.started_at
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=start.tzinfo)
    return start <= anchor < end


def _card_sort_key(card: dict[str, Any], local_tz: tzinfo | None) -> datetime:
    """把卡片时间解析成**真实时刻**用于排序，而非裸 ISO 字符串。

    agent 卡(aware，带偏移后缀)与 dream 卡(可能 naive，无后缀/微秒)的串格式不一，
    lexicographic 比较 != 真实先后——同墙钟时刻下 naive 串是 aware 串的前缀，会被排在
    前面（issue #449）。naive 值按 ``local_tz`` 解释，与 ``_dream_in_window`` /
    ``_agenda_items`` 一致。``enqueued_at`` 始终存在，故时间串非空。
    """
    dt = datetime.fromisoformat(card["started_at"] or card["enqueued_at"])
    return dt.replace(tzinfo=local_tz) if dt.tzinfo is None else dt


@router.get("/runs", response_model=DataResponse[RunsResponse], tags=["dashboard"])
def runs(
    range: Annotated[  # noqa: A002 — public query name
        str,
        Query(description="时间范围：day/week/month。start+end 都给时忽略本参数"),
    ] = "day",
    status: Annotated[
        str | None,
        Query(description="逗号分隔状态过滤，如 'queued,running'。省略=全部"),
    ] = None,
    start: Annotated[
        str | None,
        Query(description="窗口起 ISO8601（含）。需与 end 同时给"),
    ] = None,
    end: Annotated[
        str | None,
        Query(description="窗口止 ISO8601（不含）。需与 start 同时给"),
    ] = None,
) -> DataResponse[RunsResponse]:
    """返回窗口内的 agent run 卡片。start+end 都给 → 用显式窗口（供日历翻页）；
    否则按 range 相对 now。无则空 items（诚实空态）。"""
    statuses = [s.strip() for s in status.split(",") if s.strip()] if status else None
    if start and end:
        try:
            win_start = datetime.fromisoformat(start)
            win_end = datetime.fromisoformat(end)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"bad start/end: {exc}") from exc
        local_tz = datetime.now().astimezone().tzinfo
        if win_start.tzinfo is None:
            win_start = win_start.replace(tzinfo=local_tz)
        if win_end.tzinfo is None:
            win_end = win_end.replace(tzinfo=local_tz)
        range_ = "custom"
    else:
        range_ = range if range in ("day", "week", "month") else "day"
        win_start, win_end = _window(range_)

    with fts.cursor() as conn:
        agent_rows = ar_store.list_runs_in_window(
            conn, start=win_start, end=win_end, statuses=statuses
        )
        # Legacy dream history: read all, window + status filter in Python.
        dream_rows = dr_store.list_runs(conn, limit=500)

    cards = [_agent_card(r) for r in agent_rows]
    for d in dream_rows:
        if not _dream_in_window(d, win_start, win_end):
            continue
        if statuses is not None and d.status not in statuses:
            continue
        cards.append(_dream_card(d))

    cards.sort(key=lambda c: _card_sort_key(c, win_start.tzinfo), reverse=True)
    payload = RunsResponse(range=range_, items=cards, count=len(cards))  # type: ignore[arg-type]
    return DataResponse(data=payload)


@router.post("/runs", response_model=ApiResponse, tags=["dashboard"])
def create_run(body: CreateRunRequest) -> ApiResponse:
    """用户派发一个新的 agent run（已排队或已有同 kind queued 行则返回现有 id）。

    ``kind`` 必须是 KIND_REGISTRY 中已注册的 kind（闭集白名单）；其他值返回 422。
    """
    from ..runs.recorder import enqueue_run
    from ..runs.registry import KIND_REGISTRY

    if body.kind not in KIND_REGISTRY:
        raise HTTPException(status_code=422, detail=f"unknown kind '{body.kind}'")

    cfg = _get_cfg()
    title = body.title or KIND_REGISTRY[body.kind].title
    # `deduped` is intentionally NOT surfaced here — the generic POST /runs
    # contract (pinned by tests/fixtures/runs_contract.json + the Dart contract
    # test) stays {run_id, status}. Only /dream/run + /bootstrap/run, whose
    # clients give a "already queued" hint, expose `deduped` (#396).
    run_id, _deduped = enqueue_run(
        cfg,
        kind=body.kind,
        trigger="user",
        dispatch_source="api",
        title=title,
        payload=body.payload or None,
    )
    return ApiResponse(data={"run_id": run_id, "status": "queued"})


@router.patch("/runs/{run_id}", response_model=ApiResponse, tags=["dashboard"])
def patch_run(run_id: int, body: PatchRunRequest) -> ApiResponse:
    """对一个 run 执行操作（当前只支持 action=cancel）。

    - 只有 queued 状态的 run 可以被取消；running / terminal 返回 409。
    - run 不存在返回 404。
    """
    if body.action != "cancel":
        raise HTTPException(status_code=422, detail=f"unknown action '{body.action}'")

    with fts.cursor() as conn:
        run = ar_store.get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        cancelled = ar_store.cancel_run(conn, run_id)

    if not cancelled:
        raise HTTPException(
            status_code=409,
            detail=f"run {run_id} is in status '{run.status}' and cannot be cancelled",
        )
    return ApiResponse(data={"run_id": run_id, "status": "cancelled"})


@router.get("/runs/{run_id}", response_model=DataResponse[RunDetailResponse], tags=["dashboard"])
def get_run_detail(
    run_id: int,
    source: Annotated[
        str,
        Query(
            description="行来源：'agent_run'（默认，查 agent_runs 台账）或 'dream'"
            "（查 legacy dream_runs；GET /runs 列表里 dream 来源卡片的 id 属于该表）。"
            "两表 id 各自独立自增，必须靠 source 消歧。"
        ),
    ] = "agent_run",
) -> DataResponse[RunDetailResponse]:
    """返回单个 run 的详情 + 完整 events（供卡片点击进度页）。

    看板的卡片来自 agent_runs ∪ dream_runs，每张卡带自身表内的 id，故必须用 source
    路由到对应表——否则 dream 卡（历史数据的多数）的 id 在 agent_runs 里查不到而 404。
    """
    if source == "dream":
        with fts.cursor() as conn:
            drun = dr_store.get_run(conn, run_id)
            if drun is None:
                raise HTTPException(status_code=404, detail=f"dream run {run_id} not found")
            devents = dr_store.list_events(conn, run_id)
        ev_items = [
            RunEventItem(id=e.id, ts=e.ts.isoformat(), type=e.type, payload=e.payload)
            for e in devents
        ]
        started = drun.started_at.isoformat()
        detail = RunDetailResponse(
            id=drun.id,
            kind="dream",
            title=drun.summary or "每日整理",
            status=drun.status,
            trigger=drun.trigger,
            dispatch_source="system",
            enqueued_at=started,  # dream has no queue; enqueued == started
            started_at=started,
            ended_at=drun.ended_at.isoformat() if drun.ended_at else None,
            progress=None,
            progress_label="",
            summary=drun.summary,
            error=drun.error,
            events=ev_items,
        )
        return DataResponse(data=detail)

    with fts.cursor() as conn:
        run = ar_store.get_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        events = ar_store.list_events(conn, run_id)

    ev_items = [
        RunEventItem(
            id=e.id,
            ts=e.ts.isoformat(),
            type=e.type,
            payload=e.payload,
        )
        for e in events
    ]
    detail = RunDetailResponse(
        id=run.id,
        kind=run.kind,
        title=run.title,
        status=run.status,
        trigger=run.trigger,
        dispatch_source=run.dispatch_source,
        enqueued_at=run.enqueued_at.isoformat(),
        started_at=run.started_at.isoformat() if run.started_at else None,
        ended_at=run.ended_at.isoformat() if run.ended_at else None,
        progress=run.progress,
        progress_label=run.progress_label,
        summary=run.summary,
        error=run.error,
        events=ev_items,
    )
    return DataResponse(data=detail)
