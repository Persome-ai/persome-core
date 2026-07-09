"""Regression for #625: the intent-dedup SELECT-then-INSERT was not wrapped in a
transaction, so the fast path (capture pool) and the slow path (timeline task) —
each on its OWN ``fts.cursor()`` connection — could both run the dedup SELECT,
both miss, then both INSERT the same commitment → twin rows.

The two recognizers are real concurrent producers:

- fast K1 (`intent.event_source.on_capture` → `fts_store.cursor()`),
- slow trajectory (`session.tick._recognize_blocking` → `fts.cursor()`).

When both compute the SAME ``dedup_key`` for one chat commitment and their
SELECT phases interleave before either commits, the whole-history existence
check returns "absent" on both connections and both ``insert_intent`` succeed.

The constitution forbids fixing this with a UNIQUE constraint: #525 deliberately
lets a recurring commitment carry the SAME ``dedup_key`` across weeks (multiple
legitimate rows). The fix is therefore to serialize the SELECT+INSERT critical
section with ``BEGIN IMMEDIATE`` so the second connection blocks until the first
commits, then sees the row and folds instead of inserting a twin.

The test forces the exact interleave deterministically: a barrier rendezvous is
injected at the dedup-check seam so BOTH threads finish their dedup SELECT before
EITHER proceeds to insert — the structural TOCTOU window the issue describes.
"""

from __future__ import annotations

import threading

from persome.intent import sink
from persome.intent import store as intent_store
from persome.intent.ontology import Intent, IntentEvidence
from persome.store import fts


def _mk(
    *,
    scope: str,
    when: str = "周五15:00",
    ts: str = "2026-06-12T09:00",
    people: list[str] | None = None,
) -> Intent:
    """One chat commitment. Two recognizers see the SAME fact under DIFFERENT
    scopes (fast-K1 vs session-*) — scope is intentionally omitted from the
    temporal dedup_key, so both compute the identical key."""
    return Intent(
        kind="meeting",
        scope=scope,
        confidence=0.8,
        rationale="",
        ts=ts,
        payload={"when_text": when, "with": people or ["Alice"]},
        evidence=[IntentEvidence(source="timeline_block", ref_id="blk-1", quote=when)],
    )


def _count(conn) -> int:  # noqa: ANN001
    return conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0]


def test_dedup_key_identical_across_paths(ac_root) -> None:  # noqa: ANN001
    """Sanity: the fast and slow recognitions of one commitment DO collide on the
    dedup_key (so the TOCTOU is reachable — different scope, same key)."""
    fast = _mk(scope="fast-K1")
    slow = _mk(scope="session-abc")
    assert intent_store.dedup_key(fast) == intent_store.dedup_key(slow)


def test_persist_critical_section_is_serialized_no_twin(ac_root) -> None:  # noqa: ANN001
    """Deterministic proof the resolve-then-insert critical section is serialized.

    Reproduces the EXACT race ordering — slow path A does its whole persist while
    fast path B is mid-flight — by driving it through the sink's own lock:

    1. A acquires the persist lock and inserts its row (held = the critical
       section that must be mutually exclusive).
    2. B (another thread, its own connection) tries to persist the SAME
       commitment and blocks on the lock — it CANNOT interleave its dedup-miss
       between A's check and A's insert.
    3. A releases; B proceeds, re-runs its dedup against the now-committed row,
       and folds.

    Without the lock (the #625 bug) B would not block at step 2: its dedup SELECT
    would miss A's not-yet-visible/uncommitted row and it would INSERT a twin.
    The assertion (one row, B skipped, no error) holds only with the guard.
    """
    commit = _mk(scope="session-A")
    twin = _mk(scope="fast-K1")  # same dedup_key, different scope (fast path)
    assert intent_store.dedup_key(commit) == intent_store.dedup_key(twin)

    b_result: dict[str, object] = {}
    b_error: list[BaseException] = []
    b_started = threading.Event()

    def persist_b() -> None:
        b_started.set()
        try:
            with fts.cursor() as conn_b:
                b_result["res"] = sink.persist_intent_result(conn_b, twin)
        except BaseException as exc:  # noqa: BLE001
            b_error.append(exc)

    # Hold the SAME lock the sink uses; A's persist runs while it is held so B is
    # forced to wait — modelling "fast path arrives while slow path is mid-persist".
    with sink._PERSIST_LOCK:
        with fts.cursor() as conn_a:
            res_a = sink.persist_intent_result(conn_a, commit)
        assert res_a.outcome == "inserted"

        bt = threading.Thread(target=persist_b)
        bt.start()
        b_started.wait(timeout=5)
        # Give B a beat to reach (and block on) the lock before we release it.
        bt.join(timeout=0.3)
        assert bt.is_alive(), "B did not block on the persist lock — section not serialized"

    bt.join(timeout=10)
    assert not b_error, f"B raised: {b_error}"

    with fts.cursor() as conn:
        n = _count(conn)
    assert n == 1, f"twin row inserted despite serialization → {n} rows"
    assert b_result["res"].outcome == "skipped", (  # type: ignore[attr-defined]
        f"B should have folded onto A's committed row (was {b_result['res']})"  # type: ignore[index]
    )


def _persist_in_thread(
    scope: str,
    when: str,
    ts: str,
    people: list[str],
    ready: threading.Barrier,
    start: threading.Barrier,
    errors: list[BaseException],
) -> None:
    """Thread body: open a connection, rendezvous, then race the persist call.

    The connection is opened BEFORE the start barrier so connection bring-up
    (which runs ``executescript(SCHEMA)`` at connect time) never overlaps; only
    the dedup-then-insert critical section races.
    """
    try:
        with fts.cursor() as conn:
            ready.wait()
            start.wait()  # release both persists at once → maximize the race
            sink.persist_intent_result(conn, _mk(scope=scope, when=when, ts=ts, people=people))
    except BaseException as exc:  # noqa: BLE001
        errors.append(exc)


def test_concurrent_fast_slow_threads_no_twin_no_error(ac_root) -> None:  # noqa: ANN001
    """Real two-thread race: the fast pool and the slow timeline task each open
    their OWN ``fts.cursor()`` and persist the SAME commitment at the same time.

    Mirrors production exactly (two daemon threads, two autocommit connections).
    Repeated many times to make the interleave likely. The invariant: never more
    than one row per commitment, and no ``database is locked`` blow-up — the
    serialized critical section makes the loser fold rather than race a twin
    INSERT.
    """
    # Warm the schema once on a throwaway connection so the per-thread
    # ``fts.cursor()`` setup isn't itself a concurrent-DDL race — we are testing
    # the dedup critical section, not connection bring-up.
    with fts.cursor() as warm:
        warm.execute("SELECT 1")

    when = "周五15:00"
    ts = "2026-06-12T09:00"
    for i in range(25):
        scope_a = f"session-{i}"
        scope_b = "fast-K1"
        # A fully INDEPENDENT commitment per iteration: a unique counterpart keeps
        # the semantic fold (grounded, ±30min, people-overlap, 48h window) from
        # collapsing this iteration's rows onto an earlier iteration's row, so the
        # only thing that may dedup these two is each other (the race under test).
        people = [f"Person{i}"]
        start = threading.Barrier(2, timeout=10)
        ready = threading.Barrier(3, timeout=10)
        errors: list[BaseException] = []

        args_a = (scope_a, when, ts, people, ready, start, errors)
        args_b = (scope_b, when, ts, people, ready, start, errors)
        ta = threading.Thread(target=_persist_in_thread, args=args_a)
        tb = threading.Thread(target=_persist_in_thread, args=args_b)
        ta.start()
        tb.start()
        ready.wait(timeout=10)  # both connections open
        ta.join(timeout=20)
        tb.join(timeout=20)

        assert not errors, f"iteration {i}: a persist path raised: {errors}"
        with fts.cursor() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM intents WHERE dedup_key = ?",
                (intent_store.dedup_key(_mk(scope=scope_a, when=when, ts=ts, people=people)),),
            ).fetchone()[0]
        assert n == 1, f"iteration {i}: concurrent persists produced {n} twin rows"
