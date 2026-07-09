"""Filesystem-exploration tools — the agent's "eyes" for open-ended discovery.

Fixed collectors reach the *known knowns* (go read git/history/browser). A
person's highest-signal files are often things we can't enumerate ahead of time:
``~/Documents/简历.pdf``, ``~/日记/``, a ``创业计划.md``. So we let the agent
explore the filesystem the way Claude Code explores a repo — list, triage by
name, drill in, read what matters — and fold that into the profile.

The guardrails are baked into the *tools* (Python, not raw ``tree``/``cat``), so
the agent cannot get around them:

- **Home-only.** Every path is resolved and must stay under ``$HOME``.
- **Bounded.** ``list_dir`` caps depth and total entries and skips noise dirs
  (node_modules, Library, caches, venvs, .git …) so ``tree ~`` can't explode.
- **Sensitive denylist.** ``read_file`` hard-refuses secrets and private stores
  (.ssh/.aws/.env/keychains/Mail/Messages …) and anything binary/oversized,
  regardless of how "important" the name looks.

An ``FsRecorder`` notes which dirs were listed and which files were read so the
report can show the depth of exploration.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..logger import get

logger = get("persome.bootstrap")

ToolHandler = Callable[[dict[str, Any]], Any]

# Directories that are pure noise in a home tree — skip when listing.
_IGNORE_DIRS = {
    "node_modules",
    ".git",
    ".svn",
    ".hg",
    "Library",
    ".cache",
    ".Trash",
    ".npm",
    ".pnpm-store",
    ".cargo",
    ".rustup",
    ".gradle",
    ".m2",
    "go",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".next",
    "dist",
    "build",
    "target",
    "DerivedData",
    ".pyenv",
    ".nvm",
    ".rbenv",
    "vendor",
    ".terraform",
    ".gem",
}

# Path segments that mark private/secret stores — read_file hard-refuses these
# even if the agent asks for them by name.
_SENSITIVE_SEGMENTS = {
    ".ssh",
    ".aws",
    ".gnupg",
    ".gpg",
    ".password-store",
    ".kube",
    ".docker",
    ".netrc",
    "keychains",
    ".config/gh",
    "mail",
    "messages",
    "cookies",
    ".mozilla",
}

# Exact filenames that are secret stores (dotenv, credential files, …).
_SENSITIVE_EXACT = {
    "env",
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".pgpass",
    ".my.cnf",
    ".htpasswd",
    "credentials",
    "secrets",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
}
# Substrings in a filename that mark it secret regardless of location.
_SENSITIVE_NAME_TOKENS = ("credential", "secret", "password", "passwd", "_token", ".env")
_SENSITIVE_EXTS = {".pem", ".key", ".p12", ".pfx", ".keychain", ".ovpn", ".kdbx", ".asc"}

# Content backstop: a line like ``API_KEY=…`` / ``SECRET_TOKEN=…`` means this is
# a secret file no matter what it's named. Caught before any content is returned.
_SECRET_LINE = re.compile(
    r"^\s*[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*\s*=\s*\S",
    re.MULTILINE,
)

# Only read text-ish files; skip obvious binaries by extension.
_TEXT_EXTS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".org",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
    ".tsv",
    ".log",
    ".tex",
    ".html",
    ".htm",
    ".xml",
    ".ini",
    ".cfg",
    ".conf",
    "",  # extensionless (READMEs, notes)
}

_MAX_DEPTH = 3
_MAX_ENTRIES = 400
_MAX_READ_BYTES = 32_768

# Day-0 cold-start cares most about these TCC-high-value home folders. They are
# pinned to the front of the top-level walk and each gets its own node budget so
# an alphabetically-earlier deep subtree (e.g. ``.config``) can never starve them.
_PRIORITY_TOP_DIRS = ("Desktop", "Documents", "Downloads")


def _home() -> Path:
    return Path.home().resolve()


def _resolve_under_home(raw: str) -> Path | None:
    """Resolve ``raw`` (relative to home, ~-prefixed, or absolute) and confirm
    it's inside home. ``~`` maps to ``_home()`` (not the process ``$HOME``)."""
    home = _home()
    raw = (raw or "~").strip()
    if raw.startswith("~"):
        rest = raw[1:].lstrip("/")
        p = home / rest if rest else home
    else:
        p = Path(raw)
        if not p.is_absolute():
            p = home / p
    try:
        p = p.resolve()
    except OSError:
        return None
    if p != home and home not in p.parents:
        return None
    return p


def _is_sensitive(path: Path) -> bool:
    lower_parts = [seg.lower() for seg in path.parts]
    joined = "/".join(lower_parts)
    if any(seg in lower_parts for seg in _SENSITIVE_SEGMENTS):
        return True
    if any(s in joined for s in _SENSITIVE_SEGMENTS if "/" in s):
        return True
    name = path.name.lower()
    if name in _SENSITIVE_EXACT:
        return True
    if name.startswith(".env"):
        return True
    if any(tok in name for tok in _SENSITIVE_NAME_TOKENS):
        return True
    return path.suffix.lower() in _SENSITIVE_EXTS


def _rel_age(mtime: float, now: float) -> str:
    """Human relative age so the agent can weigh recency, e.g. '3天前' / '2年前'."""
    days = int((now - mtime) / 86400)
    if days <= 0:
        return "今天"
    if days < 30:
        return f"{days}天前"
    if days < 365:
        return f"{days // 30}个月前"
    return f"{days // 365}年前"


@dataclass
class FsRecorder:
    listed_dirs: list[str] = field(default_factory=list)
    read_files: list[str] = field(default_factory=list)


def _list_dir(path: str, depth: int, recorder: FsRecorder) -> dict[str, Any]:
    base = _resolve_under_home(path or "~")
    if base is None:
        return {"error": "path must be inside your home directory"}
    if not base.is_dir():
        return {"error": f"not a directory: {path}"}
    depth = max(1, min(int(depth or 2), _MAX_DEPTH))
    recorder.listed_dirs.append(str(base))

    lines: list[str] = []
    count = 0
    truncated = False
    now = time.time()

    def walk(d: Path, prefix: str, level: int) -> None:
        nonlocal count, truncated
        if level > depth or truncated:
            return
        try:
            entries = sorted(
                os.scandir(d), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower())
            )
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") and e.name not in {".config"}:
                continue
            if e.is_dir(follow_symlinks=False) and e.name in _IGNORE_DIRS:
                continue
            if count >= _MAX_ENTRIES:
                truncated = True
                return
            count += 1
            is_dir = e.is_dir(follow_symlinks=False)
            # Tag each entry with its modified age so the agent can judge
            # recency — a 2-year-old resume should not be read as current.
            try:
                age = _rel_age(e.stat(follow_symlinks=False).st_mtime, now)
            except OSError:
                age = "?"
            lines.append(f"{prefix}{e.name}{'/' if is_dir else ''}  [{age}]")
            if is_dir:
                walk(Path(e.path), prefix + "  ", level + 1)

    walk(base, "", 1)
    result: dict[str, Any] = {
        "path": str(base),
        "depth": depth,
        "tree": "\n".join(lines),
        "hint": "the [..] after each entry is its modified time; prefer recently modified ones, "
        "old files may be stale.",
    }
    if truncated:
        result["note"] = f"truncated at {_MAX_ENTRIES} entries — narrow with a deeper subpath"
    return result


def _read_file(path: str, recorder: FsRecorder) -> dict[str, Any]:
    p = _resolve_under_home(path)
    if p is None:
        return {"error": "path must be inside your home directory"}
    if _is_sensitive(p):
        return {"error": "refused: sensitive/private file (secrets, keys, mail, etc.)"}
    if not p.is_file():
        return {"error": f"not a file: {path}"}
    if p.suffix.lower() not in _TEXT_EXTS:
        return {"error": f"skipped non-text file ({p.suffix or 'binary'})"}
    try:
        st = p.stat()
    except OSError:
        return {"error": "unreadable"}
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(_MAX_READ_BYTES)
    except OSError:
        return {"error": "unreadable"}
    # Content backstop: refuse dotenv/credential-shaped files no matter the name,
    # so a misnamed secret store (e.g. a file literally called ``env``) can't leak.
    if _SECRET_LINE.search(content):
        return {"error": "refused: file contains secret-shaped key=value lines"}
    recorder.read_files.append(str(p))
    # ``modified`` lets the agent weigh how current this content is.
    out: dict[str, Any] = {
        "path": str(p),
        "modified": _rel_age(st.st_mtime, time.time()),
        "content": content,
    }
    if st.st_size > _MAX_READ_BYTES:
        out["note"] = f"truncated to first {_MAX_READ_BYTES} bytes of {st.st_size}"
    return out


def scan_home_tree(
    *,
    max_depth: int = 4,
    max_entries_per_dir: int = 40,
    max_total_nodes: int = 400,
    exclude: frozenset[str] = frozenset(),
) -> str:
    """A bounded ``ls -R ~`` for the triage LLM — names only, never file content.

    Walks the whole home tree (not just the 3 scoped folders) so the LLM can pick
    high-value folders by name, but stays cheap and privacy-safe via three caps:

    - **depth** — recurse at most ``max_depth`` levels below home.
    - **per-dir entries** — show at most ``max_entries_per_dir`` children of each
      directory (dirs first, then a few sample file names); the rest collapse into
      a ``(+N more)`` note.
    - **per-subtree nodes** — ``max_total_nodes`` is split into a per-top-level
      budget so no single subtree can eat the whole walk. When a subtree exhausts
      its share, its tail collapses into a ``(… node cap N reached)`` note, but its
      sibling top-level dirs are unaffected.

    Top-level fairness: every (non-ignored, non-sensitive, non-excluded) top-level
    home folder is *always named*, and ``Desktop``/``Documents``/``Downloads`` —
    the TCC-high-value cold-start folders — are walked first. This fixes the old
    starvation bug where a single global node budget, consumed depth-first, let an
    alphabetically-earlier deep subtree (``.config``) burn the whole budget before
    the D-folders were ever reached.

    ``_IGNORE_DIRS`` (node_modules / Library / caches …) and ``_is_sensitive``
    (.ssh / .aws / keychains …) subtrees are skipped entirely — they are neither
    descended into nor named. Output is a compact indented tree:

    ```
    ~/
      Desktop/  (5 files: screenshot.png, todo.md, …)
      Documents/  (12 files: 简历.pdf, 创业计划.md, notes.md, …)
        work/  (3 files: contract.pdf, …)
      Downloads/  (8 files: …)
      Projects/  (dirs only)
    ```

    Only names — no content — ever leave this function.
    """
    home = _home()
    max_depth = max(1, int(max_depth))
    max_entries_per_dir = max(1, int(max_entries_per_dir))
    max_total_nodes = max(1, int(max_total_nodes))

    def child_dirs_files(d: Path, level: int) -> tuple[list[os.DirEntry[str]], list[str]]:
        """Filtered + sorted (dirs, file-names) for one directory."""
        try:
            entries = list(os.scandir(d))
        except OSError:
            return [], []
        dirs: list[os.DirEntry[str]] = []
        files: list[str] = []
        for e in entries:
            name = e.name
            if name.startswith(".") and name != ".config":
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                if name in _IGNORE_DIRS or _is_sensitive(Path(e.path)):
                    continue
                # User un-checked this top-level home folder on the permission
                # screen — never scan or even name it.
                if level == 1 and name in exclude:
                    continue
                dirs.append(e)
            else:
                if _is_sensitive(Path(e.path)):
                    continue
                files.append(name)
        dirs.sort(key=lambda e: e.name.lower())
        files.sort(key=str.lower)
        return dirs, files

    lines: list[str] = ["~/"]

    # Top-level directories, fairly ordered: priority TCC folders first (in their
    # fixed order), then everything else alphabetically. The top-level *names* are
    # cheap and always emitted, so D-folders can never be starved by a sibling.
    top_dirs, top_files = child_dirs_files(home, 1)
    priority = [e for n in _PRIORITY_TOP_DIRS for e in top_dirs if e.name == n]
    rest = [e for e in top_dirs if e.name not in _PRIORITY_TOP_DIRS]
    ordered_top = priority + rest

    # Split the node budget across the top-level subtrees so each gets a fair share
    # (with a sane floor), instead of a single global cap that the first subtree can
    # exhaust. The home root's own files don't consume any subtree's budget.
    per_subtree = max(8, max_total_nodes // max(1, len(ordered_top)))

    def walk(d: Path, indent: str, level: int, budget: list[int]) -> None:
        """Recurse a single subtree, drawing nodes from this subtree's ``budget``."""
        if level > max_depth:
            return
        dirs, files = child_dirs_files(d, level)

        # Emit this directory's files as a compact one-line summary, then recurse
        # into its subdirectories (which become their own indented headers).
        shown_files = files[: min(len(files), max_entries_per_dir)]
        if files:
            sample = ", ".join(shown_files)
            extra = len(files) - len(shown_files)
            tail = f", +{extra} more" if extra > 0 else ""
            lines.append(f"{indent}  ({len(files)} files: {sample}{tail})")
        elif not dirs:
            lines.append(f"{indent}  (empty)")

        for e in dirs:
            if budget[0] <= 0:
                lines.append(f"{indent}  (… node cap {per_subtree} reached)")
                return
            budget[0] -= 1
            lines.append(f"{indent}{e.name}/")
            walk(Path(e.path), indent + "  ", level + 1, budget)

    # Home root's own loose files (shown once, not charged to any subtree).
    shown_root = top_files[: min(len(top_files), max_entries_per_dir)]
    if top_files:
        sample = ", ".join(shown_root)
        extra = len(top_files) - len(shown_root)
        tail = f", +{extra} more" if extra > 0 else ""
        lines.append(f"  ({len(top_files)} files: {sample}{tail})")

    for e in ordered_top:
        lines.append(f"{e.name}/")
        walk(Path(e.path), "  ", 2, [per_subtree])

    return "\n".join(lines)


def build_fs_tools() -> tuple[list[dict[str, Any]], dict[str, ToolHandler], FsRecorder]:
    """Return (schemas, handlers, recorder) for list_dir + read_file."""
    recorder = FsRecorder()

    schemas: list[dict[str, Any]] = [
        {
            "name": "list_dir",
            "description": (
                "List a bounded subtree of a directory under home (depth-capped, skips "
                "node_modules/Library/caches and other noise, entry-capped). Use it to discover "
                "high-value folders (résumé/notes/journal/projects). Each entry is tagged with "
                "its modified time [..] — judge recency, prefer recently modified. path is "
                "relative to home or absolute, must stay inside home; depth defaults to 2, max 3."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "directory path, defaults to ~"},
                    "depth": {"type": "integer", "description": "recursion depth 1-3, default 2"},
                },
                "required": [],
            },
        },
        {
            "name": "read_file",
            "description": (
                "Read the text body of one file under home (capped at 32KB); the result "
                "includes `modified` (its modified time) for recency. Use it to read "
                "high-value files like résumés/notes/journals. Sensitive files "
                "(keys/.env/keychain/mail/chat) are hard-refused; binary/oversized files are "
                "skipped."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "file path, inside home"}},
                "required": ["path"],
            },
        },
    ]

    def list_dir_handler(args: dict[str, Any]) -> dict[str, Any]:
        return _list_dir(str(args.get("path", "~")), args.get("depth", 2), recorder)

    def read_file_handler(args: dict[str, Any]) -> dict[str, Any]:
        return _read_file(str(args.get("path", "")), recorder)

    handlers: dict[str, ToolHandler] = {
        "list_dir": list_dir_handler,
        "read_file": read_file_handler,
    }
    return schemas, handlers, recorder
