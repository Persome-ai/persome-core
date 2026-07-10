"""Enforce English for human-authored repository text.

The bundled PP-OCRv6 recognition dictionary is the only exception: its CJK
characters are model data required to recognize a user's multilingual screen.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CJK_SOURCE_TEXT = re.compile(r"[\u3000-\u303f\u3400-\u9fff\uff00-\uffef]")
ALLOWLIST = {Path("ocr_models/PP-OCRv6_tiny_rec/inference.yml")}
TEXT_SUFFIXES = {
    ".cff",
    ".css",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".spec",
    ".sql",
    ".swift",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
TEXT_NAMES = {".gitignore", ".python-version", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES"}
SKIP_PARTS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__"}


def _text_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and not SKIP_PARTS.intersection(path.parts)
        and (path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES)
    )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings: list[str] = []
    for path in _text_files(root):
        relative = path.relative_to(root)
        if relative in ALLOWLIST:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, 1):
            if CJK_SOURCE_TEXT.search(line):
                findings.append(f"{relative}:{line_number}: {line.strip()[:160]}")
    if findings:
        print("Non-English CJK text found outside the OCR dictionary:")
        print("\n".join(f"  {item}" for item in findings))
        return 1
    print("English-language gate clean; bundled PP-OCRv6 character data is allowlisted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
