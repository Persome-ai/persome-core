"Best-effort shadow projection of Markdown writes into evomem."

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence

from .. import config as config_mod
from ..logger import get
from ..store import files as files_mod
from . import integrity
from . import store as evo_store

_log = get("persome.evomem")


_USER_ID = "default"
_AGENT_ID = "default"


_ALERT_EVERY = 5

_miss_lock = threading.Lock()
_miss_count = 0


def miss_count() -> int:
    with _miss_lock:
        return _miss_count


def reset_misses() -> None:
    global _miss_count
    with _miss_lock:
        _miss_count = 0


def _record_miss(detail: str, *, alert: bool = True) -> None:
    global _miss_count
    with _miss_lock:
        _miss_count += 1
        n = _miss_count
    _log.warning("shadow write miss (cumulative=%d): %s", n, detail)
    if alert and n % _ALERT_EVERY == 0:
        try:
            integrity.emit_alert(
                "shadow_write_lag",
                f"{n} cumulative shadow-write misses; latest: {detail}"
                "; evo_nodes is behind. Rerun `persome evomem-backfill` to catch up",
                source="shadow_write",
                structural=False,
            )
        except Exception:  # noqa: BLE001
            _log.warning("shadow_write_lag alert emission failed", exc_info=True)


def after_write(conn: sqlite3.Connection, *, name: str, entry_ids: Sequence[str]) -> None:
    try:
        _shadow_write(conn, name=name, entry_ids=[i for i in entry_ids if i])
    except Exception as exc:  # noqa: BLE001
        _record_miss(f"{name} {list(entry_ids)}: {exc!r}")


def note_out_of_band_rewrite(names: Sequence[str]) -> None:
    try:
        if not config_mod.load().evomem.shadow_write_enabled:
            return
        from . import inversion

        if inversion.evomem_active():
            return
        for name in names:
            try:
                prefix = files_mod.validate_prefix(files_mod.memory_path(name).name)
            except ValueError:
                continue
            if prefix == "event":
                continue
            _record_miss(f"{name}: full-file compaction bypassed shadow writes; evo_nodes is stale")
    except Exception:  # noqa: BLE001
        _log.warning("note_out_of_band_rewrite failed", exc_info=True)


def _evo_ready(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM evo_nodes WHERE user_id=? AND agent_id=? LIMIT 1",
            (_USER_ID, _AGENT_ID),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _shadow_write(conn: sqlite3.Connection, *, name: str, entry_ids: list[str]) -> None:
    cfg = config_mod.load()
    if not cfg.evomem.shadow_write_enabled or not entry_ids:
        return

    from . import inversion

    if inversion.evomem_active():
        return
    path = files_mod.memory_path(name)
    prefix = files_mod.validate_prefix(path.name)
    if prefix == "event":
        return
    if not _evo_ready(conn):
        _record_miss(
            f"{name}: evo_nodes is empty or missing; run `persome evomem-backfill` "
            "to establish a baseline",
            alert=False,
        )
        return

    parsed = files_mod.read_file(path)
    by_id = {e.id: e for e in parsed.entries}
    affected: list[files_mod.ParsedEntry] = []
    for eid in entry_ids:
        e = by_id.get(eid)
        if e is None:
            _record_miss(f"{name}: entry {eid} could not be parsed after write; batch skipped")
            return
        affected.append(e)

    file_ids = set(by_id)
    preds: dict[str, list[str]] = {}
    for e in parsed.entries:
        if e.superseded_by and e.superseded_by in file_ids:
            preds.setdefault(e.superseded_by, []).append(e.id)

    batch = {e.id for e in affected}
    # (external_id, required_mirror_member, mirror_column)
    required_mirrors: list[tuple[str, str, str]] = []
    for e in affected:
        if e.superseded_by and e.superseded_by in file_ids and e.superseded_by not in batch:
            required_mirrors.append((e.superseded_by, e.id, "supersedes"))
        for p in preds.get(e.id, []):
            if p not in batch:
                required_mirrors.append((p, e.id, "superseded_by"))
    if required_mirrors:
        external = sorted({ext for ext, _, _ in required_mirrors})
        placeholders = ",".join("?" * len(external))
        rows = {
            r["node_id"]: r
            for r in conn.execute(
                f"SELECT node_id, supersedes, superseded_by FROM evo_nodes"
                f" WHERE user_id=? AND agent_id=? AND node_id IN ({placeholders})",
                (_USER_ID, _AGENT_ID, *external),
            )
        }
        stale: list[str] = []
        for ext, member, column in required_mirrors:
            row = rows.get(ext)
            if row is None:
                stale.append(f"{ext} missing")
            elif member not in json.loads(row[column] or "[]"):
                stale.append(f"{ext}.{column} does not contain {member}")
        if stale:
            _record_miss(
                f"{name}: chain endpoints or reciprocal pointers are missing from evo_nodes:"
                f" {'; '.join(sorted(set(stale)))}; batch skipped to avoid a partial chain"
            )
            return

    placeholders = ",".join("?" * len(batch))
    ids = sorted(batch)
    metadata = {
        r["entry_id"]: r
        for r in conn.execute(
            f"SELECT entry_id, confidence, conflicted, occurred_at FROM entry_metadata"
            f" WHERE entry_id IN ({placeholders})",
            ids,
        )
    }
    temporal = {
        r["entry_id"]: r
        for r in conn.execute(
            f"SELECT entry_id, valid_from, valid_until FROM entry_temporal"
            f" WHERE entry_id IN ({placeholders})",
            ids,
        )
    }

    from . import backfill

    nodes = []
    for e in affected:
        nodes.append(
            backfill.map_entry_to_node(
                e,
                file_name=path.name,
                prefix=prefix,
                supersedes=preds.get(e.id, []),
                superseded_by=(
                    [e.superseded_by] if e.superseded_by and e.superseded_by in file_ids else []
                ),
                meta=metadata.get(e.id),
                temporal=temporal.get(e.id),
                user_id=_USER_ID,
                agent_id=_AGENT_ID,
            )
        )

    conn.execute("BEGIN")
    try:
        for node in nodes:
            evo_store.upsert_node(conn, node, user_id=_USER_ID, agent_id=_AGENT_ID)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    _log.debug("shadow write ok: %s → %d node(s)", name, len(nodes))
