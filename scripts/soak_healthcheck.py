#!/usr/bin/env python3
"""Soak health-check for the evomem SSOT era.

Answers one question: *after the daemon has run for a while (a day), is the
memory system still internally consistent and usable?* It probes the things
that can silently rot over time, post SSOT switch (entry_chain and the
dual-read machinery were retired in PR-7 — chain truth lives in evo_nodes):

  1. §3.3 integrity suite — the evo_nodes chain invariants (pointer symmetry,
     anti-fork, head consistency, acyclicity) plus the projection
     reconciliation ({is_latest=1 ∧ active} ≡ {entries.superseded=0}).
  2. retrieval projection ≡ rebuild — the incrementally-maintained entries
     FTS projection must match what a full `rebuild_index` re-derives from
     the current write authority's truth (evo_nodes under evomem authority,
     markdown files under markdown authority). If a day of writes drifted
     it, recall folds wrong.
  3. schema-tick output         — the daily 00:15 tick should have produced
     parseable schema-*.md files that the model schema reader can read.

Plus a smoke test through the same associative entrance used by MCP and Chat.

Run against a live or sandbox data dir (real memory is at ~/.persome):

    # safe sandbox: copy real data, point the env at it, no daemon needed
    cp -r ~/.persome /tmp/oc-soak
    PERSOME_ROOT=/tmp/oc-soak uv run python scripts/soak_healthcheck.py

    # or against live data — STOP the daemon first to avoid a write race:
    persome stop && uv run python scripts/soak_healthcheck.py

Exit 0 = all checks pass. Exit 1 = drift / inconsistency found (details printed).
"""

from __future__ import annotations

import sys

from persome import paths
from persome.evomem import integrity
from persome.store import entries as entries_mod
from persome.store import fts


def _dump_projection(conn) -> list[tuple]:
    return conn.execute("SELECT id, path, superseded FROM entries ORDER BY id").fetchall()


def main() -> int:
    failures: list[str] = []
    print(f"== soak health-check on {paths.root()} ==\n")

    with fts.cursor() as conn:
        n_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

        # ---- 1. §3.3 integrity suite on the live DB ----
        # The same checks the daemon runs at startup and after the daily
        # snapshot: evo chain invariants + projection reconciliation.
        violations = integrity.run_checks(conn)
        if violations:
            for v in violations:
                failures.append(f"integrity violation: {v.check}: {v.detail}")
        else:
            print("✓ §3.3 integrity suite passed (chain invariants + projection reconciliation)")

        # ---- 2. incrementally-maintained projection vs full rebuild ----
        # Snapshot the LIVE entries projection, then re-derive it from the
        # current write authority's truth (rebuild_index dispatches: evomem →
        # evo_nodes hybrid replay, markdown → memory/*.md replay) and compare.
        before = _dump_projection(conn)
        entries_mod.rebuild_index(conn)  # re-derive from the authority's truth
        after = _dump_projection(conn)
        print(f"[entries: before={len(before)} → rebuilt={len(after)}]")

        if before != after:
            b = {r[0]: r[1:] for r in before}
            a = {r[0]: r[1:] for r in after}
            drift = [eid for eid in set(b) | set(a) if b.get(eid) != a.get(eid)]
            failures.append(
                f"entries projection DRIFTED from rebuild on {len(drift)} entries "
                f"(e.g. {drift[:5]}) — incremental maintenance diverged from the "
                "SSOT (rebuild has now healed it; investigate the write path)"
            )
        else:
            print(f"✓ entries projection matches a full rebuild ({len(after)} rows, 0 drift)")
        n_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

        # ---- 3. schema-tick output ----
        schema_files = sorted(paths.memory_dir().glob("schema-*.md"))
        print(f"[schema-*.md files: {len(schema_files)}]")
        if not schema_files:
            print(
                "⚠ no schema-*.md yet — expected if the 00:15 schema-tick hasn't "
                "fired or there aren't ≥4 clustered facts in any file yet (not a failure)"
            )
        else:
            for f in schema_files:
                print(f"    - {f.name}")
            try:
                from persome.model import schema_reader

                inferences = schema_reader.active_schema_inferences(conn)
                print(
                    f"✓ schema reader found {len(inferences)} active inference(s) "
                    "from stable schemas"
                )
            except Exception as e:  # noqa: BLE001 — surface, don't crash the probe
                failures.append(f"active_schema_inferences raised on real schemas: {e!r}")

        # ---- 4. production retrieval smoke must not crash ----
        try:
            import re

            from persome.mcp import server as mcp_server

            hint_row = conn.execute(
                "SELECT content FROM entries WHERE superseded=0 LIMIT 1"
            ).fetchone()
            # Pull a clean alphanumeric/CJK token for the hint — markdown markers
            # (**, ##, ~~) trip the FTS special-query parser (which degrades
            # gracefully, but we don't need the noise in a smoke test).
            words = re.findall(r"[\w一-鿿]{2,}", hint_row[0]) if hint_row else []
            hint = words[0] if words else "用户"
            recall = mcp_server._search(conn, query=hint, top_k=5)
            print(
                f"✓ production associative retrieval ran "
                f"(hint={hint!r}, {len(recall['results'])} hit(s))"
            )
        except Exception as e:  # noqa: BLE001
            failures.append(f"production retrieval raised: {e!r}")

    print()
    if failures:
        print(f"✗ {len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"✓ ALL CHECKS PASSED — memory system is consistent and usable ({n_entries} entries).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
