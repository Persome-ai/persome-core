"""Book Phase 3 (X) — on-demand book generation route.

A self-contained router so it can land alongside other Book PRs without
touching :mod:`persome.api.routes`. Wired into the FastAPI app via
``include_router`` at app-assembly time.

Endpoint (under ``/book``, tag ``book``):

- ``POST /book/generate`` → run the page selector/writer for today plus the
  chapter clusterer, then return how many pages/chapters were written.

This is the manual trigger behind the app's "generate now" button. It runs the
same two functions the daily Dream run hangs off
(:func:`persome.writer.book_page.run_book_pages` and
:func:`persome.writer.book_chapters.run_book_chapters`), which are each
internally fault-tolerant — a failure inside either is logged and degrades to
"wrote nothing" rather than raising. Synchronous and simple: the call blocks
until both steps finish and returns the counts.

Concurrency (issue #354): these two functions mutate the same files/tables as
the daily Dream run's book sub-steps (``store.book_pages._alloc_stem`` is a
read-then-write TOCTOU; ``store.book_chapters.replace_generated`` is
``DELETE``-then-``INSERT``). To avoid silently losing pages / duplicating
chapters, this route and the dream sub-step both serialize on the shared
:data:`persome.writer.book_generate.book_generate_lock`. A user click that
lands while generation is already in flight gets HTTP 409 (fail fast — never
queue a button press behind a long run), matching ``/dream/run``.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from ..logger import get
from ..writer import book_chapters, book_generate, book_page
from .models import ApiResponse

logger = get("persome.api")

router = APIRouter(prefix="/book", tags=["book"])


@router.post(
    "/generate",
    response_model=ApiResponse,
    responses={409: {"description": "book generation already in progress"}},
)
def generate_book() -> ApiResponse:
    """立即为今天生成书页并重聚章节，返回写入的 page ids 与章节数。

    触发两步（与每日 Dream 同源）：先 ``run_book_pages(today)`` 选题+写页，
    再 ``run_book_chapters()`` 按 chat 历史聚章。两步各自容错，单步失败只记日志、
    退化为 0，不向上抛。

    并发约束（#354）：与每日 Dream 的 book 子步竞争同一存储，故两边共用
    :data:`book_generate.book_generate_lock` 串行化。若生成已在进行中，本接口
    **非阻塞**地返回 409（与 ``/dream/run`` 一致），不把用户点击排到长任务后面。
    """
    try:
        with book_generate.book_generate_guard(blocking=False):
            today = datetime.now().astimezone().strftime("%Y-%m-%d")
            page_ids = book_page.run_book_pages(today)
            chapters = book_chapters.run_book_chapters()
    except book_generate.BookGenerateInProgressError as exc:
        raise HTTPException(status_code=409, detail="book generation already in progress") from exc

    logger.info(
        "book/generate: wrote %d page(s), %d chapter(s) for %s",
        len(page_ids),
        chapters,
        today,
    )
    return ApiResponse(data={"pages": page_ids, "chapters": chapters})
