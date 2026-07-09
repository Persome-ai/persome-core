#!/usr/bin/env python3
"""Benchmark the fast-path LLM round-trip vs prompt length (real relay).

Calls the EXACT path the fast recognizer uses — ``writer.llm.call_llm(cfg,
"intent_recognizer", …, json_mode=True)`` → Anthropic SDK → ANTHROPIC_BASE_URL
relay → DeepSeek — so the numbers are the real hot-path cost, not a synthetic.

Measures:
  • round-trip FLOOR — a near-empty prompt (relay + model fixed cost).
  • INPUT scaling — same tiny output, body padded to N chars of realistic
    Chinese filler; the slope = per-char (≈ per-token) input cost.
  • CACHE effect — the same large (system+profile) prefix twice with
    cache_control; a faster 2nd call = prompt cache is working.

Needs a real key: `set -a; source ~/.persome/chronicle/env; set +a` first.
Each size runs --reps times; reports min / median (min = least jitter).

Usage:
    set -a; source ~/.persome/chronicle/env; set +a
    uv run python scripts/bench_llm_roundtrip.py [--reps 3]
"""

from __future__ import annotations

import argparse
import statistics as st
import time

from persome.config import load as load_config
from persome.writer import llm as llm_mod

# A realistic Chinese paragraph used as filler so token counts track char counts
# the way they will in production (visible_text is mostly Chinese).
_FILLER = (
    "今天上午和团队讨论了下个季度的产品规划，重点是把意图识别的延迟压下来，"
    "顺便对齐了一下数据管线的改造方案以及几个待办事项的优先级安排。"
)
_SYSTEM = "你是一个意图识别器。判断下面这段对话里是否有日程/会议意图，用 JSON 返回 {\"has_intent\": true/false}。"
_TASK = "\n\n对话：对方说『明天下午三点开个会过一下进度』。"


def _body(n_chars: int) -> str:
    if n_chars <= 0:
        return _TASK
    reps = (n_chars // len(_FILLER)) + 1
    return (_FILLER * reps)[:n_chars] + _TASK


def _time_call(cfg, messages, reps: int) -> list[float]:
    out: list[float] = []
    for _ in range(reps):
        t = time.monotonic()
        try:
            resp = llm_mod.call_llm(cfg, "intent_recognizer", messages=messages, json_mode=True)
            _ = llm_mod.extract_text(resp)
        except Exception as exc:  # noqa: BLE001
            print(f"    call failed: {exc}")
            continue
        out.append(time.monotonic() - t)
    return out


def _fmt(xs: list[float]) -> str:
    if not xs:
        return "—"
    return f"min={min(xs):.2f}s median={st.median(xs):.2f}s max={max(xs):.2f}s"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args()

    cfg = load_config()
    mc = cfg.model_for("intent_recognizer")
    fast = (cfg.intent_recognizer.fast_model or "").strip()
    print(f"model(intent_recognizer)={mc.model}  fast_model={fast or '(inherit)'}  "
          f"max_tokens={getattr(mc, 'max_tokens', '?')}")
    import os
    print(f"base_url={os.environ.get('ANTHROPIC_BASE_URL', '(default)')}")
    print(f"reps={args.reps}\n")

    print("── INPUT-length scaling (tiny output, json_mode) ──")
    print(f"{'body chars':>11} | latency")
    sizes = [0, 200, 1000, 4000, 10000, 20000]
    floor = None
    for n in sizes:
        msgs = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _body(n)}]
        xs = _time_call(cfg, msgs, args.reps)
        if n == 0 and xs:
            floor = min(xs)
        print(f"{n:>11} | {_fmt(xs)}")

    print("\n── CACHE effect (same big prefix twice) ──")
    big_system = _SYSTEM + "\n\n" + _body(12000)
    msgs_cached = [
        {"role": "system", "content": [{"type": "text", "text": big_system,
                                        "cache_control": {"type": "ephemeral"}}]},
        {"role": "user", "content": _TASK},
    ]
    c1 = _time_call(cfg, msgs_cached, 1)
    c2 = _time_call(cfg, msgs_cached, 1)
    print(f"  1st (cold cache write): {_fmt(c1)}")
    print(f"  2nd (warm cache read) : {_fmt(c2)}")

    if floor is not None:
        print(f"\n→ round-trip FLOOR (empty prompt): ~{floor:.2f}s "
              f"(relay + model fixed cost; nothing below this without a faster/closer model).")


if __name__ == "__main__":
    main()
