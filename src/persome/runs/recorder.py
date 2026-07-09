"""run_recorded: the single place that records an agent run's lifecycle.

Given a run row already marked 'running', it builds an on_event that (a) publishes
an 'agent_run' SSE frame, (b) appends to agent_run_events, (c) updates the row's
real progress when the event carries a numeric value — then invokes the kind's
executor and writes the terminal state (committed/skipped on success, failed on
exception). Mirrors dream's _on_event contract (writer/dream.py).
"""

from __future__ import annotations

from typing import Any

from .. import events as events_mod
from ..config import Config
from ..logger import get
from ..store import agent_runs as store
from ..store import fts
from .registry import KIND_REGISTRY

logger = get("persome.runs.recorder")


def run_recorded(cfg: Config, run_id: int) -> None:
    """Execute the run identified by run_id (already 'running') and record it."""
    with fts.cursor() as conn:
        run = store.get_run(conn, run_id)
    if run is None:
        logger.warning("run_recorded: run %s not found", run_id)
        return
    spec = KIND_REGISTRY.get(run.kind)
    if spec is None:
        with fts.cursor() as conn:
            store.fail_run(conn, run_id, error=f"unknown kind {run.kind!r}")
        return

    def _on_event(event_type: str, payload: dict[str, Any]) -> None:
        enriched = {"run_id": run_id, "kind": run.kind, **payload}
        events_mod.publish("agent_run", event_type, enriched)
        try:
            with fts.cursor() as conn:
                store.append_event(conn, run_id, event_type, payload)
                # Real progress only: numeric 'value' in [0,1]; never fabricated.
                val = payload.get("value")
                if isinstance(val, (int, float)):
                    store.update_progress(
                        conn,
                        run_id,
                        progress=float(val),
                        progress_label=str(payload.get("label", "")),
                    )
        except Exception:  # noqa: BLE001 — a tape write must not abort the run
            logger.exception("agent_run_events append failed (run=%s)", run_id)

    events_mod.publish(
        "agent_run", "stage_start", {"run_id": run_id, "kind": run.kind, "trigger": run.trigger}
    )
    try:
        outcome = spec.run(cfg, _on_event, run.payload)
    except Exception as exc:  # noqa: BLE001
        with fts.cursor() as conn:
            store.fail_run(conn, run_id, error=str(exc))
        events_mod.publish(
            "agent_run", "stage_end", {"run_id": run_id, "status": "failed", "error": str(exc)}
        )
        logger.exception("run_recorded: kind=%s run=%s failed", run.kind, run_id)
        return

    terminal_payload = {
        "run_id": run_id,
        "status": "committed" if outcome.committed else "skipped",
        "summary": outcome.summary,
        "iterations": outcome.iterations,
    }
    with fts.cursor() as conn:
        store.end_run(
            conn,
            run_id,
            committed=outcome.committed,
            summary=outcome.summary,
            result_refs=outcome.result_refs,
            iterations=outcome.iterations,
            skipped_reason=outcome.skipped_reason,
        )
        store.append_event(conn, run_id, "stage_end", terminal_payload)
    events_mod.publish("agent_run", "stage_end", terminal_payload)


def enqueue_run(
    cfg: Config,
    *,
    kind: str,
    trigger: str,
    dispatch_source: str,
    title: str = "",
    payload: dict[str, Any] | None = None,
) -> tuple[int, bool]:
    """Enqueue a run (with payload-aware dedup) and wake the dispatcher.

    Returns ``(run_id, deduped)`` where ``deduped`` is True iff this call folded
    into an existing still-queued row of the same kind + identical payload
    (#396 — lets the API surface an "already queued" hint instead of silently
    reusing). A new row, or a same-kind row with a *different* payload (#397),
    yields ``deduped=False``. Title defaults to the kind's registry label."""
    from .dispatcher import wake  # lazy import avoids circular at module level

    spec = KIND_REGISTRY.get(kind)
    eff_title = title or (spec.title if spec else kind)
    with fts.cursor() as conn:
        # Determine dedup inside the same transaction as the insert so the
        # signal matches what enqueue() actually did.
        deduped = store.find_queued_dup(conn, kind=kind, payload=payload) is not None
        rid = store.enqueue(
            conn,
            kind=kind,
            trigger=trigger,
            dispatch_source=dispatch_source,
            title=eff_title,
            payload=payload,
        )
    wake()
    return rid, deduped
