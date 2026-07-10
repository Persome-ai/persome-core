"""Async daemon wiring for the session/reducer pipeline.

Three asyncio tasks live here:

  * ``run_check_cuts`` — calls ``SessionManager.check_cuts`` every
    ``session.tick_seconds`` so idle gaps / soft cuts fire even when
    the dispatcher is quiet.
  * ``run_daily_safety_net`` — once per local day at HH:MM (from
    ``reducer.daily_tick_hour/minute``), force-ends the currently open
    session, retries any ``failed`` sessions, and covers the edge case
    where the process was offline across midnight.
  * ``build_manager`` — factory that wires ``on_session_end`` to
    persist a ``sessions`` row and spawn the S2 reducer thread.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from .. import events as events_mod
from ..config import Config
from ..evomem import backup as evo_backup
from ..evomem import integrity as evo_integrity
from ..evomem import inversion as evo_inversion
from ..logger import get
from ..store import fts
from ..store import parser_ticks as parser_ticks_store
from ..writer import classifier as classifier_mod
from ..writer import (
    contradiction_check,
    memory_decay,
    orphan_reaper,
    session_reducer,
)
from ..writer import pattern_detector as pattern_detector_mod
from . import store as session_store
from .manager import SessionManager

logger = get("persome.session")


def _prune_telemetry_tables() -> dict[str, int]:
    """Bound the parser audit table from the daily safety-net tick."""
    with fts.cursor() as conn:
        return {"parser_ticks": parser_ticks_store.prune(conn)}


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
        """Terminal reducer succeeded → classify any window the 30-min tick missed."""
        if not result.written or not result.entry_id or not result.path:
            return
        if not result.is_final:
            # Incremental flushes are handled by run_classifier_tick on its
            # own cadence — the reducer callback only fires the terminal
            # catch-up for any trailing window the tick hadn't reached yet.
            return
        window_start: datetime | None = None
        if result.end_time is not None:
            with fts.cursor() as conn:
                row = session_store.get_by_id(conn, result.session_id)
                if row and row.classified_end:
                    window_start = row.classified_end
        try:
            events_mod.publish("classifier", "stage_start", {"session_id": result.session_id})
            classify = classifier_mod.classify_after_reduce(
                cfg,
                session_id=result.session_id,
                event_daily_path=result.path,
                just_written_entry_id=result.entry_id,
                session_start=result.start_time,
                session_end=result.end_time,
                window_start=window_start,
                on_event=events_mod.make_on_event("classifier"),
            )
            if classify.committed and classify.written_ids:
                logger.info(
                    "classifier %s: wrote %d entries into %s",
                    result.session_id,
                    len(classify.written_ids),
                    ", ".join(classify.created_paths) or "existing files",
                )
            elif classify.skipped_reason:
                logger.info(
                    "classifier %s: skipped (%s)", result.session_id, classify.skipped_reason
                )
            else:
                logger.info("classifier %s: committed with no writes", result.session_id)
            events_mod.publish(
                "classifier",
                "stage_end",
                {
                    "session_id": result.session_id,
                    "summary": classify.summary or "",
                    "written": len(classify.written_ids),
                },
            )
            if classify.committed and result.end_time is not None:
                with fts.cursor() as conn:
                    session_store.set_classified_end(
                        conn,
                        result.session_id,
                        result.end_time,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("classifier %s: crashed: %s", result.session_id, exc, exc_info=True)

        # Pattern detection runs after classifier, using the same session window.
        try:
            events_mod.publish("pattern_detector", "stage_start", {"session_id": result.session_id})
            detect = pattern_detector_mod.detect_after_classify(
                cfg,
                session_id=result.session_id,
                event_daily_path=result.path,
                session_start=result.start_time,
                session_end=result.end_time,
            )
            if detect.committed and detect.written_ids:
                logger.info(
                    "pattern_detector %s: wrote %d entries into %s",
                    result.session_id,
                    len(detect.written_ids),
                    ", ".join(detect.created_paths) or "existing files",
                )
            elif detect.skipped_reason:
                logger.info(
                    "pattern_detector %s: skipped (%s)",
                    result.session_id,
                    detect.skipped_reason,
                )
            else:
                logger.info(
                    "pattern_detector %s: committed with no writes",
                    result.session_id,
                )
            events_mod.publish(
                "pattern_detector",
                "stage_end",
                {
                    "session_id": result.session_id,
                    "written": len(detect.written_ids),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "pattern_detector %s: crashed: %s", result.session_id, exc, exc_info=True
            )

        # memory_delta consolidator (Memory-rebuild Phase 0, spec §4.1/§6.2):
        # one shadow LLM read of the whole ended session → structured delta into
        # the memory_deltas table. Gated on [memory_delta] enabled (default OFF);
        # best-effort — a delta failure never affects the chain around it.
        try:
            from ..writer import memory_delta as memory_delta_mod

            delta = memory_delta_mod.run_after_session(
                cfg,
                session_id=result.session_id,
                start_time=result.start_time,
                end_time=result.end_time,
            )
            if delta.written:
                logger.info(
                    "memory_delta %s: shadow row %d (%s)",
                    result.session_id,
                    delta.delta_id,
                    ", ".join(f"{h}={n}" for h, n in delta.counts.items()),
                )
            elif delta.skipped_reason not in ("", "disabled"):
                logger.info(
                    "memory_delta %s: skipped (%s)", result.session_id, delta.skipped_reason
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("memory_delta %s: crashed: %s", result.session_id, exc)

    return SessionManager(
        gap_minutes=cfg.session.gap_minutes,
        soft_cut_minutes=cfg.session.soft_cut_minutes,
        max_session_hours=cfg.session.max_session_hours,
        on_session_start=_on_start,
        on_session_end=_on_end,
    )


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
            await asyncio.to_thread(
                session_reducer.flush_active_session,
                cfg,
                session_id=session_id,
                session_start=session_start,
                now=datetime.now().astimezone(),
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

            events_mod.publish("classifier", "stage_start", {"session_id": session_id})
            result = await asyncio.to_thread(
                classifier_mod.classify_window,
                cfg,
                session_id=session_id,
                event_daily_path=event_daily_name,
                start=window_start,
                end=now,
                include_prior_day=window_start == session_start,
                on_event=events_mod.make_on_event("classifier"),
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
            events_mod.publish(
                "classifier",
                "stage_end",
                {
                    "session_id": session_id,
                    "summary": result.summary or "",
                    "written": len(result.written_ids),
                },
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
    """从 evo_nodes 重投影 entries/entry_metadata 检索层（reader↔重建保鲜，spec 2026-07-04）。
    直接调 ``entries._rebuild_from_evo_nodes``（rebuild-index 的 evomem 混合重建腿），不依赖
    write_authority——delta 的 add_direct 只写 evo_nodes，此函数把重建投进检索读的 entries。
    幂等（DELETE+重投）。返回 (files, entries)。"""
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
                await asyncio.to_thread(session_reducer.reduce_all_pending, cfg)
            # ② reader↔重建保鲜（apply_enabled=True，delta 铸点已上线，spec 2026-07-04 §reader-cutover）：
            # add_direct 只写 evo_nodes、不投影 entries（inversion 只投 choke-point 动词），classifier
            # 又已退役 → 检索读的 entries 会随新写陈旧。每日从 evo_nodes 全量重投影 entries/entry_metadata
            # （_rebuild_from_evo_nodes，已测的 rebuild-index 混合重建腿），让检索看到重建（≤1 天 lag）。
            # fail-open，永不杀 tick。
            if getattr(getattr(cfg, "memory_delta", None), "apply_enabled", False):
                try:
                    files, ents = await asyncio.to_thread(_reproject_entries_from_evomem)
                    logger.info("daily 检索投影 evo_nodes→entries: %d 文件 / %d 条目", files, ents)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("daily 检索投影重建失败: %s", exc)
            # Semantic-contradiction self-check (memory-rebuild spec §4.4,
            # gated OFF by default — nightly LLM cost): pair same-file live
            # facts, LLM-judge a bounded batch, MARK contradictions
            # (entry_metadata.conflicted → recall's ⚠(冲突未裁决) + the
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
            # (细节链→粗摘要→一行事实) via the existing choke-point verbs.
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
            # §1.5-2 图侧孤儿收敛：delta apply 过度生产的一次性点（长不出实质边）到期遗忘。
            # delta apply 的收敛腿——side channel，绝不杀 tick。
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
            # Retention for the per-call telemetry tables (#508): they record a
            # row on every recall/recognition/parser tick and define a bounded
            # `prune` that previously had no caller. Run it once per day so the
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
            # means this tick behaves exactly as before. Failures alert via
            # the integrity_alert SSE event; neither ever kills the tick.
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


def _run_evomem_enrichment_once(cfg: Config) -> None:
    """One enrichment pass: person-graph ingest (#1) + case extraction (#2).

    Both layers gate INTERNALLY on their own flags and no-op when off, so this is
    safe to call whenever the tick fires. Person-graph ingest is deterministic (no
    LLM); case extraction makes one LLM pass over the last 24h of timeline blocks.
    Extracted so the wiring is unit-testable without driving the daily loop. Each
    layer is isolated in its own try so one failing never blocks the other.
    """
    from ..evomem.engine import EvoMemory
    from ..evomem.person_graph import PersonGraph
    from ..model.entity_source import MemoryPersonNameSource
    from ..writer import case_extractor

    if getattr(cfg, "person_graph_enabled", False):
        try:
            touched = PersonGraph(
                EvoMemory(), cfg=cfg, name_source=MemoryPersonNameSource()
            ).ingest()
            logger.info("evomem enrichment: person graph ingested %d update(s)", len(touched))
        except Exception as exc:  # noqa: BLE001 — best-effort enrichment, never crash the tick
            logger.error("evomem enrichment: person graph failed: %s", exc, exc_info=True)

    if getattr(cfg, "case_extraction_enabled", False):
        try:
            result = case_extractor.run_case_extraction(cfg)
            logger.info(
                "evomem enrichment: case extraction wrote %d card(s)",
                getattr(result, "written_count", 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("evomem enrichment: case extraction failed: %s", exc, exc_info=True)

    # Graph-memory P0-2 (#428): relation-edge extraction → SHADOW. Gates internally on
    # relation_extraction_enabled (default off) + fully fail-open, like the two layers above.
    if getattr(cfg, "relation_extraction_enabled", False):
        try:
            from ..evomem import relation_extractor

            rel = relation_extractor.run_relation_extraction(cfg)
            logger.info(
                "evomem enrichment: relation extraction wrote %d shadow edge(s) (det=%d llm=%d)",
                rel.written_count,
                rel.deterministic_count,
                rel.llm_count,
            )
            # 边转正判据 (memory-rebuild §7-3, designed WITH the RRF pool weights):
            # evidence floor + per-identity fan-out cap — promotion volume IS
            # relation-pool dilution volume, so only each identity's strongest
            # edges spread activation. Idempotent nightly; same flag, own risk
            # profile is covered by relation_pool_weight ≤ 0.3 (sweep-verified
            # zero regression). Runs in the same try — a promotion failure logs
            # with the extraction leg, never crashes the tick.
            from ..store import fts as fts_store
            from ..store import relation_edges as edges_store

            with fts_store.cursor() as conn:
                n_promoted = edges_store.promote_edges(
                    conn, max_per_identity=int(getattr(cfg, "edge_promote_fanout", 20))
                )
            if n_promoted:
                logger.info("evomem enrichment: %d relation edge(s) promoted to ACTIVE", n_promoted)
        except Exception as exc:  # noqa: BLE001 — best-effort enrichment, never crash the tick
            logger.error("evomem enrichment: relation extraction failed: %s", exc, exc_info=True)
