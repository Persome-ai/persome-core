"""Recent documents and folder shape — what the person is actively touching.

Reads file *names, types, and mtimes only*. Never opens file contents.

Scale: scans only the top level of Desktop/Documents/Downloads (no recursion)
via ``os.scandir``, and keeps just the top-N most-recent files in a bounded
min-heap — so memory stays constant even if a folder holds 10k entries. We
never build a full sorted list of everything.
"""

from __future__ import annotations

import heapq
import os
import time
from typing import Any

from .base import Signal, collector, home, top_counts

_DIRS = ["Desktop", "Documents", "Downloads"]
_SKIP = {".DS_Store", ".localized"}
_RECENT_DAYS = 30
_TOP_RECENT = 20


@collector("documents", "最近文档", "documents")
def collect() -> list[Signal]:
    signals: list[Signal] = []
    now = time.time()
    cutoff = now - _RECENT_DAYS * 86400

    # Bounded min-heap of (mtime, name, area); never larger than _TOP_RECENT.
    heap: list[tuple[float, str, str]] = []
    recent_total = 0
    ext_counter: dict[str, int] = {}
    screenshots = 0

    for area in _DIRS:
        base = home() / area
        if not base.is_dir():
            continue
        try:
            scan = os.scandir(base)
        except OSError:
            continue
        with scan:
            for entry in scan:
                if entry.name in _SKIP or entry.name.startswith("."):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError:
                    continue
                if is_file:
                    ext = os.path.splitext(entry.name)[1].lower().lstrip(".") or "(无扩展名)"
                    ext_counter[ext] = ext_counter.get(ext, 0) + 1
                    lname = entry.name.lower()
                    if (
                        "screenshot" in lname
                        or lname.startswith("截屏")
                        or lname.startswith("cleanshot")
                    ):
                        screenshots += 1
                if st.st_mtime >= cutoff:
                    recent_total += 1
                    item = (st.st_mtime, entry.name, area)
                    if len(heap) < _TOP_RECENT:
                        heapq.heappush(heap, item)
                    elif item[0] > heap[0][0]:
                        heapq.heapreplace(heap, item)

    if not heap and not ext_counter:
        return []

    rows: list[dict[str, Any]] = []
    for mtime, name, area in sorted(heap, reverse=True):
        days = int((now - mtime) / 86400)
        when = "今天" if days == 0 else f"{days}天前"
        rows.append({"name": name, "count": 0, "detail": f"{area} · {when}"})
    if rows:
        signals.append(Signal(f"近{_RECENT_DAYS}天活跃文件", rows, f"{recent_total} 个"))

    if ext_counter:
        signals.append(Signal("文件类型分布", top_counts(ext_counter, 12)))

    if screenshots:
        signals.append(Signal("截图数", screenshots, "Desktop/Documents/Downloads 顶层"))

    return signals
