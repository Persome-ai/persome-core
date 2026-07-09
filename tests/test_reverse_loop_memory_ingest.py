"""Reverse-loop P3 / G1 (spec 2026-06-26 §3.1.3) — the ONLY content-bearing
reverse channel: ``POST /memory/ingest`` → a desensitized ``task-outcome-*.md``
memory entry.

Pins the load-bearing privacy + invariant contracts:
  * the deterministic scrubber flags secrets/PII (api key / email / phone / password / home path);
  * ingest writes a searchable ``task-outcome-*`` entry, idempotent by ``task_id``;
  * **宁缺毋滥**: a summary carrying any PII is DROPPED, not stored;
  * the entry is **evo_nodes-exempt** (Q2) — it never enters the entity chain;
  * the endpoint is **OFF by default** (kill-switch) and 422s any extra field
    (the content channel still pins its field set at the boundary).

Deterministic, zero-LLM, zero-network.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from persome import paths
from persome.api import build_api_app
from persome.config import load as load_config
from persome.memory import task_outcome as t
from persome.privacy import scrub
from persome.store import fts


# ── 1. the deterministic scrubber ────────────────────────────────────────────
def test_scrub_clean_passes():
    assert scrub.is_clean("生成 Python 脚本统计 workspace 文件数，写入 count.txt")


def test_scrub_flags_secrets_and_pii():
    assert "api_key" in scrub.scan("用 sk-abcdefghij1234567890XYZ 调用").hits
    assert "email" in scrub.scan("发给 boss@example.com").hits
    assert "phone" in scrub.scan("打 13800138000").hits
    assert "password" in scrub.scan("password: hunter2xyz").hits
    assert "home_path" in scrub.scan("写到 /Users/tester/secret/x").hits
    # a genuine opaque blob (letter+digit mix, no separators) is still a hit
    assert "high_entropy" in scrub.scan("blob AbCd1234EfGh5678IjKl9012MnOp3456").hits
    # an international phone (leading +) still fires
    assert "phone" in scrub.scan("call +1 415 555 0123 now").hits


# ── 1b. #389 false-positive guard: ordinary secret-free dev/office text is clean ─
def test_scrub_does_not_flag_ordinary_dev_and_office_text():
    """The high_entropy/phone catch-alls must NOT drop plain content.

    Regression for #389: length/shape-only matching swallowed long identifiers,
    package names, meeting times, and order numbers — systematically dropping the
    G1 content channel's legitimate task-outcome cards. Each of these must scan
    clean (no ``high_entropy``, no ``phone``).
    """
    clean = [
        # long camelCase / PascalCase method names (pure-alpha, no digit)
        "Refactored useAuthenticationProviderContextManager into a hook",
        "Set up the new GithubActionsWorkflowDeploymentPipeline config",
        # kebab / snake package names (word separators break the run)
        "Bumped typescript-eslint-plugin-recommended-config to latest",
        "Wired up the data_pipeline_orchestration_service_module today",
        # meeting time + order number (space/dash grouped digits, no leading +)
        "会议时间 2026-06-30 14:00 到 15:30 已确认",
        "处理订单 1234 5678 9012 完成",
    ]
    for text in clean:
        res = scrub.scan(text)
        assert res.clean, f"unexpected {res.hits} on secret-free text: {text!r}"


# ── 1c. #398 false-negative guard: BARE home dir must still leak-block ────────
def test_scrub_flags_bare_home_path():
    """A home path ending at the username (no trailing ``/``) leaks the OS account
    name just the same, so it must fire ``home_path``.

    Regression for #398: the pattern required a trailing ``/``, so ``/Users/alice``
    at path end, before a space, or before a newline slipped both mirrored gates
    (daemon scan + app OutcomeDistiller) into durable memory. The username segment
    is now non-empty and does NOT require a following slash.
    """
    for text in (
        "saved to /Users/alice",  # username at line end
        "cwd: /home/bob",  # linux bare home
        "ls /Users/alice ",  # username followed by a space
        "done, output under /Users/alice\n后续再看",  # followed by a newline
    ):
        assert "home_path" in scrub.scan(text).hits, f"missed bare home path in {text!r}"
    # the trailing-slash form (with a following path) still fires, unchanged
    assert "home_path" in scrub.scan("写到 /Users/tester/secret/x").hits


# ── 2. ingest round-trip + idempotency + searchable ──────────────────────────
def test_ingest_round_trip_and_searchable(ac_root):
    with fts.cursor() as conn:
        r = t.ingest_task_outcome(
            conn,
            task_id="T1",
            kind="assignment",
            title="写统计脚本",
            summary="生成 Python 脚本统计 workspace 文件数，写入 count.txt",
            intent_id=501,
        )
        assert r.status == "ingested" and r.entry_id
        files = [
            row[0]
            for row in conn.execute(
                "SELECT path FROM files WHERE path LIKE 'task-outcome-%'"
            ).fetchall()
        ]
    assert files and files[0].startswith("task-outcome-")
    # the distilled content landed in the markdown SSOT (so it is FTS-projected/searchable)
    text = paths.memory_dir().joinpath(files[0]).read_text(encoding="utf-8")
    assert "统计 workspace" in text and "写统计脚本" in text


def test_ingest_idempotent_by_task_id(ac_root):
    with fts.cursor() as conn:
        a = t.ingest_task_outcome(conn, task_id="T1", kind="x", title="a", summary="第一次")
        b = t.ingest_task_outcome(conn, task_id="T1", kind="x", title="b", summary="重发")
        n = conn.execute("SELECT COUNT(*) FROM task_outcome_ingests WHERE task_id='T1'").fetchone()[
            0
        ]
    assert a.status == "ingested" and b.status == "duplicate" and n == 1


# ── 3. PII drop (宁缺毋滥) ─────────────────────────────────────────────────────
def test_ingest_drops_pii_summary(ac_root):
    with fts.cursor() as conn:
        r = t.ingest_task_outcome(
            conn,
            task_id="T2",
            kind="meeting",
            title="建会议",
            summary="密钥 sk-leak1234567890abcdefghij 不该进记忆",
        )
        # nothing written to memory; the drop is recorded (idempotent) so a resend won't retry
        files = conn.execute(
            "SELECT COUNT(*) FROM files WHERE path LIKE 'task-outcome-%'"
        ).fetchone()[0]
        rec = conn.execute("SELECT status FROM task_outcome_ingests WHERE task_id='T2'").fetchone()
    assert r.status == "dropped_pii" and "api_key" in r.dropped_categories
    assert files == 0 and rec[0] == "dropped_pii"


# ── 4. evo_nodes exemption (Q2) — never enters the entity chain ───────────────
def test_task_outcome_is_evo_exempt(ac_root):
    from persome.evomem import inversion

    assert inversion.routes_to_engine("task-outcome-2026-06-30.md") is False
    with fts.cursor() as conn:
        t.ingest_task_outcome(
            conn, task_id="T3", kind="assignment", title="x", summary="干净的产物摘要"
        )
        # evo_nodes table exists in the ac_root schema; the task-outcome entry must NOT be in it
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM evo_nodes WHERE file_name LIKE 'task-outcome-%'"
            ).fetchone()[0]
        except Exception:
            n = 0  # table absent on this build → trivially exempt
    assert n == 0


# ── 5. the endpoint: OFF by default (kill-switch) + content-free boundary ─────
def _client(monkeypatch, *, enabled: bool):
    cfg = load_config()
    cfg.memory_ingest_enabled = enabled
    import persome.api.routes as routes_mod

    monkeypatch.setattr(routes_mod, "load_config", lambda: cfg)
    return TestClient(build_api_app(cfg))


def test_endpoint_disabled_by_default(ac_root, monkeypatch):
    client = _client(monkeypatch, enabled=False)
    resp = client.post("/memory/ingest", json={"task_id": "E1", "title": "t", "summary": "s"})
    assert resp.status_code == 200 and resp.json()["data"]["status"] == "disabled"
    with fts.cursor() as conn:
        n = conn.execute("SELECT COUNT(*) FROM files WHERE path LIKE 'task-outcome-%'").fetchone()[
            0
        ]
    assert n == 0  # kill-switch: nothing written


def test_endpoint_enabled_ingests(ac_root, monkeypatch):
    client = _client(monkeypatch, enabled=True)
    resp = client.post(
        "/memory/ingest",
        json={
            "task_id": "E2",
            "title": "做完了",
            "summary": "产出了一个统计脚本",
            "intent_id": 7,
            "artifacts": [{"type": "document", "url": "file:///tmp/x"}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "ingested"


def test_endpoint_rejects_extra_field(ac_root, monkeypatch):
    client = _client(monkeypatch, enabled=True)
    resp = client.post(
        "/memory/ingest", json={"task_id": "E3", "title": "t", "summary": "s", "raw_log": "leak"}
    )
    assert resp.status_code == 422  # extra="forbid" pins the field set
