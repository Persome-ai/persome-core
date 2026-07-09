"""Tests for the cold-start bootstrap harvester.

Real-machine collectors and sub-agents aren't exercised here; instead we test the
framework guarantees offline: safe_run isolation, redactor caps, JSON parsing,
the memory sink, bounded fs tools, the raw shell, deterministic area picking,
parallel explorer fan-out (stubbed), and the orchestrated synthesize flow.
"""

from __future__ import annotations

import json

import frontmatter

from persome import config as config_mod
from persome import paths
from persome.bootstrap import redactor, runner, sink, subagent, synthesizer
from persome.bootstrap.collectors import (
    Collector,
    CollectorResult,
    Signal,
    SkipCollector,
    registry,
    safe_run,
)
from persome.store import files as files_mod

# --- collector framework --------------------------------------------------


def test_all_collectors_registered() -> None:
    names = {c.name for c in registry()}
    assert {
        "system",
        "identity",
        "toolchain",
        "shell",
        "projects",
        "apps",
        "documents",
        "browser",
        "comms",
    } <= names


def test_safe_run_isolates_exception() -> None:
    def boom() -> list[Signal]:
        raise RuntimeError("locked db")

    result = safe_run(Collector("x", "X", "system", boom))
    assert result.ok is False
    assert "RuntimeError" in result.error
    assert result.produced is False


def test_safe_run_records_skip() -> None:
    def skip() -> list[Signal]:
        raise SkipCollector("no permission")

    result = safe_run(Collector("x", "X", "system", skip))
    assert result.ok is True
    assert result.skipped == "no permission"


def test_safe_run_empty_is_skip() -> None:
    result = safe_run(Collector("x", "X", "system", lambda: []))
    assert result.skipped == "no signals found"
    assert result.produced is False


# --- redactor -------------------------------------------------------------


def test_redactor_caps_list_length() -> None:
    big = [{"name": f"d{i}.com", "count": i} for i in range(100)]
    results = [CollectorResult("browser", "浏览兴趣", "interests", [Signal("域名", big)])]
    text = redactor.build(results)
    assert "d0.com" in text
    assert f"d{redactor._MAX_LIST - 1}.com" in text
    assert f"d{redactor._MAX_LIST}.com" not in text


def test_redactor_skips_unproduced() -> None:
    results = [
        CollectorResult("a", "A", "system", [Signal("k", "v")]),
        CollectorResult("b", "B", "system", skipped="nope"),
        CollectorResult("c", "C", "system", ok=False, error="boom"),
    ]
    text = redactor.build(results)
    assert "## A" in text
    assert "## B" not in text
    assert "## C" not in text


def test_redactor_enforces_total_cap() -> None:
    huge = [Signal(f"k{i}", "x" * 500) for i in range(200)]
    results = [CollectorResult("a", "A", "system", huge)]
    text = redactor.build(results)
    assert len(text) <= redactor._MAX_TOTAL_CHARS + 32


# --- synthesizer._parse (profile JSON parsing) ----------------------------

_PROFILE_JSON = json.dumps(
    {
        "headline": "AI 工具开发者",
        "narrative": "深度使用 AI 的全栈开发者。",
        "identity_facts": [
            "职业是 AI 工具开发者",
            "主力编程语言是 Python",
            "在做 Persome agent 框架",
            "主力设备是 macOS",
        ],
        "preference_facts": [
            "偏好 CLI 工作流",
            "用 uv 管理 Python 依赖",
            "习惯 TDD",
            "偏好原子化提交",
        ],
        "projects": [{"name": "acme-mono", "facts": ["自研 AI agent 框架", "Python + Dart 单仓"]}],
        "tools": [{"name": "cmux", "facts": ["主力终端"]}],
        "topics": [{"name": "AI Agent", "facts": ["从项目推断"]}],
        "confidence_notes": "AI 方向把握高；动机不可知。",
    },
    ensure_ascii=False,
)


def test_parse_profile() -> None:
    profile = synthesizer._parse(_PROFILE_JSON)
    assert profile is not None
    assert profile.headline == "AI 工具开发者"
    assert profile.projects[0]["name"] == "acme-mono"
    assert profile.projects[0]["facts"] == ["自研 AI agent 框架", "Python + Dart 单仓"]
    assert profile.tools[0]["name"] == "cmux"
    assert profile.topics[0]["name"] == "AI Agent"
    assert profile.identity_facts == [
        "职业是 AI 工具开发者",
        "主力编程语言是 Python",
        "在做 Persome agent 框架",
        "主力设备是 macOS",
    ]
    assert profile.preference_facts[0] == "偏好 CLI 工作流"


def test_parse_legacy_note_becomes_single_fact() -> None:
    # Back-compat: an old-shape row with ``note`` coerces to a one-element facts list.
    profile = synthesizer._parse(
        json.dumps({"headline": "x", "projects": [{"name": "p", "note": "做某事"}]})
    )
    assert profile is not None
    assert profile.projects == [{"name": "p", "facts": ["做某事"]}]


def test_parse_facts_dedupes_and_strips() -> None:
    profile = synthesizer._parse(
        json.dumps({"headline": "x", "identity_facts": ["  a  ", "a", "b", ""]})
    )
    assert profile is not None
    assert profile.identity_facts == ["a", "b"]


def test_parse_unfences_json() -> None:
    profile = synthesizer._parse("```json\n" + _PROFILE_JSON + "\n```")
    assert profile is not None and profile.headline == "AI 工具开发者"


def test_parse_extracts_json_after_preamble() -> None:
    noisy = "好的，开始合成。\n\n```json\n" + _PROFILE_JSON + "\n```"
    profile = synthesizer._parse(noisy)
    assert profile is not None and profile.headline == "AI 工具开发者"


def test_parse_empty_returns_none() -> None:
    assert synthesizer._parse("") is None


def test_parse_bad_json_returns_none() -> None:
    assert synthesizer._parse("this is not json at all") is None


def test_parse_coerces_string_rows() -> None:
    profile = synthesizer._parse(json.dumps({"headline": "x", "tools": ["raw-tool-name"]}))
    assert profile is not None
    assert profile.tools == [{"name": "raw-tool-name", "facts": []}]


# --- clue kind heuristic --------------------------------------------------


def test_clue_kind_matches_discriminative_names() -> None:
    # Normal happy-path folder names still classify correctly.
    assert synthesizer._clue_kind("简历") == "resume"
    assert synthesizer._clue_kind("resume") == "resume"
    assert synthesizer._clue_kind("我的日记") == "diary"
    assert synthesizer._clue_kind("家书") == "letter"
    assert synthesizer._clue_kind("书信往来") == "letter"
    assert synthesizer._clue_kind("老照片") == "memory"
    assert synthesizer._clue_kind("相册") == "memory"
    assert synthesizer._clue_kind("工作") == "work"
    assert synthesizer._clue_kind("project-x") == "work"


def test_clue_kind_unknown_falls_back_to_other() -> None:
    assert synthesizer._clue_kind("随便起的名字") == "other"
    assert synthesizer._clue_kind("untitled folder") == "other"


def test_clue_kind_no_overbroad_single_char_letter() -> None:
    # The old single-char "信"/"家" hints mis-tagged everything containing them.
    assert synthesizer._clue_kind("微信") != "letter"
    assert synthesizer._clue_kind("信息") != "letter"
    assert synthesizer._clue_kind("信用卡") != "letter"
    assert synthesizer._clue_kind("家庭账单") != "letter"
    assert synthesizer._clue_kind("搬家清单") != "letter"


def test_clue_kind_no_overbroad_single_char_memory() -> None:
    # The old single-char "图" hint mis-tagged 图书/截图/设计图 as memory.
    assert synthesizer._clue_kind("图书") != "memory"
    assert synthesizer._clue_kind("截图") != "memory"
    assert synthesizer._clue_kind("设计图") != "memory"


def test_clue_kind_ascii_hints_are_word_bounded() -> None:
    # Bare "cv" substring used to match any name containing the letters c+v.
    assert synthesizer._clue_kind("archive") != "resume"
    assert synthesizer._clue_kind("cvtools") != "resume"
    assert synthesizer._clue_kind("recovery") != "resume"
    # But a real token "cv" (whole word / boundary) still classifies as resume.
    assert synthesizer._clue_kind("my-cv") == "resume"
    assert synthesizer._clue_kind("CV 2025") == "resume"


def test_clue_kind_ascii_work_not_substring() -> None:
    # "work" as a bare substring shouldn't fire on unrelated tokens.
    assert synthesizer._clue_kind("network-logs") != "work"
    assert synthesizer._clue_kind("homework") != "work"
    # A real whole-token "work" classifies as work.
    assert synthesizer._clue_kind("work-2025") == "work"
    assert synthesizer._clue_kind("my work") == "work"


# --- sink -----------------------------------------------------------------


def _profile() -> synthesizer.Profile:
    return synthesizer.Profile(
        headline="AI 工具开发者",
        vibe="像把人生当创业公司运营的少年",
        narrative="在代码与命盘之间的少年，夜里十一点还在敲键盘。",  # 散文，仅 UI
        identity="Persome 的开发者。",  # legacy 散文字段，仅 UI
        preferences="偏好 CLI。",  # legacy 散文字段，仅 UI
        identity_facts=[
            "职业是 AI 工具开发者",
            "主力编程语言是 Python",
            "在做 Persome agent 框架",
            "主力设备是 macOS",
        ],
        preference_facts=[
            "偏好 CLI 工作流",
            "用 uv 管理依赖",
            "习惯 TDD",
            "偏好原子化提交",
        ],
        projects=[
            {
                "name": "acme-mono",
                "facts": [
                    "自研 AI agent 框架",
                    "Python + Dart 单仓",
                    "当前在做记忆层迁移",
                    "用 worktree 并行开发",
                ],
            }
        ],
        tools=[{"name": "cmux", "facts": ["主力终端", "用于并行 agent 会话"]}],
        topics=[
            {"name": "AI Agent", "facts": ["从项目推断"]},
            {"name": "渗透测试", "facts": ["从文件推断"]},
        ],
    )


def test_sink_writes_all_files(ac_root) -> None:
    written = sink.write(_profile(), [], fallback_text="")
    assert "user-profile.md" in written
    assert "user-preferences.md" in written
    assert "project-acme-mono.md" in written
    assert "tool-cmux.md" in written
    topic_files = [w for w in written if w.startswith("topic-")]
    assert len(topic_files) == 2
    for name in written:
        assert files_mod.memory_path(name).exists()


def test_sink_profile_is_atomic_facts_not_prose(ac_root) -> None:
    """P0 验收：user-profile.md ≥4 条、每条单断言、无散文 blob、narrative 不进记忆。"""
    sink.write(_profile(), [], fallback_text="")

    prof = files_mod.read_file(files_mod.memory_path("user-profile.md"))
    live = [e for e in prof.entries if not e.superseded_by]
    assert len(live) >= 4  # 至少 4 条原子事实
    assert prof.entry_count >= 4
    # 每条都是单断言（短），且都带 bootstrap+identity provenance tag。
    for e in live:
        assert "bootstrap" in e.tags
        assert "identity" in e.tags
        assert len(e.body) < 80  # 原子断言，不是 800 字 blob
    # 散文绝不进任何记忆文件。
    for name in ("user-profile.md", "user-preferences.md"):
        post = frontmatter.load(files_mod.memory_path(name))
        assert "在代码与命盘之间的少年" not in post.content  # narrative
        assert "夜里十一点" not in post.content


def test_sink_no_narrative_in_any_memory_file(ac_root) -> None:
    """P0 验收：narrative/headline/vibe 散文不出现在任何写入的 *.md 里。"""
    written = sink.write(_profile(), [], fallback_text="")
    for name in written:
        content = files_mod.memory_path(name).read_text()
        assert "夜里十一点还在敲键盘" not in content
        assert "像把人生当创业公司运营的少年" not in content


def test_sink_preferences_are_atomic(ac_root) -> None:
    sink.write(_profile(), [], fallback_text="")
    pref = files_mod.read_file(files_mod.memory_path("user-preferences.md"))
    live = [e for e in pref.entries if not e.superseded_by]
    assert len(live) >= 4
    for e in live:
        assert "bootstrap" in e.tags and "preference" in e.tags


def test_sink_entity_file_has_multiple_facts(ac_root) -> None:
    """P1 验收：主项目文件 entry_count ≥4（够 schema miner 聚类）。"""
    sink.write(_profile(), [], fallback_text="")
    proj = files_mod.read_file(files_mod.memory_path("project-acme-mono.md"))
    live = [e for e in proj.entries if not e.superseded_by]
    assert len(live) >= 4


def test_sink_skips_entity_with_no_facts(ac_root) -> None:
    p = synthesizer.Profile(
        identity_facts=["职业是开发者"],
        topics=[{"name": "空主题", "facts": []}],
    )
    written = sink.write(p, [], fallback_text="")
    assert not any(w.startswith("topic-") for w in written)
    assert not files_mod.memory_path("topic-x").exists()


def test_sink_fallback_without_profile(ac_root) -> None:
    written = sink.write(None, [], fallback_text="raw signal dump")
    assert written == ["user-profile.md"]
    post = frontmatter.load(files_mod.memory_path("user-profile.md"))
    assert "raw signal dump" in post.content


def test_sink_chinese_topic_slug_is_safe(ac_root) -> None:
    p = synthesizer.Profile(topics=[{"name": "渗透测试", "facts": ["x"]}])
    written = sink.write(p, [], fallback_text="")
    topic = next(w for w in written if w.startswith("topic-"))
    assert topic.startswith("topic-")
    assert files_mod.memory_path(topic).exists()


# --- scale: collectors stay bounded ---------------------------------------


def test_documents_scan_is_bounded(tmp_path, monkeypatch) -> None:
    from persome.bootstrap.collectors import documents

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    for i in range(50):
        (downloads / f"f{i}.txt").write_text("x")
    monkeypatch.setattr(documents, "home", lambda: tmp_path)

    sigs = documents.collect()
    recent = next(s for s in sigs if s.label.startswith("近"))
    assert len(recent.value) == documents._TOP_RECENT
    assert "50" in recent.detail


# --- fs_tools (bounded file tools used by explorers) ----------------------


def _fs(tmp_path, monkeypatch):
    from persome.bootstrap import fs_tools

    monkeypatch.setattr(fs_tools, "_home", lambda: tmp_path.resolve())
    _schemas, handlers, recorder = fs_tools.build_fs_tools()
    return handlers, recorder


def test_list_dir_skips_noise_and_tags_mtime(tmp_path, monkeypatch) -> None:
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "简历.pdf").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x")
    handlers, _ = _fs(tmp_path, monkeypatch)

    out = handlers["list_dir"]({"path": "~", "depth": 2})
    tree = out["tree"]
    assert "Documents/" in tree
    assert "简历.pdf" in tree
    assert "node_modules" not in tree
    assert "[今天]" in tree  # recency tag
    assert "hint" in out


def test_read_file_reads_text_and_reports_modified(tmp_path, monkeypatch) -> None:
    (tmp_path / "notes.md").write_text("# 我的笔记\n创业计划……")
    handlers, recorder = _fs(tmp_path, monkeypatch)
    out = handlers["read_file"]({"path": "notes.md"})
    assert "我的笔记" in out["content"]
    assert out["modified"] == "今天"
    assert len(recorder.read_files) == 1


def test_read_file_refuses_outside_home(tmp_path, monkeypatch) -> None:
    handlers, _ = _fs(tmp_path, monkeypatch)
    assert "error" in handlers["read_file"]({"path": "/etc/hosts"})


def test_read_file_refuses_sensitive_names(tmp_path, monkeypatch) -> None:
    (tmp_path / "env").write_text("ANTHROPIC_API_KEY=sk-secret\n")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("PRIVATE KEY")
    handlers, _ = _fs(tmp_path, monkeypatch)
    assert "error" in handlers["read_file"]({"path": "env"})
    assert "error" in handlers["read_file"]({"path": ".ssh/id_rsa"})


def test_read_file_content_backstop_blocks_secret_shaped(tmp_path, monkeypatch) -> None:
    (tmp_path / "config.toml").write_text("DEEPSEEK_API_KEY=sk-abc123\nother=1\n")
    handlers, recorder = _fs(tmp_path, monkeypatch)
    out = handlers["read_file"]({"path": "config.toml"})
    assert "error" in out
    assert recorder.read_files == []


# --- scan_home_tree (bounded whole-home tree for LLM triage) --------------


def _scan(tmp_path, monkeypatch):
    from persome.bootstrap import fs_tools

    monkeypatch.setattr(fs_tools, "_home", lambda: tmp_path.resolve())
    return fs_tools


def test_scan_home_tree_skips_noise_and_sensitive(tmp_path, monkeypatch) -> None:
    fs_tools = _scan(tmp_path, monkeypatch)
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "简历.pdf").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("KEY")

    tree = fs_tools.scan_home_tree()
    assert "Documents/" in tree
    assert "简历.pdf" in tree  # names only, surfaced for the LLM
    assert "node_modules" not in tree  # noise subtree skipped
    assert ".ssh" not in tree and "id_rsa" not in tree  # sensitive subtree skipped


def test_scan_home_tree_no_file_content(tmp_path, monkeypatch) -> None:
    fs_tools = _scan(tmp_path, monkeypatch)
    (tmp_path / "notes.md").write_text("SUPER_SECRET_BODY_TEXT")
    tree = fs_tools.scan_home_tree()
    assert "notes.md" in tree
    assert "SUPER_SECRET_BODY_TEXT" not in tree  # never reads file bodies


def test_scan_home_tree_depth_cap(tmp_path, monkeypatch) -> None:
    fs_tools = _scan(tmp_path, monkeypatch)
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "buried.txt").write_text("x")
    tree = fs_tools.scan_home_tree(max_depth=2)
    assert "a/" in tree
    assert "b/" in tree
    assert "c/" not in tree  # past the depth cap
    assert "buried.txt" not in tree


def test_scan_home_tree_node_cap(tmp_path, monkeypatch) -> None:
    # The node cap now bounds *within-subtree* depth/breadth, not top-level breadth.
    # Every top-level dir is always named (cheap, fair); the cap only collapses the
    # tail of a deep subtree. Build one top-level dir with many nested subdirs and a
    # tiny budget, and assert the subtree gets truncated while the top dir is named.
    fs_tools = _scan(tmp_path, monkeypatch)
    big = tmp_path / "big"
    big.mkdir()
    for i in range(20):
        (big / f"sub{i:02d}").mkdir()
    tree = fs_tools.scan_home_tree(max_total_nodes=5)
    assert "big/" in tree  # top-level dir always named
    assert "node cap" in tree  # subtree truncated by its budget
    assert "sub19/" not in tree  # later children of the subtree never reached


def test_scan_home_tree_priority_dirs_never_starved(tmp_path, monkeypatch) -> None:
    # Regression for the starvation bug: a single global node budget consumed
    # depth-first let an alphabetically-earlier deep subtree (.config — the one
    # whitelisted dotfile) burn the whole budget before the D-folders were reached,
    # so Desktop/Documents/Downloads vanished from the output. With a per-subtree
    # budget + priority ordering, the 3 TCC folders must always survive.
    fs_tools = _scan(tmp_path, monkeypatch)
    # A big, deep .config subtree that would have eaten a global cap whole.
    deep = tmp_path / ".config"
    deep.mkdir()
    for i in range(30):
        nested = deep / f"app{i:02d}" / "nested" / "deeper"
        nested.mkdir(parents=True)
        (nested / "leaf.txt").write_text("x")
    for d in ("Desktop", "Documents", "Downloads"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "real.md").write_text("x")

    tree = fs_tools.scan_home_tree(max_total_nodes=5)
    assert ".config/" in tree  # earlier subtree still listed (and now capped)
    assert "Desktop/" in tree
    assert "Documents/" in tree
    assert "Downloads/" in tree  # all 3 TCC folders survive regardless of budget


def test_scan_home_tree_per_dir_entry_cap(tmp_path, monkeypatch) -> None:
    fs_tools = _scan(tmp_path, monkeypatch)
    big = tmp_path / "big"
    big.mkdir()
    for i in range(60):
        (big / f"file{i:02d}.txt").write_text("x")
    tree = fs_tools.scan_home_tree(max_entries_per_dir=10)
    assert "(60 files:" in tree  # true count reported
    assert "+50 more" in tree  # only 10 names shown, rest collapsed


# --- shell_tools (raw shell for explorers) --------------------------------


def test_run_shell_executes_and_caps() -> None:
    from persome.bootstrap import shell_tools

    _schemas, handlers = shell_tools.build_shell_tools()
    out = handlers["run_shell"]({"command": "echo hello-coldstart"})
    assert out["exit_code"] == 0
    assert "hello-coldstart" in out["output"]
    assert "error" in handlers["run_shell"]({"command": "   "})


# --- subagent (deterministic fan-out) -------------------------------------


def test_pick_areas_fallback_no_llm(tmp_path, monkeypatch) -> None:
    # Called with no cfg/tree → day-0 fallback: existing TCC-scoped folders, in order.
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Desktop").mkdir()
    (tmp_path / "Documents").mkdir()
    # Downloads intentionally absent; other dirs must be ignored.
    (tmp_path / "Projects").mkdir()
    (tmp_path / "node_modules").mkdir()

    areas = subagent.pick_areas()
    names = [a.rsplit("/", 1)[-1] for a in areas]
    assert names == ["Desktop", "Documents"]  # only scoped folders that exist, in order
    assert "Projects" not in names
    assert "node_modules" not in names


def _stub_pick_llm(monkeypatch, text):
    """Make the triage LLM return ``text`` so pick_areas parses it deterministically."""
    from persome.writer import llm as llm_mod

    monkeypatch.setattr(llm_mod, "call_llm", lambda *a, **k: object())
    monkeypatch.setattr(llm_mod, "extract_text", lambda resp: text)


def test_pick_areas_llm_triage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Projects").mkdir()
    (tmp_path / "日记").mkdir()
    (tmp_path / "Desktop").mkdir()
    _stub_pick_llm(monkeypatch, '```json\n["~/Projects", "~/日记", "~/Nope"]\n```')

    areas = subagent.pick_areas(config_mod.load(), tree="~/\n  Projects/", owner="tester")
    names = [a.rsplit("/", 1)[-1] for a in areas]
    assert names == ["Projects", "日记"]  # parsed in order; "~/Nope" dropped (doesn't exist)


def test_pick_areas_llm_empty_falls_back(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    _stub_pick_llm(monkeypatch, "[]")
    areas = subagent.pick_areas(config_mod.load(), tree="~/", owner="tester")
    assert [a.rsplit("/", 1)[-1] for a in areas] == ["Documents"]  # fallback


def test_pick_areas_llm_garbage_falls_back(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Downloads").mkdir()
    _stub_pick_llm(monkeypatch, "I cannot answer that.")
    areas = subagent.pick_areas(config_mod.load(), tree="~/", owner="tester")
    assert [a.rsplit("/", 1)[-1] for a in areas] == ["Downloads"]  # fallback


def test_pick_areas_llm_error_falls_back(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Desktop").mkdir()
    from persome.writer import llm as llm_mod

    def _boom(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(llm_mod, "call_llm", _boom)
    areas = subagent.pick_areas(config_mod.load(), tree="~/", owner="tester")
    assert [a.rsplit("/", 1)[-1] for a in areas] == ["Desktop"]  # fallback on exception


def test_run_explorers_parallel(monkeypatch) -> None:
    async def _fake_explore(cfg, path, owner=""):
        return {"path": path, "findings": f"found in {path}", "read_files": [f"{path}/a.md"]}

    monkeypatch.setattr(subagent, "_explore_one", _fake_explore)
    out = subagent.run_explorers(config_mod.load(), ["~/Documents", "~/Desktop"], "owner")
    assert len(out) == 2
    assert out[0]["findings"] == "found in ~/Documents"


def test_run_explorers_empty_areas(monkeypatch) -> None:
    assert subagent.run_explorers(config_mod.load(), []) == []


# --- synthesize (orchestration) -------------------------------------------


def _stub_orchestration(monkeypatch, *, profile, explorers=None):
    """Stub the orchestration deps so synthesize runs offline (no collectors now)."""
    monkeypatch.setattr(subagent, "anchor_owner", lambda: "system user: tester")
    # Don't scan the real home in unit tests — the picked areas are stubbed anyway.
    monkeypatch.setattr(synthesizer.fs_tools, "scan_home_tree", lambda **k: "~/\n  Documents/")
    monkeypatch.setattr(subagent, "pick_areas", lambda *a, **k: ["~/Documents"])
    monkeypatch.setattr(
        subagent,
        "run_explorers",
        lambda *a, **k: (
            explorers
            if explorers is not None
            else [{"path": "~/Documents", "findings": "f", "read_files": ["~/Documents/a.md"]}]
        ),
    )
    monkeypatch.setattr(synthesizer, "_synthesize_profile", lambda cfg, ctx: profile)


def test_synthesize_orchestrates(ac_root, monkeypatch) -> None:
    _stub_orchestration(monkeypatch, profile=_profile())
    profile, results, explored = synthesizer.synthesize(config_mod.load())
    assert profile is not None and profile.headline == "AI 工具开发者"
    assert results == []  # no non-file collectors anymore
    assert explored.listed_dirs == ["~/Documents"]
    assert explored.read_files == ["~/Documents/a.md"]


def test_synthesize_shallow_skips_explorers(ac_root, monkeypatch) -> None:
    called = {"explorers": False}

    def _boom_explorers(*a, **k):
        called["explorers"] = True
        return []

    monkeypatch.setattr(subagent, "anchor_owner", lambda: "owner")
    monkeypatch.setattr(subagent, "run_explorers", _boom_explorers)
    monkeypatch.setattr(synthesizer, "_synthesize_profile", lambda cfg, ctx: _profile())

    profile, _results, explored = synthesizer.synthesize(config_mod.load(), deep=False)
    assert profile is not None
    assert called["explorers"] is False  # --shallow does not explore
    assert explored.read_files == []


def test_synthesize_returns_none_on_failed_synthesis(ac_root, monkeypatch) -> None:
    _stub_orchestration(monkeypatch, profile=None)
    profile, results, _explored = synthesizer.synthesize(config_mod.load())
    assert profile is None
    assert results == []


# --- s3/s4 bootstrap events (scan_tree / clue / read / hypothesis / synth_start) ---


def _collect_events(monkeypatch):
    """Replace events.publish with a collector; return the captured list."""
    from persome import events as events_mod

    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        events_mod, "publish", lambda stage, etype, payload: seen.append((stage, etype, payload))
    )
    return seen


def test_clue_kind_filename_heuristic() -> None:
    assert synthesizer._clue_kind("简历") == "resume"
    assert synthesizer._clue_kind("My CV") == "resume"
    assert synthesizer._clue_kind("我的日记") == "diary"
    assert synthesizer._clue_kind("journal") == "diary"
    assert synthesizer._clue_kind("家书") == "letter"
    assert synthesizer._clue_kind("老照片") == "memory"
    assert synthesizer._clue_kind("项目A") == "work"
    assert synthesizer._clue_kind("杂物堆") == "other"


def test_build_clues_real_counts_and_templates(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(synthesizer.fs_tools, "_home", lambda: tmp_path.resolve())
    diary = tmp_path / "Documents" / "日记"
    diary.mkdir(parents=True)
    for i in range(3):
        (diary / f"{i}.md").write_text("x")  # 3 files, no subdirs → "3 个文件"

    clues = synthesizer.build_clues([str(diary)])
    assert len(clues) == 1
    c = clues[0]
    assert c["path"] == "Documents/日记"  # home-relative
    assert c["kind"] == "diary"
    assert c["tag"] == "随手记"
    assert c["title"] == "日记 · 你的声音"
    assert c["detail"] == "3 个文件"  # real count, not invented


def test_build_clues_detail_degrades_truthfully(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(synthesizer.fs_tools, "_home", lambda: tmp_path.resolve())
    empty = tmp_path / "工作"
    empty.mkdir()
    clues = synthesizer.build_clues([str(empty)])
    assert clues[0]["detail"] == "0 项"  # conservative, never fabricated


def test_hypothesis_phrase_rules() -> None:
    assert synthesizer.hypothesis_phrase([]) == "正在向前走的人"
    assert synthesizer.hypothesis_phrase([{"kind": "work"}, {"kind": "resume"}]) == "正在向前走的人"
    assert (
        synthesizer.hypothesis_phrase([{"kind": "letter"}, {"kind": "memory"}]) == "把牵挂收好的人"
    )
    assert (
        synthesizer.hypothesis_phrase([{"kind": "diary"}, {"kind": "diary"}])
        == "习惯把情绪写下来的人"
    )


def test_synthesize_publishes_scan_tree_clue_hypothesis_synth_start(ac_root, monkeypatch) -> None:
    seen = _collect_events(monkeypatch)
    monkeypatch.setattr(subagent, "anchor_owner", lambda: "owner")
    monkeypatch.setattr(synthesizer.fs_tools, "scan_home_tree", lambda **k: "~/\n  Documents/")
    monkeypatch.setattr(subagent, "pick_areas", lambda *a, **k: ["~/Documents"])
    # build_clues stats real paths; stub it so the test doesn't depend on real home.
    monkeypatch.setattr(
        synthesizer,
        "build_clues",
        lambda areas: [
            {
                "path": "Documents",
                "kind": "work",
                "tag": "工作",
                "title": "Documents · 你在做的事",
                "detail": "5 项",
            }
        ],
    )
    monkeypatch.setattr(subagent, "run_explorers", lambda *a, **k: [])
    monkeypatch.setattr(synthesizer, "_synthesize_profile", lambda cfg, ctx: _profile())

    synthesizer.synthesize(config_mod.load())

    by_type = {e: p for s, e, p in seen if s == "bootstrap"}
    assert by_type["scan_tree"]["tree"].startswith("~/")
    assert by_type["clue"]["kind"] == "work" and by_type["clue"]["path"] == "Documents"
    assert by_type["hypothesis"]["phrase"]  # non-empty rule-based phrase
    assert "synth_start" in by_type
    # order: scan_tree before clue before synth_start
    order = [e for s, e, _ in seen if e in ("scan_tree", "clue", "synth_start")]
    assert order == ["scan_tree", "clue", "synth_start"]


def test_synthesize_shallow_skips_scan_tree_and_clue(ac_root, monkeypatch) -> None:
    seen = _collect_events(monkeypatch)
    monkeypatch.setattr(subagent, "anchor_owner", lambda: "owner")
    monkeypatch.setattr(synthesizer, "_synthesize_profile", lambda cfg, ctx: _profile())

    synthesizer.synthesize(config_mod.load(), deep=False)

    types = {e for s, e, _ in seen if s == "bootstrap"}
    # --shallow does no scanning, so no tree/clue/hypothesis — but it still hands
    # off to synthesis.
    assert "scan_tree" not in types
    assert "clue" not in types
    assert "synth_start" in types


def test_explorer_read_handler_publishes_per_file(tmp_path, monkeypatch) -> None:
    from persome.bootstrap import fs_tools

    seen = _collect_events(monkeypatch)
    monkeypatch.setattr(fs_tools, "_home", lambda: tmp_path.resolve())
    (tmp_path / "notes.md").write_text("hello world")

    _schemas, handlers, _rec = subagent._explorer_toolset(publish_reads=True)
    out = handlers["read_file"]({"path": str(tmp_path / "notes.md")})
    assert "hello world" in out["content"]
    reads = [p for s, e, p in seen if e == "read"]
    assert reads == [{"path": "notes.md"}]  # one per-file event, home-relative


def test_explorer_read_handler_silent_on_error(tmp_path, monkeypatch) -> None:
    from persome.bootstrap import fs_tools

    seen = _collect_events(monkeypatch)
    monkeypatch.setattr(fs_tools, "_home", lambda: tmp_path.resolve())

    _schemas, handlers, _rec = subagent._explorer_toolset(publish_reads=True)
    out = handlers["read_file"]({"path": str(tmp_path / "missing.md")})
    assert "error" in out
    assert not [e for s, e, _ in seen if e == "read"]  # failed reads emit nothing


def test_explorer_toolset_default_does_not_publish(tmp_path, monkeypatch) -> None:
    from persome.bootstrap import fs_tools

    seen = _collect_events(monkeypatch)
    monkeypatch.setattr(fs_tools, "_home", lambda: tmp_path.resolve())
    (tmp_path / "notes.md").write_text("hello")

    _schemas, handlers, _rec = subagent._explorer_toolset()  # no publish_reads
    handlers["read_file"]({"path": str(tmp_path / "notes.md")})
    assert not [e for s, e, _ in seen if e == "read"]  # survey path stays silent


# --- runner ---------------------------------------------------------------


def test_runner_json_output(ac_root, monkeypatch, capsys) -> None:
    monkeypatch.setattr(subagent, "survey_areas", lambda: "## ~/Documents\n简历.md  [今天]")
    rc = runner.run(config_mod.load(), as_json=True)
    assert rc == 0
    assert "简历.md" in capsys.readouterr().out


def test_runner_no_llm_prints_tree(ac_root, monkeypatch, capsys) -> None:
    monkeypatch.setattr(subagent, "survey_areas", lambda: "## ~/Documents\nnotes.md  [今天]")
    rc = runner.run(config_mod.load(), use_llm=False)
    assert rc == 0
    assert "notes.md" in capsys.readouterr().out


def test_runner_dry_run_writes_nothing(ac_root, monkeypatch) -> None:
    _stub_orchestration(monkeypatch, profile=_profile())
    rc = runner.run(config_mod.load(), use_llm=True, dry_run=True)
    assert rc == 0
    assert not list(paths.memory_dir().glob("project-*.md"))


def test_runner_full_writes_memory(ac_root, monkeypatch) -> None:
    _stub_orchestration(monkeypatch, profile=_profile())
    rc = runner.run(config_mod.load(), use_llm=True, dry_run=False)
    assert rc == 0
    assert files_mod.memory_path("project-acme-mono.md").exists()
    assert files_mod.memory_path("tool-cmux.md").exists()


def test_run_headless_publishes_events_and_writes(ac_root, monkeypatch) -> None:
    from persome import events as events_mod

    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        events_mod, "publish", lambda stage, etype, payload: seen.append((stage, etype, payload))
    )
    _stub_orchestration(monkeypatch, profile=_profile())
    written = runner.run_headless(config_mod.load())
    assert written >= 1
    kinds = [(s, e) for s, e, _ in seen]
    assert ("bootstrap", "stage_start") in kinds
    assert ("bootstrap", "stage_end") in kinds
    # stage_end carries the profile so the app can render it off the SSE stream.
    end_payload = next(p for s, e, p in seen if e == "stage_end")
    assert end_payload["profile"]["headline"] == "AI 工具开发者"
    assert files_mod.memory_path("project-acme-mono.md").exists()


def test_synthesis_uses_pro_model(ac_root) -> None:
    # The final portrait runs on the stronger model unless the user overrides.
    cfg = config_mod.load()
    boosted = synthesizer._synthesis_cfg(cfg)
    assert boosted.model_for("bootstrap").model == "deepseek-v4-pro"


# --- P2: re-run idempotency + supersede-able by steady-state --------------


def _live_bootstrap_entries(name: str) -> list:
    """Live (non-retired) #bootstrap entries, judged from markdown (the SSOT).

    ``mark_entry_deleted`` retires an entry by striking its body (``~~...~~`` /
    ``~~~~`` sentinel) without writing a ``#superseded-by`` tag, so a struck
    entry must be filtered by its body, not by ``superseded_by``.
    """
    parsed = files_mod.read_file(files_mod.memory_path(name))
    return [
        e
        for e in parsed.entries
        if "bootstrap" in e.tags and not e.superseded_by and not e.body.startswith("~~")
    ]


def test_sink_rerun_is_idempotent_retires_prior(ac_root) -> None:
    """P2 验收①：连跑两次 bootstrap，旧 #bootstrap 条目 superseded、不重复堆积。"""
    from persome.store import entries as entries_mod
    from persome.store import fts

    sink.write(_profile(), [], fallback_text="")
    first_live = len(_live_bootstrap_entries("user-profile.md"))
    assert first_live >= 4

    # Second run: prior facts must be retired, not piled on top.
    sink.write(_profile(), [], fallback_text="")
    second_live = len(_live_bootstrap_entries("user-profile.md"))
    assert second_live == first_live  # no duplicate accumulation

    # And the retirement survives a full rebuild from markdown (durable, SSOT).
    with fts.cursor() as conn:
        entries_mod.rebuild_index(conn)
        conn.row_factory = __import__("sqlite3").Row
        rows = conn.execute(
            "SELECT superseded FROM entries WHERE path='user-profile.md' AND tags MATCH 'bootstrap'"
        ).fetchall()
    live = [r for r in rows if r["superseded"] == 0]
    superseded = [r for r in rows if r["superseded"] == 1]
    assert len(live) == first_live
    assert len(superseded) >= first_live  # the first run's facts are now history


def test_retire_prior_bootstrap_is_noop_when_empty(ac_root) -> None:
    from persome.store import entries as entries_mod
    from persome.store import fts

    with fts.cursor() as conn:
        entries_mod.write_preset_files(conn)
        assert sink.retire_prior_bootstrap(conn) == 0


def test_bootstrap_fact_superseded_by_real_observation(ac_root) -> None:
    """P2 验收②：bootstrap 原子事实可被一条矛盾的非-bootstrap 观测 supersede。

    rebuild 后只有观测 is_latest（证明 day-0 猜测能被稳态盖掉）。
    """
    import sqlite3

    from persome.store import entries as entries_mod
    from persome.store import fts

    # 1) bootstrap writes an atomic identity fact (a low-confidence guess).
    p = synthesizer.Profile(identity_facts=["主力编程语言是 Python"])
    sink.write(p, [], fallback_text="")
    boot = _live_bootstrap_entries("user-profile.md")
    assert len(boot) == 1
    boot_id = boot[0].id

    # 2) the classifier later observes the truth and supersedes the guess.
    with fts.cursor() as conn:
        new_id = entries_mod.supersede_entry(
            conn,
            name="user-profile.md",
            old_entry_id=boot_id,
            new_content="主力编程语言是 Rust",
            reason="observed real usage",
            tags=["identity"],  # a real observation — no #bootstrap tag
        )

    # 3) rebuild from markdown (SSOT): only the observation is current.
    with fts.cursor() as conn:
        entries_mod.rebuild_index(conn)
        conn.row_factory = sqlite3.Row
        latest = conn.execute(
            "SELECT e.id FROM entries e WHERE e.path='user-profile.md' AND e.superseded=0"
        ).fetchall()
    latest_ids = {r["id"] for r in latest}
    assert new_id in latest_ids
    assert boot_id not in latest_ids  # day-0 guess folded out to history


# --- P3: bootstrap atomic facts are schema-mine-able ----------------------


def test_bootstrap_facts_feed_schema_miner(ac_root) -> None:
    """P3 验收：bootstrap 产物喂 schema miner，主文件不被 skipped_small，能产 schema。

    用注入的假 llm_call（mock 管线接通）：facts≥4 → 进入 mining、不被 min_facts 跳过。
    """
    from persome.store import fts
    from persome.writer import schema_miner_stage

    sink.write(_profile(), [], fallback_text="")

    # A canned miner response so the test is offline and deterministic.
    def _fake_llm_call(messages):
        class _Resp:
            choices = [
                type(
                    "C",
                    (),
                    {
                        "message": type(
                            "M",
                            (),
                            {
                                "content": json.dumps(
                                    {
                                        "central_proposition": "用户惯于用 CLI + uv 做 Python 开发",
                                        "supporting_summary": "多条偏好事实指向 CLI 工作流",
                                        "expected_inferences": ["下一步多半在终端里操作"],
                                        "confidence": 0.8,
                                    },
                                    ensure_ascii=False,
                                )
                            },
                        )(),
                        "finish_reason": "stop",
                    },
                )()
            ]

        return _Resp()

    with fts.cursor() as conn:
        bundles = schema_miner_stage.collect_fact_bundles(conn)
        paths_with_enough = {b.source_path for b in bundles}
        # user-profile / user-preferences / project-acme-mono each carry ≥4 facts.
        assert "user-profile.md" in paths_with_enough
        assert "user-preferences.md" in paths_with_enough
        assert "project-acme-mono.md" in paths_with_enough

        _bcfg = config_mod.load()
        _bcfg.memory_delta.apply_enabled = False  # bootstrap seeds entries; mine 读 entries（apply_enabled=True→evo_nodes）
        run = schema_miner_stage.mine_schemas_for_user(
            _bcfg, conn, llm_call=_fake_llm_call
        )
    # At least one schema produced; nothing we expected was dropped as too-small.
    assert len(run.written) >= 1
    assert any(w.path.startswith("schema-") for w in run.written)


# --- POST /bootstrap/access (TCC permission pre-flight) -------------------


def test_bootstrap_access_reports_per_folder(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from persome.api import build_api_app
    from persome.api import routes as routes_mod

    # Simulate Documents denied (PermissionError), the other two readable.
    def _probe(path: str) -> bool:
        return not path.endswith("/Documents")

    monkeypatch.setattr(routes_mod, "_probe_folder", _probe)
    client = TestClient(build_api_app())
    resp = client.post("/bootstrap/access")

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    folders = {f["name"]: f for f in body["data"]["folders"]}
    assert set(folders) == {"Desktop", "Documents", "Downloads"}
    assert folders["Desktop"]["granted"] is True
    assert folders["Documents"]["granted"] is False
    assert folders["Downloads"]["granted"] is True
    assert folders["Desktop"]["path"].endswith("/Desktop")
    assert body["data"]["all_granted"] is False


def test_bootstrap_access_all_granted(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from persome.api import build_api_app
    from persome.api import routes as routes_mod

    monkeypatch.setattr(routes_mod, "_probe_folder", lambda path: True)
    client = TestClient(build_api_app())
    resp = client.post("/bootstrap/access")

    body = resp.json()
    assert body["data"]["all_granted"] is True
    assert all(f["granted"] for f in body["data"]["folders"])
