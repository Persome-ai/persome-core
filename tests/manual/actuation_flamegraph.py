#!/usr/bin/env python3
"""Flame graph + per-step / per-state timing for an actuation trace.

The actuator's ``act`` / ``snapshot`` emit an internal ``timing`` block (ms): act → snap_before /
perform / snap_after; snapshot → snapshot_ms. A computer-use harness records per-step spans
(``{step, kind, name, wall_ms, internal}``) into a trace JSON; this turns that into a per-step
table, a per-state aggregation, a Brendan-Gregg folded-stacks file, and a flame-graph PNG.

    python3 tests/manual/actuation_flamegraph.py <trace.json> [out_prefix=/tmp/flame]

Finding from the live "open Tabbit → Gmail" run (deepseek-chat, 14 steps, 30.6 s): LLM latency 65%,
app activation 21% (mostly a cold Chrome launch), AX snapshots 11%, subprocess overhead 3% — the
actual actuation (clicks/keys) was 3 ms total (~0%). In LLM-driven computer use the cost is the model
thinking + app launches + perception, not the act itself.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

from PIL import Image, ImageDraw, ImageFont

CAT_COLOR = {
    "llm": (70, 130, 180),
    "ax_snapshot": (230, 150, 40),
    "perform": (60, 170, 80),
    "activate": (200, 70, 60),
    "overhead": (150, 150, 150),
    "snap_before": (245, 190, 90),
    "snap_after": (245, 170, 70),
    "root": (180, 180, 200),
}


def node(name: str, cat: str, value: float = 0.0, children: list | None = None) -> dict:
    return {"name": name, "cat": cat, "value": value, "children": children or []}


def rollup(n: dict) -> float:
    if n["children"]:
        n["value"] = sum(rollup(c) for c in n["children"])
    return n["value"]


def build(trace: list[dict]) -> tuple[dict, dict]:
    steps: dict[int, list] = defaultdict(list)
    for s in trace:
        steps[s["step"]].append(s)
    root = node("actuation computer-use", "root")
    state: dict[str, float] = defaultdict(float)
    for si in sorted(steps):
        stepn = node(f"step {si}", "root")
        for s in steps[si]:
            it = s.get("internal") or {}
            if s["kind"] == "llm":
                stepn["children"].append(node("llm", "llm", s["wall_ms"]))
                state["llm"] += s["wall_ms"]
            elif s["name"] == "activate":
                stepn["children"].append(node("activate", "activate", s["wall_ms"]))
                state["activate"] += s["wall_ms"]
            elif s["name"] == "snapshot":
                snap = it.get("snapshot_ms", 0.0)
                ov = max(0.0, s["wall_ms"] - snap)
                stepn["children"].append(node("snapshot", "ax_snapshot", children=[
                    node("ax_snapshot", "ax_snapshot", snap), node("overhead", "overhead", ov)]))
                state["ax_snapshot"] += snap
                state["overhead"] += ov
            else:  # act verb: click / key / type
                sb, pf, sa = (it.get(k, 0.0) for k in ("snap_before_ms", "perform_ms", "snap_after_ms"))
                ov = max(0.0, s["wall_ms"] - (sb + pf + sa))
                stepn["children"].append(node(s["name"], "perform", children=[
                    node("snap_before", "snap_before", sb), node("perform", "perform", pf),
                    node("snap_after", "snap_after", sa), node("overhead", "overhead", ov)]))
                state["ax_snapshot"] += sb + sa
                state["perform"] += pf
                state["overhead"] += ov
        rollup(stepn)
        root["children"].append(stepn)
    rollup(root)
    return root, state


def fold(n: dict, stack: list[str], out: list[str]) -> None:
    s = stack + [n["name"].replace(" ", "_").replace(";", "_")]
    if n["children"]:
        for c in n["children"]:
            fold(c, s, out)
    else:
        out.append(";".join(s) + f" {int(round(n['value']))}")


def render_png(root: dict, path: str) -> None:
    width, rh, pad = 1500, 26, 2

    def depth(n: dict) -> int:
        return 1 + max((depth(c) for c in n["children"]), default=0)

    h = depth(root) * rh + 20
    img = Image.new("RGB", (width, h), (255, 255, 255))
    dr = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 11)
    except OSError:
        font = ImageFont.load_default()
    scale = (width - 2) / root["value"] if root["value"] else 1.0

    def draw(n: dict, x: float, dd: int) -> None:
        w = n["value"] * scale
        y = h - (dd + 1) * rh
        x0, x1 = x + pad, max(x + w - pad, x + pad + 0.5)
        dr.rectangle([x0, y + pad, x1, y + rh - pad],
                     fill=CAT_COLOR.get(n["cat"], (180, 180, 200)), outline=(255, 255, 255))
        if w > 46:
            dr.text((x + 5, y + 6), f'{n["name"]} {n["value"]:.0f}ms'[: int(w / 6.5)],
                    fill=(20, 20, 20), font=font)
        cx = x
        for c in n["children"]:
            draw(c, cx, dd + 1)
            cx += c["value"] * scale

    draw(root, 1, 0)
    img.save(path)


def main() -> None:
    trace_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/dscu_trace.json"
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/flame"
    with open(trace_path) as fh:
        data = json.load(fh)
    root, state = build(data["trace"])

    print("=" * 60)
    print(f"TOTAL wall: {data['total_ms']:.0f} ms   (measured spans: {root['value']:.0f} ms)")
    print("\nPER-STEP:")
    for c in root["children"]:
        parts = ", ".join(f"{k['name']}={k['value']:.0f}" for k in c["children"])
        print(f"  {c['name']:<8} {c['value']:7.0f} ms   [{parts}]")
    print("\nPER-STATE:")
    tot = sum(state.values()) or 1.0
    for k, v in sorted(state.items(), key=lambda x: -x[1]):
        print(f"  {k:<13} {v:8.0f} ms   {100 * v / tot:5.1f}%")

    folded: list[str] = []
    fold(root, [], folded)
    with open(out + ".folded", "w") as fh:
        fh.write("\n".join(folded) + "\n")
    render_png(root, out + ".png")
    print(f"\nflame graph -> {out}.png   folded -> {out}.folded")


if __name__ == "__main__":
    main()
