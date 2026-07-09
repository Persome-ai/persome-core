# Persome REST API

Persome exposes a REST HTTP API alongside its MCP server. The API runs on the same host/port as the MCP transport (`127.0.0.1:8742` by default) and is mounted under `/` (root).

- **Base URL**: `http://127.0.0.1:8742`
- **Authentication**: None (localhost-only)
- **OpenAPI schema**: `http://127.0.0.1:8742/openapi.json`
- **Interactive docs**: `http://127.0.0.1:8742/docs` (Swagger UI)

---

## Response format

All endpoints return a JSON envelope:

```json
{
  "success": true,
  "data": { ... }
}
```

On error:

```json
{
  "success": false,
  "error": "File not found",
  "detail": "memory file event-2026-05-17.md does not exist"
}
```

HTTP status codes:
- `200` — Success
- `400` — Bad request (invalid parameters)
- `404` — Resource not found
- `500` — Internal server error

---

## Endpoints

### System

#### `GET /health`

Health check. Returns immediately without touching the database.

**Response:**
```json
{
  "success": true,
  "data": { "status": "ok" }
}
```

#### `GET /permissions`

Live macOS permission state for the permissions the **daemon itself** needs. The daemon is the process that reads the AX tree (via `mac-ax-helper` / `mac-ax-watcher`), so its own `AXIsProcessTrusted()` is the authoritative Accessibility signal — the GUI app polls this instead of self-checking (which would register a second, redundant TCC principal and pop a confusing second prompt). Pure check: no system dialog is shown. `accessibility` is `granted` / `denied` (always `denied` on non-macOS hosts).

**Response:**
```json
{
  "success": true,
  "data": { "accessibility": "granted" }
}
```

#### `GET /status`

Full daemon status including version, uptime, capture health, session counts, memory stats, timeline stats, and model ping results.

**Response:**
```json
{
  "success": true,
  "data": {
    "version": "0.1.0",
    "root": "/Users/tester/.persome",
    "daemon": "running pid 12345",
    "uptime": "2h 15m",
    "health": "healthy",
    "capture": "active",
    "last_capture": "3m ago (Cursor)",
    "buffer": "42 files, last: 2026-05-17T14-30-00p08-00.json",
    "sessions": "8 total (5 reduced, 3 ended, 0 failed)",
    "memory": "12 active files, 0 dormant, 156 entries",
    "timeline": "480 blocks, last end: 2026-05-17T14:30:00+08:00",
    "models": {
      "timeline": { "stage": "timeline", "model": "gpt-5.4-nano", "ok": true, "latency_ms": 120 },
      "reducer": { "stage": "reducer", "model": "gpt-5.4-nano", "ok": true, "latency_ms": 145 }
    }
  }
}
```

---

### Memory

#### `GET /memories`

List all memory files with metadata.

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `include_dormant` | bool | `false` | Include dormant files |
| `include_archived` | bool | `false` | Include archived files |

**Response:**
```json
{
  "success": true,
  "data": {
    "count": 12,
    "files": [
      {
        "path": "user-profile.md",
        "description": "User identity and preferences",
        "tags": ["user", "profile"],
        "status": "active",
        "entry_count": 23,
        "created": "2026-04-01",
        "updated": "2026-05-17"
      }
    ]
  }
}
```

#### `GET /memories/{path}`

Read a single memory file with optional filtering.

**Path parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | Memory file path (e.g. `user-profile.md`, `event-2026-05-17.md`) |

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `since` | string | — | ISO timestamp lower bound |
| `until` | string | — | ISO timestamp upper bound |
| `tags` | list[string] | — | Filter by tags |
| `tail_n` | int | — | Return only last N entries |

**Response:**
```json
{
  "success": true,
  "data": {
    "path": "user-profile.md",
    "description": "User identity and preferences",
    "tags": ["user", "profile"],
    "status": "active",
    "updated": "2026-05-17",
    "entry_count": 23,
    "entries": [
      {
        "id": "20260517-1430-a1b2c3",
        "timestamp": "2026-05-17T14:30",
        "tags": ["preference", "workflow"],
        "body": "User prefers dark mode in all apps...",
        "superseded_by": null,
        "confidence": "high",
        "conflicted": false,
        "occurred_at": null
      }
    ]
  }
}
```

Each entry also carries the **meta-cognition** fields (Hy-Memory migration): `confidence` (`high`/`medium`/`low`, or `null` when unmarked), `conflicted` (contradicts another memory, not yet adjudicated), and `occurred_at` (the event's real time when it differs from the write `timestamp`).

**Errors:**
- `404` — Memory file not found

#### `GET /search`

BM25 full-text search over compressed memory entries.

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | **required** | Search keywords |
| `paths` | list[string] | — | Glob patterns to scope search (e.g. `event-*.md`) |
| `since` | string | — | ISO timestamp lower bound |
| `until` | string | — | ISO timestamp upper bound |
| `top_k` | int | `5` | Max results (1–50) |
| `include_superseded` | bool | `false` | Include superseded entries |

**Response:**
```json
{
  "success": true,
  "data": {
    "query": "interview",
    "results": [
      {
        "id": "20260517-1430-a1b2c3",
        "path": "event-2026-05-17.md",
        "timestamp": "2026-05-17T14:30",
        "content": "Interview with Alice about Q3 roadmap...",
        "rank": 2.35,
        "confidence": null,
        "conflicted": false,
        "occurred_at": null
      }
    ]
  }
}
```

Result entries carry the same meta-cognition fields as `GET /memories/{path}` (`confidence` / `conflicted` / `occurred_at`, joined from the `entry_metadata` index).

#### `GET /activity`

Newest-first cross-file feed of recent memory entries.

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `since` | string | — | ISO timestamp lower bound |
| `limit` | int | `20` | Max entries (1–200) |
| `prefix_filter` | list[string] | — | Scope by file prefix (e.g. `event-`, `project-`) |

**Response:**
```json
{
  "success": true,
  "data": {
    "count": 20,
    "entries": [
      {
        "id": "20260517-1430-a1b2c3",
        "path": "event-2026-05-17.md",
        "timestamp": "2026-05-17T14:30",
        "content": "Worked on Persome HTTP API..."
      }
    ]
  }
}
```

---

### Captures

#### `GET /captures/current`

One-shot snapshot of the current screen context.

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `app_filter` | string | — | Case-insensitive app name filter |
| `headline_limit` | int | `5` | Number of headline captures |
| `fulltext_limit` | int | `3` | Number of full-text captures |
| `timeline_limit` | int | `8` | Number of timeline blocks |

**Response:**
```json
{
  "success": true,
  "data": {
    "recent_captures_headline": [
      { "time": "14:30", "app_name": "Cursor", "window_title": "dream.py", "focused_role": "AXTextField", "file_stem": "2026-05-17T14-30-00p08-00" }
    ],
    "recent_captures_fulltext": [
      {
        "timestamp": "2026-05-17T14:30:00+08:00",
        "app_name": "Cursor",
        "window_title": "dream.py",
        "url": null,
        "focused_role": "AXTextField",
        "focused_value": "def run_dream():",
        "visible_text": "from __future__ import annotations...",
        "file_stem": "2026-05-17T14-30-00p08-00"
      }
    ],
    "recent_timeline_blocks": [
      {
        "start_time": "2026-05-17T14:29:00+08:00",
        "end_time": "2026-05-17T14:30:00+08:00",
        "entries": ["Editing dream.py in Cursor"],
        "apps_used": ["Cursor"],
        "capture_count": 3
      }
    ]
  }
}
```

#### `GET /captures`

BM25 search over raw screen captures (S1 buffer).

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | string | **required** | Search keywords |
| `since` | string | — | ISO timestamp lower bound |
| `until` | string | — | ISO timestamp upper bound |
| `app_name` | string | — | Case-insensitive app name filter |
| `limit` | int | `10` | Max results (1–50) |

**Response:**
```json
{
  "success": true,
  "data": {
    "query": "rate limiter",
    "results": [
      {
        "timestamp": "2026-05-17T14:30:00+08:00",
        "app_name": "Chrome",
        "bundle_id": "com.google.Chrome",
        "window_title": "GitHub - rate limiter design",
        "url": "https://github.com/...",
        "snippet": "The [rate] [limiter] uses a token bucket...",
        "rank": 3.14,
        "file_stem": "2026-05-17T14-30-00p08-00",
        "focused_role": "AXStaticText",
        "focused_value_preview": "Token bucket algorithm..."
      }
    ]
  }
}
```

#### `GET /captures/recent`

Hydrate one raw capture by time and/or filters.

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `at` | string | — | ISO timestamp or `HH:MM` (today) |
| `app_name` | string | — | Case-insensitive app name filter |
| `window_title_substring` | string | — | Window title substring |
| `include_screenshot` | bool | `false` | Include base64 JPEG |
| `max_age_minutes` | int | `15` | Max age deviation when `at` is set (1–1440) |

**Response:**
```json
{
  "success": true,
  "data": {
    "timestamp": "2026-05-17T14:30:00+08:00",
    "file": "2026-05-17T14-30-00p08-00.json",
    "app_name": "Cursor",
    "bundle_id": "com.todesktop.230313mzl4w4u92",
    "window_title": "dream.py",
    "url": null,
    "focused_element": {
      "role": "AXTextField",
      "title": "",
      "value": "def run_dream():",
      "is_editable": true,
      "value_length": 15
    },
    "visible_text": "from __future__ import annotations...",
    "screenshot_stripped": false
  }
}
```

**Errors:**
- `404` — No matching capture found

---

### Actions

#### `GET /actions`

List pending actions proposed by the active stage.

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `status` | string | `proposed` | Filter: `proposed`, `done`, `dismissed` |
| `limit` | int | `20` | Max actions (1–100) |

**Response:**
```json
{
  "success": true,
  "data": {
    "status_filter": "proposed",
    "count": 2,
    "actions": [
      {
        "id": 1,
        "kind": "calendar",
        "status": "proposed",
        "confidence": 0.92,
        "when_text": "Tomorrow 10:00",
        "with": ["Alice"],
        "channel": "meeting",
        "rationale": "Detected calendar intent tag in timeline",
        "source_block_ids": ["123", "124"],
        "created_at": "2026-05-17T14:30:00+08:00"
      }
    ]
  }
}
```

#### `PATCH /actions/{action_id}`

Mark a pending action as done or dismissed.

**Path parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `action_id` | int | Action ID |

**Request body:**
```json
{
  "action": "done",
  "note": "Confirmed with Alice"
}
```

`action` must be `done` or `dismissed`.

**Response:**
```json
{
  "success": true,
  "data": {
    "success": true,
    "action_id": 1,
    "new_status": "done"
  }
}
```

---

### Intents

The unified intent stream produced by the recognizers (timeline tagging, the
session-level trajectory recognizer, meeting packs). Used by the app's debug
view and by the R3 feedback loop.

#### `GET /intents`

List recognized intents (newest first).

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `scope` | string | (all) | Filter by scene, e.g. `timeline`, `session-<id>` |
| `status` | string | (all) | Filter: `open`, `armed`, `consumed`, `dismissed`, `expired` |
| `limit` | int | `50` | Max intents (1–200) |

**Response:**
```json
{
  "success": true,
  "data": {
    "count": 1,
    "intents": [
      {
        "id": 7,
        "kind": "meeting",
        "scope": "session-abc",
        "status": "open",
        "confidence": 0.9,
        "rationale": "周五的提议在前一分钟，用户这一分钟回复'行'",
        "ts": "2026-06-01T10:02",
        "payload": {"when_text": "周五下午3点", "with": ["张三"]},
        "evidence": [{"source": "session_trajectory", "ref_id": "blk1", "quote": "行"}]
      }
    ]
  }
}
```

#### `GET /intents/stats`

Intent-recognition hit-rate telemetry. Every recognition tick records one row in
`recognition_ticks`; a **hit** is a tick that produced a non-empty intent (i.e.
NOT "暂无识别意图"). `total_ticks` counts **LLM-ran ticks only**; ticks skipped by
the slow-path anchored pre-gate (#547, `outcome=skipped_no_anchor` — never ran
the model) are excluded from the `hit_rate` denominator and reported separately
as `skipped_ticks`. `hit_rate = hit_ticks / total_ticks`. `cooldown_suppressed`
(`{total, by_kind}`, #533) 单列被 kind 级硬冷却闸丢弃的意图——这些意图不进
`intents` 表，是 #534 再校准唯一的数据源（拒绝是金矿）。`pregate`（#609）把慢路
锚定 pre-gate 的成本侧 ROI 直接算好：`skip_rate`（跳过 ÷ 全部尝试）、
`whiteburn_rate`（LLM 跑了但 0 意图 ÷ 实跑）、`empty_capture_rate`（跳过 ÷ 全部空
tick——闸的 ROI 表，≪1 说明闸近乎空转、白烧从旁流过）。按设计正则宽=召回优先，
读到 `empty_capture_rate ≪ 1` 的杠杆在成本侧而非收紧闸。

`downstream`（#613）把**主动产出链下游**遥测出来：识别器持续产出意图，但把意图变成
用户价值的消费链（active 提案 → `pending_actions` → 用户处置 → R3/R4 校准）可能整条
断掉。`active.enabled` 默认关（opt-in），此时不写任何 `pending_actions` 提案，用户
无从 accept/dismiss，`intents` 永远没有 `consumed`/`dismissed` 行，R3 负先验与 R4
schema 反馈两条学习回路全程零输入空转。字段：`active_enabled`（提案生产者是否接上）、
`intents_by_status` / `proposals_by_status`（各表 status 计数）、`disposed_intents`
（consumed+dismissed 总数）、`r3_feedback_signals`（R3 7 天窗内被忽略的意图数=负先验
实收信号）、`r4_feedback_signals`（带终态处置的意图数=R4 schema 反馈实收信号）、
`chain_live`（下游是否真的产生过用户处置）。纯派生快照（COUNT GROUP BY 现有表 +
active 开关），无新表、无 prune 负担——区别于 `recognition_ticks` 等逐 tick 审计表。

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `since` | string | (all) | ISO8601 lower bound (inclusive) |
| `until` | string | (all) | ISO8601 upper bound (exclusive) |

**Response:**
```json
{
  "success": true,
  "data": {
    "total_ticks": 120,
    "hit_ticks": 18,
    "hit_rate": 0.15,
    "skipped_ticks": 35,
    "persisted_total": 14,
    "by_kind": {"meeting": 9, "reminder": 6, "info_need": 3},
    "cooldown_suppressed": {"total": 7, "by_kind": {"reminder": 5, "info_need": 2}},
    "pregate": {
      "attempts": 155,
      "ran_ticks": 120,
      "skipped_ticks": 35,
      "empty_ticks": 102,
      "skip_rate": 0.2258,
      "whiteburn_rate": 0.85,
      "empty_capture_rate": 0.2555
    },
    "downstream": {
      "active_enabled": false,
      "intents_by_status": {"open": 70, "expired": 9},
      "proposals_total": 0,
      "proposals_by_status": {},
      "disposed_intents": 0,
      "r3_feedback_signals": 0,
      "r3_lookback_days": 7,
      "r4_feedback_signals": 0,
      "chain_live": false,
      "since": null,
      "until": null
    },
    "since": null,
    "until": null
  }
}
```

#### `PATCH /intents/{intent_id}`

Set an intent's status — the R3 feedback write-back. `consumed` = the user acted
on it, `dismissed` = rejected (the recognizer treats recently dismissed intents
as a negative prior), `open` = reset.

**Path parameters:**
| Parameter | Type | Description |
|-----------|------|-------------|
| `intent_id` | int | Intent row ID (from `GET /intents`) |

**Request body:**
```json
{ "status": "dismissed" }
```

`status` must be one of `open`, `armed`, `consumed`, `dismissed`, `expired`
(`expired` is normally written by the daily lifecycle harvest — #546 — not by
HUD feedback).

**Response:**
```json
{
  "success": true,
  "data": {
    "success": true,
    "intent_id": 7,
    "new_status": "dismissed"
  }
}
```

---

### WorkThread（工作线"现在进行时"层）

The WorkThread layer (spec `docs/superpowers/specs/2026-06-12-workthread-design.md`)
folds micro-sessions onto "same undertaking" threads. These two endpoints are
the REST mirror of the MCP `current_work_context` / `correct_work_thread`
tools — the data source and feedback port for the app's HUD chip (S4).

#### `GET /work/context`

The current work-thread context: the active thread, background threads, and
the churn/revive telemetry.

**Response:**
```json
{
  "success": true,
  "data": {
    "active_thread": {
      "thread_id": "20260612-0900-abc123",
      "title": "Kevin 交办：意图识别链路优化",
      "goal": "把快慢路打通",
      "status": "active",
      "origin": {"type": "assignment", "actor": "Kevin", "at": "2026-06-10T09:00", "intent_id": 41},
      "since": "2026-06-10T09:00",
      "last_active": "2026-06-12T10:00",
      "total_minutes": 192,
      "approximate": true,
      "confidence": 0.8,
      "pinned": false,
      "recent_progress": ["[2026-06-12T10:00] 快路打通了"],
      "evidence_refs": [{"source": "session_summary", "quote": "这个你来跟进"}]
    },
    "background_threads": [],
    "stats": {"thread_churn": 0.1, "revive_rate": 0.05, "frozen_open": false}
  }
}
```

`total_minutes` is the daemon's deterministic span-based accumulation;
`approximate: true` means some windows had overlapping spans split fair-share
across threads (the figure is an estimate, never an over-count).
`active_thread` is `null` on an idle day.

#### `PATCH /work/threads/{thread_id}`

Apply one correction from the closed set（纠错闭集，零成本开关）. Every call
also mints a ground-truth label on the daemon side (the H1 label factory,
spec §十) that calibrates the thread's confidence.

**Body:**
```json
{"action": "confirm"}
```

| Field | Type | Description |
|-------|------|-------------|
| `action` | string | `confirm` / `not_this` / `rename` / `merge` / `pin` |
| `rename` | string | New title (action=`rename` only) |
| `into_id` | string | Absorbing thread id (action=`merge` only; pinned sources refuse absorption) |

**Response:** `200` with `data: {ok: true, thread_id, action, ...}`;
`400` on an unknown action / thread or a refused merge.

---

### Parser

#### `GET /parser/stats`

Per-app message-parser hit-rate telemetry (general observability layer). The
timeline aggregator records one row in `parser_ticks` per window, bucketed by
app `bundle_id`: **hit** (a registered per-app parser rendered a non-empty
conversation), **miss** (the app had a parser but it declined / rendered empty /
raised), or **fallback** (no app in the window had a registered parser). Use it
to prove the parsers are firing and to catch drift — e.g. a 飞书 UI revision
that breaks the parser shows up as `hit` decaying into `miss` for bundle
`com.electron.lark`. `hit_rate = hit / total` (fallback windows count as
non-hits in the denominator).

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `since` | string | (all) | ISO8601 lower bound (inclusive) |
| `until` | string | (all) | ISO8601 upper bound (exclusive) |

**Response:**
```json
{
  "success": true,
  "data": {
    "total": 240,
    "by_outcome": {"hit": 36, "miss": 4, "fallback": 200},
    "by_bundle": {
      "com.electron.lark": {"hit": 36, "miss": 4, "fallback": 0},
      "com.apple.Safari": {"hit": 0, "miss": 0, "fallback": 200}
    },
    "hit_rate": 0.15,
    "since": null,
    "until": null
  }
}
```

---

#### `GET /intents/fast-path/stats`

K1 fast-path five-gate drop/forward telemetry (#622) — makes #610's "K1 真实沉默"
attributable. The event-driven fast path (`intent.event_source.on_capture`) walks
five cheap gates in cost order and stops at exactly one; before this each gate only
`logger.debug`-ed its DROP, so when #610 saw only 2 fast-K1 recognitions over 4
days there was no way to tell which gate ate the rest. One row in `fast_path_ticks`
is recorded per capture, bucketed by `bundle_id` and `outcome`.

`outcome` (cost order): `non_user` (① origin: self-agent / render) · `no_parser`
(② no per-app parser / no `ax_tree`) · `not_conversation` (② non-K1 parse, e.g. a
browser `WebPage`) · `empty` (② empty conversation / no arrival identity) ·
`not_allowed` (K2 domain allowlist) · `no_unseen` (③ seen-set: no new arrival —
scroll / re-render / already-seen) · `cold_start` (③ baseline prime on first
post-restart capture) · `no_anchor` (⑤ regex: no schedulable anchor, slow path
covers it) · `throttled` (④ coalesce / min-interval / backoff) · `recognized`
(⑥ reached the LLM). `recognize_rate = recognized / total` is the headline gauge
(≈0 = the gates eat ~everything, then `by_outcome` says which); `whiteburn_rate =
recognized-but-persisted-0 / recognized` mirrors `recognition_ticks`.

**Query parameters:**
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `since` | string | (all) | ISO8601 lower bound (inclusive) |
| `until` | string | (all) | ISO8601 upper bound (exclusive) |

**Response:**
```json
{
  "success": true,
  "data": {
    "total": 1840,
    "by_outcome": {
      "non_user": 1200, "no_parser": 400, "not_conversation": 0,
      "empty": 10, "not_allowed": 0, "no_unseen": 210,
      "cold_start": 6, "no_anchor": 10, "throttled": 2, "recognized": 2
    },
    "by_bundle": {
      "com.electron.lark": {"no_unseen": 210, "no_anchor": 10, "recognized": 2, "...": 0}
    },
    "recognized": 2,
    "persisted_total": 2,
    "recognize_rate": 0.0011,
    "whiteburn_rate": 0.0,
    "since": null,
    "until": null
  }
}
```

---

### Reference

#### `GET /schema`

Return the memory organization spec.

**Response:**
```json
{
  "success": true,
  "data": {
    "schema": "# Memory Schema\n\n..."
  }
}
```

#### `GET /config`

Return the resolved runtime configuration.

**Response:**
```json
{
  "success": true,
  "data": {
    "models": {
      "default": { "model": "gpt-5.4-nano", "base_url": "", "api_key": "", ... }
    },
    "capture": { "event_driven": true, ... },
    "timeline": { "window_minutes": 1, ... },
    ...
  }
}
```

#### `GET /config/debug-hud`

Return the debug HUD's content allowlist (`[debug_hud] show` in `config.toml`).
Re-reads `config.toml` fresh on every call, so edits apply without a daemon
restart — the HUD polls this and re-renders. Keys: `intent` / `tool_call` /
`thinking` / `stage` (AGENT ACTIVITY event kinds), `health`, `memory`.

**Response:**
```json
{
  "success": true,
  "data": { "show": ["intent"] }
}
```

#### `PUT /config/debug-hud`

Persist the debug HUD allowlist. Backs the in-HUD **gear menu** so users pick
what the panel shows with clicks — no hand-editing `config.toml`. Writes a
targeted, formatting-preserving edit of `[debug_hud] show` (the rest of the
file is left untouched) and filters to known keys.

**Body:**
```json
{ "show": ["intent", "tool_call", "health"] }
```

**Response:** the persisted (filtered) list.
```json
{ "success": true, "data": { "show": ["intent", "tool_call", "health"] } }
```

---

### Control

#### `POST /daemon/pause`

Pause capture. The daemon stays up but skips captures.

**Response:**
```json
{
  "success": true,
  "data": { "capture": "paused" }
}
```

#### `POST /daemon/resume`

Resume capture.

**Response:**
```json
{
  "success": true,
  "data": { "capture": "active" }
}
```

#### `POST /daemon/capture-once`

Perform one immediate capture.

**Response:**
```json
{
  "success": true,
  "data": { "path": "/Users/tester/.persome/capture-buffer/2026-05-17T14-30-00p08-00.json" }
}
```

**Errors:**
- `500` — Capture failed

---

### Admin

#### `POST /indices/rebuild`

Rebuild the SQLite FTS index from Markdown files on disk.

**Response:**
```json
{
  "success": true,
  "data": { "files": 12, "entries": 156 }
}
```

#### `POST /indices/rebuild-captures`

Backfill `captures_fts` from `capture-buffer/*.json` on disk.

**Response:**
```json
{
  "success": true,
  "data": { "indexed": 42, "skipped": 0, "total": 42 }
}
```

**Errors:**
- `404` — Capture buffer directory not found or empty

---

### Dream

The dream stage runs daily at `dream.daily_tick_hour:dream.daily_tick_minute` (configurable in `config.toml`) and writes "slow-thinking" insights as `user-*.md` / `person-*.md` / `project-*.md` and skill workflows as `skills/skill-*.md` under `~/.persome/memory/`. Every run is recorded into the `dream_runs` + `dream_events` SQLite tables so the UI can show history and the live agent trace.

#### `POST /dream/run`

Manually trigger a dream stage execution. Enqueues via the run-dispatcher and **always returns 200** (there is no 409 path). If a still-queued dream row already exists, this call folds into it and returns its existing `run_id` with `deduped: true`, so the client can hint "already running" instead of pretending a fresh run started; a new enqueue returns `deduped: false`. Subscribe to [`/events/stream`](#get-eventsstream) or [`GET /runs/{run_id}`](#get-runsrun_id) to track live progress.

**Response:**
```json
{
  "success": true,
  "data": { "status": "queued", "run_id": 7, "deduped": false }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `queued` — the run was enqueued for the dispatcher. |
| `run_id` | int | The queued (or folded-into) run's id. |
| `deduped` | bool | `true` when this call folded into an already-queued dream; `false` for a fresh enqueue. |

#### `GET /dream/runs`

List recent dream runs, ordered by `started_at` descending.

**Query parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int (1–100) | `20` | Max rows to return |

**Response:**
```json
{
  "success": true,
  "data": {
    "runs": [
      {
        "id": 42,
        "started_at": "2026-05-26T23:30:00+08:00",
        "ended_at": "2026-05-26T23:33:14+08:00",
        "trigger": "daily-tick",
        "status": "committed",
        "summary": "Promoted skill-multi-agent-parallel-review to skill-candidate; appended 2 entries to user-preferences.",
        "written_count": 3,
        "iterations": 47,
        "error": "",
        "skipped_reason": "",
        "written_ids": ["20260526-2331-eaa166", "..."],
        "created_paths": ["user-preferences.md"]
      }
    ]
  }
}
```

`trigger` is `"manual"` (via `POST /dream/run`) or `"daily-tick"`. `status` is one of `running` / `committed` / `skipped` / `failed`. While `status == "running"`, `ended_at` is `null`.

#### `GET /dream/runs/{run_id}`

Get a single run's details plus its full agent event timeline.

**Response:**
```json
{
  "success": true,
  "data": {
    "run": { "id": 42, "...": "as above" },
    "events": [
      {
        "id": 1,
        "run_id": 42,
        "ts": "2026-05-26T23:30:01.123+08:00",
        "type": "llm_text",
        "payload": { "text": "Let me start with Phase 0...", "reasoning": "..." }
      },
      {
        "id": 2,
        "run_id": 42,
        "ts": "2026-05-26T23:30:02.234+08:00",
        "type": "tool_call",
        "payload": { "name": "read_memory", "arguments": { "path": "skill-foo.md" } }
      }
    ]
  }
}
```

Event `type` is one of `stage_start` / `tool_call` / `llm_text` / `stage_end`. Payload shape depends on the type.

**Errors:**
- `404` — `run_id` does not exist

---

### Book pages

Book pages are the human-readable form of the offline pipeline: once a day the dream run hangs a **book-page sub-step** off itself (after the dream commits) that reads the day's `event-*.md`, conservatively selects the worth-remembering episodes, and writes each as a second-person literary page into `~/.persome/memory/page-*.md`. Each page is a draft (`reviewed: false`) until the user accepts or dismisses it — both resolve to `reviewed: true` (only the draft banner clears; the page stays). A flat day produces zero pages, and any generation failure is swallowed so it never affects the dream run.

#### `GET /book/pages`

List book pages, newest `date` first.

**Query parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int (1–200) | `20` | Max pages to return |

**Response:**
```json
{
  "success": true,
  "data": {
    "items": [
      {
        "id": "page-2026-07-08",
        "title": "On an Unnecessary Phone Call",
        "date": "2026-07-08",
        "kind": "book_page",
        "is_draft": true,
        "source_refs": ["event:2026-07-08#3"]
      }
    ],
    "count": 1
  }
}
```

`id` is the file stem (`page-<date>`, with `-2`, `-3`… for multiple pages on the same day). `is_draft` is `true` until the page is reviewed.

#### `GET /book/pages/{page_id}`

Get one page's detail. `body` is a paragraph array (the prose split on blank lines).

**Response:**
```json
{
  "success": true,
  "data": {
    "id": "page-2026-07-08",
    "title": "On an Unnecessary Phone Call",
    "date": "2026-07-08",
    "is_draft": true,
    "body": ["You made a call today.", "It mattered more than you expected."]
  }
}
```

**Errors:**
- `404` — `page_id` does not exist

#### `PATCH /book/pages/{page_id}`

Mark a page as reviewed (clears the draft banner). The app's Review and ✕ actions both call this — the backend semantics are identical; only the front-end interaction differs.

**Request body:**
```json
{ "reviewed": true }
```

**Response:**
```json
{
  "success": true,
  "data": { "success": true, "id": "page-2026-07-08", "is_draft": false }
}
```

**Errors:**
- `400` — `reviewed` is not `true` (pages do not un-review)
- `404` — `page_id` does not exist

---

#### `POST /book/generate`

立即触发书页生成（app 内"立即生成"按钮）。同步运行与每日 Dream 同源的两步：`run_book_pages(today)` 选题并写页，`run_book_chapters()` 按 chat 历史重聚章节。两步各自容错，单步失败只记日志、退化为 0。请求无 body。

并发约束（#354）：本路由与每日 Dream 的 book 子步竞争同一存储，共用一把 book 生成锁串行化。若生成已在进行中，本接口**非阻塞**地返回 `409`（`detail: "book generation already in progress"`，与 `/dream/run` 风格一致），不把用户点击排到长任务后面。

**Response (200):**
```json
{
  "success": true,
  "data": { "pages": ["page-2026-07-08-1", "page-2026-07-08-2"], "chapters": 3 }
}
```

- `pages` — 本次写入的书页 id 列表（平淡的一天可能为空 `[]`）。
- `chapters` — 重聚后的章节数量。

`409 Conflict` — 已有一次 book 生成在进行中（手动触发与 Dream 子步互斥）。

---

### Dashboard

Home-dashboard read endpoints. Both derive **purely from real daemon state** — there is no fabricated content. When a real signal is absent, the field is omitted (sub-status lines) or returned empty (agenda items); the app renders an idle / empty state rather than placeholder data.

#### `GET /agent/now`

What the agent is doing **right now**, derived from live state. While a dream (slow-thinking consolidation) is running, this reports the running phase with a server-computed elapsed timer; otherwise it returns an idle snapshot built from the last finished run plus recent memory activity.

Data sources: `dream_runs` / `dream_events` tables, the latest `capture-buffer/*.json` capture, the paused-flag file, the live daemon pid, and the memory `entries` table (idle sub-status only).

**Response (running):**
```json
{
  "success": true,
  "data": {
    "title": "Dream 慢思考整理中（手动整理）",
    "status": "running",
    "started_at": "2026-06-04T23:30:00+08:00",
    "elapsed_seconds": 42,
    "capture": "active",
    "last_activity_ts": "2026-06-04T23:31:10+08:00",
    "sub_status": [
      { "text": "Reviewing today's activity", "ts": "2026-06-04T23:30:05+08:00" },
      { "text": "调用工具 read_memory", "ts": "2026-06-04T23:30:03+08:00" }
    ]
  }
}
```

**Response (idle):**
```json
{
  "success": true,
  "data": {
    "title": "上次整理：Promoted a skill and appended 2 preferences",
    "status": "idle",
    "started_at": null,
    "elapsed_seconds": null,
    "capture": "paused",
    "last_activity_ts": "2026-06-04T22:10:00+08:00",
    "sub_status": [
      { "text": "在 Feishu 与 Alice 讨论 roadmap", "ts": "2026-06-04T22:05:00+08:00" }
    ]
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | What the agent is doing / last did. From the running run's trigger, or the last run's `summary`, or a neutral placeholder. |
| `status` | string | `running` (a dream is in flight) or `idle`. |
| `started_at` | string \| null | Running: the dream's `started_at` (ISO8601) so the app can tick its own timer. Idle: `null`. |
| `elapsed_seconds` | int \| null | Running: seconds since `started_at`, computed once server-side. Idle: `null`. |
| `capture` | string | `active` / `paused` / `stopped` — from the paused-flag file + live daemon pid. |
| `last_activity_ts` | string \| null | Timestamp of the most recent screen capture, or `null` if none. |
| `sub_status` | array | 0–3 lines, each `{text, ts}`. Running: latest `tool_call` / `llm_text` dream events (newest first). Idle: most recent memory `entries`. A line is omitted when it has no real source. |

#### `GET /agenda`

Scheduled items for today / this week / this month, derived from the unified intent stream. Only intents carrying a temporal anchor (`payload.when_text` non-empty — typically `meeting` / `calendar` / `reminder` kinds, or any kind a scene pack tagged with a time) and recognized within the window are returned. Returns an empty list when none exist — meetings are never fabricated.

> Note: filtering is on the intent's **recognition** time `ts`, not a parsed absolute event datetime — intents do not carry a resolved event time (`when_text` is free-form natural language, used as the display label).

**Query parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `range` | string | `today` | `today` / `day` (a single calendar day, today 00:00–24:00; `day` is identical to `today` but echoed back as `day`), `week` (this week, Monday–Sunday), or `month` (this month, 1st 00:00 to the first day of next month). Any other value falls back to `today`. |

**Response:**
```json
{
  "success": true,
  "data": {
    "range": "today",
    "items": [
      {
        "time_label": "今天下午 3 点",
        "title": "meeting about the roadmap",
        "kind": "meeting",
        "ts": "2026-06-04T09:12:00+08:00",
        "source": "intent",
        "with": ["Alice"]
      }
    ],
    "count": 1
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `range` | string | Echoed effective range (`today` / `day` / `week` / `month`). |
| `items` | array | Scheduled items, newest recognition first. Empty when no anchored intent is in-window. |
| `items[].time_label` | string | Natural-language time from `payload.when_text`. |
| `items[].title` | string | From the intent's `rationale` (or `kind` if empty), truncated to 160 chars. |
| `items[].kind` | string | Intent kind, e.g. `meeting` / `calendar` / `reminder`. |
| `items[].ts` | string | ISO8601 recognition time of the intent. |
| `items[].source` | string | Provenance tag, currently `intent`. |
| `items[].with` | array | Related people, from `payload.with`. |
| `count` | int | Length of `items`. |

---

#### `GET /runs`

Agent-run cards for the Calendar work board, within a day / week / month window. Each card is backed by a **real run row** — the canonical `agent_runs` ledger UNIONed with legacy `dream_runs` history mapped into the same shape. There is no "card without a run": an empty window returns an empty list and the app renders an honest empty state; nothing is fabricated. `progress` is real-or-null (null → indeterminate, never a made-up percentage).

> Placement anchor is `started_at` if set, else `enqueued_at` — a real ISO timestamp, not a parsed natural-language label. This is what lets Day/Week/Month place cards on the real date (unlike `/agenda`, whose intent items carry only free-form `when_text`).

**Query parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `range` | string | `day` | `day` (today 00:00–24:00), `week` (this week, Monday–Sunday) or `month` (this month). Any other value falls back to `day`. Ignored when both `start` and `end` are given. |
| `status` | string | — | Optional comma-separated status filter, e.g. `queued,running`. Omit for all statuses. |
| `start` | string | — | Explicit window start ISO8601 (inclusive). Must be given together with `end`; when both are present they define the window (for calendar paging) and `range` is ignored. Naive timestamps are treated as local. |
| `end` | string | — | Explicit window end ISO8601 (exclusive). Pairs with `start`. Bad ISO → 422. |

**Response:**
```json
{
  "success": true,
  "data": {
    "range": "day",
    "items": [
      {
        "id": 12,
        "source": "agent_run",
        "kind": "bootstrap",
        "title": "冷启动画像",
        "status": "running",
        "trigger": "user",
        "enqueued_at": "2026-06-07T09:00:00+08:00",
        "started_at": "2026-06-07T09:00:05+08:00",
        "ended_at": null,
        "progress": 0.5,
        "progress_label": "阶段 2/4",
        "summary": ""
      }
    ],
    "count": 1
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `range` | string | Echoed effective range (`day` / `week` / `month`, or `custom` when an explicit `start`/`end` window was used). |
| `items` | array | Run cards whose anchor time falls in-window, newest anchor first. Empty when no run is in-window. |
| `items[].id` | int | Row id within its source table. |
| `items[].source` | string | Provenance: `agent_run` (canonical ledger) or `dream` (legacy `dream_runs`). |
| `items[].kind` | string | Run type, e.g. `dream` / `bootstrap`. |
| `items[].title` | string | Human label for the run. |
| `items[].status` | string | `queued` / `running` / `committed` / `skipped` / `failed` / `cancelled`. |
| `items[].trigger` | string | Dispatch origin: `manual` / `daily-tick` / `user` / `chat`. |
| `items[].enqueued_at` | string | ISO8601 enqueue time (queue-card anchor). |
| `items[].started_at` | string \| null | ISO8601 start time; null while still `queued`. |
| `items[].ended_at` | string \| null | ISO8601 end time; null while not finished. |
| `items[].progress` | float \| null | 0..1 real progress; null = indeterminate (never fabricated). |
| `items[].progress_label` | string | Real sub-step label, e.g. `阶段 2/4`. |
| `items[].summary` | string | Result summary. |
| `count` | int | Length of `items`. |

#### `POST /runs`

Enqueue a new agent run. If a queued row of the same `kind` already exists, the existing id is returned (dedup — prevents double-clicks from burning duplicate LLM runs). Returns immediately; the run-dispatcher picks it up in the background.

**Request body (JSON):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `kind` | string | yes | `dream` or `bootstrap` (closed allow-list; other values → 422). |
| `title` | string | no | Human label override; defaults to the kind's registry title. |
| `payload` | object | no | Extra executor parameters (e.g. `{"deep": true}` for bootstrap). |

**Response:**
```json
{"success": true, "data": {"run_id": 7, "status": "queued"}}
```

#### `PATCH /runs/{run_id}`

Update a run's state. Currently supports `action=cancel` only.

- Returns 404 if the run does not exist.
- Returns 409 if the run is in a non-cancellable state (`running` or terminal).

**Request body (JSON):**

| Field | Type | Description |
|-------|------|-------------|
| `action` | string | Must be `cancel`. |

**Response:**
```json
{"success": true, "data": {"run_id": 7, "status": "cancelled"}}
```

#### `GET /runs/{run_id}`

Return the detail of one agent run, including its full event tape (stage/progress events).

**Response:**
```json
{
  "success": true,
  "data": {
    "id": 7,
    "kind": "dream",
    "title": "每日整理",
    "status": "committed",
    "trigger": "user",
    "dispatch_source": "api",
    "enqueued_at": "2026-06-07T09:00:00+08:00",
    "started_at": "2026-06-07T09:00:05+08:00",
    "ended_at": "2026-06-07T09:03:12+08:00",
    "progress": null,
    "progress_label": "",
    "summary": "写入 3 条记忆",
    "error": "",
    "events": [
      {"id": 1, "ts": "2026-06-07T09:00:05+08:00", "type": "stage_start", "payload": {}},
      {"id": 2, "ts": "2026-06-07T09:01:00+08:00", "type": "progress", "payload": {"value": 0.5, "label": "阶段 2/4"}},
      {"id": 3, "ts": "2026-06-07T09:03:12+08:00", "type": "stage_end", "payload": {"status": "committed"}}
    ]
  }
}
```

Returns 404 if the run does not exist.

---

### Bootstrap

Day-0 cold-start profiling. A harness-orchestrated flow reads only the onboarding-scoped folders (Desktop / Documents / Downloads), runs parallel explorer sub-agents over them, then synthesizes **two outputs from one pass**: (a) a literary one-page personality/vibe profile that rides the `stage_end` SSE frame to the onboarding UI **only** — it is never written to memory; and (b) **atomic facts** written into `~/.persome/memory/` (`#bootstrap`-tagged entries) — one assertion per entry across `user-profile.md` / `user-preferences.md` / `project-*` / `tool-*` / `topic-*`, the same shape the steady-state classifier produces (so a later real observation can `supersede` a day-0 guess, and the schema miner can cluster them into predictive priors). Re-runnable/idempotent: each run first retires the prior run's live `#bootstrap` entries (markdown-durable strike), so `persome bootstrap` migrates rather than duplicates. Meant to run once right after install, when no capture history exists yet.

#### `POST /bootstrap/run`

Manually trigger a cold-start run inside the daemon. Enqueues via the run-dispatcher and **always returns 200** (no 409 path); the agent runs on a daemon background thread. Subscribe to [`/events/stream`](#get-eventsstream) to see live `bootstrap` frames as the agent works (the app's debug HUD + onboarding UI render these): `stage_start` / `tool_call` / `stage_end`, plus the onboarding-facing `scan_tree` (whole-home file tree), `clue` (`{path, kind, tag, title, detail}` detected high-value folders), `read` (per-file deep reads), `hypothesis` (`{phrase}` evolving one-line read), and `synth_start` (final synthesis began).

**Query parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `shallow` | bool | `false` | Explore directory structure only; never read file contents |
| `exclude` | string | `""` | Comma-separated top-level home folder names to skip (e.g. `Desktop,Downloads`) — the folders the user un-checked on the onboarding permission screen; never scanned, named, or read |

**Response:**
```json
{
  "success": true,
  "data": { "status": "queued", "run_id": 8, "deduped": false }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `queued`. |
| `run_id` | int | The queued (or folded-into) run's id. |
| `deduped` | bool | `true` when this call folded into an already-queued bootstrap with the **same** selection; `false` for a fresh enqueue. |

Dedup is **payload-aware**: a still-queued bootstrap only folds when its `shallow` + `exclude` selection is identical. If the user changes the selection and re-triggers before the run is claimed, a **new** row is enqueued (`deduped: false`) so the latest choice isn't silently dropped.

#### `POST /bootstrap/access`

Probe whether the daemon can read the three onboarding-scoped TCC folders (Desktop / Documents / Downloads). Used by the app's onboarding permission pre-flight: calling it from the daemon triggers the macOS Files-and-Folders permission prompt on first access (attributed to the daemon process). Reads nothing — only attempts a directory listing per folder.

**Response:**
```json
{
  "success": true,
  "data": {
    "folders": [
      { "name": "Desktop",   "path": "/Users/<you>/Desktop",   "granted": true },
      { "name": "Documents", "path": "/Users/<you>/Documents", "granted": false },
      { "name": "Downloads", "path": "/Users/<you>/Downloads", "granted": true }
    ],
    "all_granted": false
  }
}
```

`granted` is `true` when the folder could be listed; `all_granted` is `true` only when all three are readable.

---

### Events

#### `GET /events/stream`

Server-Sent Events (SSE) stream of live pipeline activity. Each frame is a `data: <json>\n\n` line carrying at minimum `{ "stage": "...", "type": "..." }`.

**Content-Type:** `text/event-stream`

**Event stages and types:**

| Stage | Types | Payload |
|-------|-------|---------|
| `dream` | `stage_start` | `{ run_id, trigger }` |
| `dream` | `tool_call` | `{ run_id, name, arguments }` |
| `dream` | `llm_text` | `{ run_id, text, reasoning }` |
| `dream` | `stage_end` | `{ run_id, status, summary, written, iterations, skipped_reason }` |
| `classifier` / `pattern_detector` / `active` | various | (see source) |

To follow only the dream agent, filter frames where `stage == "dream"` on the client side; the channel is multiplexed across all pipeline stages.

The connection stays open until the client disconnects. The daemon does not currently emit keep-alive comments, so reverse proxies with idle-timeouts may drop the connection — clients should reconnect on read errors.

---

## cURL examples

```bash
# Health check
curl http://127.0.0.1:8742/health

# Status
curl http://127.0.0.1:8742/status

# List memories
curl http://127.0.0.1:8742/memories

# Read a memory file
curl "http://127.0.0.1:8742/memories/user-profile.md"

# Search
curl "http://127.0.0.1:8742/search?query=interview&top_k=3"

# Current context
curl http://127.0.0.1:8742/captures/current

# Search captures
curl "http://127.0.0.1:8742/captures?query=error&app_name=Cursor"

# List pending actions
curl http://127.0.0.1:8742/actions

# Mark action done
curl -X PATCH http://127.0.0.1:8742/actions/1 \
  -H "Content-Type: application/json" \
  -d '{"action":"done","note":"Confirmed"}'

# List recognized intents (newest first)
curl "http://127.0.0.1:8742/intents?status=open&limit=20"

# Set an intent's status (R3 feedback)
curl -X PATCH http://127.0.0.1:8742/intents/7 \
  -H "Content-Type: application/json" \
  -d '{"status":"dismissed"}'

# Intent-recognition hit-rate telemetry
curl "http://127.0.0.1:8742/intents/stats"

# Pause capture
curl -X POST http://127.0.0.1:8742/daemon/pause

# Capture once
curl -X POST http://127.0.0.1:8742/daemon/capture-once

# Rebuild index
curl -X POST http://127.0.0.1:8742/indices/rebuild

# Trigger a manual dream run (returns immediately)
curl -X POST http://127.0.0.1:8742/dream/run

# List recent dream runs
curl http://127.0.0.1:8742/dream/runs

# Inspect a single run with its agent event timeline
curl http://127.0.0.1:8742/dream/runs/1

# Trigger a day-0 cold-start profiling run (returns immediately; watch /events/stream)
curl -X POST http://127.0.0.1:8742/bootstrap/run
curl -X POST 'http://127.0.0.1:8742/bootstrap/run?shallow=true'   # structure only

# Subscribe to live agent events (Ctrl-C to stop)
curl -N http://127.0.0.1:8742/events/stream
```

---

## MCP → REST mapping

| MCP Tool | REST Endpoint | Method |
|----------|--------------|--------|
| `list_memories()` | `/memories` | GET |
| `read_memory(path)` | `/memories/{path}` | GET |
| `search(query)` | `/search` | GET |
| `recent_activity()` | `/activity` | GET |
| `current_context()` | `/captures/current` | GET |
| `search_captures(query)` | `/captures` | GET |
| `read_recent_capture(at)` | `/captures/recent` | GET |
| `list_pending_actions()` | `/actions` | GET |
| `mark_action_done(id)` | `/actions/{id}` | PATCH |
| `list_intents()` | `/intents` | GET |
| `intent_recognition_stats()` | `/intents/stats` | GET |
| `set_intent_status(id, status)` | `/intents/{id}` | PATCH |
| `parser_stats()` | `/parser/stats` | GET |
| `fast_path_stats()` | `/intents/fast-path/stats` | GET |
| `recall_budget_stats()` | — (MCP only, no REST mirror yet) | — |
| `get_schema()` | `/schema` | GET |

| Chat Operation | REST Endpoint | Method |
|----------------|---------------|--------|
| Create session | `/chat/sessions` | POST |
| List sessions | `/chat/sessions` | GET |
| Get session | `/chat/sessions/{id}` | GET |
| Get folded messages | `/chat/sessions/{id}/messages` | GET |
| Send message | `/chat/sessions/{id}/messages` | POST |
| Archive session | `/chat/sessions/{id}` | DELETE |

| CLI Command | REST Endpoint | Method |
|-------------|--------------|--------|
| `status` | `/status` | GET |
| `config` | `/config` | GET |
| `pause` | `/daemon/pause` | POST |
| `resume` | `/daemon/resume` | POST |
| `capture-once` | `/daemon/capture-once` | POST |
| `rebuild-index` | `/indices/rebuild` | POST |
| `rebuild-captures-index` | `/indices/rebuild-captures` | POST |

| Dream / Events Surface | REST Endpoint | Method |
|------------------------|---------------|--------|
| Trigger manual dream | `/dream/run` | POST |
| List dream runs | `/dream/runs` | GET |
| Get single run + events | `/dream/runs/{run_id}` | GET |
| Trigger cold-start profiling | `/bootstrap/run` | POST |
| Probe onboarding folder access | `/bootstrap/access` | POST |
| Live agent stream | `/events/stream` | GET (SSE) |

### Book

| Book Surface | REST Endpoint | Method |
|--------------|---------------|--------|
| List book pages | `/book/pages` | GET |
| Get single page | `/book/pages/{page_id}` | GET |
| Review (clear draft) | `/book/pages/{page_id}` | PATCH |
| List highlights | `/book/highlights` | GET |
| Create highlight | `/book/highlights` | POST |
| Delete highlight | `/book/highlights/{highlight_id}` | DELETE |
| List chapters | `/book/chapters` | GET |
| Rename chapter | `/book/chapters/{chapter_id}` | PATCH |
| Generate now | `/book/generate` | POST |

---

## Chat Sessions

Stateful multi-turn chat sessions backed by the same tool-capable LLM loop used by `persome chat`. Sessions persist to disk under `chat-history/api-{id}.json` and are fully isolated from the CLI's `active.json`.

### `POST /chat/sessions`

Create a new chat session.

**Response:**
```json
{
  "success": true,
  "data": {
    "session": {
      "id": "a1b2c3d4",
      "created_at": "2026-05-17T14:30:00+08:00",
      "updated_at": "2026-05-17T14:30:00+08:00",
      "turn_count": 0,
      "title": null,
      "preview": null
    }
  }
}
```

`title` is `null` on fresh sessions; the server fills it after the first
assistant reply (see [Session title generation](#session-title-generation)
below). `preview` is the first user message truncated to ≤80 chars; UI
falls back to it while `title` is still being generated.

### `GET /chat/sessions`

List all API chat sessions (active and dormant).

**Response:**
```json
{
  "success": true,
  "data": {
    "count": 3,
    "sessions": [
      {
        "id": "a1b2c3d4",
        "created_at": "2026-05-17T14:30:00+08:00",
        "updated_at": "2026-05-17T14:35:00+08:00",
        "turn_count": 2,
        "title": "部署指南",
        "preview": "how do I deploy this service?"
      }
    ]
  }
}
```

### `GET /chat/sessions/{id}`

Get session metadata and full message history.

**Response:**
```json
{
  "success": true,
  "data": {
    "session": {
      "id": "a1b2c3d4",
      "created_at": "2026-05-17T14:30:00+08:00",
      "updated_at": "2026-05-17T14:35:00+08:00",
      "turn_count": 2,
      "title": "部署指南",
      "preview": "how do I deploy this service?"
    },
    "messages": [
      {"role": "system", "content": "..."},
      {"role": "user", "content": "帮我总结今天的活动"},
      {"role": "assistant", "content": "根据今天的 timeline..."}
    ]
  }
}
```

#### Session title generation

`title` is an LLM-generated short label (≤24 chars, in the user's
language) used as the sidebar entry. It is produced once per session,
inline at the end of the first `POST /chat/sessions/{id}/messages` call
that returns a non-error assistant reply:

1. Right after the first assistant turn is persisted, the server calls
   the **same Anthropic SDK path the chat agent uses**
   (`chat.agent.complete_sync` → `[chat] model` → `ANTHROPIC_API_KEY` /
   `ANTHROPIC_BASE_URL`) with the first user message + first assistant
   reply. This means any user who can chat at all has a working title
   generator without configuring a separate `[models.*]` stage — title goes
   through the same wire as their chat replies do.
2. The call is bounded to 8s; on timeout or failure the server logs a
   warning and leaves `title=null` (UI continues falling back to
   `preview` / `Chat MM/DD HH:MM`).
3. On success the title is written to `session.title` and persisted to
   the session JSON; subsequent turns do not regenerate it.

### `GET /chat/sessions/{id}/messages`

Get the **folded** message history of a session — a presentation projection
of the underlying agent-loop trace.

This endpoint differs from `GET /chat/sessions/{id}` in two ways:

1. **Agent-loop iterations fold into one turn.** A single user prompt that
   triggered K tool calls is persisted on disk as K+1 assistant rows
   (each carrying a `text` + `tool_use` block) interleaved with K
   tool_result-bearing user rows. This endpoint collapses every span of
   such non-real-user rows into ONE assistant message. Clients render
   one chat bubble per user prompt.
2. **`blocks` preserves the original chronological interleave.** The
   folded assistant message carries a `blocks` array reproducing the
   exact order in which the agent emitted text, tool calls, and tool
   results. Clients walk `blocks` to render `text → tool_call →
   tool_result → text` exactly as it streamed. The flat `content`
   string remains as a fallback for older clients (it is the join of
   every text block).

`system` messages are filtered out. tool_result-carrying user messages are
absorbed into the assistant turn whose `tool_use` produced them.

**Response:**
```json
{
  "success": true,
  "data": [
    {
      "role": "user",
      "content": "帮我总结今天的活动",
      "blocks": [
        {"type": "text", "text": "帮我总结今天的活动"}
      ]
    },
    {
      "role": "assistant",
      "content": "好的，让我先看看。今天你 ...",
      "blocks": [
        {"type": "text", "text": "好的，让我先看看。"},
        {"type": "tool_use", "name": "recent_activity", "input": {"limit": 20}},
        {"type": "tool_result", "name": "recent_activity", "content": "[...]", "tool_use_id": "toolu_01abc"},
        {"type": "text", "text": "今天你 ..."}
      ]
    }
  ]
}
```

Each block carries only the fields relevant to its `type` (`tool_use_id` /
`content` are absent on `tool_use`, etc.) thanks to `exclude_none`.

### `POST /chat/sessions/{id}/messages`

Send a user message and receive the assistant's response.

**Request body:**
```json
{"content": "帮我总结今天的活动"}
```

**Response:**
```json
{
  "success": true,
  "data": {
    "message": {
      "role": "assistant",
      "content": "根据今天的 activity 记录..."
    },
    "tool_calls_executed": [
      {"name": "recent_activity", "arguments": {"limit": 20}}
    ],
    "usage": {"prompt_tokens": 1200, "completion_tokens": 150},
    "did_compress": false,
    "did_microcompact": false,
    "reasoning": null
  }
}
```

If the assistant used tools, `tool_calls_executed` lists each tool call. `did_compress` indicates predictive context compression was triggered. `did_microcompact` indicates old tool results were cleared due to inactivity.

The response is an SSE stream (`text/event-stream`); each frame is a `data: <json>\n\n` line and the stream ends with `data: [DONE]\n\n`. Frame types:

| `type` | Fields | Meaning |
|---|---|---|
| `reply` | `content` | Incremental assistant token |
| `reasoning` | `content` | Incremental reasoning token |
| `tool_call` | `name`, `arguments` | Tool call started |
| `tool_result` | `name`, `content` | Tool call result |
| `error` | `message` | Turn failed |
| `done` | `ttft_ms` | Turn completed normally; `ttft_ms` is the time-to-first-token in milliseconds (`null` when no token streamed) |

### `DELETE /chat/sessions/{id}`

Archive a session. The session is removed from memory; the disk file is retained.

**Response:**
```json
{
  "success": true,
  "data": {
    "success": true,
    "session_id": "a1b2c3d4",
    "archived": true
  }
}
```

---

## cURL examples (Chat)

```bash
# Create a session
curl -X POST http://127.0.0.1:8742/chat/sessions

# List sessions
curl http://127.0.0.1:8742/chat/sessions

# Send a message
curl -X POST http://127.0.0.1:8742/chat/sessions/a1b2c3d4/messages \
  -H "Content-Type: application/json" \
  -d '{"content":"帮我总结今天的活动"}'

# Get session history
curl http://127.0.0.1:8742/chat/sessions/a1b2c3d4

# Archive session
curl -X DELETE http://127.0.0.1:8742/chat/sessions/a1b2c3d4
```

---

## Meeting assistant

Real-time meeting assistant with ASR transcription, LLM analysis, and tool-augmented push notifications.

### `POST /meeting/start`

Start the meeting assistant (audio capture + ASR + LLM analysis).

```bash
# Default microphone
curl -X POST http://127.0.0.1:8742/meeting/start

# Specify mic device and app
curl -X POST http://127.0.0.1:8742/meeting/start \
  -H "Content-Type: application/json" \
  -d '{"mic": 0, "app": "us.zoom.xos"}'
```

### `POST /meeting/stop`

Stop the meeting assistant.

```bash
curl -X POST http://127.0.0.1:8742/meeting/stop
```

### `GET /meeting/status`

Check whether the meeting assistant is running.

```bash
curl http://127.0.0.1:8742/meeting/status
# {"status": "running"} or {"status": "stopped"}
```

### `GET /meeting/events`

SSE stream of real-time meeting events. Event types:

| type | description |
|------|-------------|
| `会议` | Meeting participant transcript |
| `用户` | User transcript |
| `push` | AI analysis result (non-streaming) |
| `push_chunk` | Streaming AI output chunk |
| `push_end` | End of streaming output |
| `error` | Error message |
| `system` | System event (e.g. meeting stopped) |

```bash
curl -N http://127.0.0.1:8742/meeting/events
```

---

## Book

Book is Persome 的"书"页面。Phase 2.1 提供手动划词存的 **highlights**（用户在书页/会话里挑出的引文），纯 CRUD、无 LLM。存储为 `index.db` 的 `highlights` 表（DAO：`store/highlights.py`），路由独立于 `book_highlights_routes.py`。

### `GET /book/highlights`

按创建时间倒序返回 highlights。

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | int | `20` | Max highlights (1–200) |

`data` 形如 `{"items": [{"id", "quote", "time_label", "source_ref"}], "count"}`，其中 `time_label` 由后端从 `created_at` 派生为 `MON D · HH:MM`（如 `JUL 8 · 19:02`）。

```bash
curl http://127.0.0.1:8742/book/highlights?limit=20
```

### `POST /book/highlights`

新建一条 highlight，返回持久化后的行（`data` 为单个 highlight 对象）。`quote` 不能为空（仅空白会被 422 拒绝）；`source_ref` 为来源页 id 或 chat session id，可空。

```bash
curl -X POST http://127.0.0.1:8742/book/highlights \
  -H "Content-Type: application/json" \
  -d '{"quote": "The call only lasted eleven minutes", "source_ref": "page:7"}'
```

### `DELETE /book/highlights/{highlight_id}`

按 id 删除一条 highlight。`data` 为 `{"deleted": <id>}`；id 不存在时返回 404。

```bash
curl -X DELETE http://127.0.0.1:8742/book/highlights/7
```

### Book chapters

Phase 2.2 把 Book → Sessions 列表里写死的章节标题换成对真实 chat session 主题聚类生成的**文学章节**。每日 dream run 在 book-page 子步之后再挂一个 **book-chapters 子步**（`writer/book_chapters.py::run_book_chapters`）：读近期非归档 chat session（`chat-history/api-*.json`）→ `prompts/book_chapters.md` 聚成 0–N 主题章节（文学标题 + 副标题 + 归属 session_ids，绝不臆造关联）→ `store.book_chapters.replace_generated`（覆盖未编辑的、保留用户改过的）。存储为 `index.db` 的 `book_chapters` 表（DAO：`store/book_chapters.py`），路由独立于 `book_chapters_routes.py`。任何生成失败都被吞掉，绝不影响 dream run。

#### `GET /book/chapters`

按创建时间倒序返回所有章节。`data` 形如 `{"items": [{"id", "title", "subtitle", "from_count", "session_ids", "edited"}], "count"}`。`id` 是用于 PATCH 改名的稳定后端 id；`title` 同时是前端选中键（Sessions reader 按 title 匹配章节）；`from_count` 为归属 session 数。空时返回空列表，前端回退占位章节。

```bash
curl http://127.0.0.1:8742/book/chapters
```

#### `PATCH /book/chapters/{chapter_id}`

重命名一个章节，body `{"title": "..."}`。后端标记该行 `edited`，使后续每日重生不再覆盖用户的标题。`data` 为 `{"id", "title", "edited": true}`；`title` 仅空白会被 422 拒绝；id 不存在时返回 404。

```bash
curl -X PATCH http://127.0.0.1:8742/book/chapters/3 \
  -H "Content-Type: application/json" \
  -d '{"title": "On changing direction"}'
```
