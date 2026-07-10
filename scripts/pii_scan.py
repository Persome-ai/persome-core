"""Privacy gate for committed source, documentation, and synthetic fixtures.

Real personal captures must never enter this repository. This scanner catches
common secrets, contact data, local paths, and embedded screenshots before a
commit or release. It complements review; it is not proof of anonymization.

Usage (from persome-core/):
    uv run python scripts/pii_scan.py                 # default: the whole tree
    uv run python scripts/pii_scan.py path/to/fixtures --names 张三 李四

Exit 0 means no configured pattern matched. Extend the local name denylist when
reviewing data outside the repository. The CJK name heuristic is advisory.

NOTE: the phone pattern uses word boundaries so it does NOT false-positive on the
long digit runs inside ``domIdentifier`` (a substring there is structural, not a
phone). Keep that — scrubbing a domIdentifier would corrupt the AX structure.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
from pathlib import Path


def _load_local_names() -> list[str]:
    """Real-name denylist, supplied locally — the open-source tree ships none.

    Sources (both optional, merged): the ``PERSOME_PII_NAMES`` environment
    variable (comma-separated) and ``scripts/pii_names.local.txt`` (one name
    per line, ``#`` comments allowed; keep it out of version control)."""
    names: list[str] = []
    env_names = os.environ.get("PERSOME_PII_NAMES", "")
    names += [n.strip() for n in env_names.split(",") if n.strip()]
    here = os.path.dirname(__file__)
    candidates = [os.path.join(here, "pii_names.local.txt")]
    for local in candidates:
        try:
            with open(local, encoding="utf-8") as fh:
                names += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
        except OSError:
            pass
    return names


KNOWN_NAMES = _load_local_names()
PATTERNS = [
    # TLD must be alphabetic — an importmap version pin like `three@0.160.0` is not an email.
    ("email", re.compile(r"[\w.+-]+@(?!example\.com)[\w-]+(\.[\w-]+)*\.[A-Za-z]{2,}\b")),
    ("phone", re.compile(r"\b1[3-9]\d{9}\b")),  # \b avoids domIdentifier false-positives
    ("hex64", re.compile(r"\b[0-9a-f]{64}\b")),
    ("api-key", re.compile(r"\bsk-(?!test\b|test-|old\b|bo\b)[A-Za-z0-9_-]{16,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "home-path",
        re.compile(r"(?:/Users|/home)/(?!me\b|alice\b|bob\b|tester\b)[A-Za-z0-9._-]+"),
    ),
    ("feishu-tenant", re.compile(r"\b[a-z0-9]{12}\.feishu\.cn")),
    # base64-JSON credential shapes (JWTs, Feishu disposable_login_token in captured URLs —
    # a REAL device token shipped in a frozen capture once; catch the whole class).
    ("token-b64", re.compile(r"eyJ[A-Za-z0-9_-]{40,}")),
    # Feishu doc/wiki/slides/base path tokens — resolvable pointers to private docs.
    # Redacted captures use the literal REDACTED_TOKEN, so a real token still flags.
    (
        "feishu-doc-token",
        re.compile(
            r"feishu\.cn/(?:wiki|docx|slides|base|file|sheets|mindnotes)/(?!REDACTED_TOKEN)[A-Za-z0-9]{16,}"
        ),
    ),
]
# Embedded screenshots (image_base64) carry rendered PIXELS that the text
# anonymization + these text patterns are structurally blind to — real names,
# faces, tenant URLs live only in the JPEG. Any non-trivial image_base64 in a
# committed fixture must be stripped to "" before it ships.
_IMAGE_FIELD = re.compile(r'"image_base64"\s*:\s*"([A-Za-z0-9+/=_-]{64,})"')
# Known-synthetic literals that intentionally match a pattern (crypto test vectors, the
# canonical fake CN phone number). Exact-string matches only — anything else still flags.
SYNTHETIC_ALLOW = {
    "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
    "13800138000",
    "git@github.com",  # SSH remote URL shape, not an email
}
# Common words that precede ':'/'说' but are NOT names — keep the heuristic quiet.
_NAME_HEURISTIC = re.compile(r"([一-龥]{2,4})(?:[：:]\s|说[：:])")
_TEXT_SUFFIXES = {
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
_TEXT_NAMES = {".gitignore", ".python-version", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES"}


def _scan_files(root: str) -> list[str]:
    """Return text-bearing files that can contain credentials or personal data."""
    root_path = Path(root)
    if root_path.is_file():
        return [root]
    files: list[str] = []
    for suffix in _TEXT_SUFFIXES:
        pat = f"**/*{suffix}"
        files += glob.glob(os.path.join(root, pat), recursive=True)
    for name in _TEXT_NAMES:
        files += glob.glob(os.path.join(root, "**", name), recursive=True)
    skip = (
        "pii_scan",
        ".git/",
        "__pycache__",
        ".venv",
        ".ruff_cache",
        ".pytest_cache",
        "uv.lock",
        ".env",
    )
    return sorted(f for f in set(files) if not any(s in f for s in skip))


def scan(captures_dir: str, names: list[str]) -> dict[str, list[str]]:
    leaks: dict[str, list[str]] = {}
    for j in _scan_files(captures_dir):
        raw = Path(j).read_text(encoding="utf-8")
        hits = [n for n in names if n in raw]
        for label, rx in PATTERNS:
            for m in rx.finditer(raw):
                if m.group() not in SYNTHETIC_ALLOW:
                    hits.append(f"{label}:{m.group()[:24]}")
                    break
        # Embedded screenshot with real pixel content — text scanning cannot see it.
        if _IMAGE_FIELD.search(raw):
            hits.append("image_base64:embedded-screenshot")
        if hits:
            rel = os.path.relpath(j, captures_dir) if os.path.isdir(captures_dir) else j
            leaks[rel] = hits
    return leaks


def heuristic_names(captures_dir: str) -> collections.Counter:
    """CJK tokens before ':'/'说' that aren't obviously common words — review candidates."""
    allow = set(
        [
            "同事",
            "工作",
            "项目",
            "用户",
            "消息",
            "通知",
            "会议",
            "任务",
            "文件",
            "设置",
            "系统",
            "终端",
            "提醒",
            "内容",
            "时间",
            "群聊",
            "评论",
            "文档",
            "桌面",
            "邮件",
            "报告",
            "问题",
            "助手",
            "智能",
            "模型",
            "数据",
            "服务",
            "接口",
            "记忆",
            "意图",
            "测试",
            "方案",
            "代码",
            "分支",
            "合并",
            "提交",
            "配置",
            "架构",
            "内存",
            "缓存",
        ]
    )
    cand: collections.Counter = collections.Counter()
    for j in glob.glob(os.path.join(captures_dir, "**", "*.json"), recursive=True):
        try:
            vt = json.loads(Path(j).read_text(encoding="utf-8")).get("visible_text") or ""
        except Exception:
            continue
        for m in _NAME_HEURISTIC.findall(vt):
            if m not in allow and not m.startswith("同事"):
                cand[m] += 1
    return cand


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("captures_dir", nargs="?", default=".")
    ap.add_argument("--names", nargs="*", default=[], help="extra real names to scrub-check")
    args = ap.parse_args()

    names = KNOWN_NAMES + args.names
    leaks = scan(args.captures_dir, names)
    n = len(_scan_files(args.captures_dir))
    if leaks:
        print(f"⚠️  {len(leaks)}/{n} files with possible PII:")
        for f, h in leaks.items():
            print(f"   {f}: {h}")
    else:
        print(f"✅ clean — zero known raw PII across {n} files")

    heur = heuristic_names(args.captures_dir)
    if heur:
        print("\nheuristic name-like tokens before ':'/'说' (review for MISSED names):")
        for t, c in heur.most_common(15):
            print(f"   {t}: {c}")

    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
