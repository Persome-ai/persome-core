"""Drift and local-link guards for the generated code-fact atlas."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

from scripts import generate_code_atlas as atlas_generator

REPO = Path(__file__).resolve().parent.parent
ATLAS_DIR = REPO / "docs" / "code-atlas"
GENERATOR = REPO / "scripts" / "generate_code_atlas.py"
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def test_code_atlas_generated_artifacts_are_current() -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_code_atlas_markdown_local_links_resolve() -> None:
    findings: list[str] = []
    markdown_files = sorted(ATLAS_DIR.rglob("*.md"))
    assert markdown_files, "code atlas has no Markdown documents"

    for source in markdown_files:
        text = source.read_text(encoding="utf-8")
        for match in LINK.finditer(text):
            target = match.group(1).strip().split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            relative = unquote(target.split("#", 1)[0].split("?", 1)[0])
            resolved = (source.parent / relative).resolve()
            line = text.count("\n", 0, match.start()) + 1
            try:
                resolved.relative_to(REPO)
            except ValueError:
                findings.append(f"{source.relative_to(REPO)}:{line}: link escapes repo: {target}")
                continue
            if not resolved.exists():
                findings.append(f"{source.relative_to(REPO)}:{line}: missing {target}")

    assert not findings, "Broken code-atlas local links:\n  " + "\n  ".join(findings)


def test_drift_check_rejects_stale_generated_artifacts(tmp_path: Path, monkeypatch, capsys) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    expected = generated / "expected.md"
    expected.write_text("current\n", encoding="utf-8")
    stale = generated / "old-name.md"
    stale.write_text("obsolete\n", encoding="utf-8")

    monkeypatch.setattr(atlas_generator, "ROOT", tmp_path)
    monkeypatch.setattr(atlas_generator, "GENERATED_DIR", generated)
    monkeypatch.setattr(atlas_generator, "GENERATED_PATHS", {expected})

    assert atlas_generator._check({expected: "current\n"}) == 1
    assert "old-name.md (stale generated artifact)" in capsys.readouterr().err


def test_python_stage_symbols_require_real_definitions(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        '"""Mentions dispatch in prose, but does not define it."""\n'
        "PUBLIC_SURFACE = object()\n"
        "def real_function():\n"
        "    dispatch = 'local variable'\n"
        "class RealClass:\n"
        "    def real_method(self):\n"
        "        return None\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(atlas_generator, "ROOT", tmp_path)

    definitions = atlas_generator._python_symbol_definitions(source)
    names = {definition.name for definition in definitions}

    assert {"PUBLIC_SURFACE", "real_function", "RealClass", "real_method"} <= names
    assert "dispatch" not in names


def test_native_stage_symbols_ignore_comment_only_mentions(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "sample.swift"
    source.write_text(
        "// func lineCommentOnly() {}\n"
        "/*\n"
        "func blockCommentOnly() {}\n"
        "*/\n"
        "final class RealType {}\n"
        "func realFunction() {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(atlas_generator, "ROOT", tmp_path)

    definitions = atlas_generator._text_symbol_definitions(source)
    names = {definition.name for definition in definitions}

    assert names == {"RealType", "realFunction"}
