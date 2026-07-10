"Lossy disaster recovery of canonical nodes from Markdown projections."

from __future__ import annotations

from dataclasses import dataclass, field

from ..logger import get
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts
from . import backup, integrity
from .store import NodeStore, upsert_node

_log = get("persome.evomem")


class RestoreError(RuntimeError):
    """Raised when the restore must abort before touching evo_nodes."""


@dataclass
class RestoreReport:
    """One restore run's outcome."""

    dry_run: bool
    files: int = 0
    skipped_event_files: int = 0
    nodes: int = 0
    projection_files: int = 0
    projection_entries: int = 0
    violations: list[integrity.Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def import_from_markdown(*, dry_run: bool = False) -> RestoreReport:
    from ..store import projector

    report = RestoreReport(dry_run=dry_run)
    parsed_files: list[tuple[str, list[files_mod.ParsedEntry]]] = []
    for path in files_mod.list_memory_files():
        try:
            prefix = files_mod.validate_prefix(path.name)
        except ValueError as exc:
            _log.warning("restore: skipping %s: %s", path.name, exc)
            continue
        if prefix == "event":
            report.skipped_event_files += 1
            continue
        parsed_files.append((path.name, files_mod.read_file(path).entries))
        report.files += 1

    nodes = projector.rebuild_nodes_from_projection(parsed_files)
    report.nodes = len(nodes)
    if dry_run:
        return report

    if backup.create_snapshot(structural_only=True) is None:
        raise RestoreError(
            "pre-restore snapshot failed (VACUUM INTO / verification) — aborting,"
            " evo_nodes untouched"
        )
    integrity.ensure_writes_allowed()
    NodeStore()  # ensures table + migration
    # Restore REPLACES only the scopes the projection actually rebuilds. An
    # unscoped `DELETE FROM evo_nodes` would also wipe nodes in any scope the

    # those never get re-inserted → the disaster tool itself becomes a data-loss
    # source (#583). Delete per (user_id, agent_id) of the rebuilt nodes only.
    rebuilt_scopes = {(node.user_id, node.agent_id) for node in nodes}
    with fts.cursor() as conn:
        conn.execute("BEGIN")
        try:
            for uid, aid in rebuilt_scopes:
                conn.execute("DELETE FROM evo_nodes WHERE user_id = ? AND agent_id = ?", (uid, aid))
            for node in nodes:
                upsert_node(conn, node, user_id=node.user_id, agent_id=node.agent_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        report.projection_files, report.projection_entries = entries_mod.rebuild_index(conn)
        report.violations = integrity.run_checks(conn)

    _log.info(
        "import_from_markdown: %d file(s) parsed (%d event-* skipped) → %d node(s),"
        " projection %d file(s)/%d entr(ies), ok=%s",
        report.files,
        report.skipped_event_files,
        report.nodes,
        report.projection_files,
        report.projection_entries,
        report.ok,
    )
    return report
