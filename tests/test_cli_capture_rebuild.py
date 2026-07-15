"""Recovery-safe capture-index reconciliation CLI tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from persome import cli, paths
from persome.store import fts


def _seed_snapshot_only_capture() -> None:
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id="snapshot-only",
            timestamp="2026-06-01T00:00:00+00:00",
            app_name="Archive",
            bundle_id="com.persome.archive",
            window_title="Older snapshot capture",
            focused_role="AXTextArea",
            focused_value="historical",
            visible_text="historical snapshot evidence",
            url="https://example.test/archive",
        )


def _write_buffer_capture() -> Path:
    target = paths.capture_buffer_dir() / "buffer-new.json"
    target.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-12T00:00:00+00:00",
                "window_meta": {
                    "app_name": "Browser",
                    "bundle_id": "com.persome.browser",
                    "title": "New buffer capture",
                },
                "focused_element": {"role": "AXTextField", "value": "fresh"},
                "visible_text": "fresh retained buffer evidence",
                "url": "https://example.test/fresh",
            }
        ),
        encoding="utf-8",
    )
    return target


def test_rebuild_captures_merge_preserves_snapshot_rows_and_upserts_buffer(ac_root: Path) -> None:
    _seed_snapshot_only_capture()
    _write_buffer_capture()

    result = CliRunner().invoke(cli.app, ["rebuild-captures-index", "--merge"])

    assert result.exit_code == 0, result.output
    assert "Captures index merged" in result.output
    with fts.cursor() as conn:
        ids = {row[0] for row in conn.execute("SELECT id FROM captures").fetchall()}
        historical_hits = fts.search_captures(conn, query="historical", limit=5)
        fresh_hits = fts.search_captures(conn, query="fresh", limit=5)
    assert ids == {"snapshot-only", "buffer-new"}
    assert [hit.id for hit in historical_hits] == ["snapshot-only"]
    assert [hit.id for hit in fresh_hits] == ["buffer-new"]


def test_rebuild_captures_merge_only_writes_missing_or_changed_rows(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def write_capture(capture_id: str, visible_text: str) -> None:
        (paths.capture_buffer_dir() / f"{capture_id}.json").write_text(
            json.dumps(
                {
                    "timestamp": "2026-07-12T00:00:00+00:00",
                    "window_meta": {
                        "app_name": "Browser",
                        "bundle_id": "com.persome.browser",
                        "title": "Retained capture",
                    },
                    "focused_element": {"role": "AXTextField", "value": "fresh"},
                    "visible_text": visible_text,
                    "url": "https://example.test/fresh",
                }
            ),
            encoding="utf-8",
        )

    write_capture("identical", "same retained evidence")
    write_capture("changed", "new retained evidence")
    write_capture("missing", "missing retained evidence")
    with fts.cursor() as conn:
        for capture_id, visible_text in (
            ("identical", "same retained evidence"),
            ("changed", "old retained evidence"),
        ):
            fts.insert_capture(
                conn,
                id=capture_id,
                timestamp="2026-07-12T00:00:00+00:00",
                app_name="Browser",
                bundle_id="com.persome.browser",
                window_title="Retained capture",
                focused_role="AXTextField",
                focused_value="fresh",
                visible_text=visible_text,
                url="https://example.test/fresh",
            )

    original_insert = fts.insert_capture
    written_ids: list[str] = []

    def tracking_insert(conn, **kwargs):
        written_ids.append(kwargs["id"])
        return original_insert(conn, **kwargs)

    monkeypatch.setattr(fts, "insert_capture", tracking_insert)
    result = CliRunner().invoke(cli.app, ["rebuild-captures-index", "--merge"])

    assert result.exit_code == 0, result.output
    assert "3 indexed (1 inserted, 1 updated, 1 unchanged)" in result.output
    assert written_ids == ["changed", "missing"]
    with fts.cursor() as conn:
        assert fts.search_captures(conn, query="old") == []
        assert [hit.id for hit in fts.search_captures(conn, query="new")] == ["changed"]
        assert [hit.id for hit in fts.search_captures(conn, query="missing")] == ["missing"]


def test_rebuild_captures_default_remains_exact_buffer_reconciliation(ac_root: Path) -> None:
    _seed_snapshot_only_capture()
    _write_buffer_capture()

    result = CliRunner().invoke(cli.app, ["rebuild-captures-index"])

    assert result.exit_code == 0, result.output
    assert "Captures index rebuilt" in result.output
    assert "1 indexed (1 inserted, 0 updated, 0 unchanged)" in result.output
    with fts.cursor() as conn:
        ids = {row[0] for row in conn.execute("SELECT id FROM captures").fetchall()}
    assert ids == {"buffer-new"}


@pytest.mark.parametrize(("pid", "lock_held"), [(4242, False), (None, True)])
def test_rebuild_captures_refuses_a_running_or_ambiguous_runtime_before_init(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    pid: int | None,
    lock_held: bool,
) -> None:
    monkeypatch.setattr(cli, "_read_pid", lambda: pid)
    monkeypatch.setattr(cli, "_daemon_lock_is_held", lambda: lock_held)
    monkeypatch.setattr(
        cli,
        "_init",
        lambda *args, **kwargs: pytest.fail("runtime refusal must happen before DB initialization"),
    )

    result = CliRunner().invoke(cli.app, ["rebuild-captures-index", "--merge"])

    assert result.exit_code == 1
    output = " ".join(result.output.split())
    assert "Refusing to rebuild the capture index" in output
    assert "persome stop" in output


def test_rebuild_captures_indexes_inside_exclusive_transaction(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_buffer_capture()
    original_insert = fts.insert_capture
    transaction_states: list[bool] = []
    maintenance_states: list[bool] = []

    def tracking_insert(conn, **kwargs):
        transaction_states.append(conn.in_transaction)
        maintenance_states.append(fts._in_exclusive_maintenance())  # noqa: SLF001
        return original_insert(conn, **kwargs)

    monkeypatch.setattr(fts, "insert_capture", tracking_insert)

    result = CliRunner().invoke(cli.app, ["rebuild-captures-index", "--merge"])

    assert result.exit_code == 0, result.output
    assert transaction_states == [True]
    assert maintenance_states == [True]


def test_rebuild_captures_interrupt_rolls_back_exact_reconciliation(
    ac_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_snapshot_only_capture()
    _write_buffer_capture()
    original_insert = fts.insert_capture

    def interrupting_insert(conn, **kwargs):
        original_insert(conn, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(fts, "insert_capture", interrupting_insert)

    result = CliRunner().invoke(cli.app, ["rebuild-captures-index"])

    assert result.exit_code != 0
    with fts.cursor() as conn:
        ids = {row[0] for row in conn.execute("SELECT id FROM captures").fetchall()}
    assert ids == {"snapshot-only"}


def test_rebuild_captures_sanitizes_historical_placeholder_projection(ac_root: Path) -> None:
    phrase = "Ask for follow-up changes"
    target = paths.capture_buffer_dir() / "placeholder-old.json"
    target.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-12T00:00:00+00:00",
                "window_meta": {
                    "app_name": "Chat",
                    "bundle_id": "com.example.chat",
                    "title": "Conversation",
                },
                "focused_element": {
                    "role": "AXTextArea",
                    "value": phrase,
                    "is_editable": True,
                    "value_length": len(phrase),
                },
                "visible_text": f"[TextArea] {phrase}",
                "ax_tree": {
                    "apps": [
                        {
                            "name": "Chat",
                            "bundle_id": "com.example.chat",
                            "is_frontmost": True,
                            "focused_element": {
                                "role": "AXTextArea",
                                "value": phrase,
                                "is_editable": True,
                            },
                            "windows": [
                                {
                                    "title": "Conversation",
                                    "focused": True,
                                    "elements": [
                                        {
                                            "role": "AXTextArea",
                                            "value": phrase,
                                            "children": [
                                                {
                                                    "role": "AXGroup",
                                                    "domClassList": ["placeholder"],
                                                    "children": [
                                                        {"role": "AXStaticText", "value": phrase}
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli.app, ["rebuild-captures-index"])

    assert result.exit_code == 0, result.output
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT focused_value, visible_text FROM captures WHERE id='placeholder-old'"
        ).fetchone()
    assert row is not None
    assert row["focused_value"] == ""
    assert phrase not in row["visible_text"]


def test_rebuild_captures_preserves_db_only_ocr_backfill(ac_root: Path) -> None:
    target = paths.capture_buffer_dir() / "ocr-only.json"
    target.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-12T00:01:00+00:00",
                "window_meta": {
                    "app_name": "WeChat",
                    "bundle_id": "com.tencent.xinWeChat",
                    "title": "Conversation",
                },
                "focused_element": {},
                "visible_text": "",
                "ocr_submitted": True,
            }
        ),
        encoding="utf-8",
    )
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id=target.stem,
            timestamp="2026-07-12T00:01:00+00:00",
            app_name="WeChat",
            bundle_id="com.tencent.xinWeChat",
            window_title="Conversation",
            focused_role="",
            focused_value="",
            visible_text="recognized OCR body",
            url="",
        )

    result = CliRunner().invoke(cli.app, ["rebuild-captures-index"])

    assert result.exit_code == 0, result.output
    with fts.cursor() as conn:
        row = conn.execute("SELECT visible_text FROM captures WHERE id='ocr-only'").fetchone()
    assert row is not None
    assert row["visible_text"] == "recognized OCR body"


def test_rebuild_captures_merge_skips_identical_db_only_ocr_backfill(
    ac_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = paths.capture_buffer_dir() / "ocr-only.json"
    target.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-12T00:01:00+00:00",
                "window_meta": {
                    "app_name": "WeChat",
                    "bundle_id": "com.tencent.xinWeChat",
                    "title": "Conversation",
                },
                "focused_element": {},
                "visible_text": "",
                "ocr_submitted": True,
            }
        ),
        encoding="utf-8",
    )
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id=target.stem,
            timestamp="2026-07-12T00:01:00+00:00",
            app_name="WeChat",
            bundle_id="com.tencent.xinWeChat",
            window_title="Conversation",
            focused_role="",
            focused_value="",
            visible_text="recognized OCR body",
            url="",
        )

    monkeypatch.setattr(
        fts,
        "insert_capture",
        lambda *args, **kwargs: pytest.fail("identical OCR projection must not be rewritten"),
    )
    result = CliRunner().invoke(cli.app, ["rebuild-captures-index", "--merge"])

    assert result.exit_code == 0, result.output
    assert "1 indexed (0 inserted, 0 updated, 1 unchanged)" in result.output
    with fts.cursor() as conn:
        row = conn.execute("SELECT visible_text FROM captures WHERE id='ocr-only'").fetchone()
        hits = fts.search_captures(conn, query="recognized")
    assert row is not None and row["visible_text"] == "recognized OCR body"
    assert [hit.id for hit in hits] == ["ocr-only"]


def test_rebuild_captures_filters_placeholder_from_db_only_ocr_backfill(
    ac_root: Path,
) -> None:
    phrase = "Ask for follow-up changes"
    target = paths.capture_buffer_dir() / "ocr-placeholder.json"
    target.write_text(
        json.dumps(
            {
                "timestamp": "2026-07-12T00:02:00+00:00",
                "window_meta": {
                    "app_name": "Chat",
                    "bundle_id": "com.example.chat",
                    "title": "Conversation",
                },
                "focused_element": {"role": "AXTextArea", "value": phrase},
                "visible_text": "",
                "ocr_submitted": True,
                "ax_tree": {
                    "apps": [
                        {
                            "name": "Chat",
                            "bundle_id": "com.example.chat",
                            "is_frontmost": True,
                            "windows": [
                                {
                                    "title": "Conversation",
                                    "focused": True,
                                    "elements": [
                                        {
                                            "role": "AXTextArea",
                                            "value": phrase,
                                            "children": [
                                                {
                                                    "role": "AXGroup",
                                                    "domClassList": ["placeholder"],
                                                    "children": [
                                                        {
                                                            "role": "AXStaticText",
                                                            "value": phrase,
                                                        }
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    with fts.cursor() as conn:
        fts.insert_capture(
            conn,
            id=target.stem,
            timestamp="2026-07-12T00:02:00+00:00",
            app_name="Chat",
            bundle_id="com.example.chat",
            window_title="Conversation",
            focused_role="AXTextArea",
            focused_value=phrase,
            visible_text=f"{phrase}\nrecognized OCR body",
            url="",
        )

    result = CliRunner().invoke(cli.app, ["rebuild-captures-index"])

    assert result.exit_code == 0, result.output
    with fts.cursor() as conn:
        row = conn.execute(
            "SELECT focused_value, visible_text FROM captures WHERE id='ocr-placeholder'"
        ).fetchone()
    assert row is not None
    assert row["focused_value"] == ""
    assert row["visible_text"] == "recognized OCR body"
