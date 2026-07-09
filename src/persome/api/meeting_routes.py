"""Meeting assistant API routes — status-only stub.

The meeting assistant runs as an independent ``persome meeting-server``
process spawned by the Flutter app (not by the daemon), because macOS Core
Audio requires the process to be in the user's GUI / Aqua session.  The daemon
(double-fork + setsid) is in a detached session where audio callbacks receive
all-zero data.

This module keeps a minimal ``/meeting/status`` endpoint so existing health
checks don't 404, and serves read-only **search** over past meetings. Audio
capture (start/stop/events) stays on the standalone meeting-server (port 8750)
because Core Audio needs a GUI session — but searching the recorded
``meeting_*.db`` files is just disk reads, so the daemon can serve it and the
chat agent can query meetings even after the meeting-server has exited.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..meeting.store import list_meetings, search_all_meetings
from .models import ApiResponse

router = APIRouter(prefix="/meeting", tags=["meeting"])


class StatusResponse(BaseModel):
    status: str
    detail: str | None = None


@router.get("/status", response_model=StatusResponse)
def meeting_status() -> StatusResponse:
    return StatusResponse(
        status="use-meeting-server",
        detail="Meeting runs on a separate process (port 8750). Connect to http://127.0.0.1:8750 instead.",
    )


@router.get("/search", response_model=ApiResponse)
def meeting_search(
    query: Annotated[
        str | None,
        Query(description="关键词，LIKE 子串匹配（中文子串可命中）。留空则按时间返回全部"),
    ] = None,
    since: Annotated[
        str | None,
        Query(description="起始时间 ISO8601，如 2026-06-06 或 2026-06-06T21:00:00。留空忽略"),
    ] = None,
    until: Annotated[str | None, Query(description="结束时间 ISO8601。留空忽略")] = None,
    limit: Annotated[int, Query(ge=1, le=200, description="返回条数上限，范围 1~200")] = 20,
) -> ApiResponse:
    """跨所有历史会议（``meeting_*.db``）做关键词（LIKE 子串）+ 时间搜索。

    关键词走 LIKE 子串而非 FTS，因此中文 2 字词也能命中；``query`` 留空即纯
    时间浏览。结果按时间倒序，每条带所属会议场次与会话开始时间。
    """
    results = search_all_meetings(query, since=since, until=until, limit=limit)
    return ApiResponse(data={"results": results})


@router.get("/list", response_model=ApiResponse)
def meeting_list(
    since: Annotated[str | None, Query(description="起始时间 ISO8601。留空忽略")] = None,
    until: Annotated[str | None, Query(description="结束时间 ISO8601。留空忽略")] = None,
) -> ApiResponse:
    """按时间列出所有会议场次（每个 ``meeting_*.db`` 一场），含开始时间与行数。"""
    return ApiResponse(data={"meetings": list_meetings(since=since, until=until)})
