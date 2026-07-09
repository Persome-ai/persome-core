"""#7 / spec E5: actionable-subset extended screenshot retention + intent→capture provenance.

Two coupled features, both exercised end-to-end on a tmp ``PERSOME_ROOT``:

1. ``capture/scheduler.cleanup_buffer`` keeps the screenshot of *actionable*
   captures (ones that produced an intent, or Enter-anchored frames) past the
   normal 24h strip, until ``capture_actionable_retention_days`` — only when the
   feature is enabled; OFF (default) is byte-for-byte the legacy strip.
2. ``intent/store`` records each intent's source capture stem in a dedicated
   ``source_capture`` column so "intent → that screenshot" is a direct reverse
   query, with a time-window-join fallback for legacy NULL rows.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from persome.capture import scheduler as scheduler_mod
from persome.capture import screenshot_crypto
from persome.intent import store as intent_store
from persome.intent.ontology import Intent, IntentEvidence
from persome.store import fts

_HOUR = 3600
_DAY = 86400


def _capture_dict(*, ts: str, text: str = "hello", enc: bool = False, trigger_type: str = "manual") -> dict:
    shot: dict = {
        "image_base64": "Y2lwaGVydGV4dA==" if not enc else "PSOMEGCM1:Y2lwaGVydGV4dA==",
        "mime_type": "image/jpeg",
        "width": 100,
        "height": 50,
    }
    if enc:
        shot["screenshot_enc"] = True
    return {
        "timestamp": ts,
        "schema_version": 2,
        "trigger": {"event_type": trigger_type},
        "window_meta": {"app_name": "Cursor", "title": "main.py", "bundle_id": "com.test.cursor"},
        "focused_element": {"role": "AXTextArea", "value": text, "is_editable": True},
        "visible_text": text,
        "url": "",
        "screenshot": shot,
    }


def _write(out: dict) -> Path:
    return scheduler_mod._write_capture(out)


def _age_file(path: Path, *, seconds_old: float) -> None:
    """Backdate a file's mtime so the retention scanner treats it as aged."""
    t = time.time() - seconds_old
    os.utime(path, (t, t))


def _has_screenshot(path: Path) -> bool:
    return "screenshot" in json.loads(path.read_text())


# --- provenance: intent → capture (new column) --------------------------------


def test_insert_intent_records_source_capture(ac_root: Path) -> None:
    intent = Intent(
        kind="reminder",
        scope="fast-K1",
        ts="2026-04-22T14:00:00+08:00",
        payload={"text": "ping bob"},
        evidence=[IntentEvidence(source="capture", ref_id="2026-04-22T14-00-00p08-00", quote="ping")],
    )
    with fts.cursor() as conn:
        row_id = intent_store.insert_intent(conn, intent)
        # Direct reverse query: capture stem → intent ids.
        ids = intent_store.intent_ids_for_capture(conn, "2026-04-22T14-00-00p08-00")
        assert ids == [row_id]
        # And the actionable-stem set used by the retention scanner.
        assert intent_store.actionable_capture_stems(conn) == {"2026-04-22T14-00-00p08-00"}


def test_slow_path_intent_has_no_source_capture(ac_root: Path) -> None:
    # A timeline_block-sourced (slow path) intent carries NO capture provenance →
    # source_capture NULL → not in the actionable set → falls back to time-window.
    intent = Intent(
        kind="reminder",
        scope="session-abc",
        ts="2026-04-22T15:00:00+08:00",
        payload={"text": "later"},
        evidence=[IntentEvidence(source="timeline_block", ref_id="tlb-1", quote="x")],
    )
    with fts.cursor() as conn:
        intent_store.insert_intent(conn, intent)
        assert intent_store.actionable_capture_stems(conn) == set()
        assert intent_store.intent_ids_for_capture(conn, "tlb-1") == []


def test_legacy_row_without_source_capture_column_is_tolerated(ac_root: Path) -> None:
    """An old DB whose ``intents`` table predates ``source_capture`` migrates
    idempotently and a legacy row reads back as NULL (fallback to time-window)."""
    with fts.cursor() as conn:
        # Simulate a pre-migration table: drop the column-bearing schema and
        # build a minimal old one, then insert a bare row.
        conn.executescript(
            """
            DROP TABLE IF EXISTS intents;
            CREATE TABLE intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL, scope TEXT NOT NULL, kind TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.0, status TEXT NOT NULL DEFAULT 'open',
                rationale TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL DEFAULT '{}',
                evidence TEXT NOT NULL DEFAULT '[]', dedup_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO intents (ts, scope, kind, created_at) VALUES (?,?,?,?)",
            ("2026-01-01T00:00:00", "old", "reminder", "2026-01-01T00:00:00"),
        )
        conn.commit()
        # ensure_schema must ADD the column without crashing on the legacy row…
        intent_store.ensure_schema(conn)
        # …and the legacy row has a NULL source_capture (excluded from the set).
        assert intent_store.actionable_capture_stems(conn) == set()
        # A fresh insert still records provenance on the migrated table.
        new_id = intent_store.insert_intent(
            conn,
            Intent(
                kind="reminder",
                scope="fast-K1",
                ts="2026-06-01T00:00:00+08:00",
                payload={"text": "x"},
                evidence=[IntentEvidence(source="capture", ref_id="stem-new")],
            ),
        )
        assert intent_store.intent_ids_for_capture(conn, "stem-new") == [new_id]


# --- retention: OFF (default) is byte-for-byte the legacy strip ----------------


def test_extended_retention_off_strips_actionable_too(ac_root: Path) -> None:
    out = _capture_dict(ts="2026-04-22T14:00:00+08:00")
    path = _write(out)
    # Make it actionable by provenance.
    with fts.cursor() as conn:
        intent_store.insert_intent(
            conn,
            Intent(
                kind="reminder",
                scope="fast-K1",
                ts="2026-04-22T14:00:00+08:00",
                payload={"text": "x"},
                evidence=[IntentEvidence(source="capture", ref_id=path.stem)],
            ),
        )
    _age_file(path, seconds_old=30 * _HOUR)  # past the 24h strip cutoff
    # Default call (extended_retention_enabled defaults False) → legacy behaviour.
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=72, screenshot_retention_hours=24
    )
    assert stats["stripped"] == 1
    assert not _has_screenshot(path)


# --- retention: ON → actionable kept, non-actionable stripped ------------------


def test_actionable_capture_kept_past_24h_when_enabled(ac_root: Path) -> None:
    actionable = _write(_capture_dict(ts="2026-04-22T14:00:00+08:00", text="aaa"))
    plain = _write(_capture_dict(ts="2026-04-22T15:00:00+08:00", text="bbb"))
    with fts.cursor() as conn:
        intent_store.insert_intent(
            conn,
            Intent(
                kind="reminder",
                scope="fast-K1",
                ts="2026-04-22T14:00:00+08:00",
                payload={"text": "aaa"},
                evidence=[IntentEvidence(source="capture", ref_id=actionable.stem)],
            ),
        )
    _age_file(actionable, seconds_old=30 * _HOUR)
    _age_file(plain, seconds_old=30 * _HOUR)
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=72,
        screenshot_retention_hours=24,
        extended_retention_enabled=True,
        actionable_retention_days=7,
    )
    # The intent-referenced capture keeps its screenshot; the plain one strips.
    assert _has_screenshot(actionable)
    assert not _has_screenshot(plain)
    assert stats["stripped"] == 1


def test_enter_anchored_capture_kept_without_intent(ac_root: Path) -> None:
    enter = _write(_capture_dict(ts="2026-04-22T16:00:00+08:00", trigger_type="UserTextInput"))
    plain = _write(_capture_dict(ts="2026-04-22T16:01:00+08:00", trigger_type="manual"))
    _age_file(enter, seconds_old=30 * _HOUR)
    _age_file(plain, seconds_old=30 * _HOUR)
    scheduler_mod.cleanup_buffer(
        retention_hours=72,
        screenshot_retention_hours=24,
        extended_retention_enabled=True,
        actionable_retention_days=7,
    )
    assert _has_screenshot(enter)  # Enter-anchored → kept (no intent needed)
    assert not _has_screenshot(plain)


def test_actionable_stripped_past_extended_cap(ac_root: Path) -> None:
    actionable = _write(_capture_dict(ts="2026-04-22T14:00:00+08:00"))
    with fts.cursor() as conn:
        intent_store.insert_intent(
            conn,
            Intent(
                kind="reminder",
                scope="fast-K1",
                ts="2026-04-22T14:00:00+08:00",
                payload={"text": "x"},
                evidence=[IntentEvidence(source="capture", ref_id=actionable.stem)],
            ),
        )
    # Older than the 7-day actionable cap → strips even though actionable.
    _age_file(actionable, seconds_old=8 * _DAY)
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=30 * 24,  # don't whole-file delete it
        screenshot_retention_hours=24,
        extended_retention_enabled=True,
        actionable_retention_days=7,
    )
    assert stats["stripped"] == 1
    assert not _has_screenshot(actionable)


def test_extended_retention_keeps_encrypted_screenshot_ciphertext(ac_root: Path) -> None:
    out = _capture_dict(ts="2026-04-22T14:00:00+08:00", enc=True)
    path = _write(out)
    before = json.loads(path.read_text())["screenshot"]
    assert before.get("screenshot_enc") is True
    with fts.cursor() as conn:
        intent_store.insert_intent(
            conn,
            Intent(
                kind="reminder",
                scope="fast-K1",
                ts="2026-04-22T14:00:00+08:00",
                payload={"text": "x"},
                evidence=[IntentEvidence(source="capture", ref_id=path.stem)],
            ),
        )
    _age_file(path, seconds_old=30 * _HOUR)
    scheduler_mod.cleanup_buffer(
        retention_hours=72,
        screenshot_retention_hours=24,
        extended_retention_enabled=True,
        actionable_retention_days=7,
    )
    after = json.loads(path.read_text())["screenshot"]
    # Kept AND byte-identical — never decrypted/re-encrypted on the retain path.
    assert after == before
    assert after["screenshot_enc"] is True
    assert after["image_base64"] == before["image_base64"]
    # Sanity: it was genuinely the at-rest ciphertext envelope, not plaintext.
    assert screenshot_crypto.is_encrypted(after["image_base64"])


def test_whole_file_delete_unaffected_by_extended_retention(ac_root: Path) -> None:
    # Extended retention only defers the SCREENSHOT strip; whole-file delete past
    # buffer_retention_hours still removes an actionable capture entirely.
    actionable = _write(_capture_dict(ts="2026-04-22T14:00:00+08:00"))
    with fts.cursor() as conn:
        intent_store.insert_intent(
            conn,
            Intent(
                kind="reminder",
                scope="fast-K1",
                ts="2026-04-22T14:00:00+08:00",
                payload={"text": "x"},
                evidence=[IntentEvidence(source="capture", ref_id=actionable.stem)],
            ),
        )
    _age_file(actionable, seconds_old=100 * _HOUR)
    stats = scheduler_mod.cleanup_buffer(
        retention_hours=72,
        screenshot_retention_hours=24,
        extended_retention_enabled=True,
        actionable_retention_days=7,
    )
    assert stats["deleted"] == 1
    assert not actionable.exists()
