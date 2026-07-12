"""Guard: no test may ever resolve the developer's real ``~/.persome``.

The conftest ``_sandbox_persome_root`` autouse fixture must point every test
at a throwaway ``PERSOME_ROOT``. This regression-tests the incident where a
test (``test_local_api_auth``) opened the developer's real index.db because
isolation was opt-in (``ac_root``) rather than the default.
"""

from __future__ import annotations

import os
from pathlib import Path

from persome import paths


def test_default_root_is_a_sandbox_not_the_real_home() -> None:
    """Any test that never requests ``ac_root`` still gets a tmp root."""
    real_root = Path.home() / ".persome"
    assert os.environ.get("PERSOME_ROOT"), "PERSOME_ROOT must be set for every test"
    assert paths.root() != real_root.resolve()
    assert not paths.root().is_relative_to(real_root.resolve())


def test_ac_root_still_overrides_the_baseline_sandbox(ac_root: Path) -> None:
    """Tests that opt into ``ac_root`` keep their own per-test root."""
    assert paths.root() == ac_root.resolve()
