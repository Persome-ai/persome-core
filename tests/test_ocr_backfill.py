"""Tests for OCR backfill: fts.backfill_capture_ocr_text + scheduler sync flow + mcp/captures.

OCR is on-device & synchronous now: scheduler._submit_ocr_async runs local inference
and backfills captures.visible_text directly (no ocr_jobs table, no poll loop). The
timeline aggregator and MCP read_recent_capture recover that text via
fts.get_ocr_result_for_capture, which now reads captures.visible_text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from persome.store import fts as fts_store

# ─── helpers ──────────────────────────────────────────────────────────────────


def _insert_capture(conn, *, id: str, visible_text: str = "") -> None:
    fts_store.insert_capture(
        conn,
        id=id,
        timestamp="2026-05-21T10:00:00+08:00",
        app_name="TestApp",
        bundle_id="com.test.app",
        window_title="Test Window",
        focused_role="AXTextArea",
        focused_value="",
        visible_text=visible_text,
        url="",
    )


def _backfill_ocr(conn, *, capture_id: str, text: str) -> None:
    """Simulate a completed OCR: the capture row exists, its visible_text holds the text."""
    _insert_capture(conn, id=capture_id, visible_text="")
    fts_store.backfill_capture_ocr_text(conn, capture_id, text)


def _write_capture_json(buf: Path, stem: str, visible_text: str = "") -> None:
    """Write a minimal capture JSON to the capture-buffer directory."""
    buf.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "timestamp": "2026-05-21T10:00:00+08:00",
        "window_meta": {"app_name": "TestApp", "title": "Test Window"},
        "focused_element": {},
        "visible_text": visible_text,
        "url": "",
    }
    (buf / f"{stem}.json").write_text(json.dumps(data))


# ─── store/fts.py: backfill_capture_ocr_text ──────────────────────────────────


class TestBackfillCaptureOcrText:
    def test_fills_empty_visible_text(self, ac_root: Path) -> None:
        with fts_store.cursor() as conn:
            _insert_capture(conn, id="c1", visible_text="")
            fts_store.backfill_capture_ocr_text(conn, "c1", "ocr result text")
            text = fts_store.get_capture_visible_text(conn, "c1")
        assert text == "ocr result text"

    def test_does_not_overwrite_existing_text(self, ac_root: Path) -> None:
        with fts_store.cursor() as conn:
            _insert_capture(conn, id="c2", visible_text="original text")
            fts_store.backfill_capture_ocr_text(conn, "c2", "ocr result text")
            text = fts_store.get_capture_visible_text(conn, "c2")
        assert text == "original text"

    def test_fills_null_visible_text(self, ac_root: Path) -> None:
        with fts_store.cursor() as conn:
            _insert_capture(conn, id="c3", visible_text="")
            # Force NULL via raw SQL to simulate pre-backfill state
            conn.execute("UPDATE captures SET visible_text = NULL WHERE id = 'c3'")
            fts_store.backfill_capture_ocr_text(conn, "c3", "from ocr")
            text = fts_store.get_capture_visible_text(conn, "c3")
        assert text == "from ocr"

    def test_fts_synced_after_backfill(self, ac_root: Path) -> None:
        """The captures_au trigger must keep captures_fts in sync."""
        with fts_store.cursor() as conn:
            _insert_capture(conn, id="c4", visible_text="")
            fts_store.backfill_capture_ocr_text(conn, "c4", "unique ocr phrase")
            hits = fts_store.search_captures(conn, query="unique ocr phrase")
        assert any(h.id == "c4" for h in hits)

    def test_no_op_on_unknown_capture(self, ac_root: Path) -> None:
        with fts_store.cursor() as conn:
            # Should not raise even if capture_id doesn't exist
            fts_store.backfill_capture_ocr_text(conn, "nonexistent", "text")


# ─── capture/scheduler.py: synchronous local-OCR backfill ─────────────────────


class TestSyncOcrBackfill:
    def test_backfills_capture_from_local_ocr(
        self, monkeypatch: pytest.MonkeyPatch, ac_root: Path
    ) -> None:
        from persome.capture import scheduler as sched_mod

        monkeypatch.setattr(
            sched_mod.ocr_local,
            "recognize_detailed",
            lambda *a, **k: (["ocr text"], [[0, 0, 0, 0]], [0.9]),
        )
        with fts_store.cursor() as conn:
            _insert_capture(conn, id="cap-stem", visible_text="")

        # structured=False (default) → raw "\n".join backfill, the pre-structuring behavior
        sched_mod._submit_ocr_async(b"jpeg", "cap-stem", "tiny")

        with fts_store.cursor() as conn:
            text = fts_store.get_capture_visible_text(conn, "cap-stem")
        assert text == "ocr text"

    def test_no_backfill_when_ocr_empty(
        self, monkeypatch: pytest.MonkeyPatch, ac_root: Path
    ) -> None:
        from persome.capture import scheduler as sched_mod

        monkeypatch.setattr(sched_mod.ocr_local, "recognize_detailed", lambda *a, **k: None)
        with fts_store.cursor() as conn:
            _insert_capture(conn, id="cap-empty", visible_text="")

        sched_mod._submit_ocr_async(b"jpeg", "cap-empty", "tiny")

        with fts_store.cursor() as conn:
            text = fts_store.get_capture_visible_text(conn, "cap-empty")
        assert text == ""

    def test_does_not_overwrite_existing_text(
        self, monkeypatch: pytest.MonkeyPatch, ac_root: Path
    ) -> None:
        from persome.capture import scheduler as sched_mod

        monkeypatch.setattr(
            sched_mod.ocr_local,
            "recognize_detailed",
            lambda *a, **k: (["new ocr text"], [[0, 0, 0, 0]], [0.9]),
        )
        with fts_store.cursor() as conn:
            _insert_capture(conn, id="cap-existing", visible_text="already has text")

        sched_mod._submit_ocr_async(b"jpeg", "cap-existing", "tiny")

        with fts_store.cursor() as conn:
            text = fts_store.get_capture_visible_text(conn, "cap-existing")
        assert text == "already has text"

    def test_structured_backfill_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch, ac_root: Path
    ) -> None:
        """structured=True → field-labeled WeChat markdown lands in visible_text."""
        from persome.capture import scheduler as sched_mod

        # one sidebar row (contact + time) so the WeChat structurer yields a chat
        monkeypatch.setattr(
            sched_mod.ocr_local,
            "recognize_detailed",
            lambda *a, **k: (
                ["罗", "14:48"],
                [[80, 60, 130, 76], [280, 62, 332, 74]],
                [0.97, 0.92],
            ),
        )
        monkeypatch.setattr(sched_mod, "_image_width", lambda b: 960)
        with fts_store.cursor() as conn:
            _insert_capture(conn, id="cap-struct", visible_text="")

        sched_mod._submit_ocr_async(
            b"jpeg",
            "cap-struct",
            "tiny",
            {"bundle_id": "com.tencent.xinWeChat", "app_name": "WeChat"},
            True,
        )

        with fts_store.cursor() as conn:
            text = fts_store.get_capture_visible_text(conn, "cap-struct")
        assert "会话列表" in text and "罗" in text  # structured, not raw "罗\n14:48"


# ─── capture/scheduler.py: _write_capture defers OCR until AFTER the row lands ──


class _InlineThread:
    """A drop-in threading.Thread that runs the target synchronously on start().

    Lets the test assert the deferred OCR actually backfills, without real threads.
    """

    def __init__(self, *, target, args=(), name=None, daemon=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


class TestWriteCaptureDefersOcr:
    """Regression: _build_capture stashes the JPEG, _write_capture runs OCR only AFTER
    the capture row is indexed — otherwise the backfill UPDATE … WHERE id=? no-ops
    against a row that isn't in the DB yet (the WeChat 'OCR submitted but never stored'
    bug). This drives the real persist path and asserts the text lands.
    """

    def test_deferred_ocr_backfills_after_row_indexed(
        self, monkeypatch: pytest.MonkeyPatch, ac_root: Path
    ) -> None:
        from persome.capture import scheduler as sched_mod

        captured_at_recognize: dict[str, str | None] = {}

        def fake_recognize(image_bytes, tier):
            # At OCR time the capture row MUST already exist (that's the fix). Record
            # whether it's queryable so the test fails loudly if the ordering regresses.
            with fts_store.cursor() as conn:
                captured_at_recognize["row"] = fts_store.get_capture_visible_text(
                    conn, captured_at_recognize["stem"]
                )
            return (["recovered wechat text"], [[0, 0, 0, 0]], [0.9])

        monkeypatch.setattr(sched_mod.ocr_local, "recognize_detailed", fake_recognize)
        monkeypatch.setattr(sched_mod.threading, "Thread", _InlineThread)

        ts = "2026-05-21T10:00:00+08:00"
        stem = sched_mod._safe_filename(ts)
        captured_at_recognize["stem"] = stem
        # _build_capture clears the header-only visible_text before stashing the JPEG,
        # so by _write_capture time the row is indexed with an empty visible_text and
        # the backfill's empty-guard passes. Mirror that here (visible_text already "").
        out = {
            "timestamp": ts,
            "window_meta": {
                "app_name": "WeChat",
                "title": "微信",
                "bundle_id": "com.tencent.xinWeChat",
            },
            "focused_element": {},
            "visible_text": "",
            "url": "",
            "_ocr_pending_jpeg": b"jpeg-bytes",
            "_ocr_tier": "tiny",
        }

        sched_mod._write_capture(out)

        # The row existed at recognize time (ordering correct), and the text landed.
        assert captured_at_recognize["row"] == ""  # row present, pre-backfill empty
        with fts_store.cursor() as conn:
            assert fts_store.get_capture_visible_text(conn, stem) == "recovered wechat text"

    def test_private_ocr_keys_not_written_to_disk(
        self, monkeypatch: pytest.MonkeyPatch, ac_root: Path
    ) -> None:
        from persome import paths
        from persome.capture import scheduler as sched_mod

        monkeypatch.setattr(
            sched_mod.ocr_local,
            "recognize_detailed",
            lambda *a, **k: (["x"], [[0, 0, 0, 0]], [0.9]),
        )
        monkeypatch.setattr(sched_mod.threading, "Thread", _InlineThread)

        ts = "2026-05-21T11:00:00+08:00"
        out = {
            "timestamp": ts,
            "window_meta": {"app_name": "WeChat", "title": "微信", "bundle_id": "com.x"},
            "focused_element": {},
            "visible_text": "## 微信",
            "url": "",
            "_ocr_pending_jpeg": b"jpeg",
            "_ocr_tier": "tiny",
            "_ocr_structured": True,
        }
        path = sched_mod._write_capture(out)

        on_disk = json.loads((paths.capture_buffer_dir() / path.name).read_text())
        # None of the private OCR control keys may leak into the on-disk capture JSON.
        assert "_ocr_pending_jpeg" not in on_disk  # raw bytes never serialized
        assert "_ocr_tier" not in on_disk
        assert "_ocr_structured" not in on_disk
        assert on_disk.get("ocr_submitted") is True  # but the marker is kept


# ─── mcp/captures.py: OCR enrichment in read_recent_capture ──────────────────


class TestReadRecentCaptureOcrEnrich:
    def _stem(self) -> str:
        """A valid safe-filename stem matching the capture-buffer naming convention."""
        # Format mirrors scheduler._safe_filename: YYYY-MM-DDTHH-MM-SSp00-00
        return "2026-05-21T10-00-00p08-00"

    def test_enriches_empty_visible_text_from_ocr(self, ac_root: Path) -> None:
        from persome import paths
        from persome.mcp.captures import read_recent_capture

        stem = self._stem()
        buf = paths.capture_buffer_dir()
        _write_capture_json(buf, stem, visible_text="")

        with fts_store.cursor() as conn:
            _backfill_ocr(conn, capture_id=stem, text="ocr enriched text")

        result = read_recent_capture()
        assert result is not None
        assert result["visible_text"] == "ocr enriched text"

    def test_does_not_overwrite_existing_visible_text(self, ac_root: Path) -> None:
        from persome import paths
        from persome.mcp.captures import read_recent_capture

        stem = self._stem()
        buf = paths.capture_buffer_dir()
        _write_capture_json(buf, stem, visible_text="original ax text")

        with fts_store.cursor() as conn:
            _backfill_ocr(conn, capture_id=stem, text="ocr text that should not win")

        result = read_recent_capture()
        assert result is not None
        assert result["visible_text"] == "original ax text"

    def test_no_ocr_available_returns_empty(self, ac_root: Path) -> None:
        from persome import paths
        from persome.mcp.captures import read_recent_capture

        stem = self._stem()
        buf = paths.capture_buffer_dir()
        _write_capture_json(buf, stem, visible_text="")

        result = read_recent_capture()
        assert result is not None
        assert result["visible_text"] == ""

    def test_enriches_with_at_filter(self, ac_root: Path) -> None:
        from persome import paths
        from persome.mcp.captures import read_recent_capture

        stem = self._stem()
        buf = paths.capture_buffer_dir()
        _write_capture_json(buf, stem, visible_text="")

        with fts_store.cursor() as conn:
            _backfill_ocr(conn, capture_id=stem, text="anchored ocr text")

        result = read_recent_capture(at="2026-05-21T10:00:00+08:00")
        assert result is not None
        assert result["visible_text"] == "anchored ocr text"


# ─── mcp/captures.py: explicit text-source + capture status (dev #raw) ────────


def _write_rich_capture_json(
    buf: Path,
    stem: str,
    *,
    visible_text: str = "",
    app_name: str = "WeChat",
    bundle_id: str = "com.tencent.xinWeChat",
    ocr_submitted: bool | None = None,
    ax_tree: Any | None = None,
) -> None:
    """A capture JSON carrying the provenance/status fields the dashboard reads."""
    buf.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "timestamp": "2026-05-21T10:00:00+08:00",
        "schema_version": 2,
        "trigger": {"event_type": "AXApplicationActivated", "bundle_id": bundle_id},
        "window_meta": {"app_name": app_name, "title": "微信", "bundle_id": bundle_id},
        "ax_metadata": {"mode": "frontmost", "depth": 100, "platform": "macos", "raw": False},
        "focused_element": {},
        "visible_text": visible_text,
        "url": "",
    }
    if ocr_submitted is not None:
        data["ocr_submitted"] = ocr_submitted
    if ax_tree is not None:
        data["ax_tree"] = ax_tree
    (buf / f"{stem}.json").write_text(json.dumps(data, ensure_ascii=False))


class TestCaptureTextSourceAndStatus:
    """The provenance split + status block the #raw dashboard surfaces."""

    _STEM = "2026-05-21T10-00-00p08-00"

    def test_ax_source_when_visible_text_present(self, ac_root: Path) -> None:
        from persome import paths
        from persome.mcp.captures import read_recent_capture

        _write_rich_capture_json(
            paths.capture_buffer_dir(), self._STEM, visible_text="  - [Button] real ax content"
        )
        r = read_recent_capture()
        assert r is not None
        assert r["text_source"] == "ax"
        assert r["ax_text"] == "  - [Button] real ax content"
        assert r["ocr_text"] == ""
        assert r["ax"]["has_content"] is True  # indented bullet line
        assert r["ocr"]["status"] == "not_run"
        # status fields surfaced
        assert r["trigger"] == "AXApplicationActivated"
        assert r["schema_version"] == 2
        assert r["ax"]["mode"] == "frontmost"

    def test_ocr_source_when_ax_empty_and_db_backfilled(self, ac_root: Path) -> None:
        from persome import paths
        from persome.mcp.captures import read_recent_capture

        _write_rich_capture_json(
            paths.capture_buffer_dir(), self._STEM, visible_text="", ocr_submitted=True
        )
        with fts_store.cursor() as conn:
            _backfill_ocr(conn, capture_id=self._STEM, text="微信 OCR 识别到的会话内容")

        r = read_recent_capture()
        assert r is not None
        assert r["text_source"] == "ocr"
        assert r["ax_text"] == ""
        assert r["ocr_text"] == "微信 OCR 识别到的会话内容"
        assert r["visible_text"] == "微信 OCR 识别到的会话内容"  # resolved, back-compat
        assert r["ocr"]["status"] == "recognized"
        assert r["ocr"]["submitted"] is True

    def test_ocr_submitted_but_empty(self, ac_root: Path) -> None:
        """OCR ran on the screenshot but read nothing — the dashboard must be able
        to explain WHY a WeChat window is blank (vs simply not attempted)."""
        from persome import paths
        from persome.mcp.captures import read_recent_capture

        _write_rich_capture_json(
            paths.capture_buffer_dir(), self._STEM, visible_text="", ocr_submitted=True
        )
        r = read_recent_capture()
        assert r is not None
        assert r["text_source"] == "none"
        assert r["ocr"]["status"] == "submitted_empty"

    def test_none_source_when_nothing(self, ac_root: Path) -> None:
        from persome import paths
        from persome.mcp.captures import read_recent_capture

        _write_rich_capture_json(paths.capture_buffer_dir(), self._STEM, visible_text="")
        r = read_recent_capture()
        assert r is not None
        assert r["text_source"] == "none"
        assert r["ocr"]["status"] == "not_run"  # never submitted

    def test_ax_node_count(self, ac_root: Path) -> None:
        from persome import paths
        from persome.mcp.captures import read_recent_capture

        tree = {
            "apps": [{"role": "AXWindow", "children": [{"role": "AXButton"}, {"role": "AXText"}]}]
        }
        _write_rich_capture_json(
            paths.capture_buffer_dir(), self._STEM, visible_text="x", ax_tree=tree
        )
        r = read_recent_capture()
        assert r is not None
        assert r["ax"]["present"] is True
        assert r["ax"]["node_count"] == 3  # window + 2 children

    def test_read_recent_capture_accepts_exact_stem(self, ac_root: Path) -> None:
        from persome import paths
        from persome.mcp.captures import read_recent_capture

        _write_rich_capture_json(
            paths.capture_buffer_dir(), self._STEM, visible_text="", ocr_submitted=True
        )
        with fts_store.cursor() as conn:
            _backfill_ocr(conn, capture_id=self._STEM, text="exact-lookup ocr text")

        r = read_recent_capture(at=self._STEM)
        assert r is not None
        assert r["file_stem"] == self._STEM
        assert r["ocr_text"] == "exact-lookup ocr text"

    def test_read_recent_capture_rejects_invalid_stem(self, ac_root: Path) -> None:
        from persome.mcp.captures import read_recent_capture

        with pytest.raises(ValueError):
            read_recent_capture(at="../../etc/passwd")
        with pytest.raises(ValueError):
            read_recent_capture(at="does-not-exist")


class TestCurrentContextHeadlinePreview:
    def test_headline_carries_preview_and_chars(self, ac_root: Path) -> None:
        from persome.mcp.captures import current_context

        with fts_store.cursor() as conn:
            _insert_capture(conn, id="ctx-1", visible_text="hello world preview text")

        ctx = current_context(headline_limit=5)
        heads = ctx["recent_captures_headline"]
        assert heads, "expected at least one headline"
        h = next(x for x in heads if x["file_stem"] == "ctx-1")
        assert h["preview"] == "hello world preview text"
        assert h["text_chars"] == len("hello world preview text")


class TestIncludeAxTreeExpand:
    """Progressive-disclosure 'expand': include_ax_tree returns the full tree
    (the folded browser chrome) on demand; default omits it."""

    _STEM = "2026-05-21T10-00-00p08-00"

    def _write(self, buf: Path) -> None:
        buf.mkdir(parents=True, exist_ok=True)
        data = {
            "timestamp": "2026-05-21T10:00:00+08:00",
            "window_meta": {"app_name": "Chrome", "title": "X", "bundle_id": "com.google.Chrome"},
            "focused_element": {},
            "visible_text": "page body",
            "url": "",
            "ax_tree": {
                "apps": [
                    {
                        "bundle_id": "com.google.Chrome",
                        "windows": [
                            {
                                "elements": [
                                    {
                                        "role": "AXToolbar",
                                        "children": [{"role": "AXButton", "title": "Bookmark"}],
                                    },
                                ]
                            }
                        ],
                    }
                ]
            },
        }
        (buf / f"{self._STEM}.json").write_text(json.dumps(data, ensure_ascii=False))

    def test_default_omits_ax_tree(self, ac_root: Path) -> None:
        from persome import paths
        from persome.mcp.captures import read_recent_capture

        self._write(paths.capture_buffer_dir())
        r = read_recent_capture(at=self._STEM)
        assert r is not None
        assert "ax_tree" not in r

    def test_include_ax_tree_returns_full_tree(self, ac_root: Path) -> None:
        from persome import paths
        from persome.mcp.captures import read_recent_capture

        self._write(paths.capture_buffer_dir())
        r = read_recent_capture(at=self._STEM, include_ax_tree=True)
        assert r is not None and isinstance(r.get("ax_tree"), dict)
        assert r["ax_tree"]["apps"][0]["bundle_id"] == "com.google.Chrome"
        # also via the time-based reader
        r2 = read_recent_capture(include_ax_tree=True)
        assert r2 is not None and "ax_tree" in r2
