"Deterministic application of gated personal-model deltas."

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..evomem import integrity as evo_integrity
from ..evomem import relation_extractor as rex
from ..evomem import store as evo_store
from ..evomem.engine import EvoMemory
from ..evomem.models import MemoryLayer, MemoryNode
from ..evomem.person_graph import _slug as _entity_slug
from ..evomem.store import NodeStore
from ..logger import get
from ..store import entries as entries_store
from ..store import relation_edges as edges_store
from ..store.relation_edges import EntityKind, Predicate

logger = get("persome.writer.delta_apply")

SELF_IDENTITY = "self"
EVENT_PREFIX = "event:"


_KIND_PREFIX = {"person": "person", "org": "org", "project": "project", "artifact": "tool"}
_KIND_ENUM = {
    "person": EntityKind.PERSON,
    "org": EntityKind.ORG,
    "project": EntityKind.PROJECT,
    "artifact": EntityKind.ARTIFACT,
    "self": EntityKind.SELF,
    "event": EntityKind.EVENT,
}


@dataclass
class ApplyResult:
    entities_minted: int = 0
    entities_seen: int = 0
    assertions_minted: int = 0
    assertions_seen: int = 0
    edges_new: int = 0
    edges_reinforced: int = 0
    edges_closed: int = 0
    events_minted: int = 0
    floor_edges: int = 0
    supersedes_applied: int = 0
    skipped_reason: str = ""
    errors: list[str] = field(default_factory=list)


class _ConnectionNodeStore(NodeStore):
    """Route deterministic Point writes through the caller's transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        # NodeStore.__init__ opens another autocommit connection. Runtime schema
        # setup already happened at fts.cursor(), so retain only the shared scope.
        self.user_id = "default"
        self.agent_id = "default"
        self._conn = conn

    def save(self, node: MemoryNode) -> None:
        evo_integrity.ensure_writes_allowed()
        self._upsert_node(self._conn, node)


def _canonical_of(who: dict[str, Any] | None) -> str | None:
    if not isinstance(who, dict):
        return None

    return who.get("ref") or who.get("new_entity") or who.get("canonical")


def _entity_kind_map(clean: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in clean.get("entities") or []:
        c = _canonical_of(e)
        if c and e.get("kind") in _KIND_PREFIX:
            out[c] = e["kind"]
    return out


def _find_entity_head(conn: sqlite3.Connection, file_name: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT node_id FROM evo_nodes WHERE file_name = ? AND is_latest = 1"
            " AND status = 'active' LIMIT 1",
            (file_name,),
        ).fetchone()
        return row[0] if row else None
    except Exception:  # noqa: BLE001
        return None


def _apply_entities(conn: sqlite3.Connection, mem: EvoMemory, clean: dict, r: ApplyResult) -> None:
    now = datetime.now(UTC).isoformat()
    ended_files: list[str] = []
    for e in clean.get("entities") or []:
        try:
            if not isinstance(e, dict):
                continue
            kind = e.get("kind")
            canonical = _canonical_of(e)
            if not canonical or kind not in _KIND_PREFIX or canonical == SELF_IDENTITY:
                continue
            stem = f"{_KIND_PREFIX[kind]}-{_entity_slug(canonical)}"
            stored = f"{stem}.md"

            head = _find_entity_head(conn, stored)
            if head is None:
                mem.add_direct(
                    canonical,
                    layer=MemoryLayer.L5_KNOWLEDGE,
                    file_name=stem,
                    tags="entity",
                )
                r.entities_minted += 1
            else:
                r.entities_seen += 1
            if e.get("ended"):
                ended_files.append(stored)
        except Exception as exc:  # noqa: BLE001
            r.errors.append(f"entity: {exc}")

    if ended_files:
        _stamp_entities_valid_until(conn, ended_files, now)


def _stamp_entities_valid_until(conn: sqlite3.Connection, file_names: list[str], at: str) -> None:
    conn.executemany(
        "UPDATE evo_nodes SET valid_until = ? WHERE file_name = ? AND is_latest = 1"
        " AND valid_until IS NULL",
        [(at, fn) for fn in file_names],
    )


def _route_assertion_stem(
    conn: sqlite3.Connection, canonical: str, kinds: dict[str, str]
) -> str | None:
    slug = _entity_slug(canonical)
    kind = kinds.get(canonical)
    if kind in _KIND_PREFIX:
        return f"{_KIND_PREFIX[kind]}-{slug}"
    for prefix in _KIND_PREFIX.values():
        if _find_entity_head(conn, f"{prefix}-{slug}.md") is not None:
            return f"{prefix}-{slug}"
    return None


def _assertion_exists(conn: sqlite3.Connection, stored: str, text: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM evo_nodes WHERE file_name = ? AND content = ? AND is_latest = 1 LIMIT 1",
            (stored, text),
        ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def _apply_assertions(
    conn: sqlite3.Connection, mem: EvoMemory, clean: dict, kinds: dict[str, str], r: ApplyResult
) -> None:
    for a in clean.get("assertions") or []:
        try:
            if not isinstance(a, dict):
                continue
            text = str(a.get("text") or "").strip()
            canonical = _canonical_of(a.get("subject"))
            if not text or not canonical or canonical == SELF_IDENTITY:
                continue
            stem = _route_assertion_stem(conn, canonical, kinds)
            if stem is None:
                continue
            if _assertion_exists(conn, f"{stem}.md", text):
                r.assertions_seen += 1
                continue
            tags = "fact"
            conf = a.get("confidence")
            if isinstance(conf, (int, float)):
                tags += f" confidence:{float(conf):.2f}"
            mem.add_direct(text, layer=MemoryLayer.L5_KNOWLEDGE, file_name=stem, tags=tags)
            r.assertions_minted += 1
        except Exception as exc:  # noqa: BLE001
            r.errors.append(f"assertion: {exc}")


def _apply_relations(
    conn: sqlite3.Connection, clean: dict, kinds: dict[str, str], r: ApplyResult
) -> None:
    seen = rex._open_edges(conn)  # noqa: SLF001
    tally = rex._Tally()  # noqa: SLF001
    now = datetime.now(UTC).isoformat()
    for rel in clean.get("relations") or []:
        try:
            if not isinstance(rel, dict):
                continue
            src = _canonical_of(rel.get("src"))
            dst = _canonical_of(rel.get("dst"))
            pred_raw = rel.get("predicate")
            if not src or not dst or pred_raw not in {p.value for p in Predicate}:
                continue
            predicate = Predicate(pred_raw)
            src_kind = _endpoint_kind(src, kinds)
            dst_kind = _endpoint_kind(dst, kinds)
            before = tally.new
            try:
                rex._upsert_shadow(  # noqa: SLF001
                    conn,
                    seen,
                    tally,
                    src=src,
                    dst=dst,
                    predicate=predicate,
                    confidence=float(rel.get("confidence", 0.5)),
                    quote=str(rel.get("quote") or ""),
                    label=rel.get("label"),
                    observations=1,
                    src_kind=src_kind,
                    dst_kind=dst_kind,
                    polarity=_norm_polarity(rel.get("polarity")),
                    additive=bool(rel.get("cooccurrence")),
                    commit=False,
                )
            except ValueError:
                continue
            if tally.new > before:
                r.edges_new += 1
            else:
                r.edges_reinforced += 1
            # ended -> close_edge (section 4.6, leg A)
            if rel.get("ended"):
                key = rex._edge_key(src, dst, predicate.value)  # noqa: SLF001
                eid = seen.get(key)
                if eid and edges_store.close_edge(conn, edge_id=eid, at=now, commit=False):
                    r.edges_closed += 1
        except Exception as exc:  # noqa: BLE001
            r.errors.append(f"relation: {exc}")


def _apply_events(
    conn: sqlite3.Connection, clean: dict, kinds: dict[str, str], r: ApplyResult
) -> None:
    seen = rex._open_edges(conn)  # noqa: SLF001
    tally = rex._Tally()  # noqa: SLF001
    for ev in clean.get("events") or []:
        try:
            if not isinstance(ev, dict):
                continue
            title = str(ev.get("title") or "").strip()
            if not title:
                continue
            eid = EVENT_PREFIX + hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]  # noqa: S324
            r.events_minted += 1
            for p in ev.get("participants") or []:
                pc = _canonical_of(p)
                if not pc:
                    continue
                try:
                    rex._upsert_shadow(  # noqa: SLF001
                        conn,
                        seen,
                        tally,
                        src=pc if pc != SELF_IDENTITY else SELF_IDENTITY,
                        dst=eid,
                        predicate=Predicate.PARTICIPATES_IN,
                        confidence=float(ev.get("confidence", 0.5)),
                        quote=str(ev.get("quote") or title),
                        label="event",
                        observations=1,
                        src_kind=_endpoint_kind(pc, kinds),
                        dst_kind=EntityKind.EVENT.value,
                        commit=False,
                    )
                except ValueError:
                    continue
        except Exception as exc:  # noqa: BLE001
            r.errors.append(f"event: {exc}")


def _apply_floor(
    conn: sqlite3.Connection, clean: dict, kinds: dict[str, str], r: ApplyResult
) -> None:
    seen = rex._open_edges(conn)  # noqa: SLF001
    tally = rex._Tally()  # noqa: SLF001
    for e in clean.get("entities") or []:
        if not isinstance(e, dict):
            continue
        canonical = _canonical_of(e)
        if not canonical or canonical == SELF_IDENTITY:
            continue
        try:
            rex._upsert_shadow(  # noqa: SLF001
                conn,
                seen,
                tally,
                src=SELF_IDENTITY,
                dst=canonical,
                predicate=Predicate.ENGAGED_WITH,
                confidence=1.0,
                quote=str(e.get("quote") or ""),
                label="engaged",
                observations=1,
                src_kind=EntityKind.SELF.value,
                dst_kind=_endpoint_kind(canonical, kinds),
                additive=True,
                status="active",  # direct observed attention, not an inferred semantic claim
                commit=False,
            )
        except ValueError:
            continue
    r.floor_edges = tally.new + tally.reinforced


def _endpoint_kind(identity: str, kinds: dict[str, str]) -> str:
    if identity == SELF_IDENTITY:
        return EntityKind.SELF.value
    if identity.startswith(EVENT_PREFIX):
        return EntityKind.EVENT.value
    k = kinds.get(identity)
    return _KIND_ENUM[k].value if k in _KIND_ENUM else EntityKind.PERSON.value


def _apply_supersede(conn: sqlite3.Connection, clean: dict, r: ApplyResult) -> None:
    for item in clean.get("supersede") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("file", "")).strip()
        eid = str(item.get("entry_id", "")).strip()
        if not name or not eid:
            continue
        try:
            repl = str(item.get("replacement", "")).strip()
            reason = str(item.get("reason", "") or "memory update")[:300]
            if repl:
                entries_store.supersede_entry(
                    conn, name=name, old_entry_id=eid, new_content=repl, reason=reason
                )
            else:
                entries_store.mark_entry_deleted(conn, name=name, entry_id=eid)
            r.supersedes_applied += 1
        except Exception as exc:  # noqa: BLE001 — one bad target never drops the rest
            r.errors.append(f"supersede {name}#{eid}: {exc}")


def apply_delta(
    conn: sqlite3.Connection,
    cfg: Any,
    clean: dict,
) -> ApplyResult:
    if not clean:
        return ApplyResult(skipped_reason="empty")

    # fts connections autocommit. A savepoint plus a connection-bound NodeStore
    # keeps Points and Lines in one transaction so an item-level failure cannot
    # leave successful siblings behind for a retry to reinforce.
    evo_store.ensure_schema(conn)
    edges_store.ensure_schema(conn)
    savepoint = "persome_delta_apply"
    original_row_factory = conn.row_factory
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        r = ApplyResult()
        mem = EvoMemory(store=_ConnectionNodeStore(conn))
        kinds = _entity_kind_map(clean)
        _apply_entities(conn, mem, clean, r)

        if getattr(getattr(cfg, "memory_delta", None), "apply_assertions", False):
            _apply_assertions(conn, mem, clean, kinds, r)
        _apply_floor(conn, clean, kinds, r)
        _apply_relations(conn, clean, kinds, r)
        _apply_events(conn, clean, kinds, r)
        # Corrections are a separate, user-directed head. Do not touch their
        # Markdown projection if any canonical model item already failed.
        if not r.errors:
            conn.row_factory = sqlite3.Row
            _apply_supersede(conn, clean, r)

        if r.errors:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return ApplyResult(skipped_reason="rolled_back", errors=list(r.errors))
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return r
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    finally:
        conn.row_factory = original_row_factory


def _norm_polarity(p: Any) -> str:
    v = str(p or "0")
    return v if v in ("+", "-", "0") else "0"
