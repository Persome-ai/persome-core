"""Route write authority between Markdown and the evomem canonical store.

When ``[evomem] write_authority = "evomem"``, this module handles the entry
write verbs from ``store/entries.py``. Each write atomically updates canonical
nodes and their FTS projection, then best-effort regenerates the human-readable
Markdown projection. Projection failures are observable but never roll back a
successful canonical write.

Event logs and files below ``skills/`` remain on the direct Markdown path.
With the default ``write_authority = "markdown"``, routing bypasses this module
and preserves the established Markdown-first behavior.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
import threading
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import config as config_mod
from ..logger import get
from ..store import files as files_mod
from ..store import fts
from . import integrity

if TYPE_CHECKING:
    from .engine import EvoMemory
    from .models import MemoryNode

_log = get("persome.evomem")

# Use the same default scope as backfill and shadow writes.
_USER_ID = "default"
_AGENT_ID = "default"

# Emit an integrity alert after every N projection misses.
_ALERT_EVERY = 5

_miss_lock = threading.Lock()
_miss_count = 0


# Write-authority routing


def authority() -> str:
    """Normalize write authority and fail safely to ``markdown``."""
    raw = (config_mod.load().evomem.write_authority or "markdown").strip().lower()
    if raw not in ("markdown", "evomem"):
        _log.warning("unknown [evomem] write_authority %r — falling back to 'markdown'", raw)
        return "markdown"
    return raw


def evomem_active() -> bool:
    return authority() == "evomem"


def routes_to_engine(name: str) -> bool:
    """Return whether an evomem-authoritative write owns this target file."""
    try:
        if "/" in name:
            return False  # Projections cannot route into skills/ subdirectories.
        if files_mod.validate_prefix(name) == "event":
            return False  # Append-only event logs never enter evo_nodes.
    except ValueError:
        return False
    return evomem_active()


# Projection failure counters


def miss_count() -> int:
    """Return the process-local Markdown projection miss count."""
    with _miss_lock:
        return _miss_count


def reset_misses() -> None:
    """Reset misses after tests or a successful full reprojection."""
    global _miss_count
    with _miss_lock:
        _miss_count = 0


def _record_miss(detail: str) -> None:
    """Record a projection miss without affecting the canonical write."""
    global _miss_count
    with _miss_lock:
        _miss_count += 1
        n = _miss_count
    _log.warning("markdown projection miss (cumulative=%d): %s", n, detail)
    if n % _ALERT_EVERY == 0:
        try:
            integrity.emit_alert(
                "markdown_projection_lag",
                f"{n} cumulative markdown-projection misses; latest: {detail}"
                "; the readable projection is behind. Run "
                "`persome evomem-project-markdown --live` to catch up",
                source="write_inversion",
                structural=False,
            )
        except Exception:  # noqa: BLE001 - alert failure must not affect writes
            _log.warning("markdown_projection_lag alert emission failed", exc_info=True)


# Internal helpers


def _engine() -> EvoMemory:
    """Create a store per write so changing ``PERSOME_ROOT`` cannot stale it."""
    from .engine import EvoMemory

    return EvoMemory(user_id=_USER_ID, agent_id=_AGENT_ID)


def _file_row(conn: sqlite3.Connection, path_name: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM files WHERE path=?", (path_name,)
    ).fetchone()
    return row


def _load_file_nodes(conn: sqlite3.Connection, path_name: str) -> list[MemoryNode]:
    from . import store as evo_store

    rows = conn.execute(
        "SELECT * FROM evo_nodes WHERE user_id=? AND agent_id=? AND file_name=? ORDER BY node_id",
        (_USER_ID, _AGENT_ID, path_name),
    ).fetchall()
    return [evo_store._row_to_node(r) for r in rows]


def _node_from_write(
    *,
    entry_id: str,
    ts: str,
    tags: list[str],
    body: str,
    path_name: str,
    prefix: str,
    supersedes: list[str],
    confidence: str | None,
    conflicted: bool,
    occurred_at: str | None,
    valid_from: str,
) -> MemoryNode:
    """Build a MemoryNode through the shared render, parse, and mapping path."""
    from . import backfill

    heading = files_mod.render_heading(timestamp=ts, entry_id=entry_id, tags=tags)
    block = f"{heading}\n{body}\n"
    # Take the first entry (our rendered heading) rather than tuple-unpacking a
    # single element: if ``body`` contains a line that matches ENTRY_HEADING_RE
    # (a user pasting another memory entry verbatim), _parse_entries returns ≥2
    # and the unpack would ValueError, hard-failing this evomem-authority write —
    # while the markdown path treats the same content as ordinary body (#577).
    entries = files_mod._parse_entries(block)
    entry = entries[0]
    return backfill.map_entry_to_node(
        entry,
        file_name=path_name,
        prefix=prefix,
        supersedes=supersedes,
        superseded_by=[],
        meta={
            "confidence": fts._norm_confidence(confidence),
            "conflicted": 1 if conflicted else 0,
            "occurred_at": occurred_at,
        },
        temporal={"valid_from": valid_from, "valid_until": None},
        user_id=_USER_ID,
        agent_id=_AGENT_ID,
    )


def _row_mapping(row: sqlite3.Row | fts.FileRow | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(row, fts.FileRow):
        return dataclasses.asdict(row)
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return row


def record_projection_state(conn: sqlite3.Connection, file_name: str, text: str) -> None:
    """Record the content hash of a successful projection."""
    conn.execute(
        "INSERT INTO projection_state(file_name, content_hash, projected_at)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT(file_name) DO UPDATE SET"
        " content_hash=excluded.content_hash, projected_at=excluded.projected_at",
        (file_name, content_hash(text), datetime.now().astimezone().isoformat(timespec="seconds")),
    )


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _project(
    conn: sqlite3.Connection,
    *,
    path: Path,
    nodes: list,
    file_row: sqlite3.Row | fts.FileRow | Mapping[str, Any],
) -> None:
    """Project Markdown best-effort, recording failures without raising."""
    from ..store import projector

    try:
        text = projector.render_projection(
            path.name, nodes, file_row=_row_mapping(file_row), marker=True
        )
        files_mod.atomic_write_text(path, text)
        record_projection_state(conn, path.name, text)
    except Exception as exc:  # noqa: BLE001 - projection is disposable derived state
        _record_miss(f"{path.name}: {exc!r}")


def _finish_file_write(
    conn: sqlite3.Connection,
    *,
    path: Path,
    prefix: str,
    soft_limit_tokens: int | None = None,
) -> None:
    """Update file metadata and project the file after a canonical write."""
    from ..store import projector

    name = path.name
    nodes = _load_file_nodes(conn, name)
    row = _file_row(conn, name)
    needs_compact = bool(row["needs_compact"]) if row is not None else False
    if soft_limit_tokens is not None and not needs_compact:
        content = projector.render_content(name, nodes)
        est_tokens = len(content) // 4
        if est_tokens > soft_limit_tokens:
            needs_compact = True
            _log.info(
                "flagged %s for compact (est %d tokens > %d)", name, est_tokens, soft_limit_tokens
            )
    file_row = fts.FileRow(
        path=name,
        prefix=prefix,
        description=(row["description"] or "") if row is not None else "",
        tags=(row["tags"] or "") if row is not None else "",
        status=(row["status"] or "active") if row is not None else "active",
        entry_count=len(nodes),
        created=(row["created"] or "") if row is not None else "",
        updated=files_mod.today(),
        needs_compact=1 if needs_compact else 0,
    )
    fts.upsert_file(conn, file_row)
    _project(conn, path=path, nodes=nodes, file_row=file_row)


# Evomem-authoritative write verbs; signatures mirror store/entries.py.


def create_file(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str,
    tags: list[str],
    status: str = "active",
) -> Path:
    """Create file metadata and an empty readable projection."""
    if not description.strip():
        raise ValueError("description is required")
    prefix = files_mod.validate_prefix(name)
    path = files_mod.memory_path(name)
    with files_mod.file_lock(path):
        if path.exists() or _file_row(conn, path.name) is not None:
            raise FileExistsError(f"{path.name} already exists")
        fm = files_mod.default_frontmatter(description=description, tags=tags, status=status)
        file_row = fts.FileRow(
            path=path.name,
            prefix=prefix,
            description=description,
            tags=" ".join(tags),
            status=status,
            entry_count=0,
            created=fm["created"],
            updated=fm["updated"],
            needs_compact=0,
        )
        fts.upsert_file(conn, file_row)
        _project(conn, path=path, nodes=[], file_row=file_row)
    _log.info("created file: %s (status=%s, authority=evomem)", path.name, status)
    return path


def append_entry(
    conn: sqlite3.Connection,
    *,
    name: str,
    content: str,
    tags: list[str],
    soft_limit_tokens: int | None = None,
    confidence: str | None = None,
    conflicted: bool = False,
    occurred_at: str | None = None,
) -> str:
    """Append through a canonical ADD, FTS update, and Markdown projection."""
    from ..store import entries as entries_mod

    path = files_mod.memory_path(name)
    prefix = files_mod.validate_prefix(path.name)
    row = _file_row(conn, path.name)
    if row is None:
        if not path.exists():
            raise FileNotFoundError(f"{path.name} does not exist; call create_file first")
        # Backfill metadata for a manually created or legacy file.
        parsed = files_mod.read_file(path)
        # Refuse to overwrite legacy entries before canonical nodes are backfilled.
        if parsed.entries and not _load_file_nodes(conn, path.name):
            raise RuntimeError(
                f"{path.name} has {len(parsed.entries)} on-disk entries but no canonical "
                "nodes. Run `persome evomem-backfill` before appending to avoid "
                "overwriting historical entries."
            )
        fts.upsert_file(
            conn,
            fts.FileRow(
                path=path.name,
                prefix=prefix,
                description=parsed.description,
                tags=" ".join(parsed.tags),
                status=parsed.status,
                entry_count=len(parsed.entries),
                created=parsed.created,
                updated=parsed.updated,
                needs_compact=1 if parsed.needs_compact else 0,
            ),
        )

    occurred_at = entries_mod._norm_occurred_at(occurred_at)
    ts = entries_mod._now_iso_minute()
    entry_id = entries_mod.make_id(ts)
    all_tags = list(tags) + entries_mod._metadata_tags(confidence, conflicted, occurred_at)
    body = content.strip()
    node = _node_from_write(
        entry_id=entry_id,
        ts=ts,
        tags=all_tags,
        body=body,
        path_name=path.name,
        prefix=prefix,
        supersedes=[],
        confidence=confidence,
        conflicted=conflicted,
        occurred_at=occurred_at,
        valid_from=ts,
    )
    with files_mod.file_lock(path):
        _engine().commit_node(node)  # Canonical atomic write.
        entries_mod.derived_append_rows(
            conn,
            entry_id=entry_id,
            path_name=path.name,
            prefix=prefix,
            ts=ts,
            tags_str=" ".join(all_tags),
            content=body,
            confidence=confidence,
            conflicted=conflicted,
            occurred_at=occurred_at,
        )
        _finish_file_write(conn, path=path, prefix=prefix, soft_limit_tokens=soft_limit_tokens)
    return entry_id


def supersede_entry(
    conn: sqlite3.Connection,
    *,
    name: str,
    old_entry_id: str,
    new_content: str,
    reason: str,
    tags: list[str] | None = None,
    refined_from: str | None = None,
    confidence: str | None = None,
    conflicted: bool = False,
    occurred_at: str | None = None,
) -> str:
    """Atomically write a new chain head, retire the old node, and project both.

    ``refined_from`` remains represented as a SUPERSEDE node for compatibility
    with existing Markdown and backfill. Metadata inherited when ``tags`` is
    omitted is written to canonical columns and the derived metadata table so
    the projection and rebuilt index converge immediately.
    """
    from ..store import entries as entries_mod

    path = files_mod.memory_path(name)
    prefix = files_mod.validate_prefix(path.name)
    if _file_row(conn, path.name) is None and not path.exists():
        raise FileNotFoundError(path.name)
    engine = _engine()
    old_node = engine.store.get(old_entry_id)
    if old_node is None or old_node.file_name != path.name:
        raise ValueError(f"entry {old_entry_id} not found in {path.name}")

    occurred_at = entries_mod._norm_occurred_at(occurred_at)
    ts = entries_mod._now_iso_minute()
    new_id = entries_mod.make_id(ts)
    if tags is None:
        # Inherit semantic tags and metacognitive columns from the old entry.
        new_tags = old_node.tags.split()
        if confidence is None and not conflicted and occurred_at is None:
            confidence = old_node.confidence
            conflicted = old_node.conflicted
            occurred_at = old_node.occurred_at
    else:
        new_tags = list(tags)
    if refined_from:
        new_tags.append(f"refined-from:{refined_from}")
    new_tags += entries_mod._metadata_tags(confidence, conflicted, occurred_at)

    body = new_content.strip()
    provenance = entries_mod._render_supersede_provenance(
        old_entry_id=old_entry_id,
        reason=reason,
    )
    content_md = f"{body}\n{provenance}" if body else provenance
    node = _node_from_write(
        entry_id=new_id,
        ts=ts,
        tags=new_tags,
        body=content_md,
        path_name=path.name,
        prefix=prefix,
        supersedes=[old_entry_id],
        confidence=confidence,
        conflicted=conflicted,
        occurred_at=occurred_at,
        valid_from=ts,
    )
    with files_mod.file_lock(path):
        engine.commit_supersede(node, old_id=old_entry_id, old_valid_until=ts)
        entries_mod.derived_supersede_rows(
            conn,
            old_entry_id=old_entry_id,
            new_entry_id=new_id,
            path_name=path.name,
            prefix=prefix,
            ts=ts,
            tags_str=" ".join(new_tags),
            content=body,
            confidence=confidence,
            conflicted=conflicted,
            occurred_at=occurred_at,
        )
        _finish_file_write(conn, path=path, prefix=prefix)
    return new_id


def mark_entry_deleted(conn: sqlite3.Connection, *, name: str, entry_id: str) -> None:
    """Retire an orphan node and update its FTS and Markdown projections."""
    from ..store import entries as entries_mod
    from .models import MemoryStatus

    path = files_mod.memory_path(name)
    prefix = files_mod.validate_prefix(path.name)
    if _file_row(conn, path.name) is None and not path.exists():
        raise FileNotFoundError(path.name)
    engine = _engine()
    node = engine.store.get(entry_id)
    if node is None or node.file_name != path.name:
        raise ValueError(f"entry {entry_id} not found in {path.name}")

    was_active = node.status is MemoryStatus.ACTIVE
    ts = entries_mod._now_iso_minute()
    with files_mod.file_lock(path):
        engine.commit_retire(entry_id, valid_until=ts)
        retired = engine.store.get(entry_id)
        if retired is not None:
            from ..store import projector

            file_nodes = _load_file_nodes(conn, path.name)
            ts_by_id = {n.node_id: projector._heading_ts(n) for n in file_nodes}
            rendered_tags = projector._render_tags(
                retired,
                prefix=prefix,
                ts_by_id=ts_by_id,
            )
            conn.execute(
                "UPDATE entries SET tags=? WHERE id=?",
                (" ".join(rendered_tags), entry_id),
            )
        entries_mod.derived_retire_rows(conn, entry_id=entry_id, ts=ts)
        if was_active:
            # Repeated retirement is idempotent and does not rewrite the file.
            _finish_file_write(conn, path=path, prefix=prefix)


def set_file_status(conn: sqlite3.Connection, *, name: str, status: str) -> None:
    """Update canonical file status and reflect it in frontmatter."""
    path = files_mod.memory_path(name)
    if _file_row(conn, path.name) is None:
        return  # Missing files are a no-op.
    conn.execute("UPDATE files SET status = ? WHERE path = ?", (status, path.name))
    with files_mod.file_lock(path):
        reproject_file(conn, path.name)
    _log.info("file status set: %s -> %s (authority=evomem)", path.name, status)


def flag_needs_compact(conn: sqlite3.Connection, *, name: str, value: bool) -> None:
    """Update canonical ``needs_compact`` state and reproject frontmatter."""
    path = files_mod.memory_path(name)
    fts.set_needs_compact(conn, path.name, value)
    with files_mod.file_lock(path):
        reproject_file(conn, path.name)


def reproject_file(conn: sqlite3.Connection, path_name: str) -> None:
    """Best-effort reprojection of one file from canonical nodes and metadata."""
    path = files_mod.memory_path(path_name)
    row = _file_row(conn, path.name)
    if row is None:
        return
    nodes = _load_file_nodes(conn, path.name)
    _project(conn, path=path, nodes=nodes, file_row=row)


# Manual-edit detection and import


@dataclasses.dataclass
class ImportReport:
    """Result of one ``import_markdown_file`` operation."""

    file_name: str
    imported: list[str] = dataclasses.field(default_factory=list)
    conflicts: list[str] = dataclasses.field(default_factory=list)
    reprojected: bool = False


def check_manual_edits(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Return projected files whose content differs from the recorded hash.

    Detection emits an alert but never imports automatically. An owner must run
    ``persome evomem-import-markdown <file>`` explicitly.
    """
    findings: list[dict[str, str]] = []
    rows = conn.execute("SELECT file_name, content_hash FROM projection_state").fetchall()
    for r in rows:
        path = files_mod.memory_path(r["file_name"])
        if not path.exists():
            findings.append({"file": r["file_name"], "kind": "missing"})
            continue
        if content_hash(path.read_text()) != r["content_hash"]:
            findings.append({"file": r["file_name"], "kind": "modified"})
    if findings:
        detail = ", ".join(f"{f['file']}({f['kind']})" for f in findings)
        try:
            integrity.emit_alert(
                "manual_edit_detected",
                f"{len(findings)} projection file(s) differ from last projected state: {detail}"
                "; Markdown is a projection and the next projection will overwrite edits. "
                "Use `persome evomem-import-markdown <file>` to import them",
                source="write_inversion",
                structural=False,
            )
        except Exception:  # noqa: BLE001
            _log.warning("manual_edit_detected alert emission failed", exc_info=True)
    return findings


def run_daily_manual_edit_check() -> list[dict[str, str]]:
    """Run manual-edit detection from the daily safety net without raising."""
    try:
        if not evomem_active():
            return []
        with fts.cursor() as conn:
            findings = check_manual_edits(conn)
        if findings:
            _log.warning("manual-edit check: %d finding(s)", len(findings))
        else:
            _log.info("manual-edit check: clean")
        return findings
    except Exception:  # noqa: BLE001
        _log.warning("manual-edit check failed", exc_info=True)
        return []


def import_markdown_file(conn: sqlite3.Connection, name: str) -> ImportReport:
    """Import safe manual additions from a projected Markdown file.

    Only plain active ADD entries are imported automatically. Chain-bearing
    additions and changes or deletions of existing entries are returned as
    conflicts for human review, and the edited file is not overwritten.
    """
    from ..store import entries as entries_mod
    from ..store import projector
    from .models import MemoryStatus

    if not evomem_active():
        raise RuntimeError(
            'evomem-import-markdown requires write_authority="evomem"; '
            "in Markdown-authoritative mode, edit the file and run `persome rebuild-index`"
        )
    path = files_mod.memory_path(name)
    if "/" in name or files_mod.validate_prefix(path.name) == "event":
        raise ValueError(f"{path.name} is exempt from projection and import")
    if not path.exists():
        raise FileNotFoundError(path.name)

    report = ImportReport(file_name=path.name)
    prefix = files_mod.validate_prefix(path.name)
    parsed = files_mod.read_file(path)
    with files_mod.file_lock(path):
        existing = {n.node_id for n in _load_file_nodes(conn, path.name)}
        candidates = {
            n.node_id: n
            for n in projector.rebuild_nodes_from_projection([(path.name, parsed.entries)])
        }
        engine = _engine()
        for e in parsed.entries:
            if e.id in existing:
                continue
            node = candidates[e.id]
            if (
                e.superseded_by
                or e.refined_from
                or e.abstracted_from
                or node.supersedes
                or node.status is not MemoryStatus.ACTIVE
            ):
                report.conflicts.append(
                    f"{e.id}: the new entry carries chain or retirement state; review required"
                )
                continue
            node.file_name = path.name
            engine.commit_node(node)
            entries_mod.derived_append_rows(
                conn,
                entry_id=e.id,
                path_name=path.name,
                prefix=prefix,
                ts=e.timestamp,
                tags_str=" ".join(e.tags),
                content=node.content,
                confidence=node.confidence,
                conflicted=node.conflicted,
                occurred_at=node.occurred_at,
            )
            report.imported.append(e.id)

        # Reproject only after canonical state fully reconciles with the edited file.
        nodes = _load_file_nodes(conn, path.name)
        row = _file_row(conn, path.name)
        if row is not None and report.imported:
            file_row = fts.FileRow(
                path=path.name,
                prefix=prefix,
                description=row["description"] or "",
                tags=row["tags"] or "",
                status=row["status"] or "active",
                entry_count=len(nodes),
                created=row["created"] or "",
                updated=files_mod.today(),
                needs_compact=int(row["needs_compact"] or 0),
            )
            fts.upsert_file(conn, file_row)
            row = _file_row(conn, path.name)
        canonical = projector.render_projection(
            path.name, nodes, file_row=_row_mapping(row) if row is not None else None, marker=True
        )
        leftover = {e.id for e in parsed.entries} - {n.node_id for n in nodes}
        if leftover:
            report.conflicts.append(
                f"entries remain outside canonical state: {', '.join(sorted(leftover))}"
            )
        if not report.conflicts:
            current = path.read_text()
            if _entries_only(current) == _entries_only(canonical):
                files_mod.atomic_write_text(path, canonical)
                record_projection_state(conn, path.name, canonical)
                report.reprojected = True
            else:
                report.conflicts.append(
                    "the imported file still differs from its canonical projection; "
                    "the file was preserved for manual review"
                )
    return report


def _entries_only(text: str) -> list[tuple[str, list[str], str]]:
    """Compare normalized entry tags and bodies while ignoring formatting noise."""
    try:
        post_body = text.split("---", 2)[2] if text.startswith("---") else text
    except IndexError:
        post_body = text
    # Sort by ID so harmless ordering differences do not block a clean import.
    return sorted((e.id, e.tags, e.body.strip()) for e in files_mod._parse_entries(post_body))


def project_live_all(conn: sqlite3.Connection) -> list[str]:
    """Idempotently reproject every non-exempt file into live memory.

    Used to repair projection lag and before returning authority to Markdown.
    Each file is best-effort so one failure does not block the rest.
    """
    done: list[str] = []
    for r in conn.execute("SELECT path FROM files ORDER BY path").fetchall():
        name = r["path"]
        try:
            if "/" in name or files_mod.validate_prefix(name) == "event":
                continue
        except ValueError:
            continue
        reproject_file(conn, name)
        done.append(name)
    return done
