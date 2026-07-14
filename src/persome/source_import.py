"""Read-only imports from local knowledge bases into the production model pipeline."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .session import store as session_store
from .store import fts
from .timeline import store as timeline_store

_TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
_MAX_FILE_BYTES = 2 * 1024 * 1024
_CHUNK_CHARS = 12_000

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


class ImportUI(Protocol):
    def confirm(self, *, title: str, message: str, action: str) -> bool: ...

    def status(self, message: str) -> None: ...


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


def _documents(root: Path) -> list[Path]:
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"import source is not a folder: {root}")
    found: list[Path] = []
    for path in root.rglob("*"):
        try:
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts):
                continue
            if not path.is_file() or path.is_symlink() or path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        found.append(path)
    return sorted(found, key=lambda item: item.relative_to(root).as_posix().casefold())


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
    documents = _documents(root)
    imported = unchanged = skipped = 0
    created_sessions: list[str] = []

    with fts.cursor() as conn:
        ensure_schema(conn)
        for path in documents:
            relative = path.relative_to(root).as_posix()
            try:
                data = path.read_bytes()
                text = data.decode("utf-8-sig")
            except (OSError, UnicodeError):
                skipped += 1
                continue
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
                        created_sessions.append(str(session_id))
                continue

            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            except OSError:
                modified = datetime.now().astimezone()
            file_sessions: list[str] = []
            for index, part in enumerate(parts):
                identity = f"{source_key}:{digest}:{index}"
                short = hashlib.blake2s(identity.encode(), digest_size=10).hexdigest()
                session_id = f"import-{short}"
                # Hash-derived microseconds keep windows stable and collision-free.
                offset = int(short[:6], 16) % 900_000
                start = modified.replace(second=0, microsecond=offset) + timedelta(minutes=index)
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


def offer_obsidian_import(ui: ImportUI, cfg: Any) -> ImportResult | None:
    """Offer the active registered vault as the optional final onboarding step."""
    vaults = discover_obsidian_vaults()
    if not vaults:
        return None
    vault = vaults[0]
    if not ui.confirm(
        title="Bring your history to Persome",
        message=(
            f"Persome found your Obsidian vault “{vault.name}”. Import its Markdown notes "
            "read-only and build your personal model now? Your vault will not be changed. "
            "You can also import a local folder or a Notion export later with "
            "‘persome import-data’."
        ),
        action="Import Obsidian",
    ):
        ui.status("• Data import skipped; run `persome import-data` whenever you are ready")
        return None
    ui.status(f"Importing Obsidian vault {vault.name} (read-only)...")
    result = import_folder(vault, source_type="obsidian")
    if result.session_ids:
        ui.status(f"Building your model from {result.imported} Obsidian notes...")
        build_imported_model(cfg)
    ui.status(
        f"✓ Obsidian import complete: {result.imported} new/changed, "
        f"{result.unchanged} already imported"
    )
    return result
