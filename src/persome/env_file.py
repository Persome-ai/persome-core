"""Dotenv-format env loader for daemon startup.

Runtime secrets live in ``~/.persome/env``. A user may edit that owner-only
file directly, or an embedding product may mirror secrets from its own secure
store. Business code stays on ``os.environ.get(...)``; this loader merges the
file's contents into ``os.environ`` once, at CLI ``start`` time, before forking.

Semantics:

* Already-set env vars win (shell ``export`` for CLI debugging keeps priority).
* Missing file is fine — returns 0.
* Format is minimal dotenv: ``KEY=VALUE`` per line, ``#`` comments, blank lines
  ignored, optional single/double-quoted values (quotes stripped, no escapes).
* No shell expansion, no ``$VAR`` interpolation — behavior is identical for
  direct CLI and embedding-product launch paths.
"""

from __future__ import annotations

import os
from pathlib import Path


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key = key.strip()
    if not key or not key.replace("_", "").isalnum():
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def load_env_file(path: Path) -> int:
    """Merge ``path`` into ``os.environ``. Returns the number of keys added.

    Pre-existing env vars are NOT overwritten. Unreadable / missing files are
    silently ignored (returns 0) — the daemon will surface a clearer error
    later when a specific ``ANTHROPIC_API_KEY`` etc. lookup comes back empty.
    """
    if not path.exists():
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    added = 0
    for line in text.splitlines():
        parsed = _parse_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if key in os.environ:
            continue
        os.environ[key] = value
        added += 1
    return added
