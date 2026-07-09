"""Collector registry. Importing this package registers every collector.

Order of import = order shown in the report. Each submodule registers its
collectors via the ``@collector`` decorator at import time.
"""

from __future__ import annotations

# Import submodules for their registration side effects. The order here is the
# order categories appear in the terminal report.
from . import (
    apps,  # noqa: E402,F401
    browser,  # noqa: E402,F401
    comms,  # noqa: E402,F401
    documents,  # noqa: E402,F401
    identity,  # noqa: E402,F401
    projects,  # noqa: E402,F401
    shell,  # noqa: E402,F401
    system,  # noqa: E402,F401
    toolchain,  # noqa: E402,F401
)
from .base import (
    Collector,
    CollectorResult,
    Signal,
    SkipCollector,
    collector,
    registry,
    run_all,
    safe_run,
)

__all__ = [
    "Collector",
    "CollectorResult",
    "Signal",
    "SkipCollector",
    "collector",
    "registry",
    "run_all",
    "safe_run",
]
