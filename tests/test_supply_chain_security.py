"""Static regression gates for installer and GitHub release trust boundaries."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installer_uses_verified_uv_and_locked_sync() -> None:
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "astral.sh/uv/install.sh" not in script
    assert "UV_BOOTSTRAP_VERSION=" in script
    assert "shasum -a 256" in script
    assert "UV_PROJECT_ENVIRONMENT=" in script
    assert " sync --project " in script
    assert "--locked --no-dev" in script
    assert "--no-install-project --no-build" in script
    assert '--python "${python_target}"' in script
    assert "--build-constraints" in script and "--require-hashes" in script
    assert 'cd "${ROOT_DIR}"' in script
    assert "build --project . --wheel" in script
    assert " pip install " in script and "--no-deps" in script
    assert "trap rollback_uncommitted_install EXIT" in script
    assert script.index("verify_install\n") < script.index("commit_install\n")
    main = script.index("main()")
    fresh_commit = script.index("commit_install\n", main)
    install_shim = script.index("install_shim\n", main)
    onboarding = script.index("run_onboarding\n", main)
    update_commit = script.index("commit_install\n", fresh_commit + 1)
    assert fresh_commit < install_shim < onboarding < update_commit
    assert "if [[ ${UPDATE_MODE} -eq 0 ]]" in script
    assert "if [[ ${UPDATE_MODE} -eq 1 ]]" in script
    assert "printf -v quoted_bin '%q'" in script
    assert 'if [[ -z "${PERSOME_ROOT:-}" ]]' in script
    assert "sqlite3.sqlite_version_info < (3, 42, 0)" in script
    assert "CREATE VIRTUAL TABLE probe USING fts5(body)" in script
    assert '"${VENV_DIR}/bin/python" - "${ROOT_DIR}/src" <<\'PY\'' in script
    assert "sys.path.insert(0, sys.argv[1])" in script
    assert "sys.path.insert(0, '${ROOT_DIR}/src')" not in script


def test_build_backend_is_exactly_pinned() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        build = tomllib.load(handle)["build-system"]
    assert build["requires"] == ["hatchling==1.31.0"]
    constraints = (ROOT / "build-constraints.txt").read_text(encoding="utf-8")
    assert "hatchling==1.31.0 --hash=sha256:" in constraints
    assert constraints.count("--hash=sha256:") == 5


def test_workflow_actions_are_pinned_to_full_commits() -> None:
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    uses = []
    for workflow in workflows:
        uses.extend(
            line.strip().removeprefix("- uses: ")
            for line in workflow.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- uses:")
        )
    assert uses
    for action in uses:
        ref = action.split("#", 1)[0].strip().rsplit("@", 1)[-1]
        assert re.fullmatch(r"[0-9a-f]{40}", ref), action


def test_release_requires_main_ancestry_and_attests_artifacts() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "permissions:\n  contents: read" in workflow
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in workflow
    assert "actions/attest-build-provenance@" in workflow
    assert "attestations: write" in workflow
    assert "subject-checksums: dist/SHA256SUMS" in workflow


def test_workflows_never_implicitly_build_project_before_hash_verification() -> None:
    for name in ("ci.yml", "release.yml"):
        workflow = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "uv sync --all-extras --locked --no-install-project --no-build" in workflow
        assert "uv build --wheel" in workflow
        assert "--build-constraints build-constraints.txt --require-hashes" in workflow
        assert "uv pip install --python .venv/bin/python --no-deps --force-reinstall" in workflow
        assert "uv run --no-sync" in workflow
        assert "uv sync --all-extras --locked\n" not in workflow
