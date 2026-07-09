"""Book-page memory store DAO.

A *book page* is a daily literary prose entry the Dream sub-step writes from
that day's worth-remembering episodes. Each page is one Markdown file under
``memory/`` named ``page-<date>[-i].md`` with YAML frontmatter:

    ---
    kind: book_page
    title: On an Unnecessary Phone Call
    date: 2026-07-08
    created_at: 2026-07-08T23:31:04+08:00
    reviewed: false
    source_refs:
    - event:2026-07-08#3
    ---
    <prose body>

``reviewed: false`` marks the page as a draft (the app shows a draft banner
with Review / ✕). Both actions resolve to ``reviewed: true`` — the page stays,
only the banner clears. Pages live in ``memory/`` so they fall inside the same
privacy / backup scope as every other memory file (no separate store).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import frontmatter

from .. import paths
from . import files as files_mod

_KIND = "book_page"
_PREFIX = "page-"


def _page_path(stem: str):  # type: ignore[no-untyped-def]
    return paths.memory_dir() / f"{stem}.md"


def _alloc_stem(date: str) -> str:
    """Return the first unused ``page-<date>[-i]`` stem for ``date``.

    The first page of a day is ``page-<date>``; the second ``page-<date>-2``,
    and so on. We only ever skip stems whose file already exists, so callers
    racing on the same day get distinct stems as long as each writes its file
    before the next allocates.
    """
    base = f"{_PREFIX}{date}"
    if not _page_path(base).exists():
        return base
    i = 2
    while _page_path(f"{base}-{i}").exists():
        i += 1
    return f"{base}-{i}"


def write_page(
    *,
    date: str,
    title: str,
    body: str,
    source_refs: tuple[str, ...] | list[str] = (),
    now: datetime | None = None,
) -> str:
    """Write a new book page and return its id (the file stem).

    ``date`` is the local date the page is *about* (``YYYY-MM-DD``); it drives
    the filename and list ordering. ``now`` is injectable for deterministic
    tests; defaults to the current local time for ``created_at``.
    """
    paths.memory_dir().mkdir(parents=True, exist_ok=True)
    stem = _alloc_stem(date)
    created_at = (now or datetime.now().astimezone()).replace(microsecond=0).isoformat()

    post = frontmatter.Post(
        body,
        kind=_KIND,
        title=title,
        date=date,
        created_at=created_at,
        reviewed=False,
        source_refs=list(source_refs),
    )
    files_mod.atomic_write_text(_page_path(stem), frontmatter.dumps(post) + "\n")
    return stem


def _load(stem: str) -> frontmatter.Post | None:
    path = _page_path(stem)
    if not path.exists():
        return None
    try:
        return frontmatter.load(path)
    except Exception:  # noqa: BLE001 — a malformed page must not crash listing/reads
        return None


def list_pages(limit: int = 20) -> list[dict[str, Any]]:
    """List book pages, newest ``date`` first (ties broken by stem desc).

    Returns lightweight rows: ``{id, title, date, kind, is_draft, source_refs}``.
    Malformed / non-book-page files are skipped.
    """
    memory_dir = paths.memory_dir()
    if not memory_dir.exists():
        return []

    rows: list[dict[str, Any]] = []
    for path in memory_dir.glob(f"{_PREFIX}*.md"):
        post = _load(path.stem)
        if post is None or post.metadata.get("kind") != _KIND:
            continue
        rows.append(
            {
                "id": path.stem,
                "title": str(post.metadata.get("title") or ""),
                "date": str(post.metadata.get("date") or ""),
                "kind": _KIND,
                "is_draft": not bool(post.metadata.get("reviewed", False)),
                "source_refs": list(post.metadata.get("source_refs") or []),
            }
        )

    rows.sort(key=lambda r: (r["date"], r["id"]), reverse=True)
    return rows[:limit]


def get_page(page_id: str) -> dict[str, Any] | None:
    """Return a single page ``{id, title, date, is_draft, body}`` or ``None``."""
    post = _load(page_id)
    if post is None or post.metadata.get("kind") != _KIND:
        return None
    return {
        "id": page_id,
        "title": str(post.metadata.get("title") or ""),
        "date": str(post.metadata.get("date") or ""),
        "is_draft": not bool(post.metadata.get("reviewed", False)),
        "body": post.content,
    }


def mark_reviewed(page_id: str) -> bool:
    """Flip ``reviewed: true`` on a page. Returns ``False`` if it doesn't exist.

    Like :func:`get_page`, this gates on ``kind == _KIND``: a ``page_id`` that
    resolves to some other (non-book-page) memory file — or to nothing — returns
    ``False`` without writing, so we never pollute unrelated frontmatter. A
    ``page_id`` containing ``/`` or ``..`` is rejected outright to block path
    traversal out of ``memory/``.
    """
    if "/" in page_id or "\\" in page_id or ".." in page_id:
        return False
    post = _load(page_id)
    if post is None or post.metadata.get("kind") != _KIND:
        return False
    files_mod.update_frontmatter(_page_path(page_id), {"reviewed": True})
    return True
