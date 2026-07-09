"""Collector framework: Signal / CollectorResult, the registry, and safe_run.

Each collector is a zero-arg function that returns a list of ``Signal``. It is
registered with ``@collector(name, title, category)`` and run through
``safe_run`` so that a single failing collector — a locked browser DB, a
missing directory, a permission error — records one ``failed`` line and never
takes down the whole run. That resilience is the difference between a live
investor demo that degrades gracefully and one that white-screens.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...logger import get

logger = get("persome.bootstrap")


# --- Data shapes ----------------------------------------------------------


@dataclass
class Signal:
    """One harvested fact.

    ``value`` is whatever shape fits — a string, a list of strings, or a list
    of small dicts (e.g. ``[{"name": ..., "count": ...}]``). ``detail`` is an
    optional one-liner shown in the report next to the value.
    """

    label: str
    value: Any
    detail: str = ""


@dataclass
class CollectorResult:
    name: str
    title: str
    category: str
    signals: list[Signal] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    skipped: str = ""

    @property
    def produced(self) -> bool:
        return self.ok and not self.skipped and bool(self.signals)


class SkipCollector(Exception):
    """Raise from a collector to record a benign skip (not an error)."""


# --- Registry -------------------------------------------------------------


@dataclass
class Collector:
    name: str
    title: str
    category: str
    fn: Callable[[], list[Signal]]


_REGISTRY: list[Collector] = []


def collector(
    name: str, title: str, category: str
) -> Callable[[Callable[[], list[Signal]]], Callable[[], list[Signal]]]:
    """Register a collector function. ``category`` groups it in the report."""

    def deco(fn: Callable[[], list[Signal]]) -> Callable[[], list[Signal]]:
        _REGISTRY.append(Collector(name=name, title=title, category=category, fn=fn))
        return fn

    return deco


def registry() -> list[Collector]:
    return list(_REGISTRY)


def safe_run(c: Collector) -> CollectorResult:
    """Run one collector, converting every failure mode into a result row."""
    try:
        signals = c.fn() or []
    except SkipCollector as exc:
        return CollectorResult(c.name, c.title, c.category, skipped=str(exc) or "skipped")
    except Exception as exc:  # noqa: BLE001 — one collector must never kill the run
        logger.warning("collector %s failed: %s", c.name, exc)
        return CollectorResult(
            c.name, c.title, c.category, ok=False, error=f"{type(exc).__name__}: {exc}"
        )
    if not signals:
        return CollectorResult(c.name, c.title, c.category, skipped="no signals found")
    return CollectorResult(c.name, c.title, c.category, signals=signals)


def run_all() -> list[CollectorResult]:
    return [safe_run(c) for c in registry()]


# --- Shared helpers used by collectors ------------------------------------


def home() -> Path:
    return Path.home()


def have(cmd: str) -> bool:
    """True if ``cmd`` is on PATH."""
    return shutil.which(cmd) is not None


def run_cmd(args: list[str], *, timeout: float = 5.0) -> str | None:
    """Run a command, returning stripped stdout, or None on any failure.

    Never raises — callers treat None as "not available". A short timeout keeps
    a hung tool from stalling the whole bootstrap.
    """
    if not args or shutil.which(args[0]) is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 — args are literal command lists, no shell
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("run_cmd %s failed: %s", args, exc)
        return None
    out_text = (out.stdout or "").strip()
    return out_text or None


def read_text(path: Path, *, max_bytes: int = 2_000_000) -> str | None:
    """Read a text file safely (bounded), returning None if unreadable."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(max_bytes)
    except (OSError, ValueError):
        return None


def top_counts(counter: dict[str, int], n: int) -> list[dict[str, Any]]:
    """Sort a {name: count} map into the top-n ``[{"name", "count"}]`` rows."""
    rows = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return [{"name": name, "count": count} for name, count in rows]
