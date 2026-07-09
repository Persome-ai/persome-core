"""Browsing interests — AGGREGATES ONLY.

This is the most "懂你" collector and the most sensitive, so it is also the
strictest: it emits top-N domains and top-N search terms, and nothing else.
Raw history rows, full URLs (with path/query), and page titles are never
retained, never printed, and never sent to the LLM.

History DBs are locked while the browser runs, so we copy each to a temp file
and open it read-only.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .base import Signal, SkipCollector, collector, home, top_counts

# Chromium-family profiles share the same History schema (urls table).
_CHROMIUM = {
    "Chrome": "Library/Application Support/Google/Chrome/Default/History",
    "Arc": "Library/Application Support/Arc/User Data/Default/History",
    "Edge": "Library/Application Support/Microsoft Edge/Default/History",
    "Brave": "Library/Application Support/BraveSoftware/Brave-Browser/Default/History",
}
_SAFARI = "Library/Safari/History.db"

_SEARCH_HOSTS = {
    "google.com": "q",
    "www.google.com": "q",
    "bing.com": "q",
    "www.bing.com": "q",
    "duckduckgo.com": "q",
    "baidu.com": "wd",
    "www.baidu.com": "wd",
}

# Domains that are pure infrastructure / noise — drop from the interest signal.
_NOISE_DOMAINS = {"", "localhost", "127.0.0.1", "newtab", "extensions"}

# Pull only the most-visited rows from each history DB. Top-visited URLs
# dominate the domain/search aggregates anyway, so this keeps memory constant
# (a few thousand rows) regardless of total history size — a 100k-row history
# is aggregated by SQLite's index, not loaded into Python.
_ROW_LIMIT = 3000


def _domain(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _search_term(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    param = _SEARCH_HOSTS.get(parts.netloc.lower())
    if not param:
        return None
    vals = parse_qs(parts.query).get(param)
    if not vals:
        return None
    term = vals[0].strip()
    # Keep word-like terms; skip empties, pure numbers, and over-long blobs
    # (likely ids/tokens) so the interest signal stays clean.
    if 2 <= len(term) <= 60 and not term.isdigit():
        return term
    return None


def _query_chromium(db: Path) -> tuple[dict[str, int], dict[str, int]]:
    domains: dict[str, int] = {}
    searches: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "History"
        shutil.copy2(db, copy)
        uri = f"file:{copy}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        try:
            cur = conn.execute(
                "SELECT url, visit_count FROM urls ORDER BY visit_count DESC LIMIT ?",
                (_ROW_LIMIT,),
            )
            for url, visits in cur:
                v = int(visits or 0)
                dom = _domain(url)
                if dom and dom not in _NOISE_DOMAINS:
                    domains[dom] = domains.get(dom, 0) + max(v, 1)
                term = _search_term(url)
                if term:
                    searches[term] = searches.get(term, 0) + 1
        finally:
            conn.close()
    return domains, searches


def _query_safari(db: Path) -> dict[str, int]:
    domains: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "History.db"
        shutil.copy2(db, copy)
        uri = f"file:{copy}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        try:
            cur = conn.execute(
                "SELECT hi.url, hi.visit_count FROM history_items hi "
                "ORDER BY hi.visit_count DESC LIMIT ?",
                (_ROW_LIMIT,),
            )
            for url, visits in cur:
                dom = _domain(url)
                if dom and dom not in _NOISE_DOMAINS:
                    domains[dom] = domains.get(dom, 0) + max(int(visits or 0), 1)
        finally:
            conn.close()
    return domains


@collector("browser", "浏览兴趣", "interests")
def collect() -> list[Signal]:
    domains: dict[str, int] = {}
    searches: dict[str, int] = {}
    sources: list[str] = []

    for name, rel in _CHROMIUM.items():
        db = home() / rel
        if not db.exists():
            continue
        try:
            d, s = _query_chromium(db)
        except (sqlite3.Error, OSError):
            continue
        for k, v in d.items():
            domains[k] = domains.get(k, 0) + v
        for k, v in s.items():
            searches[k] = searches.get(k, 0) + v
        sources.append(name)

    safari = home() / _SAFARI
    if safari.exists():
        try:
            d = _query_safari(safari)
            for k, v in d.items():
                domains[k] = domains.get(k, 0) + v
            sources.append("Safari")
        except (sqlite3.Error, OSError):
            # Safari's DB needs Full Disk Access; a denial is expected, not an error.
            pass

    if not domains and not searches:
        raise SkipCollector("no readable browser history (浏览器开着或无权限)")

    signals: list[Signal] = []
    if domains:
        signals.append(Signal("常访问域名", top_counts(domains, 20), " · ".join(sources)))
    if searches:
        rows: list[dict[str, Any]] = top_counts(searches, 15)
        signals.append(Signal("搜索关键词", rows))

    return signals
