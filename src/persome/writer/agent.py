"""Shared terminal session finalizer and manual writer recovery entry point.

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
from datetime import datetime
from typing import Any

from .. import paths
from ..config import Config
from ..logger import get
from ..session import store as session_store
from ..store import fts
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


def finalize_session(
    cfg: Config,
    *,
    session_id: str,
    event_daily_path: str = "",
    just_written_entry_id: str = "",
) -> SessionModelResult:
    """Run terminal model stages once for a reduced session.

    A kernel ``flock`` coordinates the daemon callback with manual/model-build
    recovery. ``modeled_at`` is written only after every enabled stage either
    completes or returns a deliberate no-work result, so a crash remains
    retryable without repeating a successful memory-delta LLM call.
    """
    result = SessionModelResult(session_id=session_id)
    lock_path = paths.session_model_lock()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        lock_path.chmod(0o600)
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
                )
                classify_ok = bool(result.classifier.committed or result.classifier.skipped_reason)
                if classify_ok:
                    with fts.cursor() as conn:
                        session_store.set_classified_end(conn, session_id, row.end_time)
                else:
                    result.errors.append("classifier ended without commit")
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"classifier: {type(exc).__name__}: {exc}")
                logger.warning("classifier %s crashed: %s", session_id, exc, exc_info=True)

            try:
                result.pattern = pattern_detector_mod.detect_after_classify(
                    cfg,
                    session_id=session_id,
                    event_daily_path=path,
                    session_start=row.start_time,
                    session_end=row.end_time,
                )
                pattern_ok = bool(result.pattern.committed or result.pattern.skipped_reason)
                if pattern_ok:
                    with fts.cursor() as conn:
                        session_store.set_pattern_detected_end(conn, session_id, row.end_time)
                else:
                    result.errors.append("pattern detector ended without commit")
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"pattern_detector: {type(exc).__name__}: {exc}")
                logger.warning("pattern_detector %s crashed: %s", session_id, exc, exc_info=True)

            try:
                result.delta = memory_delta_mod.ensure_after_session(
                    cfg,
                    session_id=session_id,
                    start_time=row.start_time,
                    end_time=row.end_time,
                )
                benign = {
                    "disabled",
                    "no_blocks",
                    "no_window",
                    "already_processed",
                    "resumed_apply",
                }
                delta_ok = result.delta.written or result.delta.skipped_reason in benign
                if getattr(cfg.memory_delta, "apply_enabled", False):
                    delta_ok = delta_ok and (
                        result.delta.applied or result.delta.skipped_reason == "no_blocks"
                    )
                if not delta_ok:
                    result.errors.append(
                        f"memory_delta: {result.delta.skipped_reason or 'not applied'}"
                    )
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"memory_delta: {type(exc).__name__}: {exc}")
                logger.warning("memory_delta %s crashed: %s", session_id, exc, exc_info=True)

            if not result.errors:
                with fts.cursor() as conn:
                    session_store.mark_modeled(conn, session_id, datetime.now().astimezone())
                result.completed = True
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run(cfg: Config) -> WriterRunResult:
    """Reduce pending sessions, then finish every unmodeled reduced session."""
    result = WriterRunResult()
    if not cfg.reducer.enabled:
        logger.info("writer run: reducer disabled, nothing to do")
        return result

    reduce_results = session_reducer.reduce_all_pending(cfg)
    result.reduced = sum(1 for rr in reduce_results if rr.succeeded)

    with fts.cursor() as conn:
        pending = session_store.list_pending_modeling(conn)
    reduced_by_id = {rr.session_id: rr for rr in reduce_results}
    for row in pending:
        rr = reduced_by_id.get(row.id)
        modeled = finalize_session(
            cfg,
            session_id=row.id,
            event_daily_path=rr.path if rr else "",
            just_written_entry_id=rr.entry_id if rr else "",
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
