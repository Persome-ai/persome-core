"""Code projects: scan common dev roots for git repos and profile each."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .base import Signal, collector, home, read_text, run_cmd, top_counts

# Where developers keep code. We scan one level deep under each.
_ROOTS = ["Projects", "Code", "code", "Developer", "dev", "repos", "repo", "work", "src", "git"]

# Home top-level dirs that are never code roots — skip when scanning ~ directly
# (some people keep repos right in their home dir, e.g. ~/acme-mono).
_HOME_SKIP = {
    "Library",
    "Applications",
    "Desktop",
    "Downloads",
    "Documents",
    "Pictures",
    "Music",
    "Movies",
    "Public",
    ".Trash",
}

# manifest filename -> (language label, name-extraction regex group)
_MANIFESTS = [
    ("pyproject.toml", "Python", r'name\s*=\s*["\']([^"\']+)'),
    ("package.json", "JS/TS", r'"name"\s*:\s*"([^"]+)"'),
    ("Cargo.toml", "Rust", r'name\s*=\s*["\']([^"\']+)'),
    ("go.mod", "Go", r"module\s+(\S+)"),
    ("pom.xml", "Java", r"<artifactId>([^<]+)</artifactId>"),
    ("Gemfile", "Ruby", None),
    ("pubspec.yaml", "Dart/Flutter", r"name:\s*(\S+)"),
    ("composer.json", "PHP", r'"name"\s*:\s*"([^"]+)"'),
]


def _origin_org(repo: Path) -> tuple[str | None, str | None]:
    """Return (host, owner/repo) parsed from origin URL, if any."""
    url = run_cmd(["git", "-C", str(repo), "config", "--get", "remote.origin.url"])
    if not url:
        return None, None
    # git@github.com:owner/repo.git  or  https://github.com/owner/repo.git
    m = re.search(r"(?:@|//)([^/:]+)[/:]+(.+?)(?:\.git)?$", url)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _detect_language(repo: Path) -> tuple[str | None, str | None]:
    """Return (language, project-name) from the first manifest found."""
    for fname, lang, name_rx in _MANIFESTS:
        mf = repo / fname
        if not mf.exists():
            continue
        name = None
        if name_rx:
            text = read_text(mf, max_bytes=200_000) or ""
            m = re.search(name_rx, text)
            if m:
                name = m.group(1)
        return lang, name
    return None, None


def _last_commit_epoch(repo: Path) -> int | None:
    out = run_cmd(["git", "-C", str(repo), "log", "-1", "--format=%ct"])
    if out and out.isdigit():
        return int(out)
    return None


def _candidate_dirs() -> list[Path]:
    """Directories that might be git repos: children of known dev roots, plus
    home's own immediate children (some keep repos at ~/<name>)."""
    dirs: list[Path] = []
    for root_name in _ROOTS:
        root = home() / root_name
        if not root.is_dir():
            continue
        try:
            dirs.extend(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
    try:
        for p in home().iterdir():
            if p.is_dir() and not p.name.startswith(".") and p.name not in _HOME_SKIP:
                dirs.append(p)
    except OSError:
        pass
    return dirs


@collector("projects", "代码项目", "projects")
def collect() -> list[Signal]:
    seen: set[Path] = set()
    repos: list[dict[str, Any]] = []

    for child in _candidate_dirs():
        if (child / ".git").exists() and child not in seen:
            seen.add(child)
            host, slug = _origin_org(child)
            lang, name = _detect_language(child)
            epoch = _last_commit_epoch(child)
            repos.append(
                {
                    "name": name or child.name,
                    "path": child.name,
                    "host": host,
                    "slug": slug,
                    "lang": lang,
                    "epoch": epoch or 0,
                }
            )

    if not repos:
        return []

    # Most-recently-active first; cap so the report (and LLM input) stay tight.
    repos.sort(key=lambda r: r["epoch"], reverse=True)
    top = repos[:20]

    rows: list[dict[str, Any]] = []
    for r in top:
        bits = [b for b in (r["lang"], r["slug"]) if b]
        detail = " · ".join(bits)
        rows.append({"name": r["name"], "count": 0, "detail": detail})

    signals = [Signal("Git 仓库", rows, f"共发现 {len(repos)} 个")]

    # Aggregate: which orgs/hosts and languages dominate.
    org_counter: dict[str, int] = {}
    lang_counter: dict[str, int] = {}
    for r in repos:
        if r["slug"]:
            owner = r["slug"].split("/")[0]
            org_counter[f"{r['host']}/{owner}"] = org_counter.get(f"{r['host']}/{owner}", 0) + 1
        if r["lang"]:
            lang_counter[r["lang"]] = lang_counter.get(r["lang"], 0) + 1
    if org_counter:
        signals.append(Signal("代码托管/组织", top_counts(org_counter, 8)))
    if lang_counter:
        signals.append(Signal("项目语言分布", top_counts(lang_counter, 8)))

    return signals
