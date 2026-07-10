"""Guard: committed ``docs/db-schema.sql`` must match the DDL in the code.

The DDL is deliberately spread across per-module stores (each owns its
``CREATE TABLE IF NOT EXISTS`` + migrate). ``docs/db-schema.sql`` is the
generated whole-picture reference. If any module's schema changes without
regenerating the file, or a new module creates a table the dump does not
know about, these tests fail the build.

To fix a failing drift test::

    uv run python scripts/regen_db_schema.py
"""

from __future__ import annotations

import re
from pathlib import Path

from persome.store.schema_dump import render_schema_sql

REPO = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO / "docs" / "db-schema.sql"
SRC = REPO / "src" / "persome"

# Matches real DDL heads only (tables, indexes, triggers, views — an
# unregistered module contributing only an index or trigger must fail the
# completeness guard too). The lookaheads keep prose from matching: the
# (?!IF\b) guard stops "``CREATE TABLE IF NOT EXISTS``" in a comment from
# yielding a table named IF, and the trailing lookahead requires what real
# DDL puts after the name — "(", ON, USING, AFTER/BEFORE/INSTEAD, or AS —
# so "the CREATE TABLE statement above" does not yield a table "statement".
# Known limitation: dynamically built names (f"CREATE TABLE {name}") are
# invisible to this scan.
_CREATE_RE = re.compile(
    r"CREATE\s+(?:VIRTUAL\s+)?(?:TABLE|INDEX|TRIGGER|VIEW)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?!IF\b)[\"'`]?([A-Za-z_][A-Za-z0-9_]*)[\"'`]?"
    r"(?=\s*(?:\(|ON\b|USING\b|AFTER\b|BEFORE\b|INSTEAD\b|AS\b))",
    re.IGNORECASE,
)


def _table_names(sql_text: str) -> set[str]:
    return {m.group(1) for m in _CREATE_RE.finditer(sql_text)}


def test_db_schema_sql_matches_code() -> None:
    """The committed ``docs/db-schema.sql`` byte-matches the generated dump."""
    committed = SCHEMA_PATH.read_text(encoding="utf-8")
    rendered = render_schema_sql()

    if committed == rendered:
        return

    only_in_code = sorted(_table_names(rendered) - _table_names(committed))
    only_in_file = sorted(_table_names(committed) - _table_names(rendered))
    raise AssertionError(
        "docs/db-schema.sql is out of sync with the store modules.\n"
        f"Tables only in code: {only_in_code or 'none'}\n"
        f"Tables only in committed file: {only_in_file or 'none'}\n"
        "(If no table names differ, a column/index/trigger changed.)\n"
        "Run: uv run python scripts/regen_db_schema.py"
    )


def test_every_create_table_in_source_is_dumped() -> None:
    """Every ``CREATE [VIRTUAL] TABLE`` in ``src/persome`` appears in the dump.

    This is the guard against a *new* module creating tables lazily without
    being registered in ``schema_dump``: the drift test alone cannot see
    tables the dump never knew about.
    """
    in_source: set[str] = set()
    for py in sorted(SRC.rglob("*.py")):
        in_source |= _table_names(py.read_text(encoding="utf-8"))
    # sqlite_master is queried, never created; keep the scan honest anyway.
    assert in_source, "scan found no CREATE TABLE statements — regex broken?"

    dumped = _table_names(render_schema_sql())
    missing = sorted(in_source - dumped)
    assert not missing, (
        f"Tables created somewhere in src/persome but absent from the dump: {missing}. "
        "Register the module in src/persome/store/schema_dump.py (and rerun "
        "scripts/regen_db_schema.py)."
    )


def test_dump_never_touches_persome_root(ac_root: Path) -> None:
    """Rendering the schema must not create or open any real store."""
    render_schema_sql()
    leftovers = [p for p in ac_root.rglob("*") if p.is_file()]
    assert not leftovers, f"render_schema_sql() wrote into PERSOME_ROOT: {leftovers}"
