"""Shared active/terminal model writer and manual recovery entry point.

The writer is driven by session boundaries. ``SessionManager.on_session_end``
spawns the reducer asynchronously (see ``session/tick.py``), then this module
runs every enabled terminal modeling stage exactly once: the legacy classifier
compatibility path, pattern detection, and the structured memory delta that
mints Points and Lines by default. ``persome writer run``, the retry tick, the
daily safety net, and ``model build`` all recover through this same entrance.
"""

from __future__ import annotations

import fcntl
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .. import paths
from ..config import Config
from ..logger import get
from ..session import store as session_store
from ..store import fts
from ..store import memory_deltas as deltas_store
from ..timeline import store as timeline_store
from . import classifier as classifier_mod
from . import memory_delta as memory_delta_mod
from . import pattern_detector as pattern_detector_mod
from . import session_reducer

logger = get("persome.writer")


@dataclass
class WriterRunResult:
    reduced: int = 0
    classified: int = 0
    modeled: int = 0
    written_ids: list[str] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)


@dataclass
class SessionModelResult:
    session_id: str
    completed: bool = False
    skipped_reason: str = ""
    classifier: Any = None
    pattern: Any = None
    delta: Any = None
    errors: list[str] = field(default_factory=list)


def _event_path(row: session_store.SessionRow) -> str:
    return f"event-{row.start_time.date().isoformat()}.md"


def _delta_completed(cfg: Config, delta: Any) -> bool:
    benign = {
        "disabled",
        "no_blocks",
        "no_window",
        "already_processed",
        "resumed_apply",
    }
    complete = bool(delta.written or delta.skipped_reason in benign)
    if getattr(cfg.memory_delta, "apply_enabled", False):
        complete = complete and bool(
            delta.applied or delta.skipped_reason in {"no_blocks", "no_window"}
        )
    return complete


def _advance_delta_watermark(
    session_id: str,
    window_end: datetime,
    delta: Any,
) -> None:
    with fts.cursor() as conn:
        session_store.set_delta_end(conn, session_id, window_end)
        if sum(delta.counts.values()) > 0:
            session_store.increment_system_state(conn, "model_structure_dirty")


def _model_delta_range(
    cfg: Config,
    *,
    session_id: str,
    window_start: datetime,
    window_end: datetime,
    terminal: bool,
) -> tuple[Any, str]:
    """Resume persisted windows first, then model only the unprocessed tail."""
    if window_start >= window_end:
        return memory_delta_mod.DeltaResult(
            session_id=session_id,
            skipped_reason="no_window",
        ), ""

    cursor = window_start
    last_delta: Any = None
    while cursor < window_end:
        with fts.cursor() as conn:
            persisted = deltas_store.next_for_session_start(
                conn,
                session_id,
                window_start=cursor,
                through=window_end,
            )
        persisted_end = None
        persisted_final = False
        if persisted is not None:
            try:
                persisted_end = datetime.fromisoformat(str(persisted["window_end"]))
                persisted_final = bool(persisted["is_final"])
            except (TypeError, ValueError):
                persisted_end = None
        target = persisted_end or window_end
        use_terminal = persisted_final or (terminal and target == window_end)
        ensure = (
            memory_delta_mod.ensure_after_session
            if use_terminal
            else memory_delta_mod.ensure_active_window
        )
        last_delta = ensure(
            cfg,
            session_id=session_id,
            start_time=cursor,
            end_time=target,
        )
        if not _delta_completed(cfg, last_delta):
            return last_delta, f"memory_delta: {last_delta.skipped_reason or 'not applied'}"
        _advance_delta_watermark(session_id, target, last_delta)
        cursor = target

    return last_delta, ""


def model_active_session(cfg: Config, *, session_id: str) -> SessionModelResult:
    """Turn the latest flushed active-session window into Points and Lines."""
    result = SessionModelResult(session_id=session_id)
    lock_path = paths.session_model_lock()
    with paths.open_private_lock_file(lock_path) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with fts.cursor() as conn:
                row = session_store.get_by_id(conn, session_id)
            if row is None or row.status != "active" or row.flush_end is None:
                result.skipped_reason = "session not ready for active modeling"
                return result
            window_start = row.delta_end or row.start_time
            window_end = row.flush_end
            result.delta, error = _model_delta_range(
                cfg,
                session_id=session_id,
                window_start=window_start,
                window_end=window_end,
                terminal=False,
            )
            if error:
                result.errors.append(error)
                return result
            result.completed = True
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def finalize_session(
    cfg: Config,
    *,
    session_id: str,
    event_daily_path: str = "",
    just_written_entry_id: str = "",
    stage_clock: datetime | None = None,
) -> SessionModelResult:
    """Run terminal model stages once for a reduced session.

    A kernel ``flock`` coordinates the daemon callback with manual/model-build
    recovery. ``modeled_at`` is written only after every enabled stage either
    completes or returns a deliberate no-work result, so a crash remains
    retryable without repeating a successful memory-delta LLM call.
    """
    result = SessionModelResult(session_id=session_id)
    clock = _resolve_stage_clock(stage_clock)
    retry_reasons: list[str] = []
    hard_error = False
    lock_path = paths.session_model_lock()
    with paths.open_private_lock_file(lock_path) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with fts.cursor() as conn:
                row = session_store.get_by_id(conn, session_id)
            if row is None or row.status != "reduced" or row.end_time is None:
                result.skipped_reason = "session not ready for modeling"
                return result
            if row.modeled_at is not None:
                result.completed = True
                result.skipped_reason = "already modeled"
                return result

            path = event_daily_path or _event_path(row)
            try:
                result.classifier = classifier_mod.classify_after_reduce(
                    cfg,
                    session_id=session_id,
                    event_daily_path=path,
                    just_written_entry_id=just_written_entry_id,
                    session_start=row.start_time,
                    session_end=row.end_time,
                    window_start=row.classified_end,
                    stage_clock=row.end_time,
                    processing_clock=clock,
                )
                classify_ok = bool(
                    not getattr(result.classifier, "retryable", False)
                    and (result.classifier.committed or result.classifier.skipped_reason)
                )
                if classify_ok:
                    with fts.cursor() as conn:
                        session_store.set_classified_end(conn, session_id, row.end_time)
                elif getattr(result.classifier, "retryable", False):
                    retry_reasons.append(
                        str(result.classifier.skipped_reason or "evidence unavailable")
                    )
                    result.errors.append(
                        f"classifier: {result.classifier.skipped_reason or 'evidence unavailable'}"
                    )
                else:
                    hard_error = True
                    result.errors.append("classifier ended without commit")
            except Exception as exc:  # noqa: BLE001
                hard_error = True
                result.errors.append(f"classifier: {type(exc).__name__}: {exc}")
                logger.warning("classifier %s crashed: %s", session_id, exc, exc_info=True)

            try:
                result.pattern = pattern_detector_mod.detect_after_classify(
                    cfg,
                    session_id=session_id,
                    event_daily_path=path,
                    session_start=row.start_time,
                    session_end=row.end_time,
                    stage_clock=row.end_time,
                )
                pattern_ok = bool(
                    not getattr(result.pattern, "retryable", False)
                    and (result.pattern.committed or result.pattern.skipped_reason)
                )
                if pattern_ok:
                    with fts.cursor() as conn:
                        session_store.set_pattern_detected_end(conn, session_id, row.end_time)
                elif getattr(result.pattern, "retryable", False):
                    retry_reasons.append(
                        str(result.pattern.skipped_reason or "evidence unavailable")
                    )
                    result.errors.append(
                        "pattern_detector: "
                        f"{result.pattern.skipped_reason or 'evidence unavailable'}"
                    )
                else:
                    hard_error = True
                    result.errors.append("pattern detector ended without commit")
            except Exception as exc:  # noqa: BLE001
                hard_error = True
                result.errors.append(f"pattern_detector: {type(exc).__name__}: {exc}")
                logger.warning("pattern_detector %s crashed: %s", session_id, exc, exc_info=True)

            try:
                result.delta, error = _model_delta_range(
                    cfg,
                    session_id=session_id,
                    window_start=row.delta_end or row.start_time,
                    window_end=row.end_time,
                    terminal=True,
                )
                if error:
                    result.errors.append(error)
                    retry_reasons.append(
                        str(getattr(result.delta, "skipped_reason", "") or "memory_delta_failed")
                    )
            except Exception as exc:  # noqa: BLE001
                hard_error = True
                result.errors.append(f"memory_delta: {type(exc).__name__}: {exc}")
                logger.warning("memory_delta %s crashed: %s", session_id, exc, exc_info=True)

            if not result.errors:
                with fts.cursor() as conn:
                    session_store.mark_modeled(conn, session_id, clock)
                result.completed = True
            else:
                unique_retry_reasons = set(retry_reasons)
                retry_reason = (
                    next(iter(unique_retry_reasons))
                    if not hard_error and len(unique_retry_reasons) == 1
                    else "stage_error"
                )
                with fts.cursor() as conn:
                    session_store.set_model_retry_reason(conn, session_id, retry_reason)
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run(
    cfg: Config,
    *,
    limit: int | None = None,
    stage_clock: datetime | None = None,
) -> WriterRunResult:
    """Reduce pending sessions, then finish every unmodeled reduced session."""
    result = WriterRunResult()
    if not cfg.reducer.enabled:
        logger.info("writer run: reducer disabled, nothing to do")
        return result

    reduction_limit = limit
    if limit is not None:
        # Existing reduced sessions already consume this request's allowance.
        # Only reduce enough additional sessions to keep the unique session
        # count within the user-approved bound.
        with fts.cursor() as conn:
            already_pending = session_store.list_pending_modeling(conn)
        reduction_limit = max(0, limit - len(already_pending[:limit]))

    clock = _resolve_stage_clock(stage_clock)
    if stage_clock is None:
        reduce_results = session_reducer.reduce_all_pending(cfg, limit=reduction_limit)
    else:
        reduce_results = session_reducer.reduce_all_pending(
            cfg,
            limit=reduction_limit,
            stage_clock=clock,
        )
    result.reduced = sum(1 for rr in reduce_results if rr.succeeded)

    with fts.cursor() as conn:
        pending = session_store.list_pending_modeling(conn)
    if limit is not None:
        pending = pending[: max(0, limit)]
    reduced_by_id = {rr.session_id: rr for rr in reduce_results}
    for row in pending:
        rr = reduced_by_id.get(row.id)
        modeled = finalize_session(
            cfg,
            session_id=row.id,
            event_daily_path=rr.path if rr else "",
            just_written_entry_id=rr.entry_id if rr else "",
            stage_clock=clock,
        )
        if modeled.completed:
            result.modeled += 1
        cr = modeled.classifier
        if cr is not None and cr.committed:
            result.classified += 1
            result.written_ids.extend(cr.written_ids)
            if cr.summary:
                result.summaries.append(cr.summary)
    return result


def retry_pending_modeling(
    cfg: Config,
    *,
    limit: int | None = None,
    stage_clock: datetime | None = None,
) -> list[SessionModelResult]:
    """Retry only closing-block waits that became newly eligible.

    Generic classifier, pattern, store, and LLM errors are deliberately left to
    boot/daily/manual recovery rather than hammered every minute.  The minute
    loop handles one cheap, provable transition only: a reduced session was
    persisted as ``awaiting_closing_block`` and that exact wall block now exists.
    """
    with fts.cursor() as conn:
        pending = session_store.list_pending_modeling(conn)
        eligible: list[session_store.SessionRow] = []
        window_minutes = max(1, int(getattr(cfg.timeline, "window_minutes", 1)))
        step = timedelta(minutes=window_minutes)
        for row in pending:
            if row.model_retry_reason != "awaiting_closing_block" or row.end_time is None:
                continue
            closing_start = timeline_store.floor_to_window(row.end_time, window_minutes)
            if closing_start >= row.end_time:
                continue
            if timeline_store.get_window(conn, closing_start, closing_start + step) is not None:
                eligible.append(row)
    if limit is not None:
        eligible = eligible[: max(0, limit)]
    clock = _resolve_stage_clock(stage_clock)
    return [finalize_session(cfg, session_id=row.id, stage_clock=clock) for row in eligible]


def _resolve_stage_clock(value: datetime | None) -> datetime:
    clock = value or datetime.now().astimezone()
    if clock.tzinfo is None:
        raise ValueError("writer stage_clock must be timezone-aware")
    return clock
