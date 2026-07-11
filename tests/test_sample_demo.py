from __future__ import annotations

import logging
import os

import pytest

from scripts.sample_demo import seed_sample


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
