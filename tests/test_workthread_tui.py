"""H1 标注 TUI 的状态机测试（终端 IO 不进测试，ReviewController 全键路可测）."""

from __future__ import annotations

from datetime import datetime

import pytest
from rich.console import Console

from persome.store import fts
from persome.tui.thread_review import ReviewController
from persome.workthread import store as wt_store
from persome.workthread.model import Binding, WorkThread

TODAY = datetime.now().strftime("%Y-%m-%d")


@pytest.fixture
def seeded(ac_root):
    """Three threads touched today: main (active) / side / a needs_label one."""
    ids = {}
    with fts.cursor() as conn:
        for key, title, status, minutes in [
            ("main", "Kevin 交办：意图识别链路优化", "active", 252),
            ("side", "周报草稿", "background", 66),
            ("warn", "数据看板二期", "background", 30),
        ]:
            t = WorkThread(
                id="",
                title=title,
                status=status,
                origin_actor="Kevin" if key == "main" else "",
                first_seen=f"{TODAY}T09:00",
                last_active=f"{TODAY}T18:00",
                confidence=0.7,
                bindings=[
                    Binding(
                        window_id=f"{TODAY}T12:00#{key}",
                        spans=[["09:00", f"{9 + minutes // 60:02d}:{minutes % 60:02d}"]],
                    )
                ],
            )
            wt_store.insert_thread(conn, t)
            ids[key] = t.id
        wt_store.add_label(
            conn,
            day=TODAY,
            thread_id=ids["warn"],
            action="needs_label",
            payload={},
            source="disagreement",
        )
    return ids


def _row_ids(c: ReviewController) -> list[str]:
    return [r.thread_id for r in c.rows]


def test_needs_label_rows_sort_first(seeded):
    c = ReviewController()
    assert c.rows[0].thread_id == seeded["warn"]  # ⚠ 分歧行置顶（spec 10.4）
    assert c.rows[0].needs_label


def test_confirm_all_is_one_key(seeded):
    c = ReviewController()
    c.handle_key("a")
    assert all(r.verdict for r in c.rows)
    with fts.cursor() as conn:
        labels = wt_store.labels_for_day(conn, TODAY)
        assert sum(1 for r in labels if r["action"] == "confirm") == 3
        # H2 needs_label 队列被本次标注消耗（10.4 闭环）
        assert wt_store.pending_label_queue(conn) == []


def test_navigation_and_single_confirm(seeded):
    c = ReviewController()
    c.handle_key("j")  # move to second row
    target = c.rows[c.selected].thread_id
    c.handle_key("y")
    with fts.cursor() as conn:
        labels = wt_store.labels_for_day(conn, TODAY)
    assert any(r["thread_id"] == target and r["action"] == "confirm" for r in labels)
    assert any(r.thread_id == target and r.verdict for r in c.rows)


def test_not_this_supersedes_and_drops_from_live(seeded):
    c = ReviewController()
    # select the main thread row
    while c.rows[c.selected].thread_id != seeded["main"]:
        c.handle_key("j")
    c.handle_key("x")
    with fts.cursor() as conn:
        assert wt_store.get_thread(conn, seeded["main"]).status == "superseded"


def test_rename_flow_with_escape_and_enter(seeded):
    c = ReviewController()
    c.handle_key("r")
    assert c.mode == "rename_input"
    for ch in "新名":
        c.handle_key(ch)
    c.handle_key("\x1b")  # Esc cancels
    assert c.mode == "normal"
    with fts.cursor() as conn:
        t = wt_store.get_thread(conn, c.rows[c.selected].thread_id)
    assert t.title != "新名"

    c.handle_key("r")
    for ch in "意图识别二期":
        c.handle_key(ch)
    c.handle_key("\r")
    with fts.cursor() as conn:
        t = wt_store.get_thread(conn, c.rows[c.selected].thread_id)
    assert t.title == "意图识别二期"


def test_rename_backspace(seeded):
    c = ReviewController()
    c.handle_key("r")
    for ch in "ab":
        c.handle_key(ch)
    c.handle_key("\x7f")
    assert c.rename_buffer == "a"


def test_merge_pick_flow(seeded):
    c = ReviewController()
    # start merge from the warn row (selected=0), pick the next row as target
    src = c.rows[0].thread_id
    c.handle_key("m")
    assert c.mode == "merge_pick"
    c.handle_key("j")
    dst = c.rows[c.selected].thread_id
    c.handle_key("\r")
    assert c.mode == "normal"
    with fts.cursor() as conn:
        assert wt_store.get_thread(conn, src).status == "superseded"
        assert wt_store.get_thread(conn, dst).user_corrected >= 1


def test_merge_escape_cancels(seeded):
    c = ReviewController()
    c.handle_key("m")
    c.handle_key("\x1b")
    assert c.mode == "normal"
    with fts.cursor() as conn:
        assert all(t.status != "superseded" for t in wt_store.list_threads(conn))


def test_pin_key(seeded):
    c = ReviewController()
    c.handle_key("p")
    with fts.cursor() as conn:
        assert wt_store.get_thread(conn, c.rows[c.selected].thread_id).pinned


def test_day_toggle_and_quit(seeded):
    c = ReviewController()
    c.handle_key("\t")
    assert c.day != TODAY  # yesterday
    c.handle_key("\t")
    assert c.day == TODAY
    c.handle_key("q")
    assert c.quit


def test_render_smoke(seeded):
    """Rendering stays a pure function of state — capture it, don't eyeball it."""
    c = ReviewController()
    console = Console(width=100, record=True)
    console.print(c.render())
    out = console.export_text()
    assert "当前工作线" in out
    assert "工作线重建——对吗？" in out
    assert "意图识别链路优化" in out
    assert "⚠" in out  # 分歧行标记
    assert "4.2h" in out  # 252min → 4.2h
    assert "a 都对" in out  # keybar


def test_render_empty_day(ac_root):
    c = ReviewController()
    console = Console(width=100, record=True)
    console.print(c.render())
    out = console.export_text()
    assert "该日没有触及任何工作线" in out


def test_arrow_keys_move(seeded):
    c = ReviewController()
    before = c.selected
    c.handle_key("\x1b[B")  # down arrow
    assert c.selected == (before + 1) % len(c.rows)
    c.handle_key("\x1b[A")  # up arrow
    assert c.selected == before


# ── KeyDecoder（裸 fd 字节流 → 键事件；select+缓冲 stdin 丢键 bug 的回归测试）──


def test_keydecoder_paste_multibyte_all_chars_arrive():
    """粘贴整段中文必须逐字全到——不是只有第一个字（修复前的实测症状）。"""
    from persome.tui.thread_review import KeyDecoder

    d = KeyDecoder()
    keys = d.feed("数据看板二期·图表重构".encode())
    assert "".join(keys) == "数据看板二期·图表重构"


def test_keydecoder_split_utf8_across_reads():
    from persome.tui.thread_review import KeyDecoder

    d = KeyDecoder()
    raw = "数".encode()
    assert d.feed(raw[:1]) == []  # 半个多字节字符不出键
    assert d.feed(raw[1:]) == ["数"]


def test_keydecoder_arrow_sequence_grouped():
    from persome.tui.thread_review import KeyDecoder

    d = KeyDecoder()
    assert d.feed(b"\x1b[Bj") == ["\x1b[B", "j"]


def test_keydecoder_arrow_split_across_reads():
    from persome.tui.thread_review import KeyDecoder

    d = KeyDecoder()
    assert d.feed(b"\x1b") == []
    assert d.feed(b"[A") == ["\x1b[A"]


def test_keydecoder_bare_esc_flushes():
    from persome.tui.thread_review import KeyDecoder

    d = KeyDecoder()
    assert d.feed(b"\x1b") == []  # held: might be an arrow
    assert d.flush() == ["\x1b"]  # quiet gap → it was a real Esc
    assert d.flush() == []


def test_keydecoder_parameterised_csi_not_mis_split():
    """#585: 带参数的 CSI（Delete/PageUp/Ctrl+方向键）必须整段成一个键,不能 3 字节
    硬切让尾字节（~/;/数字）漏成可打印输入污染 rename。"""
    from persome.tui.thread_review import KeyDecoder

    d = KeyDecoder()
    assert d.feed(b"\x1b[3~") == ["\x1b[3~"]  # Delete
    assert d.feed(b"\x1b[5~") == ["\x1b[5~"]  # PageUp
    assert d.feed(b"\x1b[1;5A") == ["\x1b[1;5A"]  # Ctrl+Up
    assert d.feed(b"\x1bOA") == ["\x1bOA"]  # SS3 keypad up
    # 紧跟普通字符不被吞
    assert d.feed(b"\x1b[3~x") == ["\x1b[3~", "x"]


def test_keydecoder_partial_csi_held_until_complete():
    """#585: 未读全的参数化 CSI 必须 hold，下次读到 final byte 才成键。"""
    from persome.tui.thread_review import KeyDecoder

    d = KeyDecoder()
    assert d.feed(b"\x1b[3") == []  # 缺 final byte → hold
    assert d.feed(b"~") == ["\x1b[3~"]


def test_reload_repositions_merge_source_by_thread_id(seeded):
    """#572: reload 重排行后 merge_source 必须按 thread_id 重定位（跟线不跟槽位），
    否则合并模式下后台刷新会让 merge_source 指向错误的线。"""
    c = ReviewController()
    # 进入合并模式：merge_source = 当前选中行
    c.handle_key("m")
    src_id = c.rows[c.merge_source].thread_id
    c.reload()  # 模拟后台自动刷新触发的重排
    assert 0 <= c.merge_source < len(c.rows)
    assert c.rows[c.merge_source].thread_id == src_id  # 仍指向同一条线


def test_confirm_all_does_not_mark_failed_rows(seeded, monkeypatch):
    """#571: 整屏确认时 apply 失败的行不能被无差别标「✓ 已确认」——反馈须与后端实际
    状态一致（与单行 _correct_selected 同语义）。"""
    c = ReviewController()
    fail_id = c.rows[0].thread_id
    orig_apply = c._apply

    def _fake_apply(thread_id, action, **kw):  # noqa: ANN001, ANN002, ANN003
        if thread_id == fail_id and action == "confirm":
            return {"ok": False, "error": "boom"}
        return orig_apply(thread_id, action, **kw)

    monkeypatch.setattr(c, "_apply", _fake_apply)
    c.handle_key("a")  # 整屏确认
    failed_row = next(r for r in c.rows if r.thread_id == fail_id)
    assert not failed_row.verdict  # 失败行未被标记
    assert any(r.verdict for r in c.rows if r.thread_id != fail_id)  # 其余成功行已标
