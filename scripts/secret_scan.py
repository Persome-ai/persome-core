"""Fail when repository text contains a likely credential or private key."""

from __future__ import annotations

import re
import sys
from pathlib import Path

PATTERNS = {
    "private key": re.compile("-----BEGIN " + r"[A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "provider API key": re.compile(r"\bsk-(?!test(?:\b|-)|synthetic\b)[A-Za-z0-9_-]{20,}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
}

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
SKIP_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "dist",
    "ocr_models",
}


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
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in PATTERNS.items():
            match = pattern.search(text)
            if match:
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{path.relative_to(root)}:{line}: {label}")
    if findings:
        print("Possible secrets found:")
        print("\n".join(f"  {item}" for item in findings))
        return 1
    print("Secret scan clean; no known credential shapes found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
