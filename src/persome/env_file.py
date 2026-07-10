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
import secrets
import tempfile
from pathlib import Path
from typing import Literal

SCREENSHOT_KEY_ENV = "PERSOME_SCREENSHOT_KEY"
_SCREENSHOT_KEY_HEX_LENGTH = 64

ScreenshotKeyStatus = Literal["existing", "generated"]


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


def is_valid_screenshot_key(value: str | None) -> bool:
    if value is None or len(value) != _SCREENSHOT_KEY_HEX_LENGTH:
        return False
    try:
        return len(bytes.fromhex(value)) == 32
    except ValueError:
        return False


def ensure_screenshot_key(path: Path) -> ScreenshotKeyStatus:
    """Ensure ``path`` contains one valid machine-local screenshot key.

    The installer calls this after creating its virtualenv. Existing canonical
    keys are preserved. Missing or malformed values are replaced with a freshly
    generated 256-bit key. The key is never returned or logged, and the dotenv
    file is atomically rewritten with mode ``0600``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""

    lines = text.splitlines()
    canonical: str | None = None
    kept: list[str] = []
    for line in lines:
        parsed = _parse_line(line)
        if parsed is None:
            kept.append(line)
            continue
        key, value = parsed
        if key == SCREENSHOT_KEY_ENV:
            if canonical is None and is_valid_screenshot_key(value):
                canonical = value
            continue
        kept.append(line)

    if canonical is not None:
        value = canonical
        status: ScreenshotKeyStatus = "existing"
    else:
        value = secrets.token_hex(32)
        status = "generated"

    kept.append(f"{SCREENSHOT_KEY_ENV}={value}")
    payload = "\n".join(kept).rstrip("\n") + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return status
