"""Nightly semantic-contradiction self-check — memory-rebuild spec §4.4.

Two live facts in the same memory file that are mutually exclusive ("Alex owns
payments" vs "Alex left the company") poison every downstream consumer that treats memory as
equally true. This check runs at the 23:55 harvest (nightly maintenance, zero new
timers): it pairs candidate facts deterministically, asks one bounded LLM judge
per pair, and — on a contradiction verdict — **marks, never resolves**:

- both entries get ``entry_metadata.conflicted = 1`` → recall's existing
  metacognition layer renders an unresolved-conflict warning and model consumers
  down-weights them (the production consumer already in place);
- the pair lands in ``memory_contradictions`` (``store/contradictions.py``) —
  the human adjudication queue (``persome contradictions`` /
  ``contradictions-resolve``). SUPERSEDE stays a human/consolidation verb;
  auto-deleting one side of a disagreement the model may have misjudged is the
  one mistake this check must be unable to make.

Determinism/cost discipline:
- **candidate pairing is zero-LLM**: same-file live fact entries whose
  char-bigram Jaccard falls in a mid band — similar enough to be about the
  same subject, below near-duplicate (that's dedup's job, not contradiction's);
- the LLM only ever sees ≤ ``contradiction_max_pairs`` pairs per night,
  strongest-similarity first;
- every judged pair is remembered (any status) — no pair is ever re-judged, so
  cost decays to zero on a stable memory and a human "not a contradiction"
  permanently silences the pair.

Self-gating on ``[evomem] contradiction_check_enabled`` (default OFF — it
spends nightly LLM calls; the flag is the activation, the CLI + conflicted
annotation are the consumers). Fail-open: any error is logged, the tick never
dies.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import prompts
from ..config import Config
from ..logger import get
from ..store import contradictions as contradictions_store
from ..store import fts
from . import llm as llm_mod
from .schema_miner_stage import _FACT_PREFIXES

logger = get("persome.writer")

# Similarity band for "same subject, different claim". Below the floor the two
# facts are probably about different things (a judge call is wasted); above the
# ceiling they are near-duplicates — re-statements are the dedup/fold family's
# job, and a duplicate is not a contradiction.
BAND_FLOOR = 0.30
BAND_CEILING = 0.92
# Per-file entry cap keeps the pairwise scan O(cap²) per file, not O(n²) global.
_MAX_ENTRIES_PER_FILE = 40

_STAGE = "contradiction_check"


@dataclass
class CandidatePair:
    a_id: str
    b_id: str
    path: str
    a_body: str
    b_body: str
    similarity: float


@dataclass
class ContradictionRunResult:
    candidates: int = 0
    judged: int = 0
    flagged: int = 0


def _bigrams(text: str) -> set[str]:
    folded = "".join((text or "").split()).casefold()
    return {folded[i : i + 2] for i in range(len(folded) - 1)}


def _similarity(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def find_candidate_pairs(
    conn: sqlite3.Connection,
    *,
    max_pairs: int,
    skip: set[str] | None = None,
) -> list[CandidatePair]:
    """Deterministic candidate pairing: same-file live fact entries with
    band-similarity, strongest first, already-judged pairs excluded."""
    skip = skip or set()
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(_FACT_PREFIXES))
    rows = conn.execute(
        f"SELECT id, path, content FROM entries "
        f"WHERE prefix IN ({placeholders}) AND superseded = 0 "
        f"ORDER BY path, timestamp",
        _FACT_PREFIXES,
    ).fetchall()

    by_file: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if (row["content"] or "").strip():
            bucket = by_file.setdefault(row["path"], [])
            if len(bucket) < _MAX_ENTRIES_PER_FILE:
                bucket.append(row)

    out: list[CandidatePair] = []
    for path in sorted(by_file):
        entries = by_file[path]
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                a, b = entries[i], entries[j]
                if contradictions_store.pair_key(a["id"], b["id"]) in skip:
                    continue
                sim = _similarity(a["content"], b["content"])
                if BAND_FLOOR <= sim <= BAND_CEILING:
                    out.append(
                        CandidatePair(
                            a_id=a["id"],
                            b_id=b["id"],
                            path=path,
                            a_body=a["content"].strip(),
                            b_body=b["content"].strip(),
                            similarity=sim,
                        )
                    )
    out.sort(key=lambda p: (-p.similarity, p.a_id, p.b_id))
    return out[:max_pairs]


def _build_llm_call(cfg: Config) -> Callable[[list[dict]], Any]:
    def _call(messages: list[dict]) -> Any:
        return llm_mod.call_llm(cfg, _STAGE, messages=messages)

    return _call


def _parse_verdict(resp: Any) -> tuple[bool, str]:
    content = resp.choices[0].message.content or ""
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in judge output: {content[:120]!r}")
    obj = json.loads(content[start : end + 1])
    return bool(obj.get("contradictory")), str(obj.get("reason") or "").strip()


def _mark_conflicted(conn: sqlite3.Connection, entry_id: str, *, conflicted: bool) -> None:
    """Flip one entry's conflicted bit while PRESERVING its other meta-cognition
    fields — ``set_entry_metadata`` is a whole-row upsert, so a blind write
    would erase an existing confidence tag."""
    meta = fts.get_entry_metadata(conn, entry_id) or {}
    fts.set_entry_metadata(
        conn,
        entry_id,
        confidence=meta.get("confidence"),
        conflicted=conflicted,
        occurred_at=meta.get("occurred_at"),
    )


def clear_conflicted(conn: sqlite3.Connection, *entry_ids: str) -> None:
    """Human-verdict side effect: drop the ⚠ annotation from adjudicated entries."""
    for entry_id in entry_ids:
        _mark_conflicted(conn, entry_id, conflicted=False)


def run_contradiction_check(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    llm_call: Callable[[list[dict]], Any] | None = None,
) -> ContradictionRunResult:
    """One nightly pass. Self-gated on the config flag; returns run stats."""
    result = ContradictionRunResult()
    if not cfg.evomem.contradiction_check_enabled:
        return result
    seen = contradictions_store.seen_pairs(conn)
    pairs = find_candidate_pairs(conn, max_pairs=cfg.evomem.contradiction_max_pairs, skip=seen)
    result.candidates = len(pairs)
    if not pairs:
        return result

    call = llm_call if llm_call is not None else _build_llm_call(cfg)
    template = prompts.load("contradiction_check.md")
    for pair in pairs:
        # plain replace, NOT str.format — the template's JSON example carries
        # literal braces that format() would treat as fields
        prompt = (
            template.replace("{path}", pair.path)
            .replace("{a_id}", pair.a_id)
            .replace("{b_id}", pair.b_id)
            .replace("{a_body}", pair.a_body)
            .replace("{b_body}", pair.b_body)
        )
        try:
            verdict, reason = _parse_verdict(call([{"role": "user", "content": prompt}]))
        except Exception:  # noqa: BLE001 — one bad judge reply never kills the pass
            logger.exception("contradiction judge failed on %s", pair.path)
            continue
        result.judged += 1
        # Record EVERY verdict (the dedup ledger must silence judged-clean
        # pairs too), but only a contradiction gets the ledger row status
        # 'open' + the ⚠ marks — a clean pair is closed as dismissed by code.
        key = contradictions_store.record(
            conn,
            a_id=pair.a_id,
            b_id=pair.b_id,
            path=pair.path,
            a_body=pair.a_body,
            b_body=pair.b_body,
            reason=reason or ("mutually exclusive" if verdict else "not mutually exclusive"),
        )
        if verdict:
            result.flagged += 1
            _mark_conflicted(conn, pair.a_id, conflicted=True)
            _mark_conflicted(conn, pair.b_id, conflicted=True)
            logger.info("contradiction flagged in %s: %s ↔ %s", pair.path, pair.a_id, pair.b_id)
        else:
            contradictions_store.close(conn, key, status="dismissed")
    return result
