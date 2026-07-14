# HTTP API

Persome exposes a deliberately small loopback HTTP API from the same ASGI
application that hosts MCP. HTTP owns health, trusted capture ingestion, and
the model explorer. Memory retrieval and correction live in MCP.

The generated contract is [`openapi.json`](../openapi.json). Regenerate it after
route or model changes:

```bash
uv run python scripts/regen_openapi.py
```

`tests/test_openapi_drift.py` requires the committed file to byte-match the live
runtime schema.

## Runtime routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness plus compact OCR readiness (`ok` or `degraded`). |
| POST | `/auth/browser-bootstrap` | Exchange the bearer for a 60-second, one-use viewer URL. |
| GET | `/permissions` | macOS Accessibility and Screen Recording state. |
| GET | `/status` | Daemon, capture, OCR, session, memory, and provider status. |
| POST | `/captures/ingest` | Ingest one bearer-authenticated capture from a trusted local producer. |
| POST | `/health-events/import` | Import up to 1,000 normalized wearable/health events from a trusted local connector. |
| GET | `/model` | Open the offline Point/Line/Face/Volume/Root explorer. |
| GET | `/model/graph` | Read the canonical versioned model snapshot. |
| GET | `/model/evidence?ref=...` | Resolve a model ID or receipt into direct sources and separately labeled nearby context. |
| GET | `/model/node?id=...` | Resolve a snapshot Point ID or relation endpoint to receipts and its relation tree. |

### Wearable and health event import

`POST /health-events/import` is the local connector boundary for Apple HealthKit,
Health Connect, vendor APIs, and file/BLE adapters. Every event needs a stable
provider `event_id`; repeated imports are accepted and counted as duplicates.
If the same provider ID arrives with changed normalized content, it corrects the
existing observation and is counted as `corrected`; an identical replay remains
a `duplicate`.
Times must be ISO 8601 values with an explicit offset. The Runtime stores raw
normalized observations and provenance locally; it does not treat consumer
device measurements as medical diagnoses.

The model page renders snapshot Points and Lines directly, then derives the
Face, Volume, and Root hierarchy from their declared `members`. It loads its
pinned Three.js modules from `/model/assets/*`; those package resources are
intentionally omitted from OpenAPI.

Receipt buttons in the viewer call `/model/evidence`. The response is a
progressive-disclosure node with `sources` for explicit stored lineage and
`context` for time-adjacent captures. Human-readable `label` values let clients
present evidence without exposing internal IDs; Point version links are kept in
`history`. The local viewer organizes this as Overview, Evidence, and History,
with drill-down breadcrumbs and raw receipts under technical details. An unknown
or retention-expired payload returns `status=missing` while preserving the
original receipt for audit.

`/status.data.llm_profile` reports the effective provider, protocol, model,
endpoint, key variable name, credential presence, and legacy-migration state.
It never returns the credential value. Provider network probes run only for
the explicit `GET /status?check_models=true` request and are cached briefly.
`/status.data.ocr` reports the configured tier, Runtime and model availability,
kill switch, Screen Recording, and effective readiness. `/permissions` does not
infer Accessibility from the terminal or Python daemon: in daemon mode it runs
the source-versioned helper and optional watcher self-checks plus the Runtime's
Screen Recording preflight. In trusted-ingest mode those OS permissions belong
to the producer and are reported as not applicable to the daemon. `/health`
exposes only compact OCR state because it is the unauthenticated liveness route.

## Model contract

`GET /model/graph` wraps a `model` object with the same schema returned by the
MCP `get_model_snapshot` tool and CLI `persome model export`:

```text
schema_version, generated_at, build,
points, lines, faces, volumes, root, receipts, stats
```

Every Line derived from activity carries `source_kind`, `source_id`, and
`source_receipt`. Legacy `event:<id>` identities are normalized to
`event:intent:<id>` and are read only when an old `intents` table exists.

The loopback viewer receives raw local graph/model detail so its owner can
inspect the real person model. `persome model export` and MCP
`get_model_snapshot` apply deterministic redaction by default; `/model/graph`
is not a publication endpoint.

The authenticated viewer polls for model changes, but the Runtime keeps one
owner-local graph payload in memory for at most 15 seconds and makes refresh
single-flight. This bounds repeated snapshot work across polling tabs without
writing raw graph content to another file. The browser also coalesces overlapping
polls and turns a request that exceeds 45 seconds into an explicit retry state.

## Security boundary

- The server is restricted to loopback and defaults to `127.0.0.1`; wildcard
  and LAN binds are rejected even with a bearer because the server has no TLS.
- Origin and host guards reject non-loopback browser access.
- Every API/MCP route except canonical `GET /health` requires the dedicated
  local bearer. The generated OpenAPI contract declares `LocalBearer` globally;
  the browser viewer may instead use the bearer-derived capability below.
- Use `persome model open`; the viewer bootstrap never puts the long-lived
  bearer in a URL. It exchanges the one-use nonce for an HttpOnly cookie scoped
  to a fresh unguessable viewer path (localhost cookies have no port boundary),
  and protected responses are not cacheable.
- `/captures/ingest` assumes a trusted local producer that obtains the owner
  token through an approved local secret channel and sends the bearer header;
  it is not a public upload API.
- Model assets and graph data load from the same loopback server with no CDN dependency.
- LLM and embedding egress only use endpoints configured by the user.
- Unknown and removed product/admin routes return `404`.
