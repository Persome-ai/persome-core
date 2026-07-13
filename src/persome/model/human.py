"""Owner-local ``HUMAN.md`` projection of the versioned personal model."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import paths
from .snapshot import SCHEMA_VERSION, validate_snapshot

HUMAN_SCHEMA_VERSION = 1
HUMAN_RENDERER_VERSION = 1
_PROJECTION_NAME = "persome-model"
_MAX_FACES = 8
_MAX_VOLUMES = 8
_HANDLE_RE = re.compile(r"⟨([^⟨⟩]+)⟩")
_SUPPORTED_BUILD_STATUSES = frozenset({"complete", "degraded"})


class HumanMarkdownConflict(RuntimeError):
    """The target exists but is not a Persome-managed HUMAN.md projection."""


@dataclass(frozen=True)
class _ManagedHuman:
    values: dict[str, Any]
    identity: tuple[int, int]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


def _yaml_scalar(value: Any) -> str:
    """Return a JSON scalar, which is also a safe YAML scalar."""
    return json.dumps(value, ensure_ascii=False)


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _safe_markdown(value: Any, *, multiline: bool = False) -> str:
    """Neutralize Markdown links, images, HTML, code fences, and block markers."""
    text = str(value or "")
    if not multiline:
        text = _one_line(text)
    text = (
        text.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    if not multiline:
        return text

    safe_lines: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]{2,}", " ", line).rstrip()
        if re.match(r"^\s*(?:#{1,6}|>|[-+*]\s|\d+[.)]\s|~~~)", line):
            first = len(line) - len(line.lstrip())
            line = f"{line[:first]}\\{line[first:]}"
        safe_lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(safe_lines)).strip()


def _schema_sort_key(item: dict[str, Any]) -> tuple[float, float, str]:
    return (
        -float(item.get("observations") or 0),
        -float(item.get("confidence") or 0.0),
        str(item.get("id") or ""),
    )


def _known_handle_map(
    root: dict[str, Any] | None,
    volumes: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    members = (root or {}).get("members")
    if isinstance(members, list):
        root_members = {str(member) for member in members}
        eligible = [item for item in volumes if str(item.get("id") or "") in root_members]
    else:
        eligible = volumes
    return {
        _one_line(item.get("signature")).casefold(): item
        for item in eligible
        if _one_line(item.get("signature"))
    }


def _portrait_and_handles(
    signature: str,
    known_handles: dict[str, dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        value = _one_line(match.group(1))
        item = known_handles.get(value.casefold())
        if item is None:
            return "" if value.casefold() == "truncated" else match.group(0)
        item_id = str(item.get("id") or "")
        if item_id and item_id not in seen:
            seen.add(item_id)
            selected.append(item)
        return ""

    portrait = _HANDLE_RE.sub(replace, signature)
    portrait = re.sub(r"[ \t]*\(\s*\)", "", portrait)
    portrait = re.sub(r"[ \t]+([,.;:!?])", r"\1", portrait)
    portrait = _safe_markdown(portrait, multiline=True)
    return portrait, selected


def _schema_bullet(item: dict[str, Any]) -> str:
    signature = _safe_markdown(item.get("signature"))
    observations = int(item.get("observations") or 0)
    confidence = float(item.get("confidence") or 0.0)
    return f"- {signature} _(evidence {observations}; confidence {confidence:.2f})_"


def _selected_volumes(
    root: dict[str, Any] | None,
    volumes: list[dict[str, Any]],
    referenced: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected = list(referenced)
    seen = {str(item.get("id") or "") for item in selected}
    members = (root or {}).get("members")
    if isinstance(members, list):
        root_members = {str(member) for member in members}
        candidates = [item for item in volumes if str(item.get("id") or "") in root_members]
    else:
        candidates = volumes
    for item in sorted(candidates, key=_schema_sort_key):
        item_id = str(item.get("id") or "")
        if item_id and item_id not in seen:
            seen.add(item_id)
            selected.append(item)
    return selected[:_MAX_VOLUMES]


def _frontmatter(
    snapshot: dict[str, Any],
    *,
    redacted: bool,
) -> list[str]:
    root = snapshot.get("root") if isinstance(snapshot.get("root"), dict) else None
    build = snapshot.get("build") if isinstance(snapshot.get("build"), dict) else {}
    return [
        "---",
        f"human_schema_version: {HUMAN_SCHEMA_VERSION}",
        f"renderer_version: {HUMAN_RENDERER_VERSION}",
        f"model_schema_version: {_yaml_scalar(snapshot.get('schema_version'))}",
        f"generated_at: {_yaml_scalar(snapshot.get('generated_at'))}",
        f"build_id: {_yaml_scalar(build.get('build_id'))}",
        f"build_status: {_yaml_scalar(build.get('status'))}",
        f"root_id: {_yaml_scalar(root.get('id') if root else None)}",
        'visibility: "owner-only"',
        f"redacted: {str(redacted).lower()}",
        f"projection: {_yaml_scalar(_PROJECTION_NAME)}",
        "---",
    ]


def render_human_markdown(snapshot: dict[str, Any], *, redacted: bool = False) -> str:
    """Render a deterministic, compact Markdown view without another LLM call."""
    validate_snapshot(snapshot)
    root = snapshot.get("root") if isinstance(snapshot.get("root"), dict) else None
    build = snapshot.get("build") if isinstance(snapshot.get("build"), dict) else {}
    volumes = [dict(item) for item in snapshot.get("volumes") or []]
    known_handles = _known_handle_map(root, volumes)
    portrait, referenced = _portrait_and_handles(
        str(root.get("signature") or "") if root else "",
        known_handles,
    )
    faces = sorted(
        (dict(item) for item in snapshot.get("faces") or [] if _one_line(item.get("signature"))),
        key=_schema_sort_key,
    )[:_MAX_FACES]
    selected_volumes = _selected_volumes(root, volumes, referenced)
    stats = snapshot.get("stats") if isinstance(snapshot.get("stats"), dict) else {}

    lines = [
        *_frontmatter(snapshot, redacted=redacted),
        "",
        "# HUMAN.md",
        "",
        "> Generated locally by Persome from evidence on this Mac.",
        "> This living model is incomplete by design and changes as new evidence arrives.",
        "> Direct edits are replaced on refresh; use `persome correct` to change the model.",
        "",
        "## Persome's portrait of me",
        "",
    ]
    if root is None:
        lines.extend(
            [
                "Persome has not formed a verified Root yet. No identity portrait is being ",
                "claimed. This file will update automatically after the model has enough ",
                "evidence and a completed `persome model build`.",
            ]
        )
    else:
        lines.append(portrait or "The current Root does not contain a readable portrait yet.")

    if faces:
        lines.extend(["", "## Stable patterns", ""])
        lines.extend(_schema_bullet(item) for item in faces)
        if len(snapshot.get("faces") or []) > len(faces):
            lines.append(f"> Showing {len(faces)} of {len(snapshot['faces'])} Faces.")
    if selected_volumes:
        lines.extend(["", "## Cross-domain patterns", ""])
        lines.extend(_schema_bullet(item) for item in selected_volumes)
        if len(volumes) > len(selected_volumes):
            lines.append(f"> Showing {len(selected_volumes)} of {len(volumes)} Volumes.")

    evolution_lines = int(stats.get("evolution_lines") or 0)
    relation_lines = int(stats.get("relation_lines") or 0)
    degraded = (
        build.get("degraded_stages") if isinstance(build.get("degraded_stages"), list) else []
    )
    lines.extend(
        [
            "",
            "## Model provenance",
            "",
            f"- Build: `{_safe_markdown(build.get('build_id'))}` "
            f"({_safe_markdown(build.get('status')) or 'unknown'})",
            f"- Generated: `{_safe_markdown(snapshot.get('generated_at'))}`",
            f"- Geometry: {int(stats.get('points') or 0)} Points, "
            f"{evolution_lines + relation_lines} Lines, "
            f"{int(stats.get('faces') or 0)} Faces, "
            f"{int(stats.get('volumes') or 0)} Volumes, "
            f"{int(stats.get('roots') or 0)} Root",
            f"- Evidence receipts: {int(stats.get('receipts') or 0)}",
            f"- Degraded stages: {_safe_markdown(', '.join(map(str, degraded))) or 'none'}",
            "- Full evidence: `persome model export` or MCP `get_model_snapshot`",
            "",
        ]
    )
    return "\n".join(lines)


def _managed_metadata(path: Path) -> _ManagedHuman | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(3):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise HumanMarkdownConflict(f"refusing to replace non-regular HUMAN.md: {path}")
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HumanMarkdownConflict(f"cannot safely inspect HUMAN.md: {path}") from exc
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                continue
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise HumanMarkdownConflict(f"refusing to replace non-regular HUMAN.md: {path}")
            try:
                header_bytes = os.read(fd, 65_537)
                existing = header_bytes.decode("utf-8")
                if not existing.startswith("---\n"):
                    raise ValueError("missing Persome frontmatter")
                header = existing.split("---", 2)[1]
            except (OSError, UnicodeDecodeError, IndexError, ValueError) as exc:
                raise HumanMarkdownConflict(
                    f"refusing to replace unrecognized HUMAN.md: {path}"
                ) from exc
            values: dict[str, Any] = {}
            for line in header.splitlines():
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                try:
                    values[key.strip()] = json.loads(value.strip())
                except json.JSONDecodeError:
                    values[key.strip()] = value.strip()
            if values.get("projection") != _PROJECTION_NAME:
                raise HumanMarkdownConflict(f"refusing to replace user-owned HUMAN.md: {path}")
            os.fchmod(fd, 0o600)
            return _ManagedHuman(values=values, identity=(opened.st_dev, opened.st_ino))
        finally:
            os.close(fd)
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    raise HumanMarkdownConflict(f"HUMAN.md changed during ownership inspection: {path}")


@contextmanager
def _human_lock(target: Path) -> Iterator[None]:
    lock_path = target.parent / f".{target.name}.lock"
    if target == paths.human_file():
        handle = paths.open_private_lock_file(lock_path)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = (
            os.O_RDWR
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise HumanMarkdownConflict(f"cannot safely lock HUMAN.md: {target}") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise HumanMarkdownConflict(f"unsafe HUMAN.md lock target: {lock_path}")
            os.fchmod(fd, 0o600)
            handle = os.fdopen(fd, "a+b")
        except BaseException:
            os.close(fd)
            raise
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HumanMarkdownConflict(f"HUMAN.md refresh is already active: {target}") from exc
        try:
            _recover_hardlink_crash(target)
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _stage_private_text(target: Path, content: str) -> Path:
    """Write a complete private inode next to target without publishing it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def _fsync_parent(target: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(target.parent, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _recover_hardlink_crash(target: Path) -> None:
    """Drop only our same-inode temp names after a link-publication crash."""
    try:
        target_metadata = target.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(target_metadata.st_mode) or target_metadata.st_nlink <= 1:
        return

    identity = (target_metadata.st_dev, target_metadata.st_ino)
    lock_name = f".{target.name}.lock"
    removed = False
    try:
        candidates = tuple(target.parent.glob(f".{target.name}.*"))
    except OSError as exc:
        raise HumanMarkdownConflict(f"cannot inspect HUMAN.md crash artifacts: {target}") from exc
    for candidate in candidates:
        if candidate.name == lock_name:
            continue
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == identity:
            try:
                candidate.unlink()
            except OSError as exc:
                raise HumanMarkdownConflict(
                    f"cannot recover HUMAN.md crash artifact: {candidate}"
                ) from exc
            removed = True
    if removed:
        _fsync_parent(target)


def _move_existing_target(target: Path) -> Path:
    """Move the exact current target aside so it can be verified before replacement."""
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.replaced.", dir=target.parent)
    os.close(fd)
    displaced = Path(name)
    try:
        os.replace(target, displaced)
    except BaseException:
        displaced.unlink(missing_ok=True)
        raise
    return displaced


def _restore_displaced(target: Path, displaced: Path) -> bool:
    """Restore a captured unknown inode without overwriting a newer target."""
    try:
        metadata = displaced.lstat()
        if stat.S_ISREG(metadata.st_mode):
            os.link(displaced, target, follow_symlinks=False)
        elif stat.S_ISLNK(metadata.st_mode):
            os.symlink(os.readlink(displaced), target)
        else:
            return False
    except OSError:
        return False
    displaced.unlink(missing_ok=True)
    _fsync_parent(target)
    return True


def _raise_changed_target(target: Path, displaced: Path) -> None:
    if _restore_displaced(target, displaced):
        raise HumanMarkdownConflict(f"HUMAN.md changed during refresh and was preserved: {target}")
    raise HumanMarkdownConflict(
        f"HUMAN.md changed during refresh; preserved the displaced file at {displaced}"
    )


def _capture_managed_target(target: Path, expected: _ManagedHuman) -> Path:
    try:
        displaced = _move_existing_target(target)
    except FileNotFoundError as exc:
        raise HumanMarkdownConflict(f"HUMAN.md disappeared during refresh: {target}") from exc
    try:
        captured = _managed_metadata(displaced)
    except HumanMarkdownConflict:
        _raise_changed_target(target, displaced)
    if captured is None or captured.identity != expected.identity:
        _raise_changed_target(target, displaced)
    return displaced


def _publish_staged(target: Path, staged: Path, expected: _ManagedHuman | None) -> None:
    displaced: Path | None = None
    if expected is not None:
        displaced = _capture_managed_target(target, expected)
    try:
        # Hard-link publication is create-if-absent. Unlike os.replace(), it
        # cannot destroy an unknown file an editor placed after our check.
        os.link(staged, target, follow_symlinks=False)
    except FileExistsError as exc:
        if displaced is not None:
            displaced.unlink(missing_ok=True)
        raise HumanMarkdownConflict(
            f"refusing to replace HUMAN.md created during refresh: {target}"
        ) from exc
    except BaseException as exc:
        if displaced is not None and not _restore_displaced(target, displaced):
            raise RuntimeError(
                f"HUMAN.md refresh failed; previous projection preserved at {displaced}"
            ) from exc
        raise
    staged.unlink(missing_ok=True)
    if displaced is not None:
        displaced.unlink(missing_ok=True)
    _fsync_parent(target)


def remove_managed_human_markdown(path: Path | None = None) -> bool:
    """Remove only a Persome-owned projection; preserve unknown user files."""
    target = path or paths.human_file()
    with _human_lock(target):
        metadata = _managed_metadata(target)
        if metadata is None:
            return False
        displaced = _capture_managed_target(target, metadata)
        displaced.unlink(missing_ok=True)
        _fsync_parent(target)
        return True


def materialize_human_markdown(
    snapshot: dict[str, Any],
    *,
    out_path: Path | None = None,
    redacted: bool = False,
) -> Path:
    """Atomically write a truthful HUMAN.md, including an honest cold-start view."""
    validate_snapshot(snapshot)
    target = out_path or paths.human_file()
    content = render_human_markdown(snapshot, redacted=redacted)
    with _human_lock(target):
        metadata = _managed_metadata(target)
        staged = _stage_private_text(target, content)
        try:
            _publish_staged(target, staged, metadata)
        finally:
            staged.unlink(missing_ok=True)
    return target


def _placeholder_snapshot(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": manifest.get("completed_at") or manifest.get("started_at"),
        "build": manifest,
        "points": [],
        "lines": [],
        "faces": [],
        "volumes": [],
        "root": None,
        "receipts": [],
        "stats": {
            "points": 0,
            "active_points": 0,
            "evolution_lines": 0,
            "relation_lines": 0,
            "faces": 0,
            "volumes": 0,
            "roots": 0,
            "receipts": 0,
            "redactions": {},
        },
    }


def sync_live_human_markdown() -> Path:
    """Backfill or refresh HUMAN.md from an existing Runtime model.

    This is the upgrade path for users whose model predates the file. Matching
    build and renderer versions are a cheap no-op. Missing Roots receive an
    explicit forming-state file rather than a fabricated portrait.
    """
    from ..store import fts
    from .build import build_live_snapshot, load_live_manifest

    target = paths.human_file()
    manifest = load_live_manifest()
    current = _managed_metadata(target)
    build_id = manifest.get("build_id")
    if (
        current is not None
        and current.get("human_schema_version") == HUMAN_SCHEMA_VERSION
        and current.get("renderer_version") == HUMAN_RENDERER_VERSION
        and current.get("build_id") == build_id
        and current.get("build_status") == manifest.get("status")
    ):
        return target

    if manifest.get("status") == "building" and current is not None:
        return target
    if manifest.get("status") not in _SUPPORTED_BUILD_STATUSES:
        return materialize_human_markdown(_placeholder_snapshot(manifest), out_path=target)

    with fts.cursor() as conn:
        snapshot = build_live_snapshot(conn, redact=False)
    snapshot_build = snapshot.get("build") if isinstance(snapshot.get("build"), dict) else {}
    if snapshot_build.get("status") not in _SUPPORTED_BUILD_STATUSES:
        return materialize_human_markdown(_placeholder_snapshot(snapshot_build), out_path=target)
    return materialize_human_markdown(snapshot, out_path=target)
