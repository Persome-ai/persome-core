"""Timestamp helpers for current and legacy capture-buffer records.

Capture IDs are filename-safe ISO timestamps. Current writers emit fixed-width
UTC values, while existing installations can still contain aware-local, naive,
or raw negative-offset filenames. Parse to an aware UTC instant before any
ordering or retention decision so upgrades do not fall back to lexical time.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

_STEM_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}|\d{8})[T ]"
    r"(?P<time>(?:\d{2}-\d{2}-\d{2}|\d{6})(?:[.,]\d{1,6})?)"
    r"(?:(?P<marker>[pm])(?P<marked_hour>\d{2})-?(?P<marked_minute>\d{2})"
    r"|-(?P<legacy_hour>\d{2})-?(?P<legacy_minute>\d{2})"
    r"|(?P<zulu>Z))?$"
)
_TIMESTAMP_FIELD_RE = re.compile(rb'"timestamp"\s*:\s*"(?P<value>[^"\\]{1,256})"')
_TIMESTAMP_HEAD_BYTES = 16 * 1024


def parse_capture_timestamp(value: str) -> datetime | None:
    """Parse an ISO capture timestamp and return its UTC instant.

    Very old trusted-ingest clients could submit a naive ISO value. Interpret
    those with the machine's local offset, matching the historical runtime
    behavior, then make the result aware so mixed rows remain comparable.
    """
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            # astimezone() asks the OS to resolve the local UTC offset for this
            # historical date (including DST). Replacing with today's fixed
            # offset can shift an old winter/summer record by one hour.
            parsed = parsed.astimezone()
        return parsed.astimezone(UTC)
    except (OverflowError, TypeError, ValueError):
        return None


def capture_timestamp_epoch(value: object) -> float | None:
    """SQLite-safe epoch conversion for every historically accepted ISO form."""
    if not isinstance(value, str):
        return None
    parsed = parse_capture_timestamp(value)
    if parsed is None:
        return None
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def _stem_iso_value(match: re.Match[str]) -> str:
    raw_time = match.group("time")
    time_part = raw_time.replace("-", ":") if "-" in raw_time[:8] else raw_time
    marker = match.group("marker")
    if marker is not None:
        sign = "+" if marker == "p" else "-"
        suffix = f"{sign}{match.group('marked_hour')}:{match.group('marked_minute')}"
    elif match.group("legacy_hour") is not None:
        suffix = f"-{match.group('legacy_hour')}:{match.group('legacy_minute')}"
    elif match.group("zulu") is not None:
        suffix = "Z"
    else:
        suffix = ""
    return f"{match.group('date')}T{time_part}{suffix}"


def parse_capture_stem(stem: str) -> datetime | None:
    """Invert capture filename sanitization and return an aware UTC instant.

    Supported forms include the current ``...p00-00`` form, the documented
    ``p``/``m`` offset markers, legacy raw negative offsets, and old naive or
    ``Z`` timestamps.
    """
    match = _STEM_RE.fullmatch(stem)
    return parse_capture_timestamp(_stem_iso_value(match)) if match is not None else None


def capture_timestamp_is_ambiguous_local_time(value: str) -> bool:
    """Whether a naive ISO value maps to two local offsets at a DST edge."""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return False
        fold_zero = parsed.replace(fold=0).astimezone()
        fold_one = parsed.replace(fold=1).astimezone()
        return fold_zero.utcoffset() != fold_one.utcoffset()
    except (OSError, OverflowError, TypeError, ValueError):
        return True


def _capture_path_timestamp_value(path: Path) -> str | None:
    match = _STEM_RE.fullmatch(path.stem)
    if match is not None:
        return _stem_iso_value(match)
    # Old ingest accepted Python ISO forms whose filename sanitization is not
    # fully reversible (week dates, arbitrary separators, offset seconds).
    # `_write_capture` serialized timestamp first; inspect only a bounded head
    # so one malformed local file cannot create an unbounded read.
    try:
        with path.open("rb") as handle:
            head = handle.read(_TIMESTAMP_HEAD_BYTES)
    except OSError:
        return None
    field = _TIMESTAMP_FIELD_RE.search(head)
    if field is None:
        return None
    try:
        return field.group("value").decode("utf-8")
    except UnicodeDecodeError:
        return None


def parse_capture_path_timestamp(path: Path) -> datetime | None:
    """Parse a capture path, falling back to its bounded JSON timestamp field."""
    value = _capture_path_timestamp_value(path)
    return parse_capture_timestamp(value) if value is not None else None


def capture_path_has_ambiguous_local_time(path: Path) -> bool:
    """Whether a path's recoverable timestamp is an ambiguous naive local time."""
    value = _capture_path_timestamp_value(path)
    return capture_timestamp_is_ambiguous_local_time(value) if value is not None else False


def newest_capture_path(candidates: Iterable[Path]) -> Path | None:
    """Return the path with the latest parseable capture instant.

    Malformed names never outrank valid personal-data records. If every name is
    malformed, retain the historical deterministic filename fallback so status
    reporting can still expose a damaged buffer for diagnosis.
    """
    paths = list(candidates)
    timestamped = [
        (timestamp, path.name, path)
        for path in paths
        if (timestamp := parse_capture_path_timestamp(path)) is not None
    ]
    if timestamped:
        return max(timestamped)[2]
    return max(paths, key=lambda path: path.name, default=None)
