"""Transcription storage — SQLite with LIKE-based keyword search."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path

from .transcript import Transcript


class TranscriptStore:
    """Stores raw ASR transcriptions and supports keyword search."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def save(self, transcript: Transcript) -> int:
        cur = self._conn.execute(
            "INSERT INTO transcripts (timestamp, source, text, sentence_id) VALUES (?, ?, ?, ?)",
            (transcript.timestamp, transcript.source, transcript.text, transcript.sentence_id),
        )
        self._conn.commit()
        return cur.lastrowid or 0

    def get_recent(self, seconds: float = 30.0, source: str | None = None) -> list[dict]:
        """Get transcriptions from the last N seconds."""
        cutoff = time.time() - seconds
        if source:
            rows = self._conn.execute(
                "SELECT timestamp, source, text FROM transcripts "
                "WHERE timestamp > ? AND source = ? ORDER BY timestamp",
                (cutoff, source),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT timestamp, source, text FROM transcripts "
                "WHERE timestamp > ? ORDER BY timestamp",
                (cutoff,),
            ).fetchall()
        return [{"timestamp": r[0], "source": r[1], "text": r[2]} for r in rows]

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Substring search across all transcriptions."""
        rows = self._conn.execute(
            "SELECT timestamp, source, text FROM transcripts "
            "WHERE text LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [{"timestamp": r[0], "source": r[1], "text": r[2]} for r in rows]

    def get_recent_pushes(self, limit: int = 5) -> list[str]:
        """Get recent AI push messages (stored separately)."""
        rows = self._conn.execute(
            "SELECT text FROM pushes ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]

    def save_push(self, text: str) -> None:
        """Record a push message for deduplication."""
        self._conn.execute(
            "INSERT INTO pushes (timestamp, text) VALUES (?, ?)",
            (time.time(), text),
        )
        self._conn.commit()

    def _init_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                source TEXT NOT NULL,
                text TEXT NOT NULL,
                sentence_id INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_transcripts_timestamp ON transcripts(timestamp);
            CREATE TABLE IF NOT EXISTS pushes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                text TEXT NOT NULL
            );
        """)
        self._conn.commit()

    def search_history(self, query: str, limit: int = 10) -> list[dict]:
        """Search across all historical meeting databases in ~/.persome/."""
        oc_root = Path.home() / ".persome"
        if not oc_root.exists():
            return []
        results: list[dict] = []
        for db_file in sorted(oc_root.glob("meeting_*.db"), reverse=True):
            if str(db_file) == self._db_path:
                continue
            try:
                conn = sqlite3.connect(str(db_file), check_same_thread=False)
                try:
                    rows = conn.execute(
                        "SELECT timestamp, source, text FROM transcripts "
                        "WHERE text LIKE ? ORDER BY timestamp DESC LIMIT ?",
                        (f"%{query}%", limit),
                    ).fetchall()
                finally:
                    conn.close()
                db_name = db_file.stem
                for r in rows:
                    results.append(
                        {"timestamp": r[0], "source": r[1], "text": r[2], "meeting": db_name}
                    )
            except Exception:
                continue
            if len(results) >= limit:
                break
        return results[:limit]

    def close(self) -> None:
        self._conn.close()


# ── Cross-meeting query helpers (read-only, no live store needed) ────────────
# These scan every ``meeting_*.db`` on disk so the chat agent can search past
# meetings even after the meeting-server has exited. Keyword search is LIKE
# substring (NOT FTS) — that's deliberate: FTS5's unicode61/trigram tokenizers
# can't match 2-char CJK substrings ("天气" inside "今天天气怎么样"), whereas
# LIKE matches any substring regardless of language.


def _meeting_db_root(root: Path | None) -> Path:
    return root or (Path.home() / ".persome")


def _parse_session_dt(db_path: Path) -> datetime | None:
    """A meeting's start time, parsed from ``meeting_YYYYMMDD_HHMMSS.db``."""
    raw = db_path.stem.replace("meeting_", "")
    try:
        return datetime.strptime(raw, "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _iso_to_epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def search_all_meetings(
    query: str | None = None,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    root: Path | None = None,
) -> list[dict]:
    """Keyword (LIKE substring) + time search across ALL ``meeting_*.db`` files.

    ``query`` is an optional case-insensitive substring — omit it to retrieve
    everything in the time window (pure time-based browsing). ``since`` /
    ``until`` are ISO8601 bounds matched against each line's timestamp. Rows
    come back newest-first, each tagged with its meeting (db stem) and the
    session start time.
    """
    oc_root = _meeting_db_root(root)
    if not oc_root.exists():
        return []
    since_epoch = _iso_to_epoch(since)
    until_epoch = _iso_to_epoch(until)

    clauses: list[str] = []
    args: list[str | float] = []
    if query:
        clauses.append("text LIKE ?")
        args.append(f"%{query}%")
    if since_epoch is not None:
        clauses.append("timestamp >= ?")
        args.append(since_epoch)
    if until_epoch is not None:
        clauses.append("timestamp <= ?")
        args.append(until_epoch)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    results: list[dict] = []
    for db_file in sorted(oc_root.glob("meeting_*.db"), reverse=True):
        dt = _parse_session_dt(db_file)
        started_at = dt.isoformat() if dt else db_file.stem
        try:
            conn = sqlite3.connect(str(db_file), check_same_thread=False)
            try:
                rows = conn.execute(
                    "SELECT timestamp, source, text FROM transcripts"
                    + where
                    + " ORDER BY timestamp DESC LIMIT ?",
                    (*args, limit),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            continue
        for r in rows:
            results.append(
                {
                    "meeting": db_file.stem,
                    "started_at": started_at,
                    "timestamp": datetime.fromtimestamp(r[0]).astimezone().isoformat(),
                    "source": r[1],
                    "text": r[2],
                }
            )
        if len(results) >= limit:
            break
    return results[:limit]


def list_meetings(
    *,
    since: str | None = None,
    until: str | None = None,
    root: Path | None = None,
) -> list[dict]:
    """List meeting sessions (one per ``meeting_*.db``), newest-first, with start
    time and line count. ``since`` / ``until`` filter by session start time."""
    oc_root = _meeting_db_root(root)
    if not oc_root.exists():
        return []
    since_epoch = _iso_to_epoch(since)
    until_epoch = _iso_to_epoch(until)

    sessions: list[dict] = []
    for db_file in sorted(oc_root.glob("meeting_*.db"), reverse=True):
        dt = _parse_session_dt(db_file)
        start_epoch = dt.timestamp() if dt else None
        if start_epoch is not None:
            if since_epoch is not None and start_epoch < since_epoch:
                continue
            if until_epoch is not None and start_epoch > until_epoch:
                continue
        try:
            conn = sqlite3.connect(str(db_file), check_same_thread=False)
            try:
                count = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
            finally:
                conn.close()
        except Exception:
            continue
        sessions.append(
            {
                "meeting": db_file.stem,
                "started_at": dt.isoformat() if dt else db_file.stem,
                "line_count": count,
            }
        )
    return sessions
