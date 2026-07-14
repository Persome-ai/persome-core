"""Normalized wearable/health event import contract."""

from __future__ import annotations

from fastapi.testclient import TestClient

from persome.api import build_api_app
from persome.store import fts


def _client() -> TestClient:
    return TestClient(build_api_app(auth_enabled=False))


def _payload() -> dict:
    return {
        "schema_version": 1,
        "events": [
            {
                "event_id": "HKQuantitySample:heart-rate:abc123",
                "source": {
                    "provider": "apple_health",
                    "device": "Apple Watch",
                    "device_id": "watch-local-id",
                },
                "metric": "heart_rate",
                "value": 72,
                "unit": "bpm",
                "started_at": "2026-07-15T09:30:00+08:00",
                "ended_at": "2026-07-15T09:30:05+08:00",
                "timezone": "Asia/Shanghai",
                "metadata": {"source_revision": "watchOS"},
            }
        ],
    }


def test_import_persists_normalized_event(ac_root) -> None:
    response = _client().post("/health-events/import", json=_payload())
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "schema_version": 1,
        "received": 1,
        "inserted": 1,
        "corrected": 0,
        "duplicates": 0,
    }

    with fts.cursor() as conn:
        row = conn.execute("SELECT * FROM health_events").fetchone()
    assert row["provider"] == "apple_health"
    assert row["external_id"] == "HKQuantitySample:heart-rate:abc123"
    assert row["metric"] == "heart_rate"
    assert row["value_json"] == "72.0"
    assert row["started_at"] == "2026-07-15T09:30:00+08:00"


def test_import_is_idempotent(ac_root) -> None:
    client = _client()
    assert client.post("/health-events/import", json=_payload()).status_code == 200
    response = client.post("/health-events/import", json=_payload())
    assert response.json()["data"]["inserted"] == 0
    assert response.json()["data"]["corrected"] == 0
    assert response.json()["data"]["duplicates"] == 1


def test_same_id_with_changed_content_corrects_existing_event(ac_root) -> None:
    client = _client()
    assert client.post("/health-events/import", json=_payload()).status_code == 200

    corrected = _payload()
    corrected["events"][0]["value"] = 76
    corrected["events"][0]["metadata"] = {"source_revision": "watchOS", "sync": 2}
    response = client.post("/health-events/import", json=corrected)

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "schema_version": 1,
        "received": 1,
        "inserted": 0,
        "corrected": 1,
        "duplicates": 0,
    }
    with fts.cursor() as conn:
        rows = conn.execute("SELECT value_json, metadata_json FROM health_events").fetchall()
    assert [(row["value_json"], row["metadata_json"]) for row in rows] == [
        ("76.0", '{"source_revision":"watchOS","sync":2}')
    ]


def test_import_rejects_naive_or_reversed_timestamps(ac_root) -> None:
    payload = _payload()
    payload["events"][0]["started_at"] = "2026-07-15T09:30:00"
    assert _client().post("/health-events/import", json=payload).status_code == 422

    payload = _payload()
    payload["events"][0]["ended_at"] = "2026-07-15T09:29:00+08:00"
    assert _client().post("/health-events/import", json=payload).status_code == 422


def test_import_rejects_empty_and_oversized_batches(ac_root) -> None:
    assert (
        _client()
        .post("/health-events/import", json={"schema_version": 1, "events": []})
        .status_code
        == 422
    )

    event = _payload()["events"][0]
    response = _client().post(
        "/health-events/import",
        json={"schema_version": 1, "events": [event] * 1001},
    )
    assert response.status_code == 422
