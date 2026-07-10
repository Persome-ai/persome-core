"Canonical identity normalization and alias resolution."

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

# Resolution layers, outermost-cheapest first. Values are stable API — the
# identity golden and future production counters key on them.
LAYER_EXACT = "exact"
LAYER_NORMALIZED = "normalized"
LAYER_ALIAS = "alias"
LAYER_HONORIFIC = "honorific"
LAYER_NONE = "none"


_HONORIFIC_SUFFIXES = (
    "\u603b",
    "\u8001\u5e08",
    "\u54e5",
    "\u59d0",
    "\u5de5",
    "\u8001\u677f",
    "\u7ecf\u7406",
    "\u540c\u5b66",
)
_FAMILIAR_PREFIXES = ("\u5c0f", "\u8001", "\u963f")


def norm(name: str) -> str:
    """NFKC + whitespace fold + casefold — byte-compatible with person_graph._norm."""
    folded = unicodedata.normalize("NFKC", name or "").strip()
    folded = " ".join(folded.split())
    return folded.casefold()


@dataclass(frozen=True)
class Resolution:
    canonical: str | None  # roster canonical name, or None (mint a candidate)
    layer: str  # which funnel layer matched (LAYER_*)

    @property
    def matched(self) -> bool:
        return self.canonical is not None


@dataclass
class Roster:
    canonicals: list[str] = field(default_factory=list)
    # normalized form -> canonical (covers canonicals AND aliases)
    _by_norm: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, entries: list[tuple[str, list[str]]]) -> Roster:
        """``entries`` = [(canonical, aliases), ...] — person_graph shape."""
        roster = cls()
        for canonical, aliases in entries:
            if not canonical:
                continue
            roster.canonicals.append(canonical)
            for name in (canonical, *aliases):
                key = norm(name)
                if key:
                    # first writer wins — a colliding alias must not silently
                    # re-point an existing identity (merge conservatively)
                    roster._by_norm.setdefault(key, canonical)
        return roster

    def __contains__(self, name: str) -> bool:
        return norm(name) in self._by_norm

    def __len__(self) -> int:
        return len(self.canonicals)


def load_roster(cfg, *, memory=None, limit: int | None = None) -> Roster:
    """Build the roster from person_graph (the consolidated past layer).

    Fail-open: any read error → empty roster (callers then mint candidates;
    nothing breaks). ``memory`` is an injectable EvoMemory for tests — the
    default reads the SAME default-user scope production writes.
    """
    try:
        from .engine import EvoMemory
        from .person_graph import PersonGraph

        persons = PersonGraph(memory or EvoMemory(), cfg=cfg).list_persons()
        if limit is not None:
            persons = persons[:limit]
        return Roster.build([(p.canonical, list(getattr(p, "aliases", []))) for p in persons])
    except Exception:  # noqa: BLE001 — the roster is best-effort by design
        return Roster()


def _strip_honorific(mention: str) -> str | None:
    for suffix in _HONORIFIC_SUFFIXES:
        if mention.endswith(suffix) and len(mention) > len(suffix):
            return mention[: -len(suffix)]
    for prefix in _FAMILIAR_PREFIXES:
        if mention.startswith(prefix) and len(mention) > len(prefix):
            return mention[len(prefix) :]
    return None


def resolve_identity(mention: str, roster: Roster) -> Resolution:
    """Resolve one mention through the layered funnel (§4.3).

    Never raises; never force-matches. ``Resolution(None, LAYER_NONE)`` means
    "unknown — mint a shadow candidate", which is a correct answer, not a
    failure.
    """
    raw = (mention or "").strip()
    if not raw:
        return Resolution(None, LAYER_NONE)

    # ① exact canonical (byte-for-byte — the LLM copied the roster line)
    if raw in roster.canonicals:
        return Resolution(raw, LAYER_EXACT)

    # ② NFKC-normalized canonical/alias (width/case/whitespace drift)
    key = norm(raw)
    canonical = roster._by_norm.get(key)
    if canonical is not None:
        layer = LAYER_NORMALIZED if key == norm(canonical) else LAYER_ALIAS
        return Resolution(canonical, layer)

    # ③ honorific/familiar stripping — trusted only on a UNIQUE prefix match
    stem = _strip_honorific(raw)
    if stem:
        stem_key = norm(stem)
        candidates = {c for k, c in roster._by_norm.items() if k.startswith(stem_key)}
        if len(candidates) == 1:
            return Resolution(next(iter(candidates)), LAYER_HONORIFIC)

    return Resolution(None, LAYER_NONE)


def scan_mentions(text: str, roster: Roster) -> list[str]:
    """§3.2 associative-Q entity slot — ZERO-LLM distillation of the present.

    Scan the (normalized) text for every roster canonical/alias as a substring
    and return the matched CANONICAL identities, deduped, ordered by first
    occurrence. This is the "weights arm perception" loop made literal: the
    bigger the graph's roster, the more the runtime can see. Substring matching
    is deliberate — Chinese runs tokenize as one FTS token, so word-boundary
    matching would go blind exactly where the entity head matters most.
    """
    hay = norm(text)
    if not hay:
        return []
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for key, canonical in roster._by_norm.items():
        if canonical in seen:
            continue
        pos = hay.find(key)
        if pos >= 0:
            found.append((pos, canonical))
            seen.add(canonical)
    return [canonical for _pos, canonical in sorted(found)]
