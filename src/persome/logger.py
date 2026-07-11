"""Logging setup — three separate sinks (writer / compact / capture) + console.

File sinks emit structured JSON lines (one object per line, ``jq .``-parseable)
so the diagnostic bundle (#168) and downstream tooling can index logs by field.
Each line carries ``timestamp`` (ISO8601), ``level``, ``logger``, ``message``,
``trace_id`` (#169) and an ``extra`` object for any caller-supplied fields.

Set ``DEBUG=1`` (or pass ``verbose=True``) to fall back to the previous
human-readable single-line format on every sink — handy when tailing a log by
eye during development. Console output is always human-readable.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

from . import paths
from .trace import get_trace_id

_INITIALIZED = False
_HUMAN_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(trace_prefix)s%(message)s"

# LogRecord attributes that are always present; anything else a caller passed via
# ``logger.info(..., extra={...})`` is hoisted into the JSON line's ``extra`` map.
_RESERVED_RECORD_KEYS = frozenset(
    logging.makeLogRecord({}).__dict__.keys() | {"message", "asctime", "trace_prefix", "trace_id"}
)


class _TraceFilter(logging.Filter):
    """Attach the request-scoped trace id to every record.

    Sets both ``trace_id`` (consumed by :class:`JsonFormatter`) and
    ``trace_prefix`` (the ``[trace=<id>] `` string used by the human format).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        tid = get_trace_id()
        record.trace_id = tid
        record.trace_prefix = f"[trace={tid}] " if tid else ""
        return True


class JsonFormatter(logging.Formatter):
    """Render a :class:`logging.LogRecord` as a single JSON line.

    Field set is kept aligned with the Flutter ``AppLogLine`` so daemon and app
    lines share one schema across the diagnostic bundle:

        {"timestamp": "...", "level": "INFO", "logger": "...",
         "message": "...", "trace_id": "...", "extra": {...}}
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": _dt.datetime.fromtimestamp(record.created, tz=_dt.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", "") or "",
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        extra = {k: v for k, v in record.__dict__.items() if k not in _RESERVED_RECORD_KEYS}
        if extra:
            payload["extra"] = extra
        return json.dumps(payload, ensure_ascii=False, default=str)


def _human_mode() -> bool:
    """DEBUG=1 (or any truthy DEBUG) flips file sinks back to readable text."""
    return os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes", "on")


def _file_formatter(*, human: bool) -> logging.Formatter:
    return logging.Formatter(_HUMAN_FORMAT) if human else JsonFormatter()


_trace_filter = _TraceFilter()


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Rotating handler that enforces 0600 on every newly opened base file."""

    def _open(self) -> TextIO:
        return paths.open_private_append_text(
            Path(self.baseFilename),
            encoding=self.encoding,
            errors=self.errors,
        )


def _sink(
    name: str, filename: str, *, level: int = logging.INFO, human: bool = False
) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    log_path = paths.logs_dir() / filename
    fh = _PrivateRotatingFileHandler(
        log_path,
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(_file_formatter(human=human))
    fh.addFilter(_trace_filter)
    logger.addHandler(fh)
    logger.propagate = False
    return logger


def _remove_stale_chat_logs() -> None:
    """Drop ``chat.log*`` left behind by the removed Chat feature's sink.

    No sink writes there anymore, so rotation would never reclaim these files;
    they are personal data and must not linger on upgraded installs."""
    for stale in paths.logs_dir().glob("chat.log*"):
        with contextlib.suppress(OSError):
            stale.unlink()


def setup(*, console: bool = True, verbose: bool = False) -> None:
    global _INITIALIZED
    if _INITIALIZED:
        return
    paths.ensure_dirs()
    _remove_stale_chat_logs()

    level = logging.DEBUG if verbose else logging.INFO
    # File sinks emit JSON by default; DEBUG=1 or --verbose makes them readable.
    human_files = verbose or _human_mode()

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    if console:
        # Console is a developer affordance — always human-readable.
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter(_HUMAN_FORMAT))
        sh.addFilter(_trace_filter)
        root.addHandler(sh)

    _sink("persome.writer", "writer.log", level=level, human=human_files)
    _sink("persome.compact", "compact.log", level=level, human=human_files)
    _sink("persome.capture", "capture.log", level=level, human=human_files)
    _sink("persome.timeline", "timeline.log", level=level, human=human_files)
    _sink("persome.session", "session.log", level=level, human=human_files)
    _sink("persome.daemon", "daemon.log", level=level, human=human_files)
    _sink("persome.mcp", "daemon.log", level=level, human=human_files)
    # ``persome.api.access`` covers the REST 4xx/5xx access trail written by
    # the access-log middleware in api/__init__.py.
    _sink("persome.api.access", "api.log", level=level, human=human_files)

    _INITIALIZED = True


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
