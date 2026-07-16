"""Markdown memory file I/O — read, write, parse frontmatter, parse entries."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import frontmatter

from .. import paths


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    A crash between writing the temp file and the rename leaves the
    original file untouched; a crash after the rename leaves the new
    file fully written. ``os.replace`` is atomic on POSIX (and on
    Windows for files on the same volume). The temp file lives in the
    target's parent directory so the rename is a same-filesystem move.

    Without this, a daemon SIGKILL / OOM / power loss in the middle of
    ``Path.write_text`` truncates the file — frontmatter or entry text
    half-written, the next read fails to parse.

    Permissions are preserved when overwriting an existing file.
    Newly created files keep ``mkstemp``'s 0o600 default — which is
    appropriate for private memory data; the previous code path
    inherited the umask default (typically 0o644).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # Preserve permissions of the existing file so updates don't
        # silently flip group/other-read bits set by the user.
        with contextlib.suppress(FileNotFoundError):
            os.chmod(tmp_path, path.stat().st_mode & 0o7777)
        os.replace(tmp_path, path)
        # Persist the directory entry so a power loss right after the
        # rename can't leave the dir pointing at neither old nor new.
        # macOS APFS sometimes returns EINVAL on directory fsync; the
        # call is best-effort — failure here is strictly less safe than
        # success, never less safe than skipping.
        with contextlib.suppress(OSError):
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


VALID_PREFIXES = (
    "user-",
    "project-",
    "tool-",
    "topic-",
    "person-",
    "org-",
    "event-",
    "skill-",
    "intent-",
    # (central_proposition + expected_inferences). Added so the schema miner can
    # write through the same markdown-SSOT path (create_file/append_entry →
    # validate_prefix → files table). See migration-D2-cognition §3.3 / MCP-05.
    "schema-",
)

# Subdirectories of memory_dir() that are allowed in path names.
VALID_SUBDIRS: frozenset[str] = frozenset({"skills"})


# Per-path mutex registry. The reducer fires from a daemon thread per
# session, and the daily-tick + on-demand classifiers can fire in
# parallel, so two threads can both call ``append_entry`` (or supersede)
# on the same memory file. Without serialization the read-modify-write
# in those functions silently loses one of the writes — both threads
# read the same base, both write a "+1 entry" version, and the second
# write wins. The FTS index, written outside the file, ends up holding
# rows for entries that don't exist on disk.
#
# The thread mutex is paired with a per-path owner-only ``flock`` below. This
# matters because explicit correction and recovery CLIs can overlap a running
# daemon. Unrelated files retain independent locks; the in-memory registry is
# bounded to roughly one small Lock per memory file over the process lifetime.
_lock_registry_lock = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}


def _lock_for(path: Path) -> threading.Lock:
    """Return the threading.Lock that guards write access to ``path``.

    Resolves the path before keying so a relative and an absolute spelling
    of the same file map to the same lock. ``resolve(strict=False)`` works
    on non-existent paths (the file may not be created yet) and is called
    outside the registry lock so a slow stat doesn't stall other threads.
    """
    key = str(path.resolve())
    with _lock_registry_lock:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _path_locks[key] = lock
        return lock


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Serialize same-file writers across daemon threads and CLI processes."""

    canonical = path.resolve(strict=False)
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
    process_lock_path = paths.root() / ".memory-write-locks" / f"{digest}.lock"
    with _lock_for(path), paths.open_private_lock_file(process_lock_path) as process_lock:
        fcntl.flock(process_lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(process_lock.fileno(), fcntl.LOCK_UN)


ENTRY_HEADING_RE = re.compile(
    r"^##\s*\[(?P<ts>[^\]]+)\]\s*\{id:\s*(?P<id>[a-zA-Z0-9\-]+)\}(?P<tags>[^\n]*)$",
    re.MULTILINE,
)


@dataclass
class ParsedEntry:
    id: str
    timestamp: str
    tags: list[str]
    heading_line: str
    body: str
    superseded_by: str | None = None

    # successor of an earlier entry — a same-direction sharpening rather than a
    # contradiction. It is orthogonal to ``superseded-by``: an UPDATE retires the
    # OLD version (the predecessor carries ``#superseded-by:{this}``, folded out)
    # while THIS new head carries ``refined-from`` so the trail can render

    # tag only forces ``superseded=0`` for the entry CARRYING it (the live head),
    # since a refined head never also carries its own ``superseded-by``.
    refined_from: str | None = None
    # WRITE-02: ``#abstracted-from:{id1,id2,...}`` is a multi-value PROVENANCE tag
    # on a synthesized entry that absorbed N sources. It is NOT a linear chain link
    # (chain semantics ②) — it records which entries were merged, for traversal,
    # and does NOT feed the supersede back-map.
    abstracted_from: list[str] = field(default_factory=list)
    # Meta-cognition layer (Hy-Memory migration): reliability metadata carried as
    # heading colon-tags, orthogonal to chain/supersede semantics (they never feed
    # _superseded_from_tags / the chain back-map).
    #   #confidence:high|medium|low — how trustworthy this memory is (observed vs inferred)
    #   #conflicted                 — contradicts another belief, not yet adjudicated
    #   #occurred:<iso>             — when the underlying event happened (≠ write-time ts)
    confidence: str | None = None
    conflicted: bool = False
    occurred_at: str | None = None


@dataclass
class ParsedFile:
    path: Path
    description: str
    tags: list[str]
    status: str
    created: str
    updated: str
    entry_count: int
    needs_compact: bool
    entries: list[ParsedEntry] = field(default_factory=list)


def memory_path(name: str) -> Path:
    """Resolve a logical memory filename to an absolute path inside memory_dir().

    Accepts either a bare filename (``skill-foo.md``) or a one-level subdir
    path from VALID_SUBDIRS (``skills/skill-foo.md``).
    """
    if "\\" in name:
        raise ValueError(f"memory path must not contain backslashes: {name!r}")
    parts = name.split("/")
    if len(parts) == 2:
        subdir, filename = parts
        if subdir not in VALID_SUBDIRS:
            raise ValueError(
                f"memory subdirectory {subdir!r} not allowed; valid: {sorted(VALID_SUBDIRS)}"
            )
        if not filename.endswith(".md"):
            filename = filename + ".md"
        return paths.memory_dir() / subdir / filename
    if len(parts) != 1:
        raise ValueError(f"memory path must have at most one slash: {name!r}")
    if not name.endswith(".md"):
        name = name + ".md"
    return paths.memory_dir() / name


def validate_prefix(name: str) -> str:
    # Strip optional subdirectory (e.g. "skills/skill-foo.md" → "skill-foo.md")
    filename = name.split("/")[-1]
    stem = filename.removesuffix(".md")
    for p in VALID_PREFIXES:
        if stem.startswith(p) and len(stem) > len(p):
            return p.rstrip("-")
    raise ValueError(f"filename {name!r} must start with one of: {', '.join(VALID_PREFIXES)}")


def today() -> str:
    return date.today().isoformat()


def default_frontmatter(
    *, description: str, tags: list[str], status: str = "active"
) -> dict[str, Any]:
    return {
        "description": description,
        "tags": tags,
        "status": status,
        "created": today(),
        "updated": today(),
        "entry_count": 0,
        "needs_compact": False,
    }


def write_file(path: Path, fm: dict[str, Any], body: str) -> None:
    post = frontmatter.Post(content=body, **fm)
    text = frontmatter.dumps(post) + ("\n" if not body.endswith("\n") else "")
    atomic_write_text(path, text)


def read_file(path: Path) -> ParsedFile:
    if not path.exists():
        raise FileNotFoundError(path)
    post = frontmatter.load(path)
    fm = dict(post.metadata)
    body = post.content
    entries = _parse_entries(body)
    return ParsedFile(
        path=path,
        description=str(fm.get("description", "")),
        tags=list(fm.get("tags", []) or []),
        status=str(fm.get("status", "active")),
        created=str(fm.get("created", "")),
        updated=str(fm.get("updated", "")),
        entry_count=int(fm.get("entry_count", len(entries)) or 0),
        needs_compact=bool(fm.get("needs_compact", False)),
        entries=entries,
    )


def _parse_entries(body: str) -> list[ParsedEntry]:
    entries: list[ParsedEntry] = []
    matches = list(ENTRY_HEADING_RE.finditer(body))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        tag_str = m.group("tags") or ""
        raw_tags = [t.strip() for t in tag_str.split() if t.strip().startswith("#")]
        tags = [t[1:] for t in raw_tags]  # strip leading #
        superseded_by = None
        refined_from = None
        abstracted_from: list[str] = []
        confidence = None
        conflicted = False
        occurred_at = None
        for t in tags:
            if t.startswith("superseded-by:"):
                superseded_by = t.split(":", 1)[1]
            elif t.startswith("refined-from:"):
                refined_from = t.split(":", 1)[1]
            elif t.startswith("abstracted-from:"):
                # multi-value: ``abstracted-from:a,b,c`` → ["a", "b", "c"]
                abstracted_from = [s for s in t.split(":", 1)[1].split(",") if s]
            elif t.startswith("confidence:"):
                confidence = t.split(":", 1)[1] or None
            elif t == "conflicted":
                conflicted = True
            elif t.startswith("occurred:"):
                # ISO timestamp value: split once so the time's own colons survive.
                occurred_at = t.split(":", 1)[1] or None
        entries.append(
            ParsedEntry(
                id=m.group("id"),
                timestamp=m.group("ts"),
                tags=tags,
                heading_line=m.group(0),
                body=body[start:end].strip("\n"),
                superseded_by=superseded_by,
                refined_from=refined_from,
                abstracted_from=abstracted_from,
                confidence=confidence,
                conflicted=conflicted,
                occurred_at=occurred_at,
            )
        )
    return entries


def render_heading(*, timestamp: str, entry_id: str, tags: list[str]) -> str:
    tag_part = "".join(f" #{t}" for t in tags) if tags else ""
    return f"## [{timestamp}] {{id: {entry_id}}}{tag_part}"


def update_frontmatter(path: Path, updates: dict[str, Any]) -> None:
    with file_lock(path):
        post = frontmatter.load(path)
        post.metadata.update(updates)
        atomic_write_text(path, frontmatter.dumps(post) + "\n")


def list_memory_files(*, strict: bool = False) -> list[Path]:
    """List supported memory Markdown without following filesystem links.

    Normal reads skip unsafe or transiently unreadable candidates. Recovery
    callers pass ``strict=True`` because treating an incomplete discovery as a
    complete source set could incorrectly delete snapshot-backed canonical
    nodes that merely failed discovery.
    """
    memory_dir = paths.memory_dir()
    if not memory_dir.exists():
        return []
    if memory_dir.is_symlink():
        if strict:
            raise RuntimeError(f"memory directory must not be a symlink: {memory_dir}")
        return []
    candidates: list[Path] = []
    try:
        with os.scandir(memory_dir) as root_entries:
            for entry in root_entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        if entry.name.endswith(".md") and entry.name != "index.md":
                            candidates.append(Path(entry.path))
                        continue
                    if entry.name.endswith(".md") and entry.name != "index.md":
                        if strict:
                            raise RuntimeError(
                                f"memory Markdown must be one regular file: {entry.path}"
                            )
                        continue
                    if entry.name in VALID_SUBDIRS and entry.is_symlink():
                        if strict:
                            raise RuntimeError(
                                f"memory subdirectory must not be a symlink: {entry.path}"
                            )
                        continue
                    if entry.name not in VALID_SUBDIRS or not entry.is_dir(follow_symlinks=False):
                        continue
                    with os.scandir(entry.path) as nested_entries:
                        for nested in nested_entries:
                            if nested.is_file(follow_symlinks=False):
                                if nested.name.endswith(".md") and nested.name != "index.md":
                                    candidates.append(Path(nested.path))
                                continue
                            if nested.name.endswith(".md") and nested.name != "index.md" and strict:
                                raise RuntimeError(
                                    f"memory Markdown must be one regular file: {nested.path}"
                                )
                except OSError as exc:
                    if strict:
                        raise RuntimeError(
                            f"memory discovery failed for {entry.path}: {exc}"
                        ) from exc
                    continue
    except OSError as exc:
        if strict:
            raise RuntimeError(f"memory discovery failed for {memory_dir}: {exc}") from exc
        return []

    files: list[Path] = []
    memory_root = memory_dir.resolve()
    for path in candidates:
        try:
            paths.ensure_private_file(path)
            path.resolve(strict=True).relative_to(memory_root)
            memory_name(path)
        except (OSError, RuntimeError, ValueError) as exc:
            if strict:
                raise RuntimeError(f"unsafe or unreadable memory file {path}: {exc}") from exc
            continue
        files.append(path)
    return sorted(files, key=memory_name)


def memory_name(path: Path) -> str:
    """Return the canonical root-relative name for a supported memory file."""
    try:
        name = path.relative_to(paths.memory_dir()).as_posix()
    except ValueError as exc:
        raise ValueError(f"memory file is outside the data root: {path}") from exc
    expected = memory_path(name)
    if expected != path:
        raise ValueError(f"unsupported memory file path: {path}")
    return name
