"""resolve_identity — the ONE identity-resolution funnel (memory-rebuild spec §4.3).

The seam between LLM free strings ("张总") and canonical graph identities
(person_graph canonical names). §4.3 discipline:

- **Single implementation**: every SURVIVING read/write path resolves mentions
  through THIS module — the consolidator's delta gates today, the associative
  Q construction (§3.2) and the MCP pull tomorrow. A fork is drift, and drift
  is a miss (§5 red line). The legacy ``relation_extractor`` keeps its local
  alias map on purpose: it is on the §6.4 retirement list — migrating doomed
  code is wasted motion.
- **Layered funnel, merge conservatively**: exact → NFKC-normalized → alias
  set → honorific stripping — each layer only matches when the candidate is
  UNIQUE (合并宁缺毋滥: wrongly merging two 张伟 poisons the graph; a miss is
  just a shadow candidate with a TTL). A failed resolution is the caller's cue
  to mint a candidate (候选宁滥毋缺), never to force-match.
- Layer labels ride the result so the seam oracle (§7-5: identity golden +
  production hit-rate/orphan counters) can attribute hits per layer and tune
  the funnel with data instead of hunches.

Deliberately NOT here yet (honest deferral, added when data demands):
- pinyin matching — needs a pypinyin dependency; no evidence yet the layer
  earns its weight;
- embedding fallback — reuses the semantic head's index once the §3 read path
  lands;
- alias write-back (miss = training data) — belongs to the delta APPLY path
  (Phase 1, when the consolidator becomes a real write path), not the gate.

Normalization matches ``person_graph._norm`` byte-for-byte (NFKC + whitespace
fold + casefold) so identities resolved here agree with what the graph stored.
"""

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

# Chinese address suffixes that wrap a surname/short name ("张总", "李老师",
# "王哥"); and the familiar prefixes ("小张", "老王"). Stripping is only
# trusted when the残 stem uniquely prefixes ONE roster identity.
_HONORIFIC_SUFFIXES = ("总", "老师", "哥", "姐", "工", "老板", "经理", "同学")
_FAMILIAR_PREFIXES = ("小", "老", "阿")


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
    """The known-identity menu (§4.1 选择题) with its lookup indexes."""

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
    """ "张总"→"张"、"小张"→"张" — the stem, or None when nothing was stripped."""
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
