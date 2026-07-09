"""Tests for the POST /book/generate endpoint (Phase 3 X)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from persome.api import build_api_app


def _client(ac_root) -> TestClient:
    return TestClient(build_api_app())


def test_generate_returns_counts(ac_root, monkeypatch) -> None:
    """POST /book/generate triggers both run functions and reports their counts."""
    from persome.api import book_generate_routes

    calls: dict[str, int] = {"pages": 0, "chapters": 0}

    def fake_run_pages(date: str, **_kwargs) -> list[str]:
        calls["pages"] += 1
        return ["page-1", "page-2"]

    def fake_run_chapters(**_kwargs) -> int:
        calls["chapters"] += 1
        return 3

    monkeypatch.setattr(book_generate_routes.book_page, "run_book_pages", fake_run_pages)
    monkeypatch.setattr(book_generate_routes.book_chapters, "run_book_chapters", fake_run_chapters)

    client = _client(ac_root)
    res = client.post("/book/generate")

    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert body["data"] == {"pages": ["page-1", "page-2"], "chapters": 3}
    assert calls == {"pages": 1, "chapters": 1}


def test_generate_empty_day_is_ok(ac_root, monkeypatch) -> None:
    """A flat day (no pages, no chapters) returns 200 with zero counts."""
    from persome.api import book_generate_routes

    monkeypatch.setattr(book_generate_routes.book_page, "run_book_pages", lambda *a, **k: [])
    monkeypatch.setattr(book_generate_routes.book_chapters, "run_book_chapters", lambda *a, **k: 0)

    client = _client(ac_root)
    res = client.post("/book/generate")

    assert res.status_code == 200
    assert res.json()["data"] == {"pages": [], "chapters": 0}


def test_generate_409_when_generation_in_flight(ac_root, monkeypatch) -> None:
    """When the shared book-generation lock is already held (a dream's book
    sub-step — or another /book/generate — is mid-run), the endpoint returns
    409 and never touches storage, instead of racing the in-flight run on the
    same files/tables (#354).
    """
    from persome.api import book_generate_routes
    from persome.writer import book_generate

    calls: dict[str, int] = {"pages": 0, "chapters": 0}

    def fake_run_pages(date: str, **_kwargs) -> list[str]:
        calls["pages"] += 1
        return ["page-1"]

    def fake_run_chapters(**_kwargs) -> int:
        calls["chapters"] += 1
        return 1

    monkeypatch.setattr(book_generate_routes.book_page, "run_book_pages", fake_run_pages)
    monkeypatch.setattr(book_generate_routes.book_chapters, "run_book_chapters", fake_run_chapters)

    client = _client(ac_root)

    # Simulate the other writer holding the single book-generation slot.
    assert book_generate.try_acquire_book_generate() is True
    try:
        res = client.post("/book/generate")
    finally:
        book_generate.release_book_generate()

    assert res.status_code == 409
    # The contended run must not have written anything.
    assert calls == {"pages": 0, "chapters": 0}


def test_generate_releases_lock_for_next_caller(ac_root, monkeypatch) -> None:
    """A successful /book/generate releases the shared lock so the next caller
    (or a dream sub-step) can acquire it — no leak on the happy path.
    """
    from persome.api import book_generate_routes
    from persome.writer import book_generate

    monkeypatch.setattr(book_generate_routes.book_page, "run_book_pages", lambda *a, **k: ["p"])
    monkeypatch.setattr(book_generate_routes.book_chapters, "run_book_chapters", lambda *a, **k: 1)

    client = _client(ac_root)
    assert client.post("/book/generate").status_code == 200

    # Lock is free again after the request completed.
    assert book_generate.try_acquire_book_generate() is True
    book_generate.release_book_generate()


def test_generate_releases_lock_on_failure(ac_root, monkeypatch) -> None:
    """If a run function raises, the lock is still released (finally), so a
    failed generation can never wedge the slot shut for everyone after it.
    """
    from persome.api import book_generate_routes
    from persome.writer import book_generate

    def boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(book_generate_routes.book_page, "run_book_pages", boom)
    monkeypatch.setattr(book_generate_routes.book_chapters, "run_book_chapters", lambda *a, **k: 0)

    client = _client(ac_root)
    # The route does not catch generic errors, so TestClient re-raises it.
    import pytest

    with pytest.raises(RuntimeError, match="kaboom"):
        client.post("/book/generate")

    # Lock must not be leaked despite the failure.
    assert book_generate.try_acquire_book_generate() is True
    book_generate.release_book_generate()


def test_dream_book_substeps_share_lock_with_api(ac_root, monkeypatch) -> None:
    """The dream executor's book sub-steps run inside the SAME lock the API
    uses, so the two can never enter the critical section concurrently (#354).

    We assert mutual exclusion directly: while the lock is held, the dream
    executor's book sub-steps must NOT run (their fakes record no call until
    the lock is free).
    """
    from persome.config import load as load_config
    from persome.runs import registry
    from persome.writer import book_chapters, book_generate, book_page
    from persome.writer import dream as dream_mod

    ran: list[str] = []

    monkeypatch.setattr(book_page, "run_book_pages", lambda *a, **k: ran.append("pages") or [])
    monkeypatch.setattr(
        book_chapters, "run_book_chapters", lambda *a, **k: ran.append("chapters") or 0
    )
    # Stub the dream stage itself so the executor only exercises the book steps.
    from persome.writer.dream import DreamResult

    monkeypatch.setattr(
        dream_mod,
        "run_dream",
        lambda *a, **k: DreamResult(committed=False, summary="", written_ids=[], created_paths=[]),
    )

    # Hold the lock, then run the executor in a thread. It must block on the
    # book sub-steps (blocking=True) and not record any book call until release.
    import threading

    assert book_generate.try_acquire_book_generate() is True
    done = threading.Event()

    def _run() -> None:
        registry._dream_executor(load_config(), lambda *a, **k: None, {})
        done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # While we hold the lock the book sub-steps are blocked → no calls recorded.
    finished_early = done.wait(timeout=0.3)
    assert finished_early is False
    assert ran == []  # dream ran, but book steps are waiting on the lock

    # Release → the executor proceeds and runs both book sub-steps.
    book_generate.release_book_generate()
    assert done.wait(timeout=2.0) is True
    assert ran == ["pages", "chapters"]
