"""Async daemon wiring for the session/reducer pipeline.

The daemon tasks wired here include:

  * ``run_check_cuts`` — calls ``SessionManager.check_cuts`` every
    ``session.tick_seconds`` so idle gaps / soft cuts fire even when
    the dispatcher is quiet.
  * ``run_daily_safety_net`` — once per local day at HH:MM (from
    ``reducer.daily_tick_hour/minute``), force-ends the currently open
    session, retries any ``failed`` sessions, and covers the edge case
    where the process was offline across midnight.
  * ``run_reducer_retry_tick`` — retries due failed reductions once per minute
    and sends terminal success or heuristic fallback through model finalization.
  * ``build_manager`` — factory that wires ``on_session_end`` to
    persist a ``sessions`` row and spawn the S2 reducer thread.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from ..config import Config
from ..evomem import backup as evo_backup
from ..evomem import integrity as evo_integrity
from ..evomem import inversion as evo_inversion
from ..logger import get
from ..store import fts
from ..store import parser_ticks as parser_ticks_store
from ..writer import agent as writer_agent
from ..writer import classifier as classifier_mod
from ..writer import (
    contradiction_check,
    memory_decay,
    orphan_reaper,
    session_reducer,
)
from . import store as session_store
from .manager import SessionManager

logger = get("persome.session")


def _prune_telemetry_tables() -> dict[str, int]:
    """Bound the parser audit table from the daily safety-net tick."""
    with fts.cursor() as conn:
        return {"parser_ticks": parser_ticks_store.prune(conn)}


def recover_stranded_sessions(*, now: datetime | None = None) -> list[session_store.SessionRow]:
    """Close active rows owned by a daemon process that is no longer running."""
    recovered_at = now or datetime.now().astimezone()
    with fts.cursor() as conn:
        recovered = session_store.recover_active(conn, recovered_at=recovered_at)
    if recovered:
        logger.warning(
            "boot recovery: closed %d stranded active session(s): %s",
            len(recovered),
            ", ".join(row.id for row in recovered),
        )
    return recovered


def build_manager(cfg: Config) -> SessionManager:
    """Construct a SessionManager whose end-callback wires the reducer."""

    def _on_start(session_id: str, start: datetime) -> None:
        """Persist an 'active' row immediately so crashes are recoverable."""
        with fts.cursor() as conn:
            session_store.insert(
                conn,
                session_store.SessionRow(
                    id=session_id,
                    start_time=start,
                    status="active",
                ),
            )

    def _on_end(session_id: str, start: datetime, end: datetime) -> None:
        with fts.cursor() as conn:
            existing = session_store.get_by_id(conn, session_id)
            if existing is None:
                session_store.insert(
                    conn,
                    session_store.SessionRow(
                        id=session_id,
                        start_time=start,
                        end_time=end,
                        status="ended",
                    ),
                )
            else:
                session_store.mark_ended(conn, session_id, end)

        if not cfg.reducer.enabled:
            logger.info("reducer disabled — session %s stored without reduce", session_id)
            return

        session_reducer.reduce_session_async(
            cfg,
            session_id=session_id,
            start_time=start,
            end_time=end,
            on_done=_after_reduce,
        )

    def _after_reduce(result: session_reducer.ReduceResult) -> None:
        """Terminal reducer completion -> run the shared model finalizer."""
        if not result.is_final:
            return
        modeled = writer_agent.finalize_session(
            cfg,
            session_id=result.session_id,
            event_daily_path=result.path,
            just_written_entry_id=result.entry_id,
        )
        if modeled.completed:
            logger.info("session %s: terminal model stages complete", result.session_id)
        else:
            logger.warning(
                "session %s: terminal model stages incomplete (%s)",
                result.session_id,
                "; ".join(modeled.errors) or modeled.skipped_reason,
            )

    return SessionManager(
        gap_minutes=cfg.session.gap_minutes,
        soft_cut_minutes=cfg.session.soft_cut_minutes,
        max_session_hours=cfg.session.max_session_hours,
        on_session_start=_on_start,
        on_session_end=_on_end,
    )


async def run_reducer_retry_tick(cfg: Config) -> None:
    """Catch up persisted work at boot, then retry due terminal reductions."""
    if not cfg.reducer.enabled:
        logger.info("reducer retry loop not started (reducer disabled)")
        return
    interval = 60
    logger.info("reducer retry loop started (every %ds)", interval)
    try:
        startup = await asyncio.to_thread(writer_agent.run, cfg)
        if startup.reduced or startup.modeled:
            logger.info(
                "boot recovery: reduced %d session(s), modeled %d session(s)",
                startup.reduced,
                startup.modeled,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("boot writer recovery failed: %s", exc, exc_info=True)
    while True:
        try:
            await asyncio.sleep(interval)
            results = await asyncio.to_thread(session_reducer.retry_due, cfg)
            for reduced in results:
                modeled = await asyncio.to_thread(
                    writer_agent.finalize_session,
                    cfg,
                    session_id=reduced.session_id,
                    event_daily_path=reduced.path,
                    just_written_entry_id=reduced.entry_id,
                )
                if modeled.completed:
                    logger.info(
                        "session %s: retry completed terminal model stages",
                        reduced.session_id,
                    )
                elif modeled.skipped_reason != "session not ready for modeling":
                    logger.warning(
                        "session %s: retry left model stages incomplete (%s)",
                        reduced.session_id,
                        "; ".join(modeled.errors) or modeled.skipped_reason,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("reducer retry tick failed: %s", exc, exc_info=True)


async def run_check_cuts(cfg: Config, manager: SessionManager) -> None:
    """Periodic check_cuts tick."""
    interval = max(5, int(cfg.session.tick_seconds))
    logger.info("session check_cuts loop started (every %ds)", interval)
    while True:
        try:
            await asyncio.to_thread(manager.check_cuts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("session check_cuts failed: %s", exc, exc_info=True)
        await asyncio.sleep(interval)


async def run_flush_tick(cfg: Config, manager: SessionManager) -> None:
    """Incremental reducer tick for the active session.

    Every ``session.flush_minutes`` (min 5) checks for an active session and
    reduces any closed timeline blocks since the last flush into a partial
    entry in the event-daily file. Classifier is not fired here — it only
    runs on the terminal reduce at session end.
    """
    if not cfg.reducer.enabled:
        logger.info("flush tick loop not started (reducer disabled)")
        return
    interval = max(300, int(cfg.session.flush_minutes) * 60)
    logger.info("session flush loop started (every %ds)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            snap = manager.current_snapshot()
            if snap is None:
                continue
            session_id, session_start = snap
            flushed = await asyncio.to_thread(
                session_reducer.flush_active_session,
                cfg,
                session_id=session_id,
                session_start=session_start,
                now=datetime.now().astimezone(),
            )
            if flushed is not None:
                modeled = await asyncio.to_thread(
                    writer_agent.model_active_session,
                    cfg,
                    session_id=session_id,
                )
                if modeled.completed:
                    logger.info("session %s: active Point/Line window modeled", session_id)
                elif modeled.errors:
                    logger.warning(
                        "session %s: active modeling incomplete (%s)",
                        session_id,
                        "; ".join(modeled.errors),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("session flush tick failed: %s", exc, exc_info=True)


async def run_classifier_tick(cfg: Config, manager: SessionManager) -> None:
    """Periodic durable-fact classification for the active session.

    Every ``classifier.interval_minutes`` (min 5) checks for an active
    session and classifies any event-daily entries tagged with the
    session that have landed since the last classifier pass. The
    terminal reduce runs its own catch-up for the trailing window, so
    this tick is a pure incremental step — no effect at session end.
    """
    if not cfg.reducer.enabled:
        logger.info("classifier tick loop not started (reducer disabled)")
        return
    interval = max(300, int(cfg.classifier.interval_minutes) * 60)
    logger.info("classifier tick loop started (every %ds)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            snap = manager.current_snapshot()
            if snap is None:
                continue
            session_id, session_start = snap
            now = datetime.now().astimezone()
            event_daily_name = f"event-{session_start.strftime('%Y-%m-%d')}.md"

            window_start = session_start
            with fts.cursor() as conn:
                row = session_store.get_by_id(conn, session_id)
                if row and row.classified_end:
                    window_start = row.classified_end

            if now - window_start < timedelta(seconds=interval):
                continue

            result = await asyncio.to_thread(
                classifier_mod.classify_window,
                cfg,
                session_id=session_id,
                event_daily_path=event_daily_name,
                start=window_start,
                end=now,
                include_prior_day=window_start == session_start,
            )

            if result.committed and result.written_ids:
                logger.info(
                    "classifier tick %s: wrote %d entries into %s",
                    session_id,
                    len(result.written_ids),
                    ", ".join(result.created_paths) or "existing files",
                )
            elif result.skipped_reason:
                logger.info(
                    "classifier tick %s: skipped (%s)",
                    session_id,
                    result.skipped_reason,
                )
            else:
                logger.info(
                    "classifier tick %s: committed with no writes",
                    session_id,
                )
            if result.committed or result.skipped_reason:
                with fts.cursor() as conn:
                    session_store.set_classified_end(conn, session_id, now)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("classifier tick failed: %s", exc, exc_info=True)


async def run_vector_embed_tick(cfg: Config) -> None:
    """Drain the dense-retrieval embed queue (Phase 1 of the production hybrid-retrieval spec).

    Every 60s, when ``[search] hybrid_enabled`` is on, embed up to ``embed_tick_max``
    pending entries (te3-large via the relay) into ``entry_vectors``. Off the capture
    path, batched, fail-open: a failed batch leaves its entries queued for the next tick
    and they stay BM25-only meanwhile. No-op when hybrid is disabled.
    """
    if not cfg.search.hybrid_enabled:
        logger.info("vector-embed tick loop not started (hybrid disabled)")
        return
    interval = 60
    logger.info("vector-embed tick loop started (every %ds)", interval)
    from .. import vectors_tick

    while True:
        try:
            await asyncio.sleep(interval)
            embedded, queued = await asyncio.to_thread(vectors_tick.run_embed_once, cfg)
            if embedded:
                logger.info("vector-embed tick: +%d vectors (%d still queued)", embedded, queued)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("vector-embed tick failed: %s", exc, exc_info=True)


def _seconds_until_next_local(hour: int, minute: int) -> float:
    """Seconds from now until the next local-time HH:MM."""
    now = datetime.now().astimezone()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


def _reproject_entries_from_evomem() -> tuple[int, int]:
    from ..store import entries as entries_mod

    with fts.cursor() as conn:
        return entries_mod._rebuild_from_evo_nodes(conn)  # noqa: SLF001


async def run_daily_safety_net(cfg: Config, manager: SessionManager) -> None:
    """Once per local day at HH:MM, force-end open session + retry failed."""
    hour = cfg.reducer.daily_tick_hour
    minute = cfg.reducer.daily_tick_minute
    logger.info("daily safety-net loop started (fires at %02d:%02d local)", hour, minute)
    while True:
        try:
            wait = _seconds_until_next_local(hour, minute)
            await asyncio.sleep(wait)
            logger.info("daily safety-net tick: force-ending open session + reducing pending rows")
            await asyncio.to_thread(manager.force_end, reason="daily-safety-net")
            if cfg.reducer.enabled:
                # Give the just-force-ended session's async reducer thread a
                # chance to finish before the catch-up pass would re-process it.
                await asyncio.sleep(2)
                await asyncio.to_thread(writer_agent.run, cfg)

            if getattr(getattr(cfg, "memory_delta", None), "apply_enabled", False):
                try:
                    files, ents = await asyncio.to_thread(_reproject_entries_from_evomem)
                    logger.info(
                        "daily retrieval projection evo_nodes->entries: %d files / %d entries",
                        files,
                        ents,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("daily retrieval projection rebuild failed: %s", exc)
            # Semantic-contradiction self-check (memory-rebuild spec §4.4,
            # gated OFF by default — nightly LLM cost): pair same-file live
            # facts, LLM-judge a bounded batch, MARK contradictions

            # memory_contradictions adjudication queue). Never auto-supersedes.
            # Side channel — never kills the tick.
            if cfg.evomem.contradiction_check_enabled:
                try:

                    def _run_contradictions() -> contradiction_check.ContradictionRunResult:
                        with fts.cursor() as conn:
                            return contradiction_check.run_contradiction_check(cfg, conn)

                    cres = await asyncio.to_thread(_run_contradictions)
                    logger.info(
                        "daily contradiction check: %d candidate(s), %d judged, %d flagged",
                        cres.candidates,
                        cres.judged,
                        cres.flagged,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("daily contradiction check failed: %s", exc)
            # Text-axis graded forgetting (memory-rebuild §1.5-5, spec
            # 2026-07-03-text-axis-graded-forgetting-design.md; gated OFF by
            # default — lossy transform + nightly LLM cost): distill old,
            # never-retrieved durable fact clusters into coarser summaries

            # Side channel — never kills the tick.
            if cfg.memory_decay.enabled:
                try:

                    def _run_decay() -> memory_decay.DecayRunResult:
                        with fts.cursor() as conn:
                            return memory_decay.run_memory_decay(cfg, conn)

                    dres = await asyncio.to_thread(_run_decay)
                    logger.info(
                        "daily memory decay: %d cluster(s) considered, %d decayed"
                        " (%d entries retired), gated=%s",
                        dres.clusters_considered,
                        dres.clusters_decayed,
                        dres.entries_retired,
                        dres.gated or "-",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("daily memory decay failed: %s", exc)

            if getattr(getattr(cfg, "orphan_reaper", None), "enabled", False):
                try:

                    def _run_reap() -> orphan_reaper.ReapResult:
                        with fts.cursor() as conn:
                            return orphan_reaper.run_orphan_reap(cfg, conn)

                    rres = await asyncio.to_thread(_run_reap)
                    logger.info(
                        "daily orphan reap: %d candidate(s), %d forgotten",
                        rres.candidates,
                        rres.reaped,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("daily orphan reap failed: %s", exc)
            # Truncate the WAL sidecar after the heavy daily writes settle —
            # auto-checkpoint resets the WAL pointer but never shrinks the
            # file, so without this the sidecar drifts unbounded. It also
            # guarantees the snapshot below reads a fresh main DB (evomem
            # SSOT switch design §3.2: checkpoint BEFORE snapshot).
            try:
                busy, log_pages, ckpt_pages = await asyncio.to_thread(fts.checkpoint)
                logger.info(
                    "daily wal_checkpoint(TRUNCATE): busy=%d log=%d checkpointed=%d",
                    busy,
                    log_pages,
                    ckpt_pages,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("daily wal_checkpoint failed: %s", exc)
            # Retention for parser-tick telemetry (#508): the table defines a
            # bounded `prune` that previously had no caller. Run it once per day so the
            # advertised bound actually holds. Pure side channel — failures
            # alert in the log and never kill the tick.
            try:
                pruned = await asyncio.to_thread(_prune_telemetry_tables)
                if any(pruned.values()):
                    logger.info("daily telemetry prune: %s", pruned)
            except Exception as exc:  # noqa: BLE001
                logger.warning("daily telemetry prune failed: %s", exc)
            # evomem survivability base (design §3.2/§3.3, PR-1): daily verified
            # VACUUM INTO snapshot + retention, then the chain-invariant
            # self-check on the live DB. Both are side channels — config off
            # means this tick behaves exactly as before. Failures emit
            # structured error logs; neither ever kills the tick.
            if cfg.evomem.snapshot_enabled:
                try:
                    await asyncio.to_thread(evo_backup.run_daily_backup, cfg)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("daily evomem snapshot failed: %s", exc)
            if cfg.evomem.integrity_check_enabled:
                try:
                    await asyncio.to_thread(
                        evo_integrity.check_and_handle,
                        source="daily-tick",
                        freeze_on_failure=cfg.evomem.freeze_writes_on_failure,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("daily evomem integrity check failed: %s", exc)
            # Manual-edit detection (SSOT switch PR-6b, Q1(b)): while markdown
            # is a projection (write_authority="evomem"), compare each projected
            # file's content hash against projection_state; mismatch alerts
            # (check=manual_edit_detected, alert-only) and points the human at
            # `persome evomem-import-markdown` — never auto-reimports.
            # Pure no-op under the default "markdown" authority.
            try:
                await asyncio.to_thread(evo_inversion.run_daily_manual_edit_check)
            except Exception as exc:  # noqa: BLE001
                logger.warning("daily manual-edit check failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("daily safety-net failed: %s", exc, exc_info=True)
            # Sleep a minute so a tight error loop doesn't hammer the CPU.
            await asyncio.sleep(60)


async def run_schema_tick(cfg: Config) -> None:
    """Once per local day, run the shared personal-model build service."""
    if not cfg.schema.enabled:
        logger.info("schema tick loop not started (disabled)")
        return
    hour = cfg.schema.daily_tick_hour
    minute = cfg.schema.daily_tick_minute
    logger.info("schema tick loop started (fires at %02d:%02d local)", hour, minute)
    while True:
        try:
            wait = _seconds_until_next_local(hour, minute)
            await asyncio.sleep(wait)
            logger.info("schema tick: running shared personal-model build")
            from ..model import ModelBuildBusy, run_model_build

            try:
                result = await asyncio.to_thread(
                    run_model_build,
                    cfg,
                    wait_seconds=0.0,
                    trigger="daemon-schema-tick",
                )
            except ModelBuildBusy:
                logger.info("schema tick: model build already in progress; skipping")
                continue
            logger.info(
                "schema tick: model build %s (points=%d lines=%d faces=%d volumes=%d roots=%d)",
                result.status,
                result.stats["points"],
                result.stats["evolution_lines"] + result.stats["relation_lines"],
                result.stats["faces"],
                result.stats["volumes"],
                result.stats["roots"],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("schema tick failed: %s", exc, exc_info=True)
            await asyncio.sleep(60)


async def run_model_refresh_tick(cfg: Config) -> None:
    """Refresh Face/Volume/Root after new Point/Line evidence, at a bounded cadence."""
    if not cfg.schema.enabled:
        logger.info("model refresh loop not started (schema disabled)")
        return
    interval = max(300, int(cfg.schema.refresh_minutes) * 60)
    logger.info("model refresh loop started (every %ds when structure is dirty)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            with fts.cursor() as conn:
                dirty = session_store.get_system_state(
                    conn,
                    "model_structure_dirty",
                    default="0",
                )
            if dirty == "0":
                continue

            from ..model import ModelBuildBusy, run_model_build

            try:
                result = await asyncio.to_thread(
                    run_model_build,
                    cfg,
                    wait_seconds=0.0,
                    trigger="daemon-model-refresh",
                )
            except ModelBuildBusy:
                logger.info("model refresh: build already in progress; keeping dirty state")
                continue

            failed = any(stage.get("status") == "failed" for stage in result.stages.values())
            cleared = False
            if not failed:
                with fts.cursor() as conn:
                    cleared = session_store.compare_and_set_system_state(
                        conn,
                        "model_structure_dirty",
                        expected=dirty,
                        value="0",
                    )
                if not cleared:
                    logger.info("model refresh: newer evidence arrived; keeping dirty state")
            logger.info(
                "model refresh: %s (points=%d lines=%d faces=%d volumes=%d roots=%d, dirty=%s)",
                result.status,
                result.stats["points"],
                result.stats["evolution_lines"] + result.stats["relation_lines"],
                result.stats["faces"],
                result.stats["volumes"],
                result.stats["roots"],
                "cleared" if cleared else "kept",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("model refresh failed: %s", exc, exc_info=True)
            await asyncio.sleep(60)


def _run_evomem_enrichment_once(
    cfg: Config,
    *,
    raise_on_error: bool = False,
) -> dict[str, object]:
    """One enrichment pass: person-graph ingest (#1) + case extraction (#2).

    Both layers gate INTERNALLY on their own flags and no-op when off, so this is
    safe to call whenever the tick fires. Person-graph ingest is deterministic (no
    LLM); case extraction makes one LLM pass over the last 24h of timeline blocks.
    Extracted so the wiring is unit-testable without driving the daily loop. Each
    layer is isolated in its own try so one failure never blocks the others.
    Scheduled callers stay fail-open; the model build passes ``raise_on_error``
    so its manifest cannot label a partially failed enrichment stage complete.
    """
    from ..evomem.engine import EvoMemory
    from ..evomem.person_graph import PersonGraph
    from ..model.entity_source import MemoryPersonNameSource
    from ..writer import case_extractor

    report: dict[str, object] = {"person_updates": 0, "case_cards": 0, "relation_edges": 0}
    errors: list[str] = []

    if getattr(cfg, "person_graph_enabled", False):
        try:
            touched = PersonGraph(
                EvoMemory(), cfg=cfg, name_source=MemoryPersonNameSource()
            ).ingest()
            report["person_updates"] = len(touched)
            logger.info("evomem enrichment: person graph ingested %d update(s)", len(touched))
        except Exception as exc:  # noqa: BLE001 — best-effort enrichment, never crash the tick
            logger.error("evomem enrichment: person graph failed: %s", exc, exc_info=True)
            errors.append(f"person_graph: {type(exc).__name__}: {exc}")

    if getattr(cfg, "case_extraction_enabled", False):
        try:
            result = case_extractor.run_case_extraction(cfg)
            report["case_cards"] = getattr(result, "written_count", 0)
            logger.info(
                "evomem enrichment: case extraction wrote %d card(s)",
                getattr(result, "written_count", 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("evomem enrichment: case extraction failed: %s", exc, exc_info=True)
            errors.append(f"case_extraction: {type(exc).__name__}: {exc}")

    # Graph-memory P0-2 (#428): relation-edge extraction → SHADOW. Gates internally on
    # relation_extraction_enabled (default off) + fully fail-open, like the two layers above.
    if getattr(cfg, "relation_extraction_enabled", False):
        try:
            from ..evomem import relation_extractor

            rel = relation_extractor.run_relation_extraction(cfg)
            report["relation_edges"] = rel.written_count
            logger.info(
                "evomem enrichment: relation extraction wrote %d shadow edge(s) (det=%d llm=%d)",
                rel.written_count,
                rel.deterministic_count,
                rel.llm_count,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort enrichment, never crash the tick
            logger.error("evomem enrichment: relation extraction failed: %s", exc, exc_info=True)
            errors.append(f"relation_extraction: {type(exc).__name__}: {exc}")

    # Edge promotion also consumes default memory-delta evidence, so it must not
    # live behind the optional legacy relation extractor flag. Deterministic
    # engaged_with floor edges are active at write time; repeated co-occurrence
    # knows edges promote here after the evidence floor and fan-out cap.
    try:
        from ..store import relation_edges as edges_store

        with fts.cursor() as conn:
            n_promoted = edges_store.promote_edges(
                conn,
                max_per_identity=int(getattr(cfg, "edge_promote_fanout", 20)),
            )
        report["relation_promoted"] = n_promoted
        if n_promoted:
            logger.info("evomem enrichment: %d relation edge(s) promoted to ACTIVE", n_promoted)
    except Exception as exc:  # noqa: BLE001
        logger.error("evomem enrichment: relation promotion failed: %s", exc, exc_info=True)
        errors.append(f"relation_promotion: {type(exc).__name__}: {exc}")

    report["errors"] = errors
    if errors and raise_on_error:
        raise RuntimeError("; ".join(errors))
    return report
