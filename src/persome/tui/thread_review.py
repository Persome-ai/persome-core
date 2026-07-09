"""persome thread tui — H1 标注屏的 Terminal 图形化（Rich Live TUI）.

把 spec 2026-06-12 §十 的 H1 日终标注做成可以**挂在终端里**的常驻面板：

- 上半屏：当前工作线（active ▶ / background ▷，确定性分钟数带 ≈ 透传），
  直读本机 SQLite（WAL 多读者，与 daemon 并存），周期自刷新。
- 下半屏：标注屏——选定日（默认今天，TAB 切昨天）的线重建，H2 分歧队列
  （needs_label）的行置顶并标 ⚠（高不确定样本优先消耗注意力，spec 10.4）。
- 单键即标（纠错闭集，零二级确认）：``a`` 都对（整屏 confirm）/ ``y`` 确认
  选中行 / ``x`` 不是一条线 / ``r`` 改名（行内输入）/ ``m`` 并入（再选目标
  行）/ ``p`` 钉住 / ``j``/``k`` 移动 / ``q`` 退出。每次按键即铸标签回流
  confidence——与 ``thread correct`` / HUD chip 同一闭集、同一标签工厂。

结构上拆成两层，便于单测（终端 IO 不可测，状态机可测）：

- :class:`ReviewController` — 纯状态机：行列表 + 选中 + 模式（normal /
  merge_pick / rename_input），``handle_key`` 吃一个字符、改状态、落库。
- :func:`run` — 终端壳：termios raw 读键线程 + Rich ``Live`` 渲染循环。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..store import fts
from ..workthread import review as wt_review
from ..workthread import store as wt_store

# Seconds between background data refreshes while idle (keys refresh instantly).
_REFRESH_SECONDS = 15.0


@dataclass
class ReviewRow:
    """One labelable line on the review screen."""

    thread_id: str
    title: str
    status: str
    origin_actor: str
    day_minutes: int
    approximate: bool
    confidence: float
    pinned: bool
    needs_label: bool
    # Terminal feedback after a correction landed ("✓ 已确认" …); replaces the
    # action hints for this row.
    verdict: str = ""


def _hours(minutes: int, *, approximate: bool) -> str:
    h = minutes / 60
    return f"{h:.1f}h" + ("≈" if approximate else "")


@dataclass
class ReviewController:
    """Pure-ish state machine behind the TUI (terminal-free, unit-testable).

    All DB access goes through the same :mod:`workthread.review` /
    :mod:`workthread.store` functions the CLI and REST port use — the TUI is
    just another face of the one correction closed set.
    """

    day: str = ""
    rows: list[ReviewRow] = field(default_factory=list)
    selected: int = 0
    # normal | merge_pick | rename_input
    mode: str = "normal"
    rename_buffer: str = ""
    merge_source: int = -1  # row index the merge started from
    status: str = ""
    quit: bool = False
    # Live work-context snapshot for the top panel.
    open_threads: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.day:
            self.day = datetime.now().strftime("%Y-%m-%d")
        self.reload()

    # ── data ────────────────────────────────────────────────────────────────

    def reload(self) -> None:
        """Re-read the review + live threads from the DB.

        Selection follows the THREAD, not the index — corrections re-sort the
        rows (a consumed ⚠ row loses its top slot), and the cursor must stay on
        the line the user just acted on, not jump to whatever moved into its
        old position.
        """
        selected_id = (
            self.rows[self.selected].thread_id if 0 <= self.selected < len(self.rows) else None
        )
        # merge_source is a ROW INDEX into a list that this reload re-sorts; like
        # selected it must follow its THREAD, not the slot, or a background reload
        # during merge mode leaves it pointing at a different line → 静默并错线
        # (#572). Remember its thread id, re-resolve after the re-sort below.
        merge_source_id = (
            self.rows[self.merge_source].thread_id
            if 0 <= self.merge_source < len(self.rows)
            else None
        )
        with fts.cursor() as conn:
            rv = wt_review.build_day_review(conn, day=self.day)
            needs = {r["thread_id"] for r in wt_store.pending_label_queue(conn)}
            self.open_threads = wt_store.open_threads(conn)
            self.stats = wt_store.stats(conn)
        old_verdicts = {r.thread_id: r.verdict for r in self.rows}
        rows = [
            ReviewRow(
                thread_id=line["thread_id"],
                title=line["title"],
                status=line["status"],
                origin_actor=line["origin_actor"],
                day_minutes=line["day_minutes"],
                approximate=line["approximate"],
                confidence=line["confidence"],
                pinned=line["pinned"],
                needs_label=line["thread_id"] in needs,
                verdict=old_verdicts.get(line["thread_id"], ""),
            )
            for line in rv.lines
        ]
        # ⚠ 分歧行置顶（spec 10.4），其余保持 day_minutes 降序（build_day_review 已排）。
        self.rows = sorted(rows, key=lambda r: (not r.needs_label,))
        if selected_id is not None:
            for i, r in enumerate(self.rows):
                if r.thread_id == selected_id:
                    self.selected = i
                    break
            else:
                self.selected = min(self.selected, max(0, len(self.rows) - 1))
        else:
            self.selected = min(self.selected, max(0, len(self.rows) - 1))
        # Re-resolve merge_source by thread id (or invalidate if its line is gone).
        if merge_source_id is not None:
            self.merge_source = next(
                (i for i, r in enumerate(self.rows) if r.thread_id == merge_source_id), -1
            )

    # ── key handling ────────────────────────────────────────────────────────

    def handle_key(self, ch: str) -> None:
        if self.mode == "rename_input":
            self._handle_rename_key(ch)
            return
        if self.mode == "merge_pick":
            self._handle_merge_key(ch)
            return
        self._handle_normal_key(ch)

    def _handle_normal_key(self, ch: str) -> None:
        if ch == "q":
            self.quit = True
        elif ch in ("j", "\x1b[B"):  # down
            self._move(1)
        elif ch in ("k", "\x1b[A"):  # up
            self._move(-1)
        elif ch == "\t":
            self._toggle_day()
        elif ch == "a":
            self._confirm_all()
        elif ch == "y":
            self._correct_selected("confirm", "✓ 已确认")
        elif ch == "x":
            self._correct_selected("not_this", "✗ 已划掉")
        elif ch == "p":
            self._correct_selected("pin", "📌 已钉住")
        elif ch == "r":
            if self._current() is not None:
                self.mode = "rename_input"
                self.rename_buffer = ""
                self.status = "输入新标题，Enter 确认 / Esc 取消"
        elif ch == "m" and self._current() is not None and len(self.rows) >= 2:
            self.mode = "merge_pick"
            self.merge_source = self.selected
            self.status = "选择要并入的目标线（j/k 移动，Enter 确认，Esc 取消）"

    def _handle_rename_key(self, ch: str) -> None:
        if ch in ("\x1b", "\x03"):  # Esc / Ctrl-C
            self.mode = "normal"
            self.status = "已取消改名"
        elif ch in ("\r", "\n"):
            title = self.rename_buffer.strip()
            self.mode = "normal"
            if title:
                self._correct_selected("rename", f"✎ 已改名：{title}", new_title=title)
            else:
                self.status = "已取消改名（空标题）"
        elif ch in ("\x7f", "\b"):
            self.rename_buffer = self.rename_buffer[:-1]
        elif ch.isprintable():
            self.rename_buffer += ch

    def _handle_merge_key(self, ch: str) -> None:
        if ch in ("\x1b", "\x03"):
            self.mode = "normal"
            self.status = "已取消并入"
        elif ch in ("j", "\x1b[B"):
            self._move(1)
        elif ch in ("k", "\x1b[A"):
            self._move(-1)
        elif ch in ("\r", "\n"):
            self._merge_into_selected()

    # ── actions（全部走与 CLI/REST 同一个 apply_correction）──────────────────

    def _current(self) -> ReviewRow | None:
        if 0 <= self.selected < len(self.rows):
            return self.rows[self.selected]
        return None

    def _move(self, delta: int) -> None:
        if self.rows:
            self.selected = (self.selected + delta) % len(self.rows)

    def _toggle_day(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        self.day = yesterday if self.day == today else today
        self.selected = 0
        self.reload()
        self.status = f"切换到 {self.day}"

    def _apply(self, thread_id: str, action: str, **kw: str) -> dict:
        with fts.cursor() as conn:
            return wt_review.apply_correction(
                conn, thread_id=thread_id, action=action, day=self.day, source="tui", **kw
            )

    def _correct_selected(self, action: str, verdict: str, **kw: str) -> None:
        row = self._current()
        if row is None:
            return
        result = self._apply(row.thread_id, action, **kw)
        if result.get("ok"):
            self.status = f"{verdict}：{row.title}"
            self.reload()
            for r in self.rows:
                if r.thread_id == row.thread_id:
                    r.verdict = verdict
        else:
            self.status = f"失败：{result.get('error')}"

    def _confirm_all(self) -> None:
        """整屏点头——最常见路径必须恰好一次按键。"""
        pending = [r for r in self.rows if not r.verdict]
        if not pending:
            self.status = "本日没有待确认的线"
            return
        confirmed_ids: set[str] = set()
        for row in pending:
            if self._apply(row.thread_id, "confirm").get("ok"):
                confirmed_ids.add(row.thread_id)
        self.reload()
        # 只给真正 confirm 成功的行打 ✓ —— 与单行 _correct_selected 一致,失败的行不能
        # 被无差别标「已确认」（与后端实际状态不符,误导用户）(#571)。
        for r in self.rows:
            if r.thread_id in confirmed_ids:
                r.verdict = r.verdict or "✓ 已确认"
        failed = len(pending) - len(confirmed_ids)
        if failed:
            self.status = f"✓ {len(confirmed_ids)} 条已确认 · {failed} 条失败"
        else:
            self.status = f"✓ 都对 — {len(confirmed_ids)} 条线各铸一条 confirm 标签"

    def _merge_into_selected(self) -> None:
        src_row = self.rows[self.merge_source] if 0 <= self.merge_source < len(self.rows) else None
        dst_row = self._current()
        self.mode = "normal"
        if src_row is None or dst_row is None or src_row.thread_id == dst_row.thread_id:
            self.status = "已取消并入（无效目标）"
            return
        result = self._apply(src_row.thread_id, "merge", into_id=dst_row.thread_id)
        if result.get("ok"):
            self.status = f"⇄ 已把「{src_row.title}」并入「{dst_row.title}」"
            self.reload()
        else:
            self.status = f"并入失败：{result.get('error')}"

    # ── rendering（Rich renderable；终端壳和测试都消费它）─────────────────────

    def render(self):  # type: ignore[no-untyped-def]
        return Group(self._render_live_panel(), self._render_review_panel())

    def _render_live_panel(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(width=2)
        table.add_column(ratio=1)
        table.add_column(justify="right")
        if not self.open_threads:
            table.add_row("", Text("（暂无 open 工作线）", style="dim"), "")
        for t in self.open_threads[:4]:
            marker = "▶" if t.status == "active" else "▷"
            style = "bold" if t.status == "active" else "dim"
            label = t.title + (f"（{t.origin_actor} 交办）" if t.origin_actor else "")
            mins = _hours(t.total_active_minutes, approximate=t.approximate)
            table.add_row(
                Text(marker, style="green" if t.status == "active" else "dim"),
                Text(label, style=style),
                Text(mins, style=style),
            )
        churn = self.stats.get("thread_churn", 0.0)
        frozen = " · open 已冻结" if self.stats.get("frozen_open") else ""
        return Panel(
            table,
            title="当前工作线",
            subtitle=f"churn {churn}{frozen}",
            border_style="blue",
        )

    def _render_review_panel(self) -> Panel:
        table = Table.grid(padding=(0, 1))
        table.add_column(width=2)  # cursor
        table.add_column(width=2)  # ⚠
        table.add_column(ratio=1)
        table.add_column(justify="right", width=8)
        table.add_column(width=22)
        for i, row in enumerate(self.rows):
            cursor = "›" if i == self.selected else " "
            warn = Text("⚠", style="yellow") if row.needs_label else Text(" ")
            title = row.title + (f"（{row.origin_actor}）" if row.origin_actor else "")
            if row.pinned:
                title += " 📌"
            title_style = "bold" if i == self.selected else ""
            if row.verdict:
                title_style = "dim"
            tail = (
                Text(row.verdict, style="green")
                if row.verdict
                else Text(f"[{row.status}] conf={row.confidence:.2f}", style="dim")
            )
            table.add_row(
                Text(cursor, style="bold cyan"),
                warn,
                Text(title, style=title_style),
                Text(_hours(row.day_minutes, approximate=row.approximate)),
                tail,
            )
        if not self.rows:
            table.add_row("", "", Text("（该日没有触及任何工作线）", style="dim"), "", "")

        if self.mode == "rename_input":
            keybar = f"新标题：{self.rename_buffer}▏   （Enter 确认 / Esc 取消）"
        elif self.mode == "merge_pick":
            keybar = "并入模式：j/k 选目标线 · Enter 确认 · Esc 取消"
        else:
            keybar = (
                "a 都对 · y 对 · x 不是 · r 改名 · m 并入 · p 钉住 · j/k 移动 · TAB 切日 · q 退出"
            )
        footer = Text(keybar, style="cyan")
        body = Group(table, Text(""), footer)
        if self.status:
            body = Group(table, Text(""), Text(self.status, style="yellow"), footer)
        n_pending = sum(1 for r in self.rows if not r.verdict)
        return Panel(
            body,
            title=f"{self.day} 工作线重建——对吗？",
            subtitle=f"{n_pending} 条待标 · 每次按键即铸一条真值标签",
            border_style="green" if n_pending == 0 else "yellow",
        )


# ── terminal shell ───────────────────────────────────────────────────────────


# One complete CSI (`\x1b[` params intermediates final) or SS3 (`\x1bO` + 1
# byte) escape sequence. Params = 0x30–0x3F, intermediates = 0x20–0x2F, final =
# 0x40–0x7E. A partial sequence simply doesn't match → KeyDecoder holds it.
_CSI_SEQ_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|O.)")


class KeyDecoder:
    """Bytes → key events（纯函数核，单测覆盖；终端壳只负责喂字节）.

    Incremental UTF-8 decode + CSI arrow-sequence grouping. ``feed`` returns
    the COMPLETE keys recognizable so far; a trailing lone ``\\x1b`` (or
    ``\\x1b[`` prefix) is held back — it may be the start of an arrow sequence
    whose tail hasn't arrived. The shell calls ``flush`` after a short quiet
    gap so a bare Esc keypress is still delivered as Esc.
    """

    def __init__(self) -> None:
        import codecs

        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""

    def feed(self, data: bytes) -> list[str]:
        self._pending += self._decoder.decode(data)
        out: list[str] = []
        while self._pending:
            ch = self._pending[0]
            if ch == "\x1b":
                # Parse a COMPLETE CSI / SS3 sequence by its grammar instead of
                # hard-cutting 3 bytes (#585): a 3-byte cut mis-splits any
                # parameterised sequence — Delete `\x1b[3~`, PageUp `\x1b[5~`,
                # Ctrl+方向键 `\x1b[1;5A` — leaving the tail (`~`/`;`/digits) to
                # pollute rename input or mis-fire normal-mode actions.
                m = _CSI_SEQ_RE.match(self._pending)
                if m:
                    out.append(m.group(0))
                    self._pending = self._pending[m.end() :]
                    continue
                # Lone Esc or an INCOMPLETE sequence (tail not yet read): hold —
                # the next read may complete it. flush() delivers a bare Esc.
                break
            out.append(ch)
            self._pending = self._pending[1:]
        return out

    def flush(self) -> list[str]:
        """Deliver any held-back prefix as literal keys (bare Esc path)."""
        out = list(self._pending)
        self._pending = ""
        return out


def _read_keys(on_key, should_stop) -> None:  # type: ignore[no-untyped-def]
    """Raw-mode key reader (blocking thread).

    Reads the RAW fd with ``os.read`` — never ``sys.stdin.read``: mixing
    ``select`` with Python's *buffered* text stdin is the classic lost-input
    bug (the first read drains every pending byte into Python's internal
    buffer, after which ``select`` reports the fd idle while typed/pasted
    characters sit unread in the buffer forever — 实测粘贴中文只进第一个字)。
    Decoding/grouping lives in :class:`KeyDecoder`; a short quiet gap after a
    held ``\\x1b`` flushes it as a bare Esc.
    """
    import os
    import select
    import sys
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    decoder = KeyDecoder()
    try:
        tty.setcbreak(fd)
        while not should_stop():
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                for ch in decoder.flush():  # quiet gap → held Esc is a real Esc
                    on_key(ch)
                continue
            data = os.read(fd, 1024)
            if not data:
                continue
            for ch in decoder.feed(data):
                on_key(ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def run(*, day: str = "") -> None:
    """挂终端跑：Rich Live 渲染 + 后台键线程，q 退出."""
    import queue
    import threading

    from rich.live import Live

    controller = ReviewController(day=day)
    keys: queue.Queue[str] = queue.Queue()
    reader = threading.Thread(
        target=_read_keys,
        args=(keys.put, lambda: controller.quit),
        daemon=True,
    )
    reader.start()

    console = Console()
    last_refresh = datetime.now()
    with Live(controller.render(), console=console, screen=True, refresh_per_second=8) as live:
        while not controller.quit:
            try:
                ch = keys.get(timeout=0.25)
            except queue.Empty:
                ch = ""
            try:
                if ch:
                    controller.handle_key(ch)
                    last_refresh = datetime.now()
                elif (
                    controller.mode == "normal"
                    and (datetime.now() - last_refresh).total_seconds() > _REFRESH_SECONDS
                ):
                    # Don't auto-reload while the user is mid-interaction (rename /
                    # merge) — a re-sort under their feet corrupts the in-progress
                    # action (#572). reload still re-resolves merge_source by id as
                    # a belt-and-braces guard for any explicit reload.
                    controller.reload()
                    last_refresh = datetime.now()
            except sqlite3.OperationalError as exc:
                # daemon 写锁竞争等瞬态错误：显示、不退出（挂着的面板不能崩）。
                controller.status = f"DB busy（{exc}）— 稍后自动重试"
            live.update(controller.render())
