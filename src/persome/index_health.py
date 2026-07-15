"""Index health self-check and capture heartbeat.

Persome is silent by default, so its owner must be able to distinguish
*intentional* silence (capture paused, nobody at the screen) from a *broken*
pipeline (FTS corruption, failing capture indexing, a stalled queue). This
module gives the daemon that voice:

- an in-process heartbeat the capture scheduler feeds on every capture,
  index insert, and queue drop;
- a periodic self-check task that verifies the captures/entries FTS5 indexes
  and the main B-tree, measures the buffer-vs-index backlog, and classifies
  the capture pipeline as ``active`` / ``paused`` / ``idle`` / ``broken``;
- a private sidecar file (``.index-health.json``) other processes (CLI,
  REST, MCP stdio) read, so downstream surfaces can degrade explicitly
  instead of pretending the evidence layer is healthy.

The self-check only observes and reports. Repair stays where it already
lives: startup integrity quarantine/recovery and
``persome rebuild-captures-index``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from . import paths
from .logger import get

if TYPE_CHECKING:
    from .config import Config

# Child of the persome.daemon sink so tick output lands in daemon.log.
logger = get("persome.daemon.index_health")

#: Sidecar freshness horizon: readers treat an older report as unknown rather
#: than trusting a snapshot from a daemon that stopped ticking.
STALE_AFTER_TICKS = 3

_lock = threading.Lock()
_counters: dict[str, Any] = {
    "captures_ok_total": 0,
    "last_capture_ok_at": None,
    "index_failures_total": 0,
    "consecutive_index_failures": 0,
    "last_index_ok_at": None,
    "last_index_error": None,
    "last_index_error_at": None,
    "queue_dropped_total": 0,
    "consecutive_queue_drops": 0,
    "last_queue_drop_at": None,
    "last_snapshot_ok": None,
    "last_snapshot_at": None,
    "last_snapshot_error": None,
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def record_capture_ok() -> None:
    """Heartbeat: a capture was committed to the buffer."""
    with _lock:
        _counters["captures_ok_total"] += 1
        _counters["last_capture_ok_at"] = _now_iso()


def record_index_result(ok: bool, error: str | None = None) -> None:
    """Heartbeat: outcome of one write-through captures FTS insert."""
    with _lock:
        if ok:
            _counters["consecutive_index_failures"] = 0
            _counters["last_index_ok_at"] = _now_iso()
        else:
            _counters["index_failures_total"] += 1
            _counters["consecutive_index_failures"] += 1
            _counters["last_index_error"] = (error or "")[:300]
            _counters["last_index_error_at"] = _now_iso()


def record_queue_drop() -> None:
    """Heartbeat: the bounded capture queue dropped a trigger."""
    with _lock:
        _counters["queue_dropped_total"] += 1
        _counters["consecutive_queue_drops"] += 1
        _counters["last_queue_drop_at"] = _now_iso()


def record_queue_accept() -> None:
    """Heartbeat: the capture queue accepted a trigger after any full-queue streak."""
    with _lock:
        _counters["consecutive_queue_drops"] = 0


def record_snapshot_outcome(ok: bool, error: str | None = None) -> None:
    """Record the latest daily snapshot outcome so status can surface it."""
    with _lock:
        _counters["last_snapshot_ok"] = ok
        _counters["last_snapshot_at"] = _now_iso()
        _counters["last_snapshot_error"] = None if ok else (error or "")[:300]


def _counters_snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_counters)


_CORRUPTION_MARKERS = (
    "malformed",
    "corrupt",
    "not a database",
    "disk image",
)


def is_corruption_error(exc: BaseException) -> bool:
    """Whether a SQLite error indicates index/database corruption."""
    if not isinstance(exc, sqlite3.Error):
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _CORRUPTION_MARKERS)


def _check_sqlite_indexes() -> tuple[str, list[str]]:
    """Verify the main B-tree plus both FTS5 inverted indexes.

    Returns ``(status, problems)`` where status is ``ok`` / ``corrupt`` /
    ``error``. The captures_fts check uses ``rank=1`` so the inverted index
    is also reconciled against its external-content ``captures`` table.
    """
    from .store import fts as fts_store

    problems: list[str] = []
    try:
        with fts_store.cursor() as conn:
            row = conn.execute("PRAGMA quick_check(20)").fetchall()
            findings = [str(r[0]) for r in row if str(r[0]) != "ok"]
            if findings:
                problems.extend(f"quick_check: {f}" for f in findings[:5])
            for table, external in (("captures_fts", True), ("entries", False)):
                try:
                    if external:
                        conn.execute(
                            f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)"
                        )
                    else:
                        conn.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")
                except sqlite3.Error as exc:
                    problems.append(f"{table}: {exc}")
    except sqlite3.Error as exc:
        if is_corruption_error(exc):
            return "corrupt", [str(exc)]
        return "error", [str(exc)]
    if not problems:
        return "ok", []
    corrupt = any(any(marker in p.lower() for marker in _CORRUPTION_MARKERS) for p in problems)
    return ("corrupt" if corrupt else "error"), problems


def _index_backlog() -> dict[str, int]:
    """Return exact buffer/index drift without letting stale rows hide gaps."""
    from .store import fts as fts_store

    buf = paths.capture_buffer_dir()
    buffer_ids = (
        {p.stem for p in buf.iterdir() if p.is_file() and p.suffix == ".json"}
        if buf.is_dir()
        else set()
    )
    with fts_store.cursor() as conn:
        indexed_ids = {str(row[0]) for row in conn.execute("SELECT id FROM captures")}
    missing_ids = buffer_ids - indexed_ids
    orphaned_ids = indexed_ids - buffer_ids
    return {
        "buffer_files": len(buffer_ids),
        "indexed_captures": len(indexed_ids),
        "backlog": len(missing_ids),
        "orphaned_index_rows": len(orphaned_ids),
    }


def _age_seconds(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    return (datetime.now(then.tzinfo or UTC) - then).total_seconds()


def _classify_capture_state(
    counters: dict[str, Any],
    *,
    paused: bool,
    failure_streak_threshold: int,
    queue_drop_window_seconds: int,
) -> tuple[str, str | None]:
    """Separate intentional silence from a broken pipeline.

    ``paused``  — the owner asked for silence.
    ``broken``  — captures are flowing but indexing keeps failing.
    ``active``  — a capture committed recently.
    ``idle``    — no recent capture and no recent failure: nothing on screen
                  worth capturing, which is the healthy silent state.
    """
    if paused:
        return "paused", None
    streak = int(counters.get("consecutive_index_failures") or 0)
    if streak >= failure_streak_threshold:
        return "broken", (
            f"{streak} consecutive captures FTS insert failures; "
            f"last: {counters.get('last_index_error')}"
        )
    queue_drop_age = _age_seconds(counters.get("last_queue_drop_at"))
    if queue_drop_age is not None and queue_drop_age <= queue_drop_window_seconds:
        return "broken", (
            "capture queue dropped a trigger recently; "
            f"{int(counters.get('consecutive_queue_drops') or 0)} consecutive, "
            f"{int(counters.get('queue_dropped_total') or 0)} total"
        )
    capture_age = _age_seconds(counters.get("last_capture_ok_at"))
    if capture_age is not None and capture_age < 300:
        return "active", None
    return "idle", None


def build_report(cfg: Config) -> dict[str, Any]:
    """Run one self-check pass and return the health report dict."""
    counters = _counters_snapshot()
    index_status, problems = _check_sqlite_indexes()
    backlog: dict[str, int] = {
        "buffer_files": -1,
        "indexed_captures": -1,
        "backlog": 0,
        "orphaned_index_rows": 0,
    }
    if index_status == "ok":
        try:
            backlog = _index_backlog()
        except sqlite3.Error as exc:
            index_status = "corrupt" if is_corruption_error(exc) else "error"
            problems.append(str(exc))
    paused = paths.paused_flag().exists()
    capture_state, capture_detail = _classify_capture_state(
        counters,
        paused=paused,
        failure_streak_threshold=cfg.index_health.failure_streak_threshold,
        queue_drop_window_seconds=max(300, cfg.index_health.tick_seconds * 2),
    )
    index_failure_broken = (
        int(counters.get("consecutive_index_failures") or 0)
        >= cfg.index_health.failure_streak_threshold
    )
    queue_drop_age = _age_seconds(counters.get("last_queue_drop_at"))
    recent_queue_drop = queue_drop_age is not None and queue_drop_age <= max(
        300, cfg.index_health.tick_seconds * 2
    )
    index_drift = max(backlog["backlog"], backlog["orphaned_index_rows"])
    degraded = (
        index_status != "ok"
        or capture_state == "broken"
        or index_drift > cfg.index_health.backlog_warn_threshold
        or counters.get("last_snapshot_ok") is False
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "checked_at": _now_iso(),
        "tick_seconds": cfg.index_health.tick_seconds,
        "status": "degraded" if degraded else "ok",
        "index": {"status": index_status, "problems": problems[:5]},
        "backlog": backlog,
        "capture": {
            "state": capture_state,
            "detail": capture_detail,
            "last_capture_ok_at": counters.get("last_capture_ok_at"),
            "captures_ok_total": counters.get("captures_ok_total"),
            "consecutive_index_failures": counters.get("consecutive_index_failures"),
            "index_failures_total": counters.get("index_failures_total"),
            "last_index_error": counters.get("last_index_error"),
            "queue_dropped_total": counters.get("queue_dropped_total"),
            "consecutive_queue_drops": counters.get("consecutive_queue_drops"),
            "last_queue_drop_at": counters.get("last_queue_drop_at"),
        },
        "snapshot": {
            "last_ok": counters.get("last_snapshot_ok"),
            "last_at": counters.get("last_snapshot_at"),
            "last_error": counters.get("last_snapshot_error"),
        },
    }
    if degraded:
        actions: list[str] = []
        if index_status != "ok":
            actions.append(
                "restart the daemon so startup integrity can quarantine/recover the index"
            )
        if index_drift > cfg.index_health.backlog_warn_threshold or index_failure_broken:
            actions.append(
                "run `persome stop`, then `persome rebuild-captures-index --merge`, "
                "then `persome start`"
            )
        if recent_queue_drop:
            actions.append(
                "check daemon load and capture-queue warnings; dropped triggers cannot replay"
            )
        if counters.get("last_snapshot_ok") is False:
            actions.append("check `snapshot` alerts in the daemon log")
        report["recommended_actions"] = actions
    return report


def write_report(report: dict[str, Any]) -> None:
    paths.atomic_write_private_text(
        paths.index_health_file(), json.dumps(report, ensure_ascii=False)
    )


def read_report() -> dict[str, Any] | None:
    """Read the latest health report; ``None`` when absent or unreadable.

    A stale report (older than ``STALE_AFTER_TICKS`` ticks) is returned with
    ``"stale": True`` so consumers can say "health unknown" rather than
    echoing a dead daemon's last good word.
    """
    try:
        raw = paths.index_health_file().read_text(encoding="utf-8")
        report = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    age = _age_seconds(report.get("checked_at"))
    tick = int(report.get("tick_seconds") or 300)
    if age is None or age > tick * STALE_AFTER_TICKS:
        report["stale"] = True
    return report


def degradation_note() -> dict[str, Any] | None:
    """Compact evidence-layer health context for read-tool payloads.

    ``None`` means healthy — callers add nothing. Otherwise the dict tells the
    consuming agent explicitly that results may be incomplete and why, so a
    silent index gap is never mistaken for "nothing happened on screen".
    """
    report = read_report()
    if report is None:
        return None
    if report.get("stale"):
        return {
            "status": "unknown",
            "note": (
                "index health report is stale (daemon may be down); "
                "capture-search results may be incomplete"
            ),
        }
    if report.get("status") == "ok":
        return None
    backlog = int((report.get("backlog") or {}).get("backlog") or 0)
    orphaned = int((report.get("backlog") or {}).get("orphaned_index_rows") or 0)
    capture_state = str((report.get("capture") or {}).get("state") or "unknown")
    index_status = str((report.get("index") or {}).get("status") or "unknown")
    parts: list[str] = []
    if index_status != "ok":
        parts.append(f"index integrity is {index_status}")
    if capture_state == "broken":
        parts.append("capture indexing is failing")
    if backlog:
        parts.append(f"{backlog} captured screens are not searchable yet")
    if orphaned:
        parts.append(f"{orphaned} capture index rows have no source file")
    return {
        "status": "degraded",
        "index": index_status,
        "capture_state": capture_state,
        "index_backlog": backlog,
        "orphaned_index_rows": orphaned,
        "note": "; ".join(parts) + " — results may be incomplete",
    }


def check_once(cfg: Config) -> dict[str, Any]:
    """One synchronous self-check pass: build, persist, and log the report."""
    report = build_report(cfg)
    write_report(report)
    if report["status"] != "ok":
        logger.warning(
            "index health degraded: index=%s capture=%s backlog=%d actions=%s",
            report["index"]["status"],
            report["capture"]["state"],
            report["backlog"]["backlog"],
            "; ".join(report.get("recommended_actions", [])),
        )
    return report


async def run_index_health_tick(cfg: Config) -> None:
    """Daemon task: periodic index self-check + heartbeat publication."""
    interval = max(30, int(cfg.index_health.tick_seconds))
    logger.info("index-health tick loop started (every %ds)", interval)
    while True:
        try:
            started = time.monotonic()
            report = await asyncio.to_thread(check_once, cfg)
            elapsed_ms = (time.monotonic() - started) * 1000
            logger.debug(
                "index health: %s (%.0fms)",
                report["status"],
                elapsed_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the health loop must outlive one bad pass
            logger.error("index-health tick failed: %s", exc, exc_info=True)
        await asyncio.sleep(interval)
