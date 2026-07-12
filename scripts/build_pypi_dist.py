#!/usr/bin/env python3
"""Build the public PyPI distribution from the current tagged source tree.

The root project name remains ``persome-core`` as an update-source compatibility
contract for already released runtimes. This builder stages the same tracked
source and changes only the distribution name declared in ``pyproject.toml``.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT_DISTRIBUTION = "persome-core"


def _tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [Path(item.decode()) for item in result.stdout.split(b"\0") if item]


def _rewrite_distribution_name(pyproject: Path) -> tuple[str, str]:
    raw = pyproject.read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    project = data.get("project", {})
    root_name = project.get("name")
    version = project.get("version")
    public_name = (
        data.get("tool", {}).get("persome", {}).get("release", {}).get("pypi-distribution")
    )
    if root_name != ROOT_DISTRIBUTION:
        raise RuntimeError(f"unexpected root distribution name: {root_name!r}")
    if not isinstance(version, str) or not version:
        raise RuntimeError("project.version must be a non-empty string")
    if not isinstance(public_name, str) or not public_name:
        raise RuntimeError("tool.persome.release.pypi-distribution is required")
    declaration = f'name = "{ROOT_DISTRIBUTION}"'
    if raw.count(declaration) != 1:
        raise RuntimeError(f"expected one exact {declaration!r} declaration")
    pyproject.write_text(
        raw.replace(declaration, f'name = "{public_name}"', 1),
        encoding="utf-8",
    )
    return public_name, version


def build(root: Path, out_dir: Path) -> tuple[Path, Path]:
    root = root.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="persome-pypi-stage-") as temporary:
        stage = Path(temporary) / "source"
        stage.mkdir()
        for relative in _tracked_files(root):
            source = root / relative
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target.symlink_to(source.readlink())
            elif source.is_file():
                shutil.copy2(source, target)

        public_name, version = _rewrite_distribution_name(stage / "pyproject.toml")
        normalized = public_name.replace("-", "_")
        for stale in out_dir.glob(f"{normalized}-*"):
            stale.unlink()
        subprocess.run(
            [
                "uv",
                "build",
                "--project",
                str(stage),
                "--out-dir",
                str(out_dir),
                "--build-constraints",
                str(stage / "build-constraints.txt"),
                "--require-hashes",
            ],
            check=True,
        )

    wheel = out_dir / f"{normalized}-{version}-py3-none-any.whl"
    sdist = out_dir / f"{normalized}-{version}.tar.gz"
    if not wheel.is_file() or not sdist.is_file():
        raise RuntimeError(f"missing expected PyPI artifacts: {wheel.name}, {sdist.name}")
    return wheel, sdist


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=Path("pypi-dist"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    wheel, sdist = build(root, args.out_dir)
    print(wheel)
    print(sdist)


if __name__ == "__main__":
    main()
