"""capture/scheduler.py: write-through to captures_fts + delete-through on cleanup."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from persome import paths
from persome.capture import scheduler as scheduler_mod
from persome.store import fts


def _capture_dict(
    *,
    ts: str,
    app: str,
    title: str,
    value: str,
    text: str,
) -> dict:
    return {
        "timestamp": ts,
        "schema_version": 2,
        "trigger": {"event_type": "manual"},
        "window_meta": {
            "app_name": app,
            "title": title,
            "bundle_id": "com.test." + app.lower(),
        },
        "focused_element": {
            "role": "AXTextArea",
            "value": value,
            "is_editable": True,
            "value_length": len(value),
        },
        "visible_text": text,
        "url": "",
        "screenshot": {
            "image_base64": "AAAA",
            "mime_type": "image/jpeg",
            "width": 100,
            "height": 50,
        },
    }


def test_write_capture_indexes_into_fts(ac_root: Path) -> None:
    out = _capture_dict(
        ts="2026-04-22T14:00:00+08:00",
        app="Cursor",
        title="main.py",
        value="def foo()",
        text="def foo(): return 1",
    )
    path = scheduler_mod._write_capture(out)
    assert path.exists()

    with fts.cursor() as conn:
        hits = fts.search_captures(conn, query="foo")
        assert len(hits) == 1
        assert hits[0].id == path.stem
        assert hits[0].app_name == "Cursor"


def test_restore_capture_index_sanitizes_legacy_placeholder(ac_root: Path) -> None:
    phrase = "Ask for follow-up changes"
    out = _capture_dict(
        ts="2026-07-12T23:00:00+08:00",
        app="Chat",
        title="Conversation",
        value=phrase,
        text=f"[TextArea] {phrase}",
    )
    out["ax_tree"] = {
        "apps": [
            {
                "name": "Chat",
                "bundle_id": "com.test.chat",
                "is_frontmost": True,
                "focused_element": {
                    "role": "AXTextArea",
                    "value": phrase,
                    "is_editable": True,
                },
                "windows": [
                    {
                        "title": "Conversation",
                        "elements": [
                            {
                                "role": "AXTextArea",
                                "value": phrase,
                                "children": [
                                    {
                                        "role": "AXGroup",
                                        "domClassList": ["placeholder"],
                                        "children": [{"role": "AXStaticText", "value": phrase}],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    target = paths.capture_buffer_dir() / "legacy-placeholder.json"
    target.write_text(json.dumps(out), encoding="utf-8")

    scheduler_mod._restore_capture_index(target)

    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT focused_value, visible_text FROM captures WHERE id=?",
            (target.stem,),
        ).fetchone()
    assert row is not None
    assert row["focused_value"] == ""
    assert phrase not in row["visible_text"]


def test_placeholder_repair_preserves_ocr_backfill_sentinel(ac_root: Path) -> None:
    phrase = "Ask for follow-up changes"
    out = _capture_dict(
        ts="2026-07-12T23:01:00+08:00",
        app="Chat",
        title="Conversation",
        value=phrase,
        text="",
    )
    out["ocr_submitted"] = True
    out["ax_tree"] = {
        "apps": [
            {
                "name": "Chat",
                "bundle_id": "com.test.chat",
                "is_frontmost": True,
                "focused_element": {
                    "role": "AXTextArea",
                    "value": phrase,
                    "is_editable": True,
                },
                "windows": [
                    {
                        "title": "Conversation",
                        "elements": [
                            {
                                "role": "AXTextArea",
                                "value": phrase,
                                "children": [
                                    {
                                        "role": "AXGroup",
                                        "domClassList": ["placeholder"],
                                        "children": [{"role": "AXStaticText", "value": phrase}],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }

    assert scheduler_mod._index_capture("ocr-placeholder", out) is True
    with fts.cursor() as conn:
        before = conn.execute(
            "SELECT focused_value, visible_text FROM captures WHERE id='ocr-placeholder'"
        ).fetchone()
        assert before["focused_value"] == ""
        assert before["visible_text"] == ""
        fts.backfill_capture_ocr_text(conn, "ocr-placeholder", "recognized body")
        after = conn.execute(
            "SELECT visible_text FROM captures WHERE id='ocr-placeholder'"
        ).fetchone()
    assert after["visible_text"] == "recognized body"


def test_cleanup_buffer_removes_fts_rows(ac_root: Path) -> None:
    """Time-based delete pass should also drop matching FTS rows."""
    captures = [
        ("2026-04-22T10:00:00+08:00", "old1"),
        ("2026-04-22T11:00:00+08:00", "old2"),
        ("2026-04-22T12:00:00+08:00", "keep"),
    ]
    written: list[Path] = []
    for ts, marker in captures:
        out = _capture_dict(
            ts=ts,
            app="Cursor",
            title=f"win-{marker}",
            value="",
            text=f"unique-text-{marker}",
        )
        written.append(scheduler_mod._write_capture(out))

    with fts.cursor() as conn:
        assert len(fts.recent_captures(conn, limit=10)) == 3

    # Backdate the two "old" files so the delete pass picks them up.
    long_ago = time.time() - 10 * 24 * 3600
    for p in written[:2]:
        os.utime(p, (long_ago, long_ago))

    # processed_before_ts past every stem so all are considered "absorbed".
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=24,
        processed_before_ts="2099-01-01T00:00:00+00:00",
        screenshot_retention_hours=None,
        max_mb=0,
    )
    assert stats["deleted"] == 2
    assert stats["evicted"] == 0

    with fts.cursor() as conn:
        rec = fts.recent_captures(conn, limit=10)
        assert {h.id for h in rec} == {written[2].stem}


def test_cleanup_eviction_also_drops_fts(ac_root: Path) -> None:
    """Size-based eviction should also drop matching FTS rows."""
    written: list[Path] = []
    for i in range(3):
        ts = f"2026-04-22T1{i}:00:00+08:00"
        out = _capture_dict(
            ts=ts,
            app="Cursor",
            title=f"w-{i}",
            value="",
            text="x" * 500_000,  # ~500 KB each → 1.5 MB total
        )
        written.append(scheduler_mod._write_capture(out))

    # Tight 1 MB cap forces eviction of the oldest.
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=24 * 365,
        processed_before_ts="2099-01-01T00:00:00+00:00",
        screenshot_retention_hours=None,
        max_mb=1,
    )
    assert stats["evicted"] >= 1
    with fts.cursor() as conn:
        remaining = {h.id for h in fts.recent_captures(conn, limit=10)}
    assert len(remaining) == 3 - stats["evicted"]
    # Newest survives.
    assert written[-1].stem in remaining


def test_hard_cap_continues_after_unlink_failure(ac_root: Path, monkeypatch) -> None:
    written: list[Path] = []
    for i in range(3):
        written.append(
            scheduler_mod._write_capture(
                _capture_dict(
                    ts=f"2026-04-22T1{i}:00:00+08:00",
                    app="Cursor",
                    title=f"w-{i}",
                    value="",
                    text="x" * 500_000,
                )
            )
        )
        os.utime(written[-1], (1_700_000_000 + i, 1_700_000_000 + i))

    original_unlink = Path.unlink

    def fail_oldest(path: Path, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        if path == written[0]:
            raise OSError("synthetic unlink failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_oldest)
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=24 * 365 * 10,
        processed_before_ts="2099-01-01T00:00:00+00:00",
        max_mb=1,
    )

    assert stats["evicted"] >= 1
    on_disk = {p.stem for p in paths.capture_buffer_dir().glob("*.json")}
    assert sum(p.stat().st_size for p in paths.capture_buffer_dir().glob("*.json")) <= 1024**2
    with fts.cursor() as conn:
        indexed = {row[0] for row in conn.execute("SELECT id FROM captures").fetchall()}
    assert indexed == on_disk


def test_hard_cap_reconciles_after_transient_fts_failure(ac_root: Path, monkeypatch) -> None:
    for i in range(3):
        scheduler_mod._write_capture(
            _capture_dict(
                ts=f"2026-04-22T1{i}:00:00+08:00",
                app="Cursor",
                title=f"w-{i}",
                value="",
                text="x" * 500_000,
            )
        )

    real_delete = scheduler_mod._delete_captures_from_fts
    real_prune = scheduler_mod._prune_missing_capture_rows
    monkeypatch.setattr(scheduler_mod, "_delete_captures_from_fts", lambda _stems: False)
    monkeypatch.setattr(scheduler_mod, "_prune_missing_capture_rows", lambda _buf: False)
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=24 * 365,
        processed_before_ts="2020-01-01T00:00:00+00:00",
        max_mb=1,
    )
    assert stats["evicted"] >= 1

    monkeypatch.setattr(scheduler_mod, "_delete_captures_from_fts", real_delete)
    monkeypatch.setattr(scheduler_mod, "_prune_missing_capture_rows", real_prune)
    scheduler_mod.cleanup_buffer(retention_hours=24 * 365, max_mb=0)

    on_disk = {p.stem for p in paths.capture_buffer_dir().glob("*.json")}
    with fts.cursor() as conn:
        indexed = {row[0] for row in conn.execute("SELECT id FROM captures").fetchall()}
    assert indexed == on_disk


def test_prune_does_not_delete_row_inserted_after_candidate_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    buf = tmp_path / "captures"
    buf.mkdir()
    ids = {"old"}
    deleted: list[str] = []

    class _Rows:
        def fetchall(self):  # type: ignore[no-untyped-def]
            # Model a capture that lands after the DB candidate query but before
            # the directory scan. It must not be part of the stale candidate set.
            ids.add("new")
            (buf / "new.json").write_text("{}", encoding="utf-8")
            return [("old",)]

    class _Conn:
        def execute(self, sql: str):  # type: ignore[no-untyped-def]
            return _Rows() if sql == "SELECT id FROM captures" else self

    @contextmanager
    def fake_cursor():  # type: ignore[no-untyped-def]
        yield _Conn()

    def fake_delete(_conn, stem: str) -> None:  # type: ignore[no-untyped-def]
        deleted.append(stem)
        ids.discard(stem)

    monkeypatch.setattr(scheduler_mod.fts_store, "cursor", fake_cursor)
    monkeypatch.setattr(scheduler_mod.fts_store, "delete_capture", fake_delete)

    assert scheduler_mod._prune_missing_capture_rows(buf) is True
    assert deleted == ["old"]
    assert ids == {"new"}


def test_cleanup_removes_stale_atomic_capture_temp(ac_root: Path) -> None:
    temporary = paths.capture_buffer_dir() / ".2026-07-11T12-00-00p00-00.json.crash"
    temporary.write_bytes(b"RAW_CAPTURE_SCREEN_SECRET" * 100_000)
    stale = time.time() - scheduler_mod._ATOMIC_CAPTURE_TEMP_GRACE_SECONDS - 1
    os.utime(temporary, (stale, stale))

    stats = scheduler_mod.cleanup_buffer(retention_hours=1, max_mb=1)

    assert stats["deleted"] == 1
    assert not temporary.exists()
