"Person identity consolidation and interaction history over evomem."

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from ..logger import get
from .engine import EvoMemory
from .models import MemoryLayer, MemoryNode, ReconcileAction, ReconcileOp

_log = get("persome.evomem.person_graph")


_TAG_ENTITY = "person-entity"
_TAG_EVENT = "person-event"


_META_CANONICAL = "canonical"
_META_ALIASES = "aliases"
_META_CATEGORY = "category"
_META_SIGHTINGS = "sightings"


def _now() -> datetime:
    return datetime.now(UTC)


def _norm(name: str) -> str:
    folded = unicodedata.normalize("NFKC", name or "").strip()
    folded = " ".join(folded.split())
    return folded.casefold()


def _slug(canonical: str) -> str:
    out: list[str] = []
    for ch in unicodedata.normalize("NFKC", canonical or "").strip():
        if ch.isalnum():
            out.append(ch.lower())
        elif out and out[-1] != "-":
            out.append("-")
    slug = "".join(out).strip("-")
    return slug or "unknown"


@dataclass
class PersonEvent:
    name: str
    summary: str = ""
    occurred_at: datetime | None = None
    category: str | None = None
    aliases: Sequence[str] = field(default_factory=tuple)
    confidence: float = 1.0
    source_id: str | None = None


@dataclass
class PersonEntity:
    node_id: str
    canonical: str
    aliases: list[str]
    category: str | None
    sightings: int
    last_seen: datetime | None

    @property
    def seen_once(self) -> bool:
        return self.sightings <= 1


class PersonNameSource(Protocol):
    def events(self) -> list[PersonEvent]: ...


class EmptyPersonNameSource:
    def events(self) -> list[PersonEvent]:  # noqa: D102
        return []


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO timestamp, ALWAYS returning an aware datetime (naive → UTC).

    Real stores mix naive and aware strings (minute-granularity legacy rows vs
    tz-suffixed ones); a mixed list makes ``sort`` raise TypeError — which the
    relation extractor's fail-safe then swallows into "0 people, 0 edges".
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _meta_of(node: MemoryNode) -> dict:
    try:
        data = json.loads(node.schema_summary or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


class PersonGraph:
    def __init__(
        self,
        memory: EvoMemory,
        *,
        cfg: object | None = None,
        name_source: PersonNameSource | None = None,
        min_confidence: float = 0.6,
    ) -> None:
        self._mem = memory
        self._cfg = cfg
        self._source = name_source or EmptyPersonNameSource()
        self._min_confidence = min_confidence
        self._reserved_owner_keys: set[str] | None = None

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._cfg, "person_graph_enabled", False))

    def _owner_keys(self) -> set[str]:
        if self._reserved_owner_keys is None:
            try:
                from . import owner_identity

                self._reserved_owner_keys = {
                    _norm(alias) for alias in owner_identity.reserved_aliases(self._cfg)
                }
            except Exception:  # noqa: BLE001 - identity protection is fail-safe
                self._reserved_owner_keys = set()
        return self._reserved_owner_keys

    def ingest(self) -> list[str]:
        if not self.enabled:
            return []
        touched: list[str] = []
        for event in self._source.events():
            canonical = self.record(event)
            if canonical is not None:
                touched.append(canonical)
        return touched

    def record(self, event: PersonEvent) -> str | None:
        if not self.enabled:
            return None
        norm = _norm(event.name)
        if not norm:
            return None
        owner_keys = self._owner_keys()
        if norm in owner_keys or any(_norm(alias) in owner_keys for alias in event.aliases):
            _log.debug("person_graph: reserve owner identity %r", event.name)
            return None

        existing = self._find_entity(norm, event.aliases)

        if (
            existing is not None
            and event.source_id
            and self._has_source_event(existing.canonical, event.source_id)
        ):
            return existing.canonical

        if existing is None and event.confidence < self._min_confidence:
            _log.debug("person_graph: skip low-confidence first sighting %r", event.name)
            return None

        if existing is None:
            canonical = event.name.strip()
            entity = self._create_entity(event, canonical)
        else:
            canonical = existing.canonical
            entity = self._merge_entity(existing, event)

        self._append_event(entity_canonical=entity.canonical, event=event)
        return entity.canonical

    def _create_entity(self, event: PersonEvent, canonical: str) -> PersonEntity:
        aliases = _dedup_aliases([canonical, *event.aliases])
        meta = {
            _META_CANONICAL: canonical,
            _META_ALIASES: aliases,
            _META_CATEGORY: event.category,
            _META_SIGHTINGS: 1,
        }
        nid = self._mem.add_direct(
            canonical,
            layer=MemoryLayer.L5_KNOWLEDGE,
            file_name=f"person-{_slug(canonical)}",
            tags=_TAG_ENTITY,
        )

        nid = self._supersede_entity(nid, canonical, meta)
        return PersonEntity(
            node_id=nid,
            canonical=canonical,
            aliases=aliases,
            category=event.category,
            sightings=1,
            last_seen=event.occurred_at or _now(),
        )

    def _merge_entity(self, existing: PersonEntity, event: PersonEvent) -> PersonEntity:
        aliases = _dedup_aliases([*existing.aliases, event.name, *event.aliases])
        category = existing.category or event.category
        sightings = existing.sightings + 1
        meta = {
            _META_CANONICAL: existing.canonical,
            _META_ALIASES: aliases,
            _META_CATEGORY: category,
            _META_SIGHTINGS: sightings,
        }
        nid = self._supersede_entity(existing.node_id, existing.canonical, meta)
        return PersonEntity(
            node_id=nid,
            canonical=existing.canonical,
            aliases=aliases,
            category=category,
            sightings=sightings,
            last_seen=event.occurred_at or _now(),
        )

    def _supersede_entity(self, old_id: str, canonical: str, meta: dict) -> str:
        from .engine import _new_id

        node = MemoryNode(
            node_id=_new_id(_now()),
            content=canonical,
            layer=MemoryLayer.L5_KNOWLEDGE,
            supersedes=[old_id],
            is_latest=True,
            memory_at=_now(),
            gmt_created=_now(),
            user_id=self._mem.user_id,
            agent_id=self._mem.agent_id,
            file_name=f"person-{_slug(canonical)}.md",
            tags=_TAG_ENTITY,
            schema_summary=json.dumps(meta, ensure_ascii=False),
        )
        return self._mem.commit_supersede(node, old_id=old_id)

    def _append_event(self, *, entity_canonical: str, event: PersonEvent) -> str:
        """Append one event node without superseding or entering a chain."""
        op = ReconcileOp(
            action=ReconcileAction.ADD,
            content=event.summary or f"One interaction with {entity_canonical}",
            layer=MemoryLayer.L5_KNOWLEDGE,
        )

        from .engine import _new_id

        now = _now()
        node = MemoryNode(
            node_id=_new_id(now),
            content=op.content,
            layer=MemoryLayer.L5_KNOWLEDGE,
            is_latest=True,
            memory_at=event.occurred_at or now,
            gmt_created=now,
            user_id=self._mem.user_id,
            agent_id=self._mem.agent_id,
            file_name=f"person-{_slug(entity_canonical)}.md",
            tags=_TAG_EVENT,
            occurred_at=(event.occurred_at or now).isoformat(),
            schema_summary=json.dumps(
                {_META_CANONICAL: entity_canonical, "source_id": event.source_id},
                ensure_ascii=False,
            ),
        )
        return self._mem.commit_node(node)

    def _has_source_event(self, canonical: str, source_id: str) -> bool:
        wanted = _norm(canonical)
        for node in self._mem.store.all_latest():
            if _TAG_EVENT not in (node.tags or "").split():
                continue
            meta = _meta_of(node)
            if _norm(str(meta.get(_META_CANONICAL, ""))) != wanted:
                continue
            if str(meta.get("source_id") or "") == source_id:
                return True
        return False

    def _entity_nodes(self) -> list[MemoryNode]:

        # the file taxonomy IS the kind axis's SSOT, so an adjudicated retype

        # roster (and out of knows-edge extraction) by construction.
        owner_keys = self._owner_keys()
        out: list[MemoryNode] = []
        for node in self._mem.store.all_latest():
            if _TAG_ENTITY not in (node.tags or "").split() or not (
                node.file_name or ""
            ).startswith("person-"):
                continue
            meta = _meta_of(node)
            names = [
                str(meta.get(_META_CANONICAL) or node.content),
                *(str(alias) for alias in (meta.get(_META_ALIASES) or [])),
            ]
            if any(_norm(name) in owner_keys for name in names):
                continue
            out.append(node)
        return out

    def _find_entity(self, norm_name: str, extra_aliases: Iterable[str]) -> PersonEntity | None:
        wanted = {norm_name, *(_norm(a) for a in extra_aliases)}
        wanted.discard("")
        for node in self._entity_nodes():
            meta = _meta_of(node)
            cand_canonical = _norm(meta.get(_META_CANONICAL, node.content))
            known = {_norm(a) for a in meta.get(_META_ALIASES, [])}
            known.add(cand_canonical)
            known.discard("")

            if norm_name in known or cand_canonical in wanted:
                return self._to_entity(node, meta)
        return None

    def _to_entity(self, node: MemoryNode, meta: dict | None = None) -> PersonEntity:
        meta = meta if meta is not None else _meta_of(node)
        return PersonEntity(
            node_id=node.node_id,
            canonical=meta.get(_META_CANONICAL) or node.content,
            aliases=_dedup_aliases(meta.get(_META_ALIASES, []) or [node.content]),
            category=meta.get(_META_CATEGORY),
            sightings=int(meta.get(_META_SIGHTINGS, 1) or 1),
            last_seen=node.memory_at or node.gmt_created,
        )

    def list_persons(self, *, min_sightings: int = 1) -> list[PersonEntity]:
        people = [self._to_entity(n) for n in self._entity_nodes()]
        people = [p for p in people if p.sightings >= min_sightings]
        people.sort(key=lambda p: p.last_seen or datetime.min.replace(tzinfo=UTC), reverse=True)
        return people

    def person_timeline(self, name: str) -> list[MemoryNode]:
        norm = _norm(name)
        if not norm:
            return []
        entity = self._find_entity(norm, [])
        if entity is None:
            return []
        canonical_norm = _norm(entity.canonical)
        events: list[MemoryNode] = []
        for node in self._mem.store.all_latest():
            if _TAG_EVENT not in (node.tags or "").split():
                continue
            meta = _meta_of(node)
            if _norm(meta.get(_META_CANONICAL, "")) == canonical_norm:
                events.append(node)
        events.sort(
            key=lambda n: (
                _parse_ts(n.occurred_at)
                or _aware(n.memory_at)
                or _aware(n.gmt_created)
                or datetime.min.replace(tzinfo=UTC)
            )
        )
        return events

    def build_person_context(self, name: str, *, max_events: int = 8) -> str:
        norm = _norm(name)
        if not norm:
            return ""
        entity = self._find_entity(norm, [])
        if entity is None:
            return ""
        timeline = self.person_timeline(entity.canonical)

        descr: list[str] = []
        if entity.category:
            descr.append(entity.category)
        other_aliases = [a for a in entity.aliases if _norm(a) != _norm(entity.canonical)]
        if other_aliases:
            descr.append("Aliases: " + ", ".join(other_aliases))
        descr.append(f"{entity.sightings} interaction(s)")
        header = entity.canonical + " (" + ", ".join(descr) + ")"

        lines = [header]
        for node in reversed(timeline[-max_events:]):
            when = _parse_ts(node.occurred_at) or node.memory_at or node.gmt_created
            stamp = when.strftime("%Y-%m-%d %H:%M") if when else "time unknown"
            summary = (node.content or "").strip() or "one interaction"
            lines.append(f"- {stamp} {summary}")
        if not timeline:
            lines.append("- (No interaction details recorded yet.)")
        return "\n".join(lines)


def _dedup_aliases(aliases: Iterable[str]) -> list[str]:
    """Deduplicate aliases by normalized key while preserving source order."""
    seen: set[str] = set()
    out: list[str] = []
    for a in aliases:
        text = (a or "").strip()
        key = _norm(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out
