"""Structured JSON-line logging (#192)."""

from __future__ import annotations

import json
import logging

from persome import logger as logger_mod
from persome import trace


def _record(level: int, msg: str, *, name: str = "persome.test") -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


def test_json_formatter_emits_jq_parseable_line_with_field_set() -> None:
    fmt = logger_mod.JsonFormatter()
    rec = _record(logging.INFO, "hello world")
    rec.trace_id = ""

    line = fmt.format(rec)
    obj = json.loads(line)  # jq-parseable == valid JSON

    assert set(obj) >= {"timestamp", "level", "logger", "message", "trace_id"}
    assert obj["level"] == "INFO"
    assert obj["logger"] == "persome.test"
    assert obj["message"] == "hello world"
    assert obj["trace_id"] == ""
    # timestamp is ISO8601 — parses without raising.
    import datetime as _dt

    _dt.datetime.fromisoformat(obj["timestamp"])


def test_json_formatter_carries_trace_id() -> None:
    fmt = logger_mod.JsonFormatter()
    rec = _record(logging.INFO, "POST /chat")
    rec.trace_id = "ab12cd34ef56"

    obj = json.loads(fmt.format(rec))

    assert obj["trace_id"] == "ab12cd34ef56"


def test_json_formatter_maps_all_levels() -> None:
    fmt = logger_mod.JsonFormatter()
    for level, name in [
        (logging.DEBUG, "DEBUG"),
        (logging.INFO, "INFO"),
        (logging.WARNING, "WARNING"),
        (logging.ERROR, "ERROR"),
    ]:
        rec = _record(level, "x")
        rec.trace_id = ""
        assert json.loads(fmt.format(rec))["level"] == name


def test_json_formatter_hoists_extra_fields() -> None:
    fmt = logger_mod.JsonFormatter()
    rec = _record(logging.INFO, "with extra")
    rec.trace_id = ""
    rec.duration_ms = 42  # caller-supplied via logger.info(..., extra={...})

    obj = json.loads(fmt.format(rec))

    assert obj["extra"]["duration_ms"] == 42


def test_trace_filter_attaches_current_trace_id() -> None:
    flt = logger_mod._TraceFilter()
    trace.set_trace_id("deadbeef0000")
    try:
        rec = _record(logging.INFO, "x")
        assert flt.filter(rec) is True
        assert rec.trace_id == "deadbeef0000"
        assert rec.trace_prefix == "[trace=deadbeef0000] "
    finally:
        trace.set_trace_id("")


def test_setup_writes_json_lines_to_file(ac_root) -> None:
    # Fresh module state so setup() actually runs.
    logger_mod._INITIALIZED = False
    logging.getLogger().handlers.clear()
    for name in ("persome.daemon",):
        logging.getLogger(name).handlers.clear()

    logger_mod.setup(console=False)
    log = logger_mod.get("persome.daemon")
    log.info("daemon up")
    for h in log.handlers:
        h.flush()

    from persome import paths

    contents = (paths.logs_dir() / "daemon.log").read_text(encoding="utf-8").strip()
    assert contents, "expected at least one log line"
    last = contents.splitlines()[-1]
    obj = json.loads(last)  # jq-parseable
    assert obj["message"] == "daemon up"
    assert obj["logger"] == "persome.daemon"
    assert obj["level"] == "INFO"
    assert "trace_id" in obj


def test_setup_debug_env_falls_back_to_human_format(ac_root, monkeypatch) -> None:
    monkeypatch.setenv("DEBUG", "1")
    logger_mod._INITIALIZED = False
    logging.getLogger().handlers.clear()
    logging.getLogger("persome.daemon").handlers.clear()

    logger_mod.setup(console=False)
    log = logger_mod.get("persome.daemon")
    log.info("human readable")
    for h in log.handlers:
        h.flush()

    from persome import paths

    last = (paths.logs_dir() / "daemon.log").read_text(encoding="utf-8").strip().splitlines()[-1]
    # Human format is NOT valid JSON and contains the bracketed level.
    assert "[INFO]" in last
    assert "human readable" in last
