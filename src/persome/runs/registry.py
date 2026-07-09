"""Kind registry: maps an agent-run ``kind`` to an executor that does the work.

An executor has signature ``(cfg, on_event, payload) -> RunOutcome``. It MUST
call ``on_event(type, payload)`` for progress/stages it wants taped (the
recorder turns those into SSE + agent_run_events). It returns a RunOutcome the
recorder uses to write the terminal row state. Executors never touch the
agent_runs table directly — the recorder owns all bookkeeping.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .. import paths
from ..config import Config
from ..store import fts
from ..writer import llm as llm_mod


@dataclass
class RunOutcome:
    committed: bool = False
    summary: str = ""
    result_refs: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    skipped_reason: str = ""


ExecutorFn = Callable[[Config, llm_mod.OnEventFn, dict[str, Any]], RunOutcome]


@dataclass
class KindSpec:
    kind: str
    title: str  # default human label for new rows of this kind
    run: ExecutorFn


KIND_REGISTRY: dict[str, KindSpec] = {}


def register_kind(spec: KindSpec) -> None:
    KIND_REGISTRY[spec.kind] = spec


# ── dream executor ──────────────────────────────────────────────────────────


def _dream_executor(
    cfg: Config, on_event: llm_mod.OnEventFn, payload: dict[str, Any]
) -> RunOutcome:
    """Run the dream stage + book sub-steps, reporting progress via on_event.
    Mirrors the body of the old run_reserved_dream minus the audit/lock
    bookkeeping (the recorder now owns that)."""
    import contextlib
    from datetime import datetime

    from ..writer import book_chapters, book_generate, book_page
    from ..writer import dream as dream_mod

    result = dream_mod.run_dream(cfg, on_event=on_event)

    # Book sub-steps reuse the same event stream; fully fault-tolerant — each
    # step is independently suppressed so a page failure never skips chapters.
    #
    # Concurrency (#354): these mutate the same files/tables as the manual
    # POST /book/generate route, so both serialize on the shared
    # book_generate_lock. This is an internal background task — acquire
    # *blocking* so we serialize behind any in-flight manual generation rather
    # than dropping the day's pages (the API side fails fast with 409 instead).
    # The guard itself is suppressed too, so lock bookkeeping can never flip the
    # dream's status.
    with contextlib.suppress(Exception), book_generate.book_generate_guard(blocking=True):
        target_date = datetime.now().astimezone().strftime("%Y-%m-%d")
        with contextlib.suppress(Exception):
            book_page.run_book_pages(target_date, on_event=on_event)
        with contextlib.suppress(Exception):
            book_chapters.run_book_chapters(on_event=on_event)

    refs = [{"type": "memory", "path": p} for p in result.created_paths]
    return RunOutcome(
        committed=result.committed,
        summary=result.summary,
        result_refs=refs,
        iterations=result.iterations,
        skipped_reason=result.skipped_reason,
    )


# ── bootstrap executor ──────────────────────────────────────────────────────


def _bootstrap_executor(
    cfg: Config, on_event: llm_mod.OnEventFn, payload: dict[str, Any]
) -> RunOutcome:
    """Run the cold-start profiler. run_headless publishes its OWN 'bootstrap'
    SSE events internally (it takes no on_event), so the recorder's tape will
    only carry the lifecycle stage_start/stage_end for this kind — fine, the HUD
    shows bootstrap detail via its own channel."""
    from ..bootstrap import runner as bootstrap_runner

    deep = bool(payload.get("deep", True))
    exclude = frozenset(payload.get("exclude", []))
    n = bootstrap_runner.run_headless(cfg, deep=deep, exclude=exclude)
    return RunOutcome(committed=True, summary=f"bootstrap wrote {n} files", iterations=0)


register_kind(KindSpec(kind="dream", title="每日整理", run=_dream_executor))
register_kind(KindSpec(kind="bootstrap", title="冷启动画像", run=_bootstrap_executor))


# ── evomem-compact-repair executor ──────────────────────────────────────────


def _evomem_compact_repair_executor(
    cfg: Config, on_event: llm_mod.OnEventFn, payload: dict[str, Any]
) -> RunOutcome:
    """compact 后 evo_nodes 自修（issue #526）：compact 整文件重写绕过三条主写路、
    给条目换 id，新 id 不入 evo_nodes → 切主读后折叠 recall（``recall_fold_superseded``）
    直接丢这些记忆。旧行为只靠 ``note_out_of_band_rewrite`` 记 alert-only miss、等人
    工跑 CLI；这里把自修挂进 agent_runs 调度，由 ``compact.run_pending`` accept 后自动
    enqueue，「通知人修」变「daemon 自修」。

    用 ``restore.import_from_markdown``（DELETE+全量 INSERT）而非 ``run_backfill``
    （upsert-only）：compact 换 id 后旧 id 节点在 evo_nodes 里变成**孤儿 head**
    （is_latest=1 但 entries/markdown 已无此 id）。upsert-only 的 backfill 留下孤儿，
    收尾对账永远 ok=False（``projection_reconciliation`` violation）且孤儿逐次累积；
    而 compact 重写的整文件 markdown 是 SSOT，从它整库替换重建 evo_nodes 才能精确
    收敛——孤儿被 DELETE 清掉，链头与 markdown 一致，收尾自检零 violation。

    幂等、无 LLM、无网络：等价于 ``persome evomem-restore-from-markdown``
    （含 §3.2 变更前快照 + §3.3 全套自检）。restore 的「四条有损限制」对本场景影响
    可忽略：compact 刚写的 markdown 无投影滞后窗口，分钟级时间精度对折叠 recall
    无影响。violation 非空 → skipped（自检有残差，幂等重跑续收敛），非 failed。"""
    from ..evomem import restore as restore_mod

    on_event("progress", {"value": 0.1, "label": "evomem 自修（restore-from-markdown）"})
    report = restore_mod.import_from_markdown()
    summary = (
        f"restore {report.files} files → {report.nodes} nodes,"
        f" projection {report.projection_entries} entries, ok={report.ok}"
    )
    on_event("progress", {"value": 1.0, "label": summary})
    return RunOutcome(
        committed=report.ok,
        summary=summary,
        skipped_reason="" if report.ok else "restore self-check has residual violations",
    )


register_kind(
    KindSpec(kind="evomem-compact-repair", title="记忆链自修", run=_evomem_compact_repair_executor)
)


# ── summarize-week executor ─────────────────────────────────────────────────


def _summarize_week_executor(
    cfg: Config, on_event: llm_mod.OnEventFn, payload: dict[str, Any]
) -> RunOutcome:
    """Read the past 7 days of memory entries, generate a markdown weekly
    summary via one LLM call, persist it to memory/, and return result_refs."""
    from datetime import datetime, timedelta

    on_event("progress", {"value": 0.1, "label": "收集本周记忆"})

    # Entry timestamps are stored as *local* wall clock at *minute* precision and
    # no offset (`entries._now_iso_minute` → "%Y-%m-%dT%H:%M"); `fts.recent`
    # compares them as plain strings. So the "past 7 days" lower bound must use
    # the exact same local format + precision — a UTC threshold would shift the
    # window by the local offset (e.g. +08:00 over-collects ~8h), and the old
    # second-precision string (19 chars vs the stored 16) lexicographically
    # outranked a boundary-minute entry, silently dropping it (#385).
    since_dt = datetime.now().astimezone() - timedelta(days=7)
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M")

    with fts.cursor() as conn:
        entries = fts.recent(conn, since=since_iso, limit=80)

    if not entries:
        return RunOutcome(
            committed=False,
            summary="本周暂无可总结的记忆",
            skipped_reason="no entries",
        )

    on_event("progress", {"value": 0.5, "label": "生成周报"})

    # Build bullet list from entry content (truncate long entries)
    bullets: list[str] = []
    for e in entries[:60]:
        snippet = (e.content or "").strip().replace("\n", " ")[:200]
        if snippet:
            bullets.append(f"- {snippet}")

    bullet_text = "\n".join(bullets)
    prompt = (
        "你是用户的私人助理，帮助回顾过去一周的工作与生活。\n\n"
        "下面是过去 7 天的记忆条目摘录（每条一行）：\n\n"
        f"{bullet_text}\n\n"
        "请根据上面的内容，写一篇简洁的中文周报，要求：\n"
        "1. 用 Markdown 格式，包含「本周亮点」「主要工作」「值得关注」三个小节\n"
        "2. 语言简练，每节不超过 5 条要点\n"
        "3. 不要重复原始条目，要有归纳与提炼\n"
        "4. 开头用一句话概括整体氛围或状态"
    )

    resp = llm_mod.call_llm(cfg, "reducer", messages=[{"role": "user", "content": prompt}])
    digest = llm_mod.extract_text(resp).strip()

    on_event("progress", {"value": 0.9, "label": "写入 memory"})

    # Use local time for ISO week label (e.g. "2026-W23")
    local_now = datetime.now().astimezone()
    week_label = local_now.strftime("%G-W%V")
    mem_dir = paths.memory_dir()
    mem_dir.mkdir(parents=True, exist_ok=True)
    out_path = mem_dir / f"digest-{week_label}.md"
    out_path.write_text(f"# 本周周报 {week_label}\n\n{digest}\n", encoding="utf-8")

    first_line = (digest.splitlines()[0] if digest else "周报已生成")[:160]
    return RunOutcome(
        committed=True,
        summary=first_line,
        result_refs=[{"type": "memory", "path": out_path.name}],
    )


register_kind(KindSpec(kind="summarize-week", title="本周周报", run=_summarize_week_executor))


# ── case-extraction executor (慢回路 E2：问题→解法卡) ─────────────────────────


def _case_extraction_executor(
    cfg: Config, on_event: llm_mod.OnEventFn, payload: dict[str, Any]
) -> RunOutcome:
    """Distill reusable ``error → resolution`` cards from the recent timeline.

    Deterministic pre-filter圈出 error→resolution 候选 → 每候选一次 LLM 蒸成
    ``{problem, solution}`` → 经 evomem 公共写入口落 L5_KNOWLEDGE。Off by
    default (``cfg.case_extraction_enabled``); when disabled it is a no-op
    skipped run. See ``writer/case_extractor.py``."""
    from ..writer import case_extractor

    result = case_extractor.run_case_extraction(cfg, on_event=on_event)
    refs = [{"type": "memory", "id": nid} for nid in result.created_ids]
    return RunOutcome(
        committed=result.committed,
        summary=result.summary,
        result_refs=refs,
        skipped_reason=result.skipped_reason,
    )


register_kind(KindSpec(kind="case-extraction", title="问题解法卡", run=_case_extraction_executor))
