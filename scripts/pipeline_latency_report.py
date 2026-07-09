#!/usr/bin/env python3
"""Recognition-pipeline latency report (zero-dep, self-contained HTML).

Reconstructs the message→notification latency budget from what the daemon already
records (``captures`` / ``fast_path_ticks`` / ``intents``) and renders an HTML
report with inline-SVG charts (no matplotlib / no network):

  • Fast-path funnel — where each capture's gate-walk ended (the #622 outcomes).
  • Recognized-tick latency — capture_ts → tick-recorded (≈ OCR + parse + gates +
    LLM + sink). This is the LLM-inclusive cost; the cheap-gate latency baseline
    isolates the LLM contribution.
  • Trigger-gap — gaps between consecutive captures of a chat app while focused
    (the "wait for the next AX event" cost; a message landing while the app is
    already frontmost & idle is invisible to AX until the user acts).
  • Per-app recognized latency (WeChat vs Feishu vs …).
  • A representative end-to-end waterfall, with the segments that still need
    span-level instrumentation flagged.

Usage:
    uv run python scripts/pipeline_latency_report.py [--db PATH] [--out PATH] [--days N]

The phase split inside one capture (osascript / AX read / screenshot / OCR /
LLM) is NOT in the DB yet — those rows are marked "needs 埋点" until the
``pipeline_timings`` span instrumentation lands.
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics as st
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

CHAT_BUNDLES = {
    "com.tencent.xinWeChat": "WeChat",
    "com.tencent.WeWorkMac": "WeCom",
    "com.electron.lark": "Feishu",
    "com.electron.feishu": "Feishu",
    "com.bytedance.macos.lark": "Feishu",
}
# Outcomes that DID run the lean LLM vs the cheap gates that short-circuit before it.
LLM_OUTCOME = "recognized"
CHEAP_OUTCOMES = ("no_unseen", "non_user", "throttled", "cold_start", "not_allowed",
                  "no_anchor", "no_parser", "not_conversation", "empty")


def parse_ts(s: str | None) -> float | None:
    """Epoch seconds from an ISO string; naive strings are assumed local (+08:00 here)."""
    if not s:
        return None
    s = s.strip()
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.astimezone()  # interpret naive as local wall-clock
        return dt.timestamp()
    except Exception:
        return None


def pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(len(sorted_vals) * p))
    return sorted_vals[i]


# ---------- inline-SVG primitives (no deps) ----------

def svg_hbars(items: list[tuple[str, float]], unit: str = "", width: int = 560,
              color: str = "#4c78a8") -> str:
    """Horizontal bar chart. items = [(label, value)]."""
    if not items:
        return "<p><em>no data</em></p>"
    maxv = max(v for _, v in items) or 1.0
    row_h, lab_w, bar_w = 26, 150, width - 150 - 70
    h = row_h * len(items) + 10
    out = [f'<svg width="{width}" height="{h}" font-family="ui-monospace,monospace" font-size="12">']
    for idx, (lab, v) in enumerate(items):
        y = idx * row_h + 8
        bw = max(1, int(bar_w * v / maxv))
        out.append(f'<text x="0" y="{y+14}" fill="#333">{escape(str(lab))}</text>')
        out.append(f'<rect x="{lab_w}" y="{y}" width="{bw}" height="18" rx="3" fill="{color}"/>')
        vlabel = f"{v:.1f}{unit}" if isinstance(v, float) else f"{v}{unit}"
        out.append(f'<text x="{lab_w+bw+6}" y="{y+14}" fill="#555">{vlabel}</text>')
    out.append("</svg>")
    return "".join(out)


def svg_waterfall(stages: list[tuple[str, float, bool]], width: int = 720) -> str:
    """Cumulative waterfall. stages = [(label, seconds, measured?)]."""
    total = sum(s for _, s, _ in stages) or 1.0
    row_h, lab_w = 30, 230
    plot_w = width - lab_w - 80
    h = row_h * len(stages) + 14
    out = [f'<svg width="{width}" height="{h}" font-family="ui-monospace,monospace" font-size="12">']
    cum = 0.0
    for idx, (lab, sec, measured) in enumerate(stages):
        y = idx * row_h + 8
        x = lab_w + int(plot_w * cum / total)
        bw = max(2, int(plot_w * sec / total))
        fill = "#4c78a8" if measured else "#d0d0d0"
        stroke = "" if measured else ' stroke="#999" stroke-dasharray="3"'
        out.append(f'<text x="0" y="{y+15}" fill="#333">{escape(lab)}</text>')
        out.append(f'<rect x="{x}" y="{y}" width="{bw}" height="18" rx="3" fill="{fill}"{stroke}/>')
        tag = f"{sec:.1f}s" + ("" if measured else " ⚠埋点")
        out.append(f'<text x="{lab_w+plot_w+6}" y="{y+14}" fill="#555">{tag}</text>')
        cum += sec
    out.append("</svg>")
    return "".join(out)


def histogram(vals: list[float], edges: list[float], labels: list[str]) -> list[tuple[str, float]]:
    counts = [0] * len(labels)
    for v in vals:
        placed = False
        for i, e in enumerate(edges):
            if v < e:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    return list(zip(labels, [float(c) for c in counts], strict=False))


def stat_line(vals: list[float], unit: str = "s") -> str:
    if not vals:
        return "<em>no samples</em>"
    s = sorted(vals)
    return (f"n={len(s)} · p50={st.median(s):.1f}{unit} · "
            f"p90={pct(s,0.9):.1f}{unit} · p99={pct(s,0.99):.1f}{unit} · "
            f"max={max(s):.1f}{unit} · mean={st.mean(s):.1f}{unit}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path.home() / ".persome" / "chronicle" / "index.db"))
    ap.add_argument("--out", default="/tmp/persome-pipeline-latency.html")
    ap.add_argument("--days", type=int, default=3, help="look-back window")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    since = (datetime.now().astimezone() - timedelta(days=args.days)).isoformat()

    # --- funnel ---
    funnel = conn.execute(
        "SELECT outcome, COUNT(*) FROM fast_path_ticks WHERE ts>=? GROUP BY outcome ORDER BY 2 DESC",
        (since,),
    ).fetchall()

    # --- recognized-tick latency (LLM-inclusive) ---
    rec = conn.execute(
        "SELECT ts, created_at, bundle_id FROM fast_path_ticks WHERE outcome=? AND ts>=?",
        (LLM_OUTCOME, since),
    ).fetchall()
    rec_lat: list[float] = []
    per_app: dict[str, list[float]] = {}
    for ts, ca, bundle in rec:
        a, b = parse_ts(ts), parse_ts(ca)
        if a and b and b >= a:
            rec_lat.append(b - a)
            per_app.setdefault(CHAT_BUNDLES.get(bundle, bundle or "?"), []).append(b - a)

    # --- cheap-gate latency baseline (no LLM) ---
    qmarks = ",".join("?" * len(CHEAP_OUTCOMES))
    cheap = conn.execute(
        f"SELECT ts, created_at FROM fast_path_ticks WHERE outcome IN ({qmarks}) AND ts>=?",
        (*CHEAP_OUTCOMES, since),
    ).fetchall()
    cheap_lat = [b - a for ts, ca in cheap
                 if (a := parse_ts(ts)) and (b := parse_ts(ca)) and b >= a]

    # --- trigger gaps for chat apps (consecutive captures while focused) ---
    gaps: dict[str, list[float]] = {}
    for bundle, name in {b: n for b, n in CHAT_BUNDLES.items()}.items():
        rows = conn.execute(
            "SELECT ts FROM fast_path_ticks WHERE bundle_id=? AND ts>=? ORDER BY ts",
            (bundle, since),
        ).fetchall()
        seq = [parse_ts(r[0]) for r in rows]
        seq = [x for x in seq if x]
        g = [seq[i] - seq[i - 1] for i in range(1, len(seq)) if 0 < seq[i] - seq[i - 1] < 600]
        if g:
            gaps.setdefault(name, []).extend(g)

    # --- assemble HTML ---
    H: list[str] = []
    H.append("""<!doctype html><meta charset=utf-8>
<title>Persome 识别链路延迟报告</title>
<style>
 body{font:14px/1.5 ui-sans-serif,system-ui;margin:32px;max-width:840px;color:#222}
 h1{font-size:22px} h2{font-size:17px;margin-top:32px;border-bottom:2px solid #eee;padding-bottom:4px}
 .stat{background:#f6f8fa;padding:8px 12px;border-radius:6px;font:12px ui-monospace,monospace;margin:8px 0}
 .red{color:#c0392b;font-weight:600} .note{color:#666;font-size:13px}
 table{border-collapse:collapse;margin:12px 0} td,th{border:1px solid #ddd;padding:5px 10px;text-align:left}
 .legend{font-size:12px;color:#888} .warn{background:#fff4e5;border-left:4px solid #f39c12;padding:8px 12px;border-radius:4px;margin:10px 0}
</style>""")
    H.append("<h1>Persome 识别链路延迟报告</h1>")
    H.append(f'<p class=note>数据库 <code>{escape(args.db)}</code> · 近 {args.days} 天 · '
             f'生成于 {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>')

    # Funnel
    H.append("<h2>① 快路漏斗（每次 capture 的 gate 走到哪）</h2>")
    H.append(svg_hbars([(o, float(n)) for o, n in funnel], color="#54a24b"))
    H.append('<p class=legend>recognized = 真跑了 LLM 并产出意图；其余是廉价闸提前短路（不调 LLM）。</p>')

    # Recognized latency — the headline
    H.append("<h2>② 快路 recognized 延迟（capture → 识别完成，含 OCR+LLM）<span class=red> ← 主瓶颈</span></h2>")
    H.append(f'<div class=stat>{stat_line(rec_lat)}</div>')
    edges = [1, 3, 6, 12, 20, 30]
    labels = ["<1s", "1-3s", "3-6s", "6-12s", "12-20s", "20-30s", ">30s"]
    H.append(svg_hbars(histogram(rec_lat, edges, labels), unit=" 次", color="#c0392b"))
    H.append(f'<p class=note>对照 · 廉价闸（不含 LLM）：<code>{stat_line(cheap_lat)}</code> —— '
             f'两者差值 ≈ <span class=red>LLM 调用本身的耗时</span>。</p>')

    # Per-app
    H.append("<h2>③ 分应用 recognized 延迟</h2>")
    H.append(svg_hbars([(f"{k} (p50)", st.median(v)) for k, v in
                        sorted(per_app.items(), key=lambda kv: -st.median(kv[1]))],
                       unit="s", color="#4c78a8"))

    # Trigger gaps
    H.append("<h2>④ 触发缺口（chat app 相邻 capture 间隔）</h2>")
    H.append('<p class=note>消息列表不暴露 AX，新消息到达本身不触发 capture；要等下一次用户动作（点击/切窗/打字）。'
             '若消息到达时 app 已在前台且你没动，这段就是纯等待。</p>')
    for name, g in gaps.items():
        H.append(f'<div class=stat>{escape(name)}: {stat_line(g)}</div>')
        H.append(svg_hbars(histogram(g, [3, 10, 30, 60, 120], ["<3s", "3-10s", "10-30s", "30-60s", "60-120s", ">120s"]),
                           unit=" 次", color="#9467bd"))

    # Waterfall
    H.append("<h2>⑤ 代表性端到端瀑布（一条切入微信看到的消息）</h2>")
    p50_rec = st.median(rec_lat) if rec_lat else 11.0
    p50_cheap = st.median(cheap_lat) if cheap_lat else 1.0
    llm_est = max(0.0, p50_rec - p50_cheap)
    stages = [
        ("触发缺口 (切入app即触发→~0)", 0.5, False),
        ("capture: osascript前台检测", 0.3, False),
        ("capture: AX读+截图", 0.4, False),
        ("OCR (微信窗口)", 1.0, False),
        ("快路 LLM (lean recognizer)", llm_est, True),
        ("sink 持久化", 0.2, False),
        ("SSE→app→原生通知", 0.1, False),
    ]
    H.append(svg_waterfall(stages))
    H.append('<p class=legend>蓝=已实测（来自 fast_path_ticks 差值）；灰虚线=估计值，待 span 埋点确认。</p>')

    # Recommendations
    H.append("<h2>⑥ 优化方向（按实测贡献排序）</h2>")
    H.append(f"""<table>
<tr><th>环节</th><th>实测</th><th>动作</th></tr>
<tr><td class=red>快路 LLM</td><td>p50 {p50_rec:.0f}s / p90 {pct(sorted(rec_lat),0.9):.0f}s</td>
<td>砍 prompt（OCR 整屏文本巨大→截断/只取最近 N 行）· prompt cache · 换更快模型/直连</td></tr>
<tr><td>触发缺口</td><td>见 ④</td><td>chat app 加轻量 heartbeat capture（已在前台时定期补拍）</td></tr>
<tr><td>OCR</td><td>未实测</td><td>补埋点；裁剪到聊天区域再 OCR</td></tr>
<tr><td>osascript 前台</td><td>偶发卡 5s</td><td>换原生 API / 降超时</td></tr>
</table>""")
    H.append('<div class=warn>⚠ 单 capture 内 osascript/AX/截图/OCR/LLM 的精确拆分尚无埋点 —— '
             '下一步加 <code>pipeline_timings</code> span 后本报告⑤瀑布将全部变实测。</div>')

    Path(args.out).write_text("".join(H), encoding="utf-8")
    print(f"wrote {args.out}")
    print(f"  recognized latency: {stat_line(rec_lat)}")
    print(f"  cheap-gate latency: {stat_line(cheap_lat)}")
    print(f"  est. LLM contribution (p50): {llm_est:.1f}s")


if __name__ == "__main__":
    main()
