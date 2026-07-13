"""Owner-local ``HUMAN.md`` projection of the versioned personal model."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .. import paths
from .snapshot import SCHEMA_VERSION, validate_snapshot

HUMAN_SCHEMA_VERSION = 1
HUMAN_RENDERER_VERSION = 1
_PROJECTION_NAME = "persome-model"
_MAX_FACES = 8
_MAX_VOLUMES = 8
_HANDLE_RE = re.compile(r"⟨([^⟨⟩]+)⟩")
_SUPPORTED_BUILD_STATUSES = frozenset({"complete", "degraded"})
_TRANSACTION_VERSION = 1


class HumanMarkdownConflict(RuntimeError):
    """The target exists but is not a Persome-managed HUMAN.md projection."""


class HumanMarkdownDeferred(RuntimeError):
    """Canonical publication is deferred until an in-flight update commits."""


@dataclass(frozen=True)
class _ManagedHuman:
    values: dict[str, Any]
    identity: tuple[int, int]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@dataclass(frozen=True)
class _HumanTransaction:
    operation: str
    stage_name: str
    expected: tuple[int, int] | None
    candidate: tuple[int, int] | None


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


def _is_canonical_target(target: Path) -> bool:
    return os.path.abspath(os.fspath(target)) == os.path.abspath(os.fspath(paths.human_file()))


def _raise_if_update_pending(target: Path) -> None:
    if _is_canonical_target(target) and os.path.lexists(paths.update_state_file()):
        raise HumanMarkdownDeferred("HUMAN.md publication waits for the Runtime update to commit")


def _path_identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    return metadata.st_dev, metadata.st_ino


def _parse_identity(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(item) is not int or item < 0 for item in value)
    ):
        raise ValueError("invalid HUMAN.md transaction identity")
    return value[0], value[1]


def _write_transaction(handle: BinaryIO, transaction: _HumanTransaction | None) -> None:
    if transaction is None:
        payload = (
            json.dumps({"version": _TRANSACTION_VERSION, "clear": True}, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        )
    else:
        payload = (
            json.dumps(
                {
                    "version": _TRANSACTION_VERSION,
                    "operation": transaction.operation,
                    "stage": transaction.stage_name,
                    "expected": list(transaction.expected) if transaction.expected else None,
                    "candidate": list(transaction.candidate) if transaction.candidate else None,
                },
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    # Never erase the last durable recovery record before its successor is
    # complete. A crash-shortened tail is discarded at its last newline; the
    # preceding fsynced record remains authoritative.
    handle.flush()
    handle.seek(0)
    existing = handle.read()
    complete_length = existing.rfind(b"\n") + 1
    if complete_length != len(existing):
        os.ftruncate(handle.fileno(), complete_length)
        os.fsync(handle.fileno())
    handle.seek(0, os.SEEK_END)
    written = handle.write(payload)
    if written != len(payload):
        raise OSError("short HUMAN.md transaction journal write")
    handle.flush()
    os.fsync(handle.fileno())
    if transaction is None:
        # The durable clear record makes both the old log and an empty inode
        # truthful. Compacting is therefore safe on either side of a crash.
        os.ftruncate(handle.fileno(), 0)
        os.fsync(handle.fileno())
        handle.seek(0)


def _read_transaction(target: Path, handle: BinaryIO) -> _HumanTransaction | None:
    handle.seek(0)
    if os.fstat(handle.fileno()).st_size > 16_384:
        raise HumanMarkdownConflict(f"HUMAN.md transaction journal is too large: {target}")
    payload = handle.read()
    current: _HumanTransaction | None = None
    for raw_record in payload.splitlines(keepends=True):
        if not raw_record.endswith(b"\n"):
            break
        try:
            value = json.loads(raw_record.decode("utf-8"))
            if not isinstance(value, dict) or value.get("version") != _TRANSACTION_VERSION:
                raise ValueError("invalid version")
            if value.get("clear") is True:
                current = None
                continue
            operation = value.get("operation")
            stage_name = value.get("stage")
            prefix = f".{target.name}.persome-stage."
            token = stage_name.removeprefix(prefix) if isinstance(stage_name, str) else ""
            if operation not in {"publish", "remove"}:
                raise ValueError("invalid operation")
            if (
                not isinstance(stage_name, str)
                or not stage_name.startswith(prefix)
                or not re.fullmatch(r"[0-9a-f]{32}", token)
            ):
                raise ValueError("invalid stage")
            expected = _parse_identity(value.get("expected"))
            candidate = _parse_identity(value.get("candidate"))
            if operation == "remove" and (expected is None or candidate is not None):
                raise ValueError("invalid remove transaction")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise HumanMarkdownConflict(f"invalid HUMAN.md transaction journal: {target}") from exc
        current = _HumanTransaction(operation, stage_name, expected, candidate)
    return current


def _new_stage_path(target: Path) -> Path:
    for _attempt in range(8):
        candidate = target.parent / f".{target.name}.persome-stage.{secrets.token_hex(16)}"
        if not os.path.lexists(candidate):
            return candidate
    raise HumanMarkdownConflict(f"cannot reserve a HUMAN.md transaction stage: {target}")


def _unlink_if_identity(path: Path, identity: tuple[int, int]) -> bool:
    if _path_identity(path) != identity:
        return False
    try:
        path.unlink()
    except OSError as exc:
        raise HumanMarkdownConflict(f"cannot remove HUMAN.md transaction stage: {path}") from exc
    return True


def _managed_identity_matches(path: Path, identity: tuple[int, int]) -> bool:
    try:
        managed = _managed_metadata(path)
    except HumanMarkdownConflict:
        return False
    return managed is not None and managed.identity == identity


def _clear_recovered_transaction(target: Path, handle: BinaryIO) -> None:
    _fsync_parent(target)
    _write_transaction(handle, None)


def _recover_publish_transaction(
    target: Path,
    stage: Path,
    transaction: _HumanTransaction,
    handle: BinaryIO,
) -> None:
    expected = transaction.expected
    candidate = transaction.candidate
    if candidate is None:
        # Content is written only after the candidate identity is journaled. A
        # crash in this reservation window can therefore leave at most an empty
        # owner-only inode, never raw model text.
        try:
            metadata = stage.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            safe_empty_reservation = (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink == 1
                and metadata.st_size == 0
                and metadata.st_uid == os.getuid()
                and not metadata.st_mode & 0o077
            )
            if not safe_empty_reservation:
                _write_transaction(handle, None)
                raise HumanMarkdownConflict(
                    f"preserved an unrecognized HUMAN.md transaction stage: {stage}"
                )
            stage.unlink()
        _clear_recovered_transaction(target, handle)
        return

    target_identity = _path_identity(target)
    stage_identity = _path_identity(stage)

    if expected is None:
        if target_identity == candidate:
            if stage_identity == candidate:
                _unlink_if_identity(stage, candidate)
            elif stage_identity is not None:
                _write_transaction(handle, None)
                raise HumanMarkdownConflict(
                    f"preserved a changed HUMAN.md transaction stage: {stage}"
                )
            _clear_recovered_transaction(target, handle)
            return
        if stage_identity == candidate:
            _unlink_if_identity(stage, candidate)
        elif stage_identity is not None:
            _write_transaction(handle, None)
            raise HumanMarkdownConflict(
                f"preserved an unrecognized HUMAN.md transaction stage: {stage}"
            )
        _clear_recovered_transaction(target, handle)
        return

    if target_identity == candidate:
        if stage_identity is None:
            _clear_recovered_transaction(target, handle)
            return
        if stage_identity == expected and _managed_identity_matches(stage, expected):
            _unlink_if_identity(stage, expected)
            _clear_recovered_transaction(target, handle)
            return
        try:
            paths.atomic_exchange(stage, target)
        except (OSError, ValueError) as exc:
            raise HumanMarkdownConflict(
                f"cannot restore a concurrently changed HUMAN.md from {stage}"
            ) from exc
        if not _unlink_if_identity(stage, candidate):
            _clear_recovered_transaction(target, handle)
            raise HumanMarkdownConflict(
                f"preserved a concurrently changed HUMAN.md stage after rollback: {stage}"
            )
        _clear_recovered_transaction(target, handle)
        return

    if stage_identity == candidate:
        _unlink_if_identity(stage, candidate)
        _clear_recovered_transaction(target, handle)
        return
    if stage_identity == expected and _managed_identity_matches(stage, expected):
        _unlink_if_identity(stage, expected)
        _clear_recovered_transaction(target, handle)
        return
    if stage_identity is None:
        _clear_recovered_transaction(target, handle)
        return
    if target_identity is None:
        try:
            paths.atomic_rename_noreplace(stage, target)
        except (OSError, ValueError) as exc:
            raise HumanMarkdownConflict(
                f"cannot restore a displaced user HUMAN.md from {stage}"
            ) from exc
        _clear_recovered_transaction(target, handle)
        return
    _write_transaction(handle, None)
    raise HumanMarkdownConflict(f"preserved a displaced user HUMAN.md at {stage}")


def _recover_remove_transaction(
    target: Path,
    stage: Path,
    transaction: _HumanTransaction,
    handle: BinaryIO,
) -> None:
    expected = transaction.expected
    assert expected is not None
    target_identity = _path_identity(target)
    stage_identity = _path_identity(stage)
    if stage_identity is None:
        _clear_recovered_transaction(target, handle)
        return
    if stage_identity == expected and _managed_identity_matches(stage, expected):
        _unlink_if_identity(stage, expected)
        _clear_recovered_transaction(target, handle)
        return
    if target_identity is None:
        try:
            paths.atomic_rename_noreplace(stage, target)
        except (OSError, ValueError) as exc:
            raise HumanMarkdownConflict(
                f"cannot restore a user HUMAN.md displaced during removal: {stage}"
            ) from exc
        _clear_recovered_transaction(target, handle)
        return
    _write_transaction(handle, None)
    raise HumanMarkdownConflict(f"preserved a displaced user HUMAN.md at {stage}")


def _recover_transaction(target: Path, handle: BinaryIO) -> None:
    transaction = _read_transaction(target, handle)
    if transaction is None:
        return
    stage = target.parent / transaction.stage_name
    if transaction.operation == "publish":
        _recover_publish_transaction(target, stage, transaction, handle)
    else:
        _recover_remove_transaction(target, stage, transaction, handle)


@contextmanager
def _human_lock(target: Path) -> Iterator[BinaryIO]:
    lock_path = target.parent / f".{target.name}.lock"
    if _is_canonical_target(target):
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
            _recover_transaction(target, handle)
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _stage_publish_transaction(
    target: Path,
    content: str,
    expected: _ManagedHuman | None,
    handle: BinaryIO,
) -> tuple[Path, _HumanTransaction]:
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = _new_stage_path(target)
    transaction = _HumanTransaction(
        operation="publish",
        stage_name=stage.name,
        expected=expected.identity if expected else None,
        candidate=None,
    )
    _write_transaction(handle, transaction)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(stage, flags, 0o600)
    except OSError as exc:
        _write_transaction(handle, None)
        raise HumanMarkdownConflict(f"cannot create HUMAN.md transaction stage: {stage}") from exc
    candidate: tuple[int, int] | None = None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise HumanMarkdownConflict(f"unsafe HUMAN.md transaction stage: {stage}")
        os.fchmod(fd, 0o600)
        candidate = metadata.st_dev, metadata.st_ino
        transaction = _HumanTransaction(
            operation="publish",
            stage_name=stage.name,
            expected=expected.identity if expected else None,
            candidate=candidate,
        )
        _write_transaction(handle, transaction)
        with os.fdopen(fd, "w", encoding="utf-8") as stage_handle:
            fd = -1
            stage_handle.write(content)
            stage_handle.flush()
            os.fsync(stage_handle.fileno())
        _fsync_parent(target)
        return stage, transaction
    except BaseException:
        if fd >= 0:
            os.close(fd)
        if candidate is not None:
            _unlink_if_identity(stage, candidate)
        _clear_recovered_transaction(target, handle)
        raise


def _publish_text_transaction(
    target: Path,
    content: str,
    expected: _ManagedHuman | None,
    handle: BinaryIO,
) -> None:
    stage, transaction = _stage_publish_transaction(target, content, expected, handle)
    candidate = transaction.candidate
    assert candidate is not None
    try:
        if expected is None:
            try:
                os.link(stage, target, follow_symlinks=False)
            except FileExistsError as exc:
                raise HumanMarkdownConflict(
                    f"refusing to replace HUMAN.md created during refresh: {target}"
                ) from exc
            _fsync_parent(target)
            if _path_identity(target) != candidate:
                raise HumanMarkdownConflict(f"HUMAN.md changed during publication: {target}")
            if not _unlink_if_identity(stage, candidate):
                raise HumanMarkdownConflict(
                    f"HUMAN.md transaction stage changed during publication: {stage}"
                )
        else:
            try:
                paths.atomic_exchange(stage, target)
            except (OSError, ValueError) as exc:
                raise HumanMarkdownConflict(
                    f"cannot atomically replace managed HUMAN.md: {target}"
                ) from exc
            _fsync_parent(target)
            captured = _managed_metadata(stage)
            if captured is None or captured.identity != expected.identity:
                raise HumanMarkdownConflict(f"HUMAN.md changed during refresh: {target}")
            if _path_identity(target) != candidate:
                raise HumanMarkdownConflict(f"HUMAN.md changed after publication: {target}")
            if not _unlink_if_identity(stage, expected.identity):
                raise HumanMarkdownConflict(
                    f"HUMAN.md transaction stage changed during publication: {stage}"
                )
        _fsync_parent(target)
        _write_transaction(handle, None)
    except BaseException:
        _recover_transaction(target, handle)
        raise


def remove_managed_human_markdown(path: Path | None = None) -> bool:
    """Remove only a Persome-owned projection; preserve unknown user files."""
    target = path or paths.human_file()
    with _human_lock(target) as handle:
        metadata = _managed_metadata(target)
        if metadata is None:
            return False
        stage = _new_stage_path(target)
        transaction = _HumanTransaction("remove", stage.name, metadata.identity, None)
        _write_transaction(handle, transaction)
        try:
            try:
                paths.atomic_rename_noreplace(target, stage)
            except (OSError, ValueError) as exc:
                raise HumanMarkdownConflict(f"cannot atomically remove HUMAN.md: {target}") from exc
            _fsync_parent(target)
            captured = _managed_metadata(stage)
            if captured is None or captured.identity != metadata.identity:
                raise HumanMarkdownConflict(f"HUMAN.md changed during removal: {target}")
            if not _unlink_if_identity(stage, metadata.identity):
                raise HumanMarkdownConflict(
                    f"HUMAN.md transaction stage changed during removal: {stage}"
                )
            _clear_recovered_transaction(target, handle)
            return True
        except BaseException:
            _recover_transaction(target, handle)
            raise


def materialize_human_markdown(
    snapshot: dict[str, Any],
    *,
    out_path: Path | None = None,
    redacted: bool = False,
) -> Path:
    """Atomically write a truthful HUMAN.md, including an honest cold-start view."""
    target = out_path or paths.human_file()
    _raise_if_update_pending(target)
    validate_snapshot(snapshot)
    content = render_human_markdown(snapshot, redacted=redacted)
    with _human_lock(target) as handle:
        _raise_if_update_pending(target)
        metadata = _managed_metadata(target)
        _publish_text_transaction(target, content, metadata, handle)
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
    from .build import _build_live_snapshot_from_manifest, live_model_generation

    target = paths.human_file()
    _raise_if_update_pending(target)
    with live_model_generation() as manifest:
        with _human_lock(target):
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

        if manifest.get("status") == "building":
            if current is not None:
                return target
            raise HumanMarkdownConflict("HUMAN.md backfill waits for the active model build")
        if manifest.get("status") not in _SUPPORTED_BUILD_STATUSES:
            return materialize_human_markdown(_placeholder_snapshot(manifest), out_path=target)

        with fts.cursor() as conn:
            snapshot = _build_live_snapshot_from_manifest(conn, manifest, redact=False)
        return materialize_human_markdown(snapshot, out_path=target)
