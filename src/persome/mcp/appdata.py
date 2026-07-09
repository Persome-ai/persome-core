"""Read-only projections of the Swift app's on-disk state (``~/.persome/*.json``) for the
Agent-Native Persome MCP surface (Phase 2,
``docs/superpowers/specs/2026-06-25-agent-native-persome-design.md``).

The Swift app is the *sole writer* of these files; the daemon only reads them, fresh off disk
each call (no caching — the app owns atomic writes). Everything here is **lenient**: a missing
or malformed file yields an empty/typed result, never an exception that would crash a tool call
— mirroring the app's own lenient-Codable persistence.

``read_settings`` REDACTS any secret-looking field (BYO provider keys) before returning: even
though the agent is trusted, keys should never travel needlessly (spec §5).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .. import paths

# Field names whose values are secrets to redact in settings projections. Matches the app's
# `deepseekApiKey` / `doubaoAppKey` / `doubaoAccessKey` / `doubaoResourceId` and anything else
# that looks like a credential, case-insensitively.
_SECRET_KEY_RE = re.compile(r"(key|token|secret|password|access)", re.IGNORECASE)
_REDACTED = "<redacted>"


def _clamp_limit(limit: int, hi: int = 500) -> int:
    """Bound a caller-supplied `limit` to [1, hi]."""
    return max(1, min(limit, hi))


def _read_json(path: Path) -> Any:
    """Parse ``path`` as JSON, or return None on any failure (absent / unreadable / malformed)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _tasks_file(root: Path) -> Path:
    return root / "tasks.json"


def _log_sidecar(root: Path, task_id: str) -> Path:
    return root / "logs" / f"{task_id}.json"


def _task_summary(t: dict[str, Any]) -> dict[str, Any]:
    """The light projection used by `list_tasks` — metadata only, no log bodies."""
    return {
        "id": t.get("id"),
        "title": t.get("title", ""),
        "status": t.get("status", ""),
        "agent": t.get("agent", ""),
        "provenance": t.get("provenance"),
        "workingDirectory": t.get("workingDirectory", ""),
        "createdAt": t.get("createdAt"),
        "finishedAt": t.get("finishedAt"),
        "sessionId": t.get("sessionId"),
    }


def list_tasks(
    root: Path | None = None, *, status: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Task metadata (no log bodies), newest first. Optional `status` filter; `limit` ∈ [1, 500]."""
    root = root or paths.app_data_root()
    raw = _read_json(_tasks_file(root))
    if not isinstance(raw, list):
        return []
    tasks = [t for t in raw if isinstance(t, dict)]
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    # tasks.json is append-order; surface newest createdAt first, stable for missing stamps.
    tasks.sort(key=lambda t: str(t.get("createdAt") or ""), reverse=True)
    return [_task_summary(t) for t in tasks[: _clamp_limit(limit)]]


def read_task(root: Path | None = None, *, task_id: str) -> dict[str, Any] | None:
    """One task's full metadata plus its log body (from the `logs/<id>.json` sidecar, which the
    app keeps out of the hot tasks.json). None when the id isn't found."""
    root = root or paths.app_data_root()
    raw = _read_json(_tasks_file(root))
    if not isinstance(raw, list):
        return None
    match = next((t for t in raw if isinstance(t, dict) and str(t.get("id")) == str(task_id)), None)
    if match is None:
        return None
    out = dict(match)
    sidecar = _read_json(_log_sidecar(root, str(task_id)))
    if isinstance(sidecar, dict):
        out["log"] = sidecar.get("log", out.get("log", ""))
        if isinstance(sidecar.get("turns"), list):
            out["turnLogs"] = sidecar["turns"]
    return out


def _redact(value: Any) -> Any:
    """Recursively replace secret-looking field values; preserve structure + empties.

    An empty secret stays "" (so the agent can tell "no key set" from "key withheld"); a
    non-empty secret becomes ``<redacted>``."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k) and isinstance(v, str):
                out[k] = _REDACTED if v else ""
            else:
                out[k] = _redact(v)
        return out
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def read_settings(root: Path | None = None) -> dict[str, Any]:
    """The app's settings.json with every BYO secret redacted. Empty dict when absent/malformed."""
    root = root or paths.app_data_root()
    raw = _read_json(root / "settings.json")
    if not isinstance(raw, dict):
        return {}
    return _redact(raw)


def list_meetings(root: Path | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
    """Meeting records (id / title / status / timestamps), newest first. `limit` ∈ [1, 500]."""
    root = root or paths.app_data_root()
    raw = _read_json(root / "meetings.json")
    if not isinstance(raw, list):
        return []
    meetings = [m for m in raw if isinstance(m, dict)]
    meetings.sort(key=lambda m: str(m.get("startedAt") or ""), reverse=True)

    def summary(m: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": m.get("id"),
            "title": m.get("title", ""),
            "status": m.get("status", ""),
            "startedAt": m.get("startedAt"),
            "endedAt": m.get("endedAt"),
        }

    return [summary(m) for m in meetings[: _clamp_limit(limit)]]


def read_meeting(root: Path | None = None, *, meeting_id: str) -> dict[str, Any] | None:
    """One meeting record in full (incl. transcript text if present). None when not found."""
    root = root or paths.app_data_root()
    raw = _read_json(root / "meetings.json")
    if not isinstance(raw, list):
        return None
    return next(
        (m for m in raw if isinstance(m, dict) and str(m.get("id")) == str(meeting_id)), None
    )


def read_feedback(root: Path | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
    """The most recent context-feedback verdicts (one JSON object per line in
    ``logs/context-feedback.jsonl``), newest last → returned newest first. `limit` ∈ [1, 500]."""
    root = root or paths.app_data_root()
    path = root / "logs" / "context-feedback.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
        if len(out) >= _clamp_limit(limit):
            break
    return out
