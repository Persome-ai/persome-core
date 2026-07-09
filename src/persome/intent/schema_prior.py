"""Schema-inference prior — the D2 接入点 for habit/inertia priors in recall.

The migration design (``docs/research/2026-06-06-migration-D5-intent-fusion.md``
and ``…-D2-cognition.md``) splits durable user *facts* from durable user
*inertia* — the inferred regularities ("用户偏好极简工具链", "周一上午通常排会")
that the ``schema-*.md`` memory layer holds. Those inferences are the
highest-priority context for the recognizer, so they enter
``recall.assemble_background`` as a dedicated top section.

This module is that seam. It scans ``schema-*.md`` entries that the D2 schema
miner stage writes, keeps only the **stable** ones (forming / evolving /
deprecated schemas are weak signals — design §2.4 / §4②: a wrong prior is a
compounding cost, so weak schemas must not become priors), and returns each
schema's ``expected_inferences`` lines as plain strings.

When no ``schema-*.md`` files exist yet the scan returns ``[]`` — leaving recall
byte-for-byte identical to before the layer landed (the P0 guarantee, asserted
by ``test_intent_p0_recall``). The provider stays gated upstream by
``cfg.intent_recognizer.schema_prior_enabled`` (default off).
"""

from __future__ import annotations

import sqlite3

from ..writer import schema_miner_stage as stage

# Cap on how many inference lines reach the recognizer prompt. Stable schemas are
# already a narrow set; this is a defensive bound so a runaway schema count can't
# crowd out the other recall layers that share ``assemble_background``'s budget.
# Strongest schemas first (by confidence), so when the cap bites it drops the
# weakest priors, not arbitrary ones.
_MAX_INFERENCES = 8

# The status tag that gates injection. Only ``stable`` schemas are authoritative
# enough to bias recognition (design §2.4 status table).
_STABLE_TAG = "stable"


def _confidence_of(tag_field: str) -> float:
    """Pull the ``confidence:<float>`` value out of a space-joined tag string.

    Returns 0.0 when absent or unparsable, so a malformed schema sorts last
    rather than crashing the prior assembly.
    """
    for tok in tag_field.split():
        if tok.startswith("confidence:"):
            try:
                return float(tok.split(":", 1)[1])
            except ValueError:
                return 0.0
    return 0.0


def active_schema_inferences(conn: sqlite3.Connection) -> list[str]:
    """Return the active (stable) schema-inference priors as plain strings.

    Scans non-superseded ``schema-*.md`` entries, keeps those whose tags include
    ``stable``, orders them by ``confidence`` (descending) so the most-trusted
    schemas win the limited budget, parses each entry's ``expected_inferences``
    lines out of its body (the format :func:`schema_miner_stage.render_schema_body`
    writes), and returns them de-duplicated, capped at :data:`_MAX_INFERENCES`.

    Returns ``[]`` when no stable schema entries exist — the P0 seam guarantee.

    Implemented as a projection of :func:`active_schema_inferences_with_sources`
    (same selection, same order, same strings), so the prompt a caller renders
    from this list stays byte-for-byte identical whichever entry point is used.
    """
    return [text for text, _ in active_schema_inferences_with_sources(conn)]


def active_schema_inferences_with_sources(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Like :func:`active_schema_inferences`, but each inference line is paired
    with the memory filename of the schema it came from (``schema-*.md``).

    This is the provenance half of the R4 schema feedback loop: the recognizer
    records WHICH schemas were in the injected context when an intent was
    produced (coarse "在场" attribution — honest about co-presence, not a causal
    claim), so a later HUD dismiss/accept can flow back onto those schemas'
    confidence (:mod:`intent.schema_feedback`).

    The line selection/ordering/dedup/cap is exactly the one
    :func:`active_schema_inferences` has always had — only the source filename
    rides along.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT path, tags, content FROM entries "
        "WHERE prefix = 'schema' AND superseded = 0 "
        "ORDER BY timestamp DESC"
    ).fetchall()

    # ``tags`` is stored space-joined (see store.entries.append_entry); the status
    # is a bare token, not ``#status``. Match on the whitespace-split set so a
    # ``confidence:0.8`` neighbour can't false-match a substring.
    stable = [r for r in rows if _STABLE_TAG in (r["tags"] or "").split()]
    # Sort by confidence DESC; the source query's timestamp-DESC order is the
    # stable tie-breaker (Python's sort is stable), so equal-confidence schemas
    # keep newest-first.
    stable.sort(key=lambda r: _confidence_of(r["tags"] or ""), reverse=True)

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in stable:
        for line in stage.parse_expected_inferences(row["content"] or ""):
            if line and line not in seen:
                seen.add(line)
                out.append((line, row["path"]))
                if len(out) >= _MAX_INFERENCES:
                    return out
    return out
