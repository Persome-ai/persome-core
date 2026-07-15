"""Read-only imports from local knowledge bases into the production model pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from . import paths
from .session import store as session_store
from .store import fts
from .timeline import store as timeline_store

_TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_DOCUMENTS = 2_000
_MAX_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_IMPORT_SESSIONS = 2_000
_CHUNK_CHARS = 12_000
_COUNT_CACHE_TTL_SECONDS = 60.0
_count_cache_lock = threading.Lock()
_count_cache: dict[Path, tuple[float, int]] = {}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_imports (
    source_key TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    session_ids TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ImportResult:
    source_type: str
    root: Path
    discovered: int = 0
    imported: int = 0
    unchanged: int = 0
    skipped: int = 0
    session_ids: list[str] = field(default_factory=list)


class ImportLimitError(ValueError):
    """A source exceeded a bounded, owner-actionable import limit."""


class ImportUI(Protocol):
    def status(self, message: str) -> None: ...

    def choose_import_sources(self, choices: list[str]) -> list[str]: ...

    def choose_folder(self, prompt: str) -> Path | None: ...


def ensure_schema(conn: Any) -> None:
    conn.executescript(_SCHEMA)


def discover_obsidian_vaults(home: Path | None = None) -> list[Path]:
    """Return registered desktop vaults, with the currently open vault first."""
    home = home or Path.home()
    registry = home / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    try:
        raw = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    vaults = raw.get("vaults", raw) if isinstance(raw, dict) else {}
    if not isinstance(vaults, dict):
        return []
    ranked: list[tuple[bool, int, Path]] = []
    for value in vaults.values():
        if not isinstance(value, dict) or not isinstance(value.get("path"), str):
            continue
        path = Path(value["path"]).expanduser()
        if path.is_dir() and (path / ".obsidian").is_dir():
            ranked.append((bool(value.get("open")), int(value.get("ts") or 0), path.resolve()))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return list(dict.fromkeys(path for _, _, path in ranked))


def notion_is_installed(home: Path | None = None) -> bool:
    home = home or Path.home()
    return any(
        path.is_dir()
        for path in (
            Path("/Applications/Notion.app"),
            home / "Applications" / "Notion.app",
        )
    )


def _documents(root: Path) -> list[Path]:
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"import source is not a folder: {root}")
    found: list[Path] = []
    total_bytes = 0
    for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        directory_names[:] = sorted(
            (
                name
                for name in directory_names
                if not name.startswith(".") and not (directory_path / name).is_symlink()
            ),
            key=str.casefold,
        )
        for name in sorted(file_names, key=str.casefold):
            path = directory_path / name
            if name.startswith(".") or path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            if size > _MAX_FILE_BYTES:
                continue
            if len(found) >= _MAX_DOCUMENTS:
                raise ImportLimitError(
                    f"source has more than {_MAX_DOCUMENTS:,} importable documents; "
                    "choose a smaller folder"
                )
            if total_bytes + size > _MAX_TOTAL_BYTES:
                raise ImportLimitError(
                    f"source exceeds the {_MAX_TOTAL_BYTES // (1024 * 1024)} MiB import limit; "
                    "choose a smaller folder"
                )
            found.append(path)
            total_bytes += size
    return sorted(found, key=lambda item: item.relative_to(root).as_posix().casefold())


def count_documents(root: Path) -> int:
    """Return the safely importable text-file count without reading contents."""
    root = root.expanduser().resolve(strict=True)
    _validate_source_root(root)
    now = time.monotonic()
    with _count_cache_lock:
        cached = _count_cache.get(root)
        if cached is not None and now - cached[0] < _COUNT_CACHE_TTL_SECONDS:
            return cached[1]
    count = len(_documents(root))
    with _count_cache_lock:
        _count_cache[root] = (now, count)
    return count


def _validate_source_root(root: Path) -> None:
    """Prevent generated Persome state from feeding back into its own model."""
    private_root = paths.root().expanduser().resolve()
    if root == private_root or private_root in root.parents or root in private_root.parents:
        raise ValueError("the Persome data directory cannot be used as an import source")


def _read_stable_utf8(path: Path) -> tuple[bytes, str, int] | None:
    """Read without following a symlink and reject a file that changed mid-read."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                data = handle.read(_MAX_FILE_BYTES + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if len(data) > _MAX_FILE_BYTES:
            return None
        fingerprint_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        fingerprint_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if fingerprint_before != fingerprint_after or len(data) != after.st_size:
            return None
        return data, data.decode("utf-8-sig"), after.st_mtime_ns
    except (OSError, UnicodeError):
        return None


def _chunks(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > _CHUNK_CHARS:
        cut = text.rfind("\n\n", 0, _CHUNK_CHARS)
        if cut < _CHUNK_CHARS // 2:
            cut = _CHUNK_CHARS
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def import_folder(root: Path, *, source_type: str = "folder") -> ImportResult:
    """Stage changed text files as ended sessions; the shared writer models them."""
    root = root.expanduser().resolve(strict=True)
    _validate_source_root(root)
    documents = _documents(root)
    imported = unchanged = skipped = 0
    created_sessions: list[str] = []

    with fts.cursor() as conn:
        ensure_schema(conn)
        for path in documents:
            relative = path.relative_to(root).as_posix()
            stable = _read_stable_utf8(path)
            if stable is None:
                skipped += 1
                continue
            data, text, modified_ns = stable
            parts = _chunks(text)
            if not parts:
                skipped += 1
                continue
            digest = hashlib.sha256(data).hexdigest()
            source_key = hashlib.sha256(f"{source_type}\0{root}\0{relative}".encode()).hexdigest()
            previous = conn.execute(
                "SELECT content_hash FROM source_imports WHERE source_key=?", (source_key,)
            ).fetchone()
            if previous is not None and previous[0] == digest:
                unchanged += 1
                row = conn.execute(
                    "SELECT session_ids FROM source_imports WHERE source_key=?", (source_key,)
                ).fetchone()
                try:
                    prior_ids = json.loads(row[0]) if row else []
                except (TypeError, ValueError):
                    prior_ids = []
                for session_id in prior_ids:
                    session = session_store.get_by_id(conn, str(session_id))
                    if session is not None and session.modeled_at is None:
                        if len(created_sessions) >= _MAX_IMPORT_SESSIONS:
                            raise ImportLimitError(
                                f"source exceeds the {_MAX_IMPORT_SESSIONS:,}-session import "
                                "limit; choose a smaller folder"
                            )
                        created_sessions.append(str(session_id))
                continue

            modified = datetime.fromtimestamp(modified_ns / 1_000_000_000).astimezone()
            latest = min(modified, datetime.now().astimezone()).replace(second=0)
            if len(created_sessions) + len(parts) > _MAX_IMPORT_SESSIONS:
                raise ImportLimitError(
                    f"source exceeds the {_MAX_IMPORT_SESSIONS:,}-session import limit; "
                    "choose a smaller folder"
                )
            file_sessions: list[str] = []
            for index, part in enumerate(parts):
                identity = f"{source_key}:{digest}:{index}"
                short = hashlib.blake2s(identity.encode(), digest_size=10).hexdigest()
                session_id = f"import-{short}"
                # Hash-derived microseconds keep windows stable and collision-free.
                offset = int(short[:6], 16) % 900_000
                start = latest.replace(microsecond=offset) - timedelta(
                    minutes=len(parts) - 1 - index
                )
                end = start + timedelta(seconds=59)
                heading = f"Imported {source_type} note: {relative}"
                block = timeline_store.TimelineBlock(
                    id=f"tlb-{session_id}",
                    start_time=start,
                    end_time=end,
                    timezone=str(start.tzinfo or ""),
                    entries=[heading],
                    apps_used=[source_type.title()],
                    capture_count=1,
                    focus_excerpt=(
                        f"Source: {source_type}\nPath: {relative}\n"
                        f"Content SHA-256: {digest}\n\n{part}"
                    ),
                )
                timeline_store.insert(conn, block)
                session_store.insert(
                    conn,
                    session_store.SessionRow(
                        id=session_id,
                        start_time=start,
                        end_time=end,
                        status="ended",
                    ),
                )
                file_sessions.append(session_id)
                created_sessions.append(session_id)
            conn.execute(
                """INSERT INTO source_imports
                   (source_key, source_type, source_path, content_hash, imported_at, session_ids)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_key) DO UPDATE SET
                     content_hash=excluded.content_hash,
                     imported_at=excluded.imported_at,
                     session_ids=excluded.session_ids""",
                (
                    source_key,
                    source_type,
                    str(path),
                    digest,
                    datetime.now().astimezone().isoformat(),
                    json.dumps(file_sessions),
                ),
            )
            imported += 1

    return ImportResult(
        source_type=source_type,
        root=root,
        discovered=len(documents),
        imported=imported,
        unchanged=unchanged,
        skipped=skipped,
        session_ids=created_sessions,
    )


def build_imported_model(cfg: Any) -> Any:
    """Run the existing writer/model build; imports never get a private writer path."""
    from .model import run_model_build

    return run_model_build(cfg, trigger="source-import")


def require_complete_model_build(result: Any) -> None:
    """Reject a degraded build instead of presenting partial geometry as ready."""
    manifest = getattr(result, "manifest", None)
    degraded = manifest.get("degraded_stages", []) if isinstance(manifest, dict) else []
    if getattr(result, "status", None) != "complete" or degraded:
        detail = ", ".join(str(stage) for stage in degraded) or "unknown stage"
        raise RuntimeError(f"personal model build is degraded ({detail}); retry setup")


def offer_data_import(ui: ImportUI, cfg: Any) -> list[ImportResult]:
    """Show only locally relevant sources, import selections, then build once."""
    # Older test/product UIs can complete the hard onboarding gate without the
    # optional import surface. The production OnboardingUI implements both.
    if not hasattr(ui, "choose_import_sources") or not hasattr(ui, "choose_folder"):
        return []
    vaults = discover_obsidian_vaults()
    local_label = "Local folder"
    obsidian_label = f"Obsidian — {vaults[0].name}" if vaults else ""
    notion_label = "Notion export" if notion_is_installed() else ""
    choices = [local_label]
    choices.extend(label for label in (obsidian_label, notion_label) if label)
    selected = ui.choose_import_sources(choices)
    if not selected:
        ui.status("• Data import skipped; run `persome import-data` whenever you are ready")
        return []

    sources: list[tuple[str, Path]] = []
    if obsidian_label and obsidian_label in selected:
        sources.append(("obsidian", vaults[0]))
    if local_label in selected:
        folder = ui.choose_folder("Choose a local folder of Markdown or text files")
        if folder is not None:
            sources.append(("folder", folder))
    if notion_label and notion_label in selected:
        folder = ui.choose_folder("Choose your unpacked Notion Markdown export folder")
        if folder is not None:
            sources.append(("notion", folder))

    results: list[ImportResult] = []
    for source_type, root in sources:
        ui.status(f"Importing {source_type} data from {root.name} (read-only)...")
        try:
            results.append(import_folder(root, source_type=source_type))
        except (OSError, ValueError) as exc:
            ui.status(f"• Could not import {source_type}: {exc}")
    if results:
        imported = sum(result.imported for result in results)
        ui.status(f"Building your model from {imported} new or changed documents...")
        require_complete_model_build(build_imported_model(cfg))
    if results:
        ui.status(
            "✓ Data import complete: "
            f"{sum(result.imported for result in results)} new/changed, "
            f"{sum(result.unchanged for result in results)} already imported, "
            f"{sum(result.skipped for result in results)} skipped"
        )
    return results
