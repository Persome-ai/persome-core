"""Regenerate ``openapi.json`` from the current FastAPI app.

Run after touching any FastAPI route, Pydantic response model, or SSE schema:

    uv run python scripts/regen_openapi.py

The committed ``openapi.json`` is the contract consumed by ``Mens.app``. The
CI guard in ``tests/test_openapi_drift.py`` fails the build if the file
falls out of sync with the code.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = ROOT / "openapi.json"

# Allow running from any cwd; src/ is the package root.
sys.path.insert(0, str(ROOT / "src"))

from persome.api import render_openapi_json  # noqa: E402


def main() -> int:
    rendered = render_openapi_json()
    OUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT.parent.parent)} ({len(rendered)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
