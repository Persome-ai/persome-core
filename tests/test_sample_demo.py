from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from scripts.sample_demo import seed_sample


def test_readme_hero_and_demo_keep_illustration_and_runtime_proof_distinct() -> None:
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    hero = readme.split("## What is it?", maxsplit=1)[0]
    demo = readme.split("### 1. Five-minute synthetic demo", maxsplit=1)[1].split(
        "### 2. Install with your data", maxsplit=1
    )[0]

    assert hero.startswith("# Persome: Build your Personal Model\n")
    assert sum(line.startswith("# ") for line in hero.splitlines()) == 1
    assert "[Personal Model]" not in hero
    assert "docs/assets/readme/personal-model.png" in hero
    assert (root / "docs/assets/readme/personal-model.png").is_file()
    assert "Concept illustration" in hero
    assert "docs/assets/persome-model-hero.png" not in hero
    assert "(#1-five-minute-synthetic-demo)" in hero
    assert "(#2-install-with-your-data)" in hero
    assert "(#3-connect-a-trusted-mcp-client)" in hero
    assert "docs/assets/persome-model-hero.png" in demo
    assert (root / "docs/assets/persome-model-hero.png").is_file()
    assert "424 synthetic Points, 146 Lines, 12 Faces, 4 Volumes, and 1 Root" in demo


def test_showcase_seed_builds_dense_sourced_geometry(ac_root) -> None:
    original_log_level = logging.getLogger("persome.evomem").level
    result = seed_sample(ac_root, showcase=True)
    snapshot = result["snapshot"]

    expected_stats = {
        "points": 424,
        "active_points": 279,
        "evolution_lines": 145,
        "relation_lines": 1,
        "faces": 12,
        "volumes": 4,
        "roots": 1,
        "receipts": 425,
    }
    assert {key: snapshot["stats"][key] for key in expected_stats} == expected_stats
    assert snapshot["stats"]["redactions"] == {}
    assert all(face["source_receipts"] for face in snapshot["faces"])
    assert all(volume["source_receipts"] for volume in snapshot["volumes"])
    assert snapshot["root"]["source_receipts"]
    top_result = result["search"]["results"][0]
    assert top_result["content"] == ("The user reserves mornings for focused writing and review.")
    assert top_result["id"] in {point["id"] for point in snapshot["points"]}
    assert any(
        receipt["source_kind"] == "point" and receipt["source_id"] == top_result["id"]
        for receipt in snapshot["receipts"]
    )
    assert logging.getLogger("persome.evomem").level == original_log_level


def test_seed_restores_existing_root_when_setup_fails(ac_root) -> None:
    sentinel = str(ac_root)
    invalid_root = ac_root / "not-a-directory"
    invalid_root.write_text("synthetic blocker", encoding="utf-8")

    with pytest.raises(FileExistsError):
        seed_sample(invalid_root)

    assert os.environ["PERSOME_ROOT"] == sentinel
