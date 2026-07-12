from pathlib import Path
from types import SimpleNamespace

import pytest

from persome import cli
from scripts import build_pypi_dist


def test_rewrite_distribution_name_keeps_runtime_package_and_changes_public_name(
    tmp_path: Path,
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """[project]
name = "persome-core"
version = "0.3.1"

[tool.persome.release]
pypi-distribution = "personal-model"
""",
        encoding="utf-8",
    )

    assert build_pypi_dist._rewrite_distribution_name(pyproject) == (
        "personal-model",
        "0.3.1",
    )
    rewritten = pyproject.read_text(encoding="utf-8")
    assert 'name = "personal-model"' in rewritten
    assert 'pypi-distribution = "personal-model"' in rewritten


def test_rewrite_distribution_name_rejects_unexpected_root_name(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """[project]
name = "personal-model"
version = "0.3.1"

[tool.persome.release]
pypi-distribution = "personal-model"
""",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unexpected root distribution"):
        build_pypi_dist._rewrite_distribution_name(pyproject)


def test_install_source_prefers_public_distribution(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def distribution(name: str) -> SimpleNamespace:
        seen.append(name)
        return SimpleNamespace(
            read_text=lambda filename: (
                '{"url":"file:///tmp/personal-model"}' if filename == "direct_url.json" else None
            )
        )

    monkeypatch.setattr("importlib.metadata.distribution", distribution)
    assert cli._install_source() == "/tmp/personal-model"
    assert seen == ["personal-model"]


def test_install_source_falls_back_to_compatibility_distribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def distribution(name: str) -> SimpleNamespace:
        seen.append(name)
        if name == "personal-model":
            raise LookupError(name)
        return SimpleNamespace(
            read_text=lambda filename: (
                '{"url":"file:///tmp/persome-core"}' if filename == "direct_url.json" else None
            )
        )

    monkeypatch.setattr("importlib.metadata.distribution", distribution)
    assert cli._install_source() == "/tmp/persome-core"
    assert seen == ["personal-model", "persome-core"]
