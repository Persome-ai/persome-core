"""Auditable evidence and decisions for the reserved memory-owner identity.

The LLM may recognize that a visible name or handle belongs to the person whose
screen Persome observes.  That probabilistic judgment lands here as evidence;
it never writes directly into PersonGraph.  A deterministic promotion rule
turns repeated, independent evidence into an active alias of ``self``.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_REJECTED = "rejected"
STATUSES = frozenset({STATUS_PENDING, STATUS_ACTIVE, STATUS_REJECTED})

SOURCE_OWNED_ACCOUNT = "owned_account"
SOURCE_EXPLICIT_SELF = "explicit_self_identification"
SOURCE_USER_CORRECTION = "user_correction"
SOURCE_KINDS = frozenset({SOURCE_OWNED_ACCOUNT, SOURCE_EXPLICIT_SELF, SOURCE_USER_CORRECTION})

MIN_CANDIDATE_CONFIDENCE = 0.8
PROMOTION_SESSIONS = 2
PENDING_RESERVATION_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_aliases (
    alias_key      TEXT PRIMARY KEY,
    alias          TEXT NOT NULL,
    status         TEXT NOT NULL,
    confidence     REAL NOT NULL,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    activated_at   TEXT,
    decision_source TEXT NOT NULL DEFAULT 'inferred'
);
CREATE INDEX IF NOT EXISTS ix_owner_aliases_status ON owner_aliases(status);

CREATE TABLE IF NOT EXISTS owner_alias_evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    alias_key   TEXT NOT NULL,
    alias       TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    quote       TEXT NOT NULL,
    confidence  REAL NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE(alias_key, session_id),
    FOREIGN KEY(alias_key) REFERENCES owner_aliases(alias_key)
);
CREATE INDEX IF NOT EXISTS ix_owner_alias_evidence_alias
    ON owner_alias_evidence(alias_key, created_at);
"""

_GENERIC_ALIASES = frozenset(
    {
        "self",
        "user",
        "the user",
        "owner",
        "memory owner",
        "me",
        "i",
        "\u6211",
        "\u672c\u4eba",
        "\u7528\u6237",
    }
)


@dataclass(frozen=True)
class OwnerAliasState:
    alias: str
    alias_key: str
    status: str
    confidence: float
    evidence_count: int
    activated_now: bool = False


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def norm(alias: str) -> str:
    folded = unicodedata.normalize("NFKC", alias or "").strip()
    return " ".join(folded.split()).casefold()


def clean_alias(alias: str) -> str | None:
    value = unicodedata.normalize("NFKC", str(alias or "")).strip()
    value = " ".join(value.split())
    key = norm(value)
    if not value or key in _GENERIC_ALIASES or len(value) > 160:
        return None
    if not any(ch.isalnum() for ch in value):
        return None
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_state(row: sqlite3.Row | tuple, *, activated_now: bool = False) -> OwnerAliasState:
    return OwnerAliasState(
        alias=str(row[1]),
        alias_key=str(row[0]),
        status=str(row[2]),
        confidence=float(row[3]),
        evidence_count=int(row[4]),
        activated_now=activated_now,
    )


def get(conn: sqlite3.Connection, alias: str) -> OwnerAliasState | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT alias_key, alias, status, confidence, evidence_count "
        "FROM owner_aliases WHERE alias_key=?",
        (norm(alias),),
    ).fetchone()
    return _row_state(row) if row else None


def list_aliases(conn: sqlite3.Connection, *, statuses: set[str]) -> list[str]:
    ensure_schema(conn)
    wanted = sorted(statuses & STATUSES)
    if not wanted:
        return []
    placeholders = ",".join("?" for _ in wanted)
    params: list[str] = list(wanted)
    recent = ""
    if statuses == {STATUS_PENDING}:
        recent = " AND last_seen_at>=?"
        params.append((datetime.now(UTC) - timedelta(days=PENDING_RESERVATION_DAYS)).isoformat())
    rows = conn.execute(
        f"SELECT alias FROM owner_aliases WHERE status IN ({placeholders}){recent} "
        "ORDER BY first_seen_at, alias_key",
        params,
    ).fetchall()
    return [str(row[0]) for row in rows]


def _explicit_self_quote(alias: str, quote: str) -> bool:
    key = norm(alias)
    hay = norm(quote)
    if not key or key not in hay:
        return False
    prefixes = (
        "i am ",
        "i'm ",
        "i’m ",
        "my name is ",
        "my github is ",
        "my account is ",
        "my handle is ",
        "\u6211\u662f",
        "\u6211\u53eb",
        "\u672c\u4eba\u662f",
        "\u6211\u7684github\u662f",
        "\u6211\u7684\u8d26\u53f7\u662f",
        "\u6211\u7684\u8d26\u6237\u662f",
        "\u6211\u7684\u7528\u6237\u540d\u662f",
        "\u6211\u7684\u6635\u79f0\u662f",
    )
    terminators = frozenset(
        " \t\r\n,.;:!?)]}\u3002\uff0c\uff1b\uff1a\uff01\uff1f\uff09\u3011\u300b"
    )
    for prefix in prefixes:
        candidate = prefix + key
        start = hay.find(candidate)
        if start < 0:
            continue
        end = start + len(candidate)
        if end == len(hay) or hay[end] in terminators:
            return True
    return False


def record_evidence(
    conn: sqlite3.Connection,
    *,
    alias: str,
    session_id: str,
    source_kind: str,
    quote: str,
    confidence: float,
) -> OwnerAliasState | None:
    """Record one independent-session observation and apply the promotion rule."""
    ensure_schema(conn)
    value = clean_alias(alias)
    source = str(source_kind or "")
    sid = str(session_id or "").strip()
    evidence = str(quote or "").strip()
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        return None
    if (
        value is None
        or not sid
        or not evidence
        or source not in SOURCE_KINDS
        or not 0.0 <= conf <= 1.0
        or conf < MIN_CANDIDATE_CONFIDENCE
    ):
        return None

    key = norm(value)
    now = _now()
    existing = get(conn, value)
    if existing is not None and existing.status == STATUS_REJECTED:
        return existing

    if existing is None:
        conn.execute(
            "INSERT INTO owner_aliases"
            " (alias_key, alias, status, confidence, evidence_count, first_seen_at,"
            " last_seen_at, activated_at, decision_source)"
            " VALUES (?, ?, ?, ?, 0, ?, ?, NULL, 'inferred')",
            (key, value, STATUS_PENDING, conf, now, now),
        )

    conn.execute(
        "INSERT INTO owner_alias_evidence"
        " (alias_key, alias, session_id, source_kind, quote, confidence, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(alias_key, session_id) DO UPDATE SET"
        " alias=CASE WHEN excluded.confidence > confidence THEN excluded.alias ELSE alias END,"
        " source_kind=CASE WHEN excluded.confidence > confidence THEN excluded.source_kind"
        " ELSE source_kind END,"
        " quote=CASE WHEN excluded.confidence > confidence THEN excluded.quote ELSE quote END,"
        " confidence=MAX(confidence, excluded.confidence)",
        (key, value, sid, source, evidence, conf, now),
    )
    count, max_conf = conn.execute(
        "SELECT COUNT(*), MAX(confidence) FROM owner_alias_evidence WHERE alias_key=?",
        (key,),
    ).fetchone()
    current = get(conn, value)
    assert current is not None
    explicit = source == SOURCE_USER_CORRECTION or (
        source == SOURCE_EXPLICIT_SELF and conf >= 0.9 and _explicit_self_quote(value, evidence)
    )
    active = current.status == STATUS_ACTIVE or explicit or int(count) >= PROMOTION_SESSIONS
    activated_now = active and current.status != STATUS_ACTIVE
    conn.execute(
        "UPDATE owner_aliases SET alias=?, status=?, confidence=?, evidence_count=?,"
        " last_seen_at=?, activated_at=CASE WHEN ? THEN COALESCE(activated_at, ?)"
        " ELSE activated_at END, decision_source=CASE WHEN ? THEN ? ELSE decision_source END"
        " WHERE alias_key=?",
        (
            value,
            STATUS_ACTIVE if active else STATUS_PENDING,
            float(max_conf or conf),
            int(count),
            now,
            1 if active else 0,
            now,
            1 if activated_now else 0,
            "user" if source == SOURCE_USER_CORRECTION else "inferred",
            key,
        ),
    )
    updated = get(conn, value)
    assert updated is not None
    return OwnerAliasState(**{**updated.__dict__, "activated_now": activated_now})


def reject(conn: sqlite3.Connection, alias: str, *, source: str = "user") -> OwnerAliasState | None:
    ensure_schema(conn)
    value = clean_alias(alias)
    if value is None:
        return None
    key = norm(value)
    now = _now()
    conn.execute(
        "INSERT INTO owner_aliases"
        " (alias_key, alias, status, confidence, evidence_count, first_seen_at, last_seen_at,"
        " activated_at, decision_source) VALUES (?, ?, ?, 1.0, 0, ?, ?, NULL, ?)"
        " ON CONFLICT(alias_key) DO UPDATE SET status=excluded.status,"
        " last_seen_at=excluded.last_seen_at, decision_source=excluded.decision_source",
        (key, value, STATUS_REJECTED, now, now, source),
    )
    return get(conn, value)
