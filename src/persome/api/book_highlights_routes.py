"""Book Phase 2.1 — Highlights API routes (manual pull-quotes).

A self-contained router so it can land alongside other Book PRs without
touching :mod:`persome.api.routes`. Wired into the FastAPI app via
``include_router`` at app-assembly time.

Endpoints (all under ``/book``, tag ``book``):

- ``GET    /book/highlights?limit=20`` → newest-first list
- ``POST   /book/highlights``          → create one
- ``DELETE /book/highlights/{id}``     → delete one

Storage is the ``highlights`` SQLite table (see
:mod:`persome.store.highlights`). No LLM — plain CRUD.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..store import fts
from ..store import highlights as highlights_store
from .models import ApiResponse

router = APIRouter(prefix="/book", tags=["book"])


class CreateHighlightRequest(BaseModel):
    quote: str = Field(description="划词存下的引文文本")
    source_ref: str = Field(
        default="",
        description="来源引用：来源页 id 或 chat session id（字符串，可空）",
    )


def _serialize(h: highlights_store.Highlight) -> dict[str, object]:
    """Shape one highlight for the API: id + quote + derived time_label + source_ref."""
    return {
        "id": h.id,
        "quote": h.quote,
        "time_label": h.time_label(),
        "source_ref": h.source_ref,
    }


@router.get("/highlights", response_model=ApiResponse)
def list_highlights(
    limit: Annotated[int, Query(ge=1, le=200, description="返回条目数量上限，范围 1~200")] = 20,
) -> ApiResponse:
    """按创建时间倒序返回手动划词存下的 highlights。"""
    with fts.cursor() as conn:
        items = highlights_store.list_recent(conn, limit=limit)
    return ApiResponse(
        data={
            "items": [_serialize(h) for h in items],
            "count": len(items),
        }
    )


@router.post("/highlights", response_model=ApiResponse)
def create_highlight(body: CreateHighlightRequest) -> ApiResponse:
    """新建一条 highlight，返回持久化后的行。"""
    quote = body.quote.strip()
    if not quote:
        raise HTTPException(status_code=422, detail="quote must not be empty")
    with fts.cursor() as conn:
        created = highlights_store.insert(conn, quote=quote, source_ref=body.source_ref)
    return ApiResponse(data=_serialize(created))


@router.delete("/highlights/{highlight_id}", response_model=ApiResponse)
def delete_highlight(highlight_id: int) -> ApiResponse:
    """按 id 删除一条 highlight。"""
    with fts.cursor() as conn:
        removed = highlights_store.delete(conn, highlight_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"highlight {highlight_id} not found")
    return ApiResponse(data={"deleted": highlight_id})
