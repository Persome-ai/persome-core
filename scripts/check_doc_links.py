"""Verify that local links and image assets in committed Markdown resolve."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SKIP_PARTS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "dist"}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings: list[str] = []
    markdown = sorted(
        path
        for path in root.rglob("*.md")
        if not SKIP_PARTS.intersection(path.relative_to(root).parts)
    )
    for source in markdown:
        text = source.read_text(encoding="utf-8")
        for match in LINK.finditer(text):
            target = match.group(1).strip().split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            relative = unquote(target.split("#", 1)[0].split("?", 1)[0])
            resolved = (source.parent / relative).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                findings.append(f"{source.relative_to(root)}: link escapes repository: {target}")
                continue
            if not resolved.exists():
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{source.relative_to(root)}:{line}: missing {target}")
    if findings:
        print("Broken local documentation links:")
        print("\n".join(f"  {item}" for item in findings))
        return 1
    print(f"Documentation links clean across {len(markdown)} Markdown files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
