"""Regenerate ``docs/db-schema.sql`` from the store modules' DDL.

Run after touching any ``CREATE TABLE`` / index / trigger or a module
migrate in ``src/persome``:

    uv run python scripts/regen_db_schema.py

The committed ``docs/db-schema.sql`` is the whole-picture schema reference.
The CI guard in ``tests/test_db_schema_drift.py`` fails the build if the
file falls out of sync with the code.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = ROOT / "docs" / "db-schema.sql"

# Allow running from any cwd; src/ is the package root.
sys.path.insert(0, str(ROOT / "src"))

from persome.store.schema_dump import render_schema_sql  # noqa: E402


def main() -> int:
    rendered = render_schema_sql()
    OUT.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(rendered.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
