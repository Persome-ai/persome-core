"""Guard: committed ``openapi.json`` must match the live FastAPI schema.

The paper runtime publishes this file as its canonical HTTP contract. If routes
or response models change without regenerating it, clients and documentation
silently drift.

To fix a failing test::

    uv run python scripts/regen_openapi.py
"""

from __future__ import annotations

import json
from pathlib import Path

from persome.api import render_openapi_json

REPO = Path(__file__).resolve().parent.parent
OPENAPI_PATH = REPO / "openapi.json"


def test_openapi_json_matches_live_schema() -> None:
    """The committed ``openapi.json`` byte-matches what the live app emits."""
    committed = OPENAPI_PATH.read_text(encoding="utf-8")
    rendered = render_openapi_json()

    if committed == rendered:
        return

    # Surface a precise structural diff so authors can see what drifted
    # without scrolling through the full file.
    committed_obj = json.loads(committed)
    rendered_obj = json.loads(rendered)

    committed_routes = {
        f"{m.upper()} {p}" for p, ms in committed_obj.get("paths", {}).items() for m in ms
    }
    rendered_routes = {
        f"{m.upper()} {p}" for p, ms in rendered_obj.get("paths", {}).items() for m in ms
    }
    only_in_code = sorted(rendered_routes - committed_routes)
    only_in_file = sorted(committed_routes - rendered_routes)

    raise AssertionError(
        "openapi.json is out of sync with the live FastAPI app.\n"
        f"Routes only in code: {only_in_code or 'none'}\n"
        f"Routes only in committed file: {only_in_file or 'none'}\n"
        "Run: uv run python scripts/regen_openapi.py"
    )
