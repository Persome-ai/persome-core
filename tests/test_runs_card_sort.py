"""Regression tests for /runs card sort key (issue #449).

agent cards carry tz-aware ISO strings (offset suffix); legacy dream cards may be
naive (no suffix). Sorting the merged list by the raw strings is lexicographic,
not by real instant — at the same wall-clock the naive string is a prefix of the
aware one and sorts before it regardless of real time. `_card_sort_key` parses +
tz-normalizes so the sort reflects real instants.
"""

from datetime import datetime, timedelta, timezone

from persome.api.runs_routes import _card_sort_key

_TZ = timezone(timedelta(hours=8))


def test_naive_and_aware_same_wallclock_compare_equal():
    # 同一墙钟时刻、naive(dream) vs aware(agent)：解析后是相等的真实时刻。
    dream = {"started_at": "2026-06-10T08:00:00", "enqueued_at": "2026-06-10T08:00:00"}
    agent = {
        "started_at": "2026-06-10T08:00:00+08:00",
        "enqueued_at": "2026-06-10T08:00:00+08:00",
    }
    assert _card_sort_key(dream, _TZ) == _card_sort_key(agent, _TZ)
    # 反证：旧的裸字符串比较两者不等（naive 是 aware 的前缀，会被判 "<"）。
    assert dream["started_at"] != agent["started_at"]


def test_orders_by_real_instant_not_string():
    earlier = {"started_at": "2026-06-10T08:00:00", "enqueued_at": "2026-06-10T08:00:00"}
    later = {
        "started_at": "2026-06-10T09:00:00+08:00",
        "enqueued_at": "2026-06-10T09:00:00+08:00",
    }
    assert _card_sort_key(earlier, _TZ) < _card_sort_key(later, _TZ)


def test_microsecond_suffix_does_not_misorder_at_equal_seconds():
    # naive 无微秒，aware 带微秒：真实时刻 aware 略晚，key 必须反映（裸串里 naive 是前缀）。
    naive = {"started_at": "2026-06-10T08:00:00", "enqueued_at": "2026-06-10T08:00:00"}
    aware_us = {
        "started_at": "2026-06-10T08:00:00.123456+08:00",
        "enqueued_at": "2026-06-10T08:00:00.123456+08:00",
    }
    assert _card_sort_key(naive, _TZ) < _card_sort_key(aware_us, _TZ)


def test_falls_back_to_enqueued_when_not_started():
    queued = {"started_at": None, "enqueued_at": "2026-06-10T08:00:00+08:00"}
    assert _card_sort_key(queued, _TZ) == datetime(2026, 6, 10, 8, 0, 0, tzinfo=_TZ)
