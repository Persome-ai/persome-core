"""Entity retype/adjudication verbs — the §1.2 dimension-criterion executor.

The unified dimension criterion (spec §1.2, product ruling 2026-07-03): an
element's dimension IS the type of the minimal set of other elements its
complete description must reference — ∅=point, two points=edge, a point
set=face, a face set=body; when NO reference suffices to fix a unique
referent, the candidate is not an element at all but a VALUE on some axis.

These verbs execute a human adjudication verdict over a minted entity:

- ``retype_entity(name, kind)`` — the candidate IS a point but of a different
  kind: rewrite its file-prefix (the kind's SSOT — one axis, one channel)
  across evo_nodes + entries/files projections + the markdown receipt.
  kind→prefix: org→org- · project→project- · artifact→tool- (the memory
  taxonomy has no artifact- prefix; tool- is its home).
- ``shadow_entity(name)`` — the candidate is NOT a point (a class/role/
  generic → an axis value) or an unresolved role designation: retire its
  nodes to shadow. Markdown receipts stay on disk (§2.1 — never delete).
- ``merge_alias(name, keeper, cfg)`` — the candidate IS a point but not a NEW
  one: fold it onto ``keeper`` as an alias (through person_graph's own fold,
  the single write path), then shadow the duplicate's lineage.

All three are deterministic, idempotent, and bounded; they NEVER delete —
the dimension criterion demotes/renames, receipts survive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..logger import get
from ..store import fts

logger = get("persome.evomem.retype")

# kind → memory-file prefix (the kind axis's SSOT is the file taxonomy)
KIND_PREFIX = {"org": "org-", "project": "project-", "artifact": "tool-"}


@dataclass
class RetypeResult:
    old_file: str = ""
    new_file: str = ""
    evo_rows: int = 0
    entry_rows: int = 0
    md_renamed: bool = False
    shadowed: int = 0
    alias_folded: bool = False


def _find_entity_file(conn, name: str) -> str | None:
    """The person-* file whose stem is exactly ``name`` (adjudication is by
    the displayed identity, never fuzzy)."""
    for row in conn.execute(
        "SELECT DISTINCT file_name FROM evo_nodes WHERE file_name LIKE 'person-%'"
    ):
        fn = row[0] or ""
        if fn.removeprefix("person-").removesuffix(".md") == name:
            return fn
    return None


def _rename_everywhere(conn, old: str, new: str) -> RetypeResult:
    res = RetypeResult(old_file=old, new_file=new)
    res.evo_rows = conn.execute(
        "UPDATE evo_nodes SET file_name = ? WHERE file_name = ?", (new, old)
    ).rowcount
    try:
        res.entry_rows = conn.execute(
            "UPDATE entries SET path = ? WHERE path = ?", (new, old)
        ).rowcount
        conn.execute(
            "UPDATE files SET path = ?, prefix = ? WHERE path = ?",
            (new, new.split("-", 1)[0], old),
        )
    except Exception:  # noqa: BLE001 — FTS projection is rebuildable; evo rename is the truth
        logger.exception("retype: entries/files projection rename failed for %s", old)
    conn.commit()
    from .. import paths

    src = paths.memory_dir() / old
    dst = paths.memory_dir() / new
    if src.exists() and not dst.exists():
        src.rename(dst)
        res.md_renamed = True
    return res


def retype_entity(name: str, kind: str) -> RetypeResult:
    prefix = KIND_PREFIX.get(kind)
    if prefix is None:
        raise ValueError(f"retype: kind {kind!r} not in {sorted(KIND_PREFIX)}")
    with fts.cursor() as conn:
        old = _find_entity_file(conn, name)
        if old is None:
            raise ValueError(f"retype: no person entity file for {name!r}")
        new = prefix + old.removeprefix("person-")
        res = _rename_everywhere(conn, old, new)
    logger.info("retype: %s → %s (evo=%d entries=%d)", old, new, res.evo_rows, res.entry_rows)
    return res


def shadow_entity(name: str) -> RetypeResult:
    with fts.cursor() as conn:
        old = _find_entity_file(conn, name)
        if old is None:
            raise ValueError(f"retype: no person entity file for {name!r}")
        res = RetypeResult(old_file=old)
        res.shadowed = conn.execute(
            "UPDATE evo_nodes SET status = 'shadow' WHERE file_name = ? AND status = 'active'",
            (old,),
        ).rowcount
        conn.commit()
    logger.info("retype: shadowed %s (%d nodes)", old, res.shadowed)
    return res


class _OneShotSource:
    def __init__(self, events: list[Any]):
        self._events = events

    def events(self) -> list[Any]:
        return list(self._events)


def merge_alias(name: str, keeper: str, cfg: Any, *, memory: Any | None = None) -> RetypeResult:
    """Fold ``name`` into ``keeper`` when both identify the same Point
    through person_graph's own fold (the single write path), then shadow the
    duplicate lineage."""
    from .engine import EvoMemory
    from .person_graph import PersonEvent, PersonGraph

    mem = memory or EvoMemory()
    # shadow the duplicate FIRST — otherwise the fold's alias set matches the
    # duplicate's own canonical and the merge lands on the wrong entity.
    res = shadow_entity(name)
    pg = PersonGraph(
        mem,
        cfg=cfg,
        name_source=_OneShotSource(
            [
                PersonEvent(
                    name=keeper,
                    summary=f"Human-reviewed alias merge: {name} identifies the same entity",
                    occurred_at=datetime.now(UTC),
                    aliases=[name],
                    confidence=1.0,
                )
            ]
        ),
    )
    pg.ingest()
    res.alias_folded = True
    return res
