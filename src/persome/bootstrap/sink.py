"""Write the cold-start profile into the same Markdown memory the daemon uses.

Bootstrap is **day-0 的 classifier + schema seeder**: it produces the *same shape*
the steady-state pipeline consumes — **atomic facts**, one assertion per entry,
each tagged ``bootstrap`` so it is (a) distinguishable from capture-derived
memory, (b) re-runnable/idempotent, and (c) supersede-able by later real
observations. The literary portrait (headline/vibe/narrative) is a UI artifact
and **never enters memory** (it rides the ``stage_end`` SSE frame instead).

Three guarantees:

- **P0/P1 — atomic, not prose.** ``user-profile.md`` / ``user-preferences.md`` get
  one entry *per* identity/preference fact; entity files get one entry per fact.
  No 800-字 blob, no ``narrative`` ever written to ``*.md``.
- **P2 — re-runnable.** Before writing new facts we retire (strike + supersede)
  every *live* ``#bootstrap`` entry from a prior run, so ``persome bootstrap``
  is idempotent — re-running migrates the user instead of piling duplicates.
- **provenance** — every fact carries the ``bootstrap`` tag so the classifier's
  ``search_memory`` can find it and the contradiction strategy can supersede it.

写权反转（PR-6b，SSOT 切换设计 §1.3/§5）：``write_authority="evomem"`` 时本站点
的全部写（preset create / 原子事实 append——确定性 add_direct 形态 / P2 重跑的
``mark_entry_deleted`` 退役）经 ``store/entries.py`` 的 choke-point dispatch 走
evomem engine 落 evo_nodes，markdown 由投影器再生成；``retire_prior_bootstrap``
的 ``tags MATCH 'bootstrap'`` 查询读的 entries FTS 在反转模式由同一组派生 helper
同步维护。逐站输出等价由 ``tests/test_evomem/test_inversion_stations.py`` 钉死。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3

from ..logger import get
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts
from .collectors import CollectorResult
from .synthesizer import Profile

logger = get("persome.bootstrap")

_TAG = "bootstrap"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not s:  # all-non-ascii (e.g. Chinese topic) → stable short hash
        s = "x" + hashlib.blake2s(name.encode("utf-8"), digest_size=4).hexdigest()
    return s[:48]


def retire_prior_bootstrap(conn: sqlite3.Connection) -> int:
    """Retire every *live* ``#bootstrap`` entry so a re-run is idempotent (P2/D2).

    Finds all non-superseded entries tagged ``bootstrap`` and strikes them via
    :func:`entries.mark_entry_deleted` — a markdown-durable retire (the body is
    wrapped in ``~~...~~`` so ``rebuild_index`` re-derives ``superseded=1`` from
    the SSOT, not just the FTS column). Returns how many entries were retired.

    Idempotent: striking an already-struck body is a no-op, so calling this twice
    in a row retires nothing the second time. We collect ``(path, id)`` up front
    (a read), then retire (writes) — ``mark_entry_deleted`` re-reads each file, so
    interleaving is safe.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, path FROM entries WHERE superseded = 0 AND tags MATCH ?",
        (_TAG,),
    ).fetchall()
    targets = [(r["path"], r["id"]) for r in rows]

    retired = 0
    for path, entry_id in targets:
        if not files_mod.memory_path(path).exists():
            continue
        try:
            entries_mod.mark_entry_deleted(conn, name=path, entry_id=entry_id)
            retired += 1
        except (FileNotFoundError, ValueError):
            # Entry already gone (file deleted, or id not in markdown) — skip.
            continue
    if retired:
        logger.info("bootstrap retired %d prior #bootstrap entries", retired)
    return retired


def _append_facts(
    conn: sqlite3.Connection,
    *,
    name: str,
    facts: list[str],
    tags: list[str],
) -> int:
    """Append each atomic fact as its own entry. Returns the count written."""
    written = 0
    for fact in facts:
        body = fact.strip()
        if not body:
            continue
        entries_mod.append_entry(conn, name=name, content=body, tags=tags)
        written += 1
    return written


def _ensure_entity_file(
    conn: sqlite3.Connection, *, name: str, description: str, tags: list[str]
) -> None:
    if not files_mod.memory_path(name).exists():
        entries_mod.create_file(conn, name=name, description=description, tags=tags)


def write(
    profile: Profile | None,
    results: list[CollectorResult],
    *,
    fallback_text: str,
) -> list[str]:
    """Persist the profile to memory as atomic facts. Returns files written.

    Re-runnable (P2): retires every prior ``#bootstrap`` entry before writing the
    new facts, so a second ``persome bootstrap`` migrates rather than
    duplicates. The literary portrait is **not** written here — only atomic
    identity/preference/entity facts.
    """
    written: list[str] = []

    with fts.cursor() as conn:
        # Ensure the two canonical user files exist before appending.
        entries_mod.write_preset_files(conn)

        # P2/D2: clear the prior run's #bootstrap facts so we don't pile dupes.
        retire_prior_bootstrap(conn)

        if profile is None:
            # No LLM synthesis — still seed user-profile with the raw aggregate
            # so day-0 memory isn't empty.
            body = "**冷启动信号(未经 LLM 合成)**\n\n" + fallback_text
            entries_mod.append_entry(
                conn, name="user-profile.md", content=body, tags=[_TAG, "raw-signals"]
            )
            return ["user-profile.md"]

        # P0: identity/preference atomic facts — one assertion per entry, no prose.
        # narrative/headline/vibe/identity blob 不再进记忆（仅 UI）。
        if _append_facts(
            conn,
            name="user-profile.md",
            facts=profile.identity_facts,
            tags=[_TAG, "identity"],
        ):
            written.append("user-profile.md")

        if _append_facts(
            conn,
            name="user-preferences.md",
            facts=profile.preference_facts,
            tags=[_TAG, "preference"],
        ):
            written.append("user-preferences.md")

        # P1: entity files — one entry per atomic fact, so each file can clear the
        # schema miner's min_facts gate.
        entity_specs = [
            (profile.projects, "project", "{name} — 冷启动从本地代码项目推断"),
            (profile.tools, "tool", "{name} — 冷启动从本地环境推断的工具"),
            (profile.topics, "topic", "{name} — 冷启动推断的关注方向"),
        ]
        for rows, kind, desc_tmpl in entity_specs:
            for row in rows:
                facts = row.get("facts", [])
                if not any(f.strip() for f in facts):
                    # No atomic facts → skip (an empty entity file is pure noise).
                    continue
                name = f"{kind}-{_slug(row['name'])}.md"
                _ensure_entity_file(
                    conn,
                    name=name,
                    description=desc_tmpl.format(name=row["name"]),
                    tags=[_TAG, kind],
                )
                if _append_facts(conn, name=name, facts=facts, tags=[_TAG, kind]):
                    written.append(name)

    logger.info("bootstrap wrote %d memory files", len(written))
    return written
