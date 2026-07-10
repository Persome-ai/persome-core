"Memory-file compaction with canonical-state safeguards."

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from ..config import Config
from ..evomem import inversion as evo_inversion
from ..evomem import shadow as evo_shadow
from ..logger import get
from ..prompts import load as load_prompt
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts
from . import llm as llm_mod

logger = get("persome.compact")

_UNIQUE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{3,}")
_PRESERVATION_THRESHOLD = 0.95  # must keep ≥95% of unique tokens


@dataclass
class CompactResult:
    path: str
    accepted: bool
    before_tokens: int
    after_tokens: int
    before_unique: int
    after_unique: int
    preservation_ratio: float
    note: str = ""


def _unique_tokens(text: str) -> set[str]:
    return {t.lower() for t in _UNIQUE_TOKEN_RE.findall(text)}


def compact_file(cfg: Config, conn: sqlite3.Connection, *, name: str) -> CompactResult:
    path = files_mod.memory_path(name)
    if not path.exists():
        return CompactResult(name, False, 0, 0, 0, 0, 0.0, "file missing")

    if evo_inversion.evomem_active():
        original = path.read_text()
        unique = len(_unique_tokens(original))
        tokens = len(original) // 4
        logger.info("compact deferred (write_authority=evomem): %s", name)
        return CompactResult(
            name,
            False,
            tokens,
            tokens,
            unique,
            unique,
            1.0,
            "deferred: evomem authority requires compaction to emit canonical operations",
        )

    original = path.read_text()
    before_unique = _unique_tokens(original)
    before_tokens = len(original) // 4

    system = load_prompt("compact.md")
    user = (
        "Compress this file. Output the full new Markdown including frontmatter.\n\n"
        "```markdown\n" + original + "\n```"
    )

    try:
        resp = llm_mod.call_llm(
            cfg,
            "compact",
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("compact llm call failed: %s", exc)
        return CompactResult(
            name,
            False,
            before_tokens,
            before_tokens,
            len(before_unique),
            len(before_unique),
            1.0,
            f"llm error: {exc}",
        )

    new_text = llm_mod.extract_text(resp).strip()
    # Strip markdown code fences if the model wrapped the output
    new_text = _unwrap_code_fence(new_text)

    if not new_text.startswith("---"):
        return CompactResult(
            name,
            False,
            before_tokens,
            len(new_text) // 4,
            len(before_unique),
            0,
            0.0,
            "response missing frontmatter — rejected",
        )

    after_unique = _unique_tokens(new_text)
    preserved = len(before_unique & after_unique)
    ratio = preserved / len(before_unique) if before_unique else 1.0

    if ratio < _PRESERVATION_THRESHOLD:
        logger.warning(
            "compact rejected: %.1f%% preservation (need %.0f%%) — %s",
            ratio * 100,
            _PRESERVATION_THRESHOLD * 100,
            name,
        )
        return CompactResult(
            name,
            False,
            before_tokens,
            len(new_text) // 4,
            len(before_unique),
            len(after_unique),
            ratio,
            f"rejected: preservation {ratio:.1%} < {_PRESERVATION_THRESHOLD:.0%}",
        )

    # Accept (A1, P3): write the compacted markdown to disk and clear the
    # needs_compact frontmatter flag. The derived FTS indexes are
    # re-ingested via a single ``rebuild_index`` — but bug_012 moves that rebuild
    # OUT of here and into ``run_pending``, which rebuilds ONCE after all flagged
    # files are compacted (was O(K·N): K full rebuilds per tick). A1 correctness is
    # unchanged: the markdown is the SSOT and is fully written here, so a later
    # single ``rebuild_index`` still applies the EVO-02 three-way superseded
    # judgment — just batched. The
    # ``accepted=True`` flag below is the signal a caller uses to decide whether a
    # rebuild is owed. A standalone ``compact_file`` caller (not via run_pending)
    # must therefore call ``entries.rebuild_index`` itself after an accept.
    files_mod.atomic_write_text(path, new_text if new_text.endswith("\n") else new_text + "\n")
    files_mod.update_frontmatter(path, {"needs_compact": False})

    logger.info(
        "compact accepted: %s  %d→%d tokens (%.1f%% preservation)",
        name,
        before_tokens,
        len(new_text) // 4,
        ratio * 100,
    )
    return CompactResult(
        name,
        True,
        before_tokens,
        len(new_text) // 4,
        len(before_unique),
        len(after_unique),
        ratio,
    )


def _unwrap_code_fence(text: str) -> str:
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def run_pending(cfg: Config, conn: sqlite3.Connection) -> list[CompactResult]:
    pending = fts.files_needing_compact(conn)
    results: list[CompactResult] = []
    for name in pending:
        results.append(compact_file(cfg, conn, name=name))
    accepted = [r.path for r in results if r.accepted]
    if accepted:
        entries_mod.rebuild_index(conn)

        evo_shadow.note_out_of_band_rewrite(accepted)
        # Compact rewrites entry ids wholesale. Repair evo_nodes immediately from
        # the markdown SSOT so the runtime does not depend on a product run queue.
        _repair_evomem_after_compact()
    return results


def _repair_evomem_after_compact() -> None:
    """Synchronously restore evo_nodes from the rewritten markdown SSOT."""
    try:
        from ..evomem import restore as restore_mod

        report = restore_mod.import_from_markdown()
        logger.info(
            "compact evomem repair: files=%d nodes=%d projection=%d ok=%s",
            report.files,
            report.nodes,
            report.projection_entries,
            report.ok,
        )
    except Exception:  # noqa: BLE001 — repair failure cannot undo an accepted compact
        logger.warning("compact evomem repair failed", exc_info=True)
