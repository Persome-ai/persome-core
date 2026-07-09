"""Book Phase 2.2 — Chapter API routes (LLM-clustered session chapters).

A self-contained router so it can land alongside other Book PRs without
touching :mod:`persome.api.routes`. Wired into the FastAPI app via
``include_router`` at app-assembly time.

Endpoints (all under ``/book``, tag ``book``):

- ``GET   /book/chapters``        → all chapters, newest-first
- ``PATCH /book/chapters/{id}``   → rename one (flips ``edited`` so the daily
                                     regeneration won't clobber the user's title)

Storage is the ``book_chapters`` SQLite table (see
:mod:`persome.store.book_chapters`). Chapters are *generated* by the Dream
sub-step (:mod:`persome.writer.book_chapters`); these endpoints only read
and rename — generation is not triggered here.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field

from ..store import book_chapters as chapters_store
from ..store import fts
from .models import ApiResponse

router = APIRouter(prefix="/book", tags=["book"])


class RenameChapterRequest(BaseModel):
    title: str = Field(description="章节的新标题（重命名后标记 edited，重生不再覆盖）")


def _serialize(c: chapters_store.Chapter) -> dict[str, object]:
    """Shape one chapter for the API.

    ``id`` is the stable PATCH-addressing id; ``title`` doubles as the
    front-end selection key (the Sessions reader matches chapters by title).
    ``from_count`` is the number of backing sessions the chapter groups.
    """
    return {
        "id": c.id,
        "title": c.title,
        "subtitle": c.subtitle,
        "from_count": len(c.session_ids),
        "session_ids": c.session_ids,
        "edited": c.edited,
    }


@router.get("/chapters", response_model=ApiResponse)
def list_chapters() -> ApiResponse:
    """返回所有章节，按创建时间倒序。空时返回空列表（前端回退占位）。"""
    with fts.cursor() as conn:
        items = chapters_store.list_chapters(conn)
    return ApiResponse(
        data={
            "items": [_serialize(c) for c in items],
            "count": len(items),
        }
    )


@router.patch("/chapters/{chapter_id}", response_model=ApiResponse)
def rename_chapter(
    chapter_id: Annotated[int, Path(description="章节 id（book_chapters 表自增主键）")],
    body: RenameChapterRequest,
) -> ApiResponse:
    """重命名一个章节并标记 ``edited``，使后续每日重生不再覆盖该标题。"""
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="title must not be empty")
    with fts.cursor() as conn:
        ok = chapters_store.mark_edited(conn, chapter_id, title)
    if not ok:
        raise HTTPException(status_code=404, detail=f"chapter {chapter_id} not found")
    return ApiResponse(data={"id": chapter_id, "title": title, "edited": True})
