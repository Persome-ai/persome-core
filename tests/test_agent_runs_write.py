"""Unit tests for agent_runs write-side DAO (Phase 1b)."""

from __future__ import annotations

from persome.store import agent_runs as store
from persome.store import fts


def test_enqueue_dedups_same_kind(ac_root) -> None:
    with fts.cursor() as conn:
        a = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        b = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
    assert a == b  # second enqueue of a still-queued kind folds into the first


def test_enqueue_distinct_after_claim(ac_root) -> None:
    with fts.cursor() as conn:
        a = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        store.mark_running(conn, a)  # no longer queued
        b = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
    assert a != b


def test_enqueue_payload_aware_same_payload_folds(ac_root) -> None:
    """#397: a second enqueue with the SAME payload folds into the queued row."""
    p = {"deep": True, "exclude": ["Desktop"]}
    with fts.cursor() as conn:
        a = store.enqueue(conn, kind="bootstrap", trigger="user", dispatch_source="user", payload=p)
        b = store.enqueue(
            conn, kind="bootstrap", trigger="user", dispatch_source="user", payload=dict(p)
        )
    assert a == b  # identical payload → fold


def test_enqueue_payload_aware_different_payload_opens_new_row(ac_root) -> None:
    """#397: a queued bootstrap with a DIFFERENT payload must NOT fold — the
    user's new selection opens a fresh row instead of being silently dropped."""
    with fts.cursor() as conn:
        a = store.enqueue(
            conn,
            kind="bootstrap",
            trigger="user",
            dispatch_source="user",
            payload={"deep": True, "exclude": []},
        )
        # User unchecks a folder + switches to shallow → different payload.
        b = store.enqueue(
            conn,
            kind="bootstrap",
            trigger="user",
            dispatch_source="user",
            payload={"deep": False, "exclude": ["Documents"]},
        )
    assert a != b  # different payload → new row, latest selection wins
    # And the new row actually carries the user's latest payload.
    with fts.cursor() as conn:
        run_b = store.get_run(conn, b)
    assert run_b.payload == {"deep": False, "exclude": ["Documents"]}


def test_find_queued_dup_payload_match(ac_root) -> None:
    """find_queued_dup reports the foldable row only on an exact payload match."""
    p = {"deep": False, "exclude": ["Downloads"]}
    with fts.cursor() as conn:
        rid = store.enqueue(
            conn, kind="bootstrap", trigger="user", dispatch_source="user", payload=p
        )
        # Same payload → found.
        assert store.find_queued_dup(conn, kind="bootstrap", payload=dict(p)) == rid
        # Different payload → not found.
        assert (
            store.find_queued_dup(conn, kind="bootstrap", payload={"deep": True, "exclude": []})
            is None
        )


def test_claim_oldest_queued_guarded(ac_root) -> None:
    with fts.cursor() as conn:
        first = store.enqueue(conn, kind="bootstrap", trigger="user", dispatch_source="user")
        # claim transitions queued→running atomically and returns the row id
        claimed = store.claim_oldest_queued(conn, kind="bootstrap")
        assert claimed == first
        run = store.get_run(conn, first)
        assert run.status == "running"
        assert run.started_at is not None
        # nothing left to claim
        assert store.claim_oldest_queued(conn, kind="bootstrap") is None


def test_count_inflight(ac_root) -> None:
    with fts.cursor() as conn:
        store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        rid = store.enqueue(conn, kind="bootstrap", trigger="user", dispatch_source="user")
        store.mark_running(conn, rid)
        assert store.count_inflight(conn, kind="bootstrap") == 1  # running counts
        assert store.count_inflight(conn, kind="dream") == 0  # queued does not


def test_end_fail_cancel(ac_root) -> None:
    with fts.cursor() as conn:
        rid = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        store.mark_running(conn, rid)
        store.end_run(
            conn,
            rid,
            committed=True,
            summary="done",
            result_refs=[{"type": "memory", "path": "x.md"}],
            iterations=3,
        )
        assert store.get_run(conn, rid).status == "committed"

        f = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        store.mark_running(conn, f)
        store.fail_run(conn, f, error="boom")
        assert store.get_run(conn, f).status == "failed"

        q = store.enqueue(conn, kind="bootstrap", trigger="user", dispatch_source="user")
        assert store.cancel_run(conn, q) is True  # queued → cancelled
        assert store.get_run(conn, q).status == "cancelled"


def test_mark_orphans_running_only(ac_root) -> None:
    with fts.cursor() as conn:
        running = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        store.mark_running(conn, running)
        queued = store.enqueue(conn, kind="bootstrap", trigger="user", dispatch_source="user")
        n = store.mark_orphans_running(conn)
    with fts.cursor() as conn:
        assert n == 1
        assert store.get_run(conn, running).status == "failed"  # running → failed
        assert store.get_run(conn, queued).status == "queued"  # queued preserved


def test_append_and_list_events(ac_root) -> None:
    with fts.cursor() as conn:
        rid = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        store.append_event(conn, rid, "progress", {"value": 0.5})
        store.append_event(conn, rid, "stage_end", {"status": "committed"})
        evs = store.list_events(conn, rid)
    assert [e.type for e in evs] == ["progress", "stage_end"]
    assert evs[0].payload == {"value": 0.5}


def test_enqueue_round_trips_payload(ac_root) -> None:
    """payload passed to enqueue is stored and read back intact by get_run —
    this is what carries bootstrap's deep/exclude through to the executor."""
    with fts.cursor() as conn:
        rid = store.enqueue(
            conn,
            kind="bootstrap",
            trigger="user",
            dispatch_source="user",
            payload={"deep": False, "exclude": ["Desktop"]},
        )
        run = store.get_run(conn, rid)
    assert run.payload == {"deep": False, "exclude": ["Desktop"]}


def test_end_run_skipped_leaves_progress_null(ac_root) -> None:
    """A skipped run did no work this cycle → progress stays NULL (honest
    indeterminate), never a fabricated full bar. Committed → 1.0."""
    with fts.cursor() as conn:
        rid = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        store.mark_running(conn, rid)
        store.end_run(
            conn, rid, committed=False, summary="nothing to do", result_refs=[], iterations=0
        )
        skipped = store.get_run(conn, rid)

        cid = store.enqueue(conn, kind="dream", trigger="user", dispatch_source="user")
        store.mark_running(conn, cid)
        store.end_run(conn, cid, committed=True, summary="done", result_refs=[], iterations=1)
        committed = store.get_run(conn, cid)
    assert skipped.status == "skipped"
    assert skipped.progress is None
    assert committed.status == "committed"
    assert committed.progress == 1.0
