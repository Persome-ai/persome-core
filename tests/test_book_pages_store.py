"""Tests for the book-page memory store DAO (Task 1)."""

from __future__ import annotations

from persome.store import book_pages


def test_write_then_list_and_get(ac_root):
    pid = book_pages.write_page(
        date="2026-07-08",
        title="On an Unnecessary Phone Call",
        body="para one.\n\npara two.",
        source_refs=["event:2026-07-08#3"],
    )
    assert pid == "page-2026-07-08"
    rows = book_pages.list_pages(limit=10)
    assert rows[0]["id"] == pid and rows[0]["is_draft"] is True
    full = book_pages.get_page(pid)
    assert full is not None
    assert full["title"].startswith("On an") and full["is_draft"] is True
    assert full["body"]  # non-empty


def test_same_day_second_page_suffixes(ac_root):
    a = book_pages.write_page(date="2026-07-08", title="A", body="x")
    b = book_pages.write_page(date="2026-07-08", title="B", body="y")
    assert a == "page-2026-07-08" and b == "page-2026-07-08-2"


def test_mark_reviewed_clears_draft(ac_root):
    pid = book_pages.write_page(date="2026-07-08", title="A", body="x")
    assert book_pages.mark_reviewed(pid) is True
    page = book_pages.get_page(pid)
    assert page is not None
    assert page["is_draft"] is False


def test_mark_reviewed_unknown_returns_false(ac_root):
    assert book_pages.mark_reviewed("page-does-not-exist") is False


def test_mark_reviewed_non_book_page_returns_false_and_no_write(ac_root):
    """A non-book_page .md under memory/ must not be touched by mark_reviewed."""
    import frontmatter

    from persome import paths
    from persome.store import files as files_mod

    paths.memory_dir().mkdir(parents=True, exist_ok=True)
    other = paths.memory_dir() / "user-profile.md"
    post = frontmatter.Post("body", kind="user_profile")
    files_mod.atomic_write_text(other, frontmatter.dumps(post) + "\n")

    assert book_pages.mark_reviewed("user-profile") is False
    # frontmatter untouched: no `reviewed` key was written.
    reloaded = frontmatter.load(other)
    assert "reviewed" not in reloaded.metadata
    assert reloaded.metadata.get("kind") == "user_profile"


def test_mark_reviewed_rejects_path_traversal(ac_root):
    """page_id containing `/` or `..` must be rejected, even if it resolves
    to an existing book_page-shaped file."""
    assert book_pages.mark_reviewed("../page-2026-07-08") is False
    assert book_pages.mark_reviewed("sub/page-2026-07-08") is False
    assert book_pages.mark_reviewed("..") is False


def test_get_unknown_returns_none(ac_root):
    assert book_pages.get_page("page-nope") is None


def test_list_sorted_date_desc(ac_root):
    book_pages.write_page(date="2026-07-08", title="older", body="x")
    book_pages.write_page(date="2026-07-10", title="newer", body="y")
    rows = book_pages.list_pages(limit=10)
    assert [r["date"] for r in rows] == ["2026-07-10", "2026-07-08"]
