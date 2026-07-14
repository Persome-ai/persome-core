"""Persistence for Face, Volume, and Root personal-model structures.

The graph geometry and ``schema-*.md`` files are two projections of one object:

    Schema = members x signature x provenance x evidence x validity x level

Two extractors feed the SAME row:
- **mined** — the D2 schema miner (signature route: it induces the central
  proposition; its fact bundle is the footprint);
- **emergent** — the enrichment clustering / cross-domain sweeper (footprint
  route: it clusters members; its summary is the signature).

Footprint-Jaccard / normalized-signature matching folds the two contributions
onto one face; a face both extractors reached escalates provenance to
``both`` is the two-signal requirement for promotion into resident memory.

**Resampling gate:** every re-mine or re-cluster
is a natural evidence resample (a different day's facts = a different
subsample). The face keeps its last N footprint snapshots; promotion requires
the min pairwise Jaccard across snapshots ≥ the stability threshold — a
cluster whose membership churns between resamples is not a regularity yet and
stays shadow. No synthetic subsampling, no randomness: the calendar does the
resampling.

Rows are bitemporal + append-friendly like ``relation_edges``: ``created_at``
is the transaction clock, ``valid_from``/``valid_to`` the validity clock;
demotion closes validity, never deletes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime

from ..evomem.models import MemoryStatus
from ..logger import get

logger = get("persome.store.schema_faces")

PROVENANCE_MINED = "mined"
PROVENANCE_EMERGENT = "emergent"
PROVENANCE_BOTH = "both"

# footprint-Jaccard floor for "same face" folding (merge conservatively —
# below it, a new face is born instead of polluting an existing one)
MATCH_JACCARD = 0.5
# promotion gate: min pairwise Jaccard across the kept footprint snapshots
STABILITY_THRESHOLD = 0.6
FOOTPRINT_HISTORY_KEEP = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_faces (
    face_id      TEXT PRIMARY KEY,
    level        INTEGER NOT NULL DEFAULT 1,   -- 1=Face, 2=Volume, 3=Root
    parent_face  TEXT,                          -- Face-to-Volume or Volume-to-Root rollup
    signature    TEXT NOT NULL DEFAULT '',      -- central proposition in Markdown projection
    members      TEXT NOT NULL DEFAULT '[]',    -- latest member-key snapshot as JSON
    footprints   TEXT NOT NULL DEFAULT '[]',    -- recent member snapshots for stability gate
    provenance   TEXT NOT NULL,                 -- mined | emergent | both
    observations INTEGER NOT NULL DEFAULT 1,    -- monotone evidence count
    confidence   REAL NOT NULL DEFAULT 0.5,     -- monotone maximum
    status       TEXT NOT NULL,                 -- MemoryStatus: shadow|active|superseded|archived
    valid_from   TEXT NOT NULL,
    valid_to     TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_faces_status ON schema_faces(status, level);
CREATE TABLE IF NOT EXISTS cross_domain_probe_state (
    pair_key       TEXT PRIMARY KEY,
    last_probed_at TEXT NOT NULL,
    probe_count    INTEGER NOT NULL DEFAULT 1,
    detected       INTEGER NOT NULL DEFAULT 0  -- last result was stable/promotable
);
CREATE INDEX IF NOT EXISTS ix_cross_domain_probe_age
    ON cross_domain_probe_state(last_probed_at, pair_key);
"""


# Columns added after first ship — backfilled onto existing tables (the
# relation_edges _EXTRA_COLUMNS pattern).
_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    # ABOUT — the mined source file's entity + roster mentions in the signature.
    # Footprint members are FACT-level hashes; anchors are the honest
    # entity-level projection that lets the face render as a hull over its
    # subjects instead of a floating plate.
    ("anchors", "TEXT NOT NULL DEFAULT '[]'"),
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    from . import fts

    if fts.is_client_process():
        return
    conn.executescript(SCHEMA)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(schema_faces)")}
    for name, decl in _EXTRA_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE schema_faces ADD COLUMN {name} {decl}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _norm_sig(signature: str) -> str:
    folded = unicodedata.normalize("NFKC", signature or "").strip()
    return " ".join(folded.split()).casefold()


def member_key(fact_body: str) -> str:
    """Stable member key for a fact body — footprint identity survives re-mines
    of unchanged facts without storing the (possibly long) body itself."""
    return hashlib.sha1(_norm_sig(fact_body).encode()).hexdigest()[:16]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def stability(footprints: list[list[str]]) -> float:
    """Min pairwise Jaccard across kept snapshots — the resampling gate's
    reading (1.0 when fewer than two snapshots exist yet ≠ promotable; the
    caller also requires ≥2 snapshots so one sighting can't self-certify)."""
    sets = [set(fp) for fp in footprints if fp]
    if len(sets) < 2:
        return 1.0
    worst = 1.0
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            worst = min(worst, _jaccard(sets[i], sets[j]))
    return worst


def _row_face(conn: sqlite3.Connection, face_id: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM schema_faces WHERE face_id = ?", (face_id,)).fetchone()


def _find_match(
    conn: sqlite3.Connection, *, signature: str, members: set[str], level: int
) -> sqlite3.Row | None:
    """Same-face detection: normalized-signature equality OR footprint Jaccard
    ≥ MATCH_JACCARD against any live face at the same level."""
    conn.row_factory = sqlite3.Row
    sig = _norm_sig(signature)
    for row in conn.execute(
        "SELECT * FROM schema_faces WHERE valid_to IS NULL AND level = ?", (level,)
    ).fetchall():
        if sig and _norm_sig(row["signature"]) == sig:
            return row
        if _jaccard(members, set(json.loads(row["members"]))) >= MATCH_JACCARD:
            return row
    return None


def record_face(
    conn: sqlite3.Connection,
    *,
    source: str,
    signature: str,
    members: Iterable[str],
    confidence: float = 0.5,
    level: int = 1,
    parent_face: str | None = None,
    anchors: list[str] | None = None,
) -> str:
    """One extractor's contribution lands on the ONE unified object.

    ``source`` ∈ {mined, emergent}. New face → born shadow with that
    provenance. Matched face → footprint snapshot appended (capped), members
    refreshed to the latest snapshot, observations+1, confidence MAX-ratchet,
    and — when the OTHER extractor reached it — provenance escalates to
    ``both`` (the two-signal bar). Returns the face_id. Fail-open is the
    CALLER's contract (wrap in try at the tick).

    **Signal-only contribution** (empty ``members``): counts as evidence
    (observations + provenance escalation + confidence ratchet) but NEVER
    touches members/footprints — an extractor that confirms a regularity
    without re-deriving its membership (e.g. the sweeper vouching for a parent
    schema) must not corrupt the resampling gate's footprint history with an
    empty or foreign-keyed snapshot.
    """
    assert source in (PROVENANCE_MINED, PROVENANCE_EMERGENT)
    ensure_schema(conn)
    member_set = {str(m) for m in members if str(m).strip()}
    anchor_set = {str(a) for a in (anchors or []) if str(a).strip()}
    now = _now()
    existing = _find_match(conn, signature=signature, members=member_set, level=level)
    if existing is None:
        face_id = f"face-{hashlib.sha1((_norm_sig(signature) + now).encode()).hexdigest()[:12]}"
        conn.execute(
            "INSERT INTO schema_faces (face_id, level, parent_face, signature, members,"
            " footprints, provenance, observations, confidence, status, valid_from, created_at,"
            " anchors)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
            (
                face_id,
                level,
                parent_face,
                signature,
                json.dumps(sorted(member_set)),
                json.dumps([sorted(member_set)] if member_set else []),
                source,
                float(confidence),
                MemoryStatus.SHADOW.value,
                now,
                now,
                json.dumps(sorted(anchor_set), ensure_ascii=False),
            ),
        )
        return face_id

    footprints = json.loads(existing["footprints"])
    members_json = existing["members"]
    if member_set:  # signal-only contributions leave the footprint history alone
        footprints.append(sorted(member_set))
        footprints = footprints[-FOOTPRINT_HISTORY_KEEP:]
        members_json = json.dumps(sorted(member_set))
    provenance = existing["provenance"]
    if provenance != PROVENANCE_BOTH and provenance != source:
        provenance = PROVENANCE_BOTH  # the other signal arrived — escalate
    try:
        merged_anchors = set(json.loads(existing["anchors"] or "[]")) | anchor_set
    except (TypeError, ValueError):
        merged_anchors = anchor_set
    conn.execute(
        "UPDATE schema_faces SET members = ?, footprints = ?, provenance = ?,"
        " observations = observations + 1, confidence = MAX(confidence, ?),"
        " signature = CASE WHEN ? != '' THEN ? ELSE signature END,"
        " anchors = ?"
        " WHERE face_id = ?",
        (
            members_json,
            json.dumps(footprints),
            provenance,
            float(confidence),
            signature.strip(),
            signature,
            json.dumps(sorted(merged_anchors), ensure_ascii=False),
            existing["face_id"],
        ),
    )
    return existing["face_id"]


def maybe_promote(
    conn: sqlite3.Connection,
    face_id: str,
    *,
    min_observations: int = 2,
    stability_threshold: float = STABILITY_THRESHOLD,
) -> bool:
    """§3.1 promotion gate for Face/Volume residency.

    Level 1 Face requires two extractor signals (``provenance=both``) plus stable
    resampling. Level 2 Volume has one honest producer, the cross-domain sweeper,
    so its independent evidence is two stable sweeper resamples; requiring
    ``provenance=both`` there made every production Volume permanently shadow.
    Idempotent; returns True iff the object is ACTIVE after the call.
    """
    row = _row_face(conn, face_id)
    if row is None or row["valid_to"] is not None:
        return False
    if row["status"] == MemoryStatus.ACTIVE.value:
        return True
    footprints = json.loads(row["footprints"])
    provenance_ready = row["provenance"] == PROVENANCE_BOTH or (
        row["level"] == 2 and row["provenance"] == PROVENANCE_EMERGENT
    )
    if (
        provenance_ready
        and row["observations"] >= min_observations
        and len(footprints) >= 2
        and stability(footprints) >= stability_threshold
    ):
        conn.execute(
            "UPDATE schema_faces SET status = ? WHERE face_id = ?",
            (MemoryStatus.ACTIVE.value, face_id),
        )
        return True
    return False


def resident_faces(conn: sqlite3.Connection, *, top_k: int = 5) -> list[sqlite3.Row]:
    """The §3.1 residency selection: ACTIVE (= promoted, both-provenance) faces,
    strongest first, capped — the O(1) tower-top block the system prompt holds.
    The level-3 root apex is excluded: it is its own resident block
    (``resident_root``/``render_root``), and a live root satisfies the
    ACTIVE∧live predicate — without the level filter it would be delivered
    twice and mislabeled as a behavior regularity by ``render_residency``."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    return list(
        conn.execute(
            "SELECT * FROM schema_faces WHERE status = ? AND valid_to IS NULL"
            " AND level != ?"
            " ORDER BY observations DESC, confidence DESC, face_id LIMIT ?",
            (MemoryStatus.ACTIVE.value, ROOT_LEVEL, top_k),
        )
    )


def render_residency(faces: list[sqlite3.Row]) -> str:
    """Render resident Face signatures only; members remain retrievable.

    The tower
    top is a budgeted digest, members stay retrievable, not resident."""
    if not faces:
        return ""
    lines = ["[Resident behavior patterns]"]
    for f in faces:
        lines.append(
            f"- {f['signature']}  [evidence {f['observations']} · confidence {f['confidence']:.2f}]"
        )
    return "\n".join(lines)


ROOT_LEVEL = 3
_ROOT_PROVENANCE = "synth"


def resident_root(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Return the single live level-3 Root, the only always-resident apex.
    None when no root has been synthesized yet (cold start → caller falls back to
    resident_faces). Singleton is an invariant (see upsert_root); LIMIT 1 is defensive."""
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM schema_faces WHERE level = ? AND status = ? AND valid_to IS NULL"
        " ORDER BY created_at DESC LIMIT 1",
        (ROOT_LEVEL, MemoryStatus.ACTIVE.value),
    ).fetchone()


def render_root(row: sqlite3.Row | None) -> str:
    """Render the resident root block. root's synthesized apex narrative lives in
    ``signature`` (token-capped at production). Empty string when no root."""
    if row is None:
        return ""
    text = (row["signature"] or "").strip()
    return "[Root: identity and durable priorities]\n" + text if text else ""


def upsert_root(
    conn: sqlite3.Connection,
    *,
    signature: str,
    members: Iterable[str],
    anchors: Iterable[str] | None = None,
    confidence: float = 1.0,
) -> str:
    """Write a fresh root as the SINGLETON level-3 apex, chain-superseding any prior
    live root (close its validity + mark superseded; the new row is born ACTIVE per
    the default-ON ruling). ``signature`` = the synthesized apex narrative (the caller
    has already token-capped it); ``members`` are the level-2 Volume IDs this apex fuses;
    ``anchors`` are entity handles named by the narrative for progressive disclosure.
    Returns the new root's face_id. observations carries forward (+1 = one more nightly
    resample of "this is the apex")."""
    ensure_schema(conn)
    now = _now()
    prior = conn.execute(
        "SELECT observations FROM schema_faces WHERE level = ? AND valid_to IS NULL",
        (ROOT_LEVEL,),
    ).fetchone()
    prior_obs = int(prior[0]) if prior else 0
    # Close every prior live root (singleton — normally ≤1, but close all defensively).
    conn.execute(
        "UPDATE schema_faces SET valid_to = ?, status = ? WHERE level = ? AND valid_to IS NULL",
        (now, MemoryStatus.SUPERSEDED.value, ROOT_LEVEL),
    )
    member_set = sorted({str(m) for m in members if str(m).strip()})
    anchor_set = sorted({str(a) for a in (anchors or []) if str(a).strip()})
    face_id = f"root-{hashlib.sha1((now + signature).encode()).hexdigest()[:12]}"
    conn.execute(
        "INSERT INTO schema_faces (face_id, level, parent_face, signature, members, footprints,"
        " provenance, observations, confidence, status, valid_from, created_at, anchors)"
        " VALUES (?, ?, NULL, ?, ?, '[]', ?, ?, ?, ?, ?, ?, ?)",
        (
            face_id,
            ROOT_LEVEL,
            signature.strip(),
            json.dumps(member_set),
            _ROOT_PROVENANCE,
            prior_obs + 1,
            float(confidence),
            MemoryStatus.ACTIVE.value,
            now,
            now,
            json.dumps(anchor_set, ensure_ascii=False),
        ),
    )
    return face_id
