"""Tests for geometry-based OCR structuring (capture/ocr_structure.py).

Pure functions, zero LLM, offline — drive them with constructed boxes and with three
real WeChat OCR fixtures (tests/fixtures/ocr/*.json) captured on-device.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from persome.capture import ocr_structure

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"
WECHAT = "com.tencent.xinWeChat"


def _fixture(name: str) -> dict:
    path = FIXTURES / name
    if not path.exists():  # team-local real capture, not distributed on GitHub
        pytest.skip(f"team-local OCR fixture not present: ocr/{name}")
    return json.loads(path.read_text())


# ─── constructed-input unit tests ──────────────────────────────────────────────


class TestWeChatStructuring:
    def _two_col(self):
        # left sidebar: contact + time on one row, preview below; right pane: a message
        texts = ["罗", "14:48", "他怎么样了", "多代理编排神库"]
        boxes = [[80, 60, 130, 76], [280, 62, 332, 74], [80, 90, 200, 106], [800, 50, 900, 66]]
        scores = [0.97, 0.92, 0.95, 0.9]
        return texts, boxes, scores

    def test_extracts_contact_time_preview(self):
        t, b, s = self._two_col()
        st = ocr_structure.structure(t, b, s, bundle_id=WECHAT, img_w=960)
        chats = st["sidebar"]["chats"]
        assert chats == [{"contact": "罗", "time": "14:48", "preview": "他怎么样了"}]
        # right pane lands in conversation (v2: typed {name,text} lines), not sidebar
        conv_text = " ".join(ln["text"] for ln in st["conversation"]["lines"])
        assert "多代理编排神库" in conv_text

    def test_drops_low_confidence_fragments(self):
        # a 'X' fragment at conf 0.3 must not appear anywhere
        t = ["罗", "14:48", "X"]
        b = [[80, 60, 130, 76], [280, 62, 332, 74], [120, 60, 130, 72]]
        s = [0.97, 0.92, 0.30]
        st = ocr_structure.structure(t, b, s, bundle_id=WECHAT, img_w=960)
        md = ocr_structure.to_markdown(st)
        assert "X" not in md

    def test_quality_metrics(self):
        t, b, s = self._two_col()
        st = ocr_structure.structure(t, b, s, bundle_id=WECHAT, img_w=960)
        q = ocr_structure.quality(st)
        assert q["chats"] == 1
        assert q["time_ok"] == 1.0
        assert q["contact_clean"] == 1.0


class TestGenericFallback:
    def test_unknown_app_degrades_to_regions_no_fields(self):
        t = ["A", "B"]
        b = [[80, 60, 120, 76], [800, 60, 840, 76]]
        s = [0.9, 0.9]
        st = ocr_structure.structure(t, b, s, bundle_id="com.unknown.x", img_w=960)
        assert st["layout"] == "generic"
        assert "sidebar" not in st  # no guessed semantics
        assert len(st["regions"]) == 2  # x-clustered into two columns

    def test_generic_markdown_has_region_headers(self):
        t = ["hello", "world"]
        b = [[80, 60, 200, 76], [80, 90, 200, 106]]
        s = [0.9, 0.9]
        md = ocr_structure.to_markdown(ocr_structure.structure(t, b, s, bundle_id="x", img_w=960))
        assert "区域1" in md and "hello" in md


class TestFailOpen:
    def test_empty_input(self):
        assert ocr_structure.structure([], [], [], bundle_id=WECHAT) == {}

    def test_all_low_confidence(self):
        st = ocr_structure.structure(["x"], [[1, 1, 2, 2]], [0.1], bundle_id=WECHAT)
        assert st == {}

    def test_to_markdown_empty(self):
        assert ocr_structure.to_markdown({}) == ""

    def test_malformed_boxes_dont_raise(self):
        # boxes shorter than 4 are skipped, not crashed on
        st = ocr_structure.structure(["a"], [[1, 2]], [0.9], bundle_id=WECHAT)
        assert st == {}


# ─── real-fixture regression (the 25-sample experiment baseline) ───────────────


class TestRealWeChatFixtures:
    @pytest.mark.parametrize("name", ["wechat_0.json", "wechat_1.json"])
    def test_main_window_fields_high_quality(self, name):
        d = _fixture(name)
        st = ocr_structure.structure(
            d["texts"], d["boxes"], d["scores"], bundle_id=WECHAT, img_w=d["img_w"]
        )
        q = ocr_structure.quality(st)
        # baseline measured in the experiment: time/contact 100%, several chats parsed
        assert q["chats"] >= 8
        assert q["time_ok"] == 1.0, f"time field accuracy regressed: {q}"
        assert q["contact_clean"] == 1.0, f"contact cleanliness regressed: {q}"
        # the structured markdown is field-labeled and non-empty
        md = ocr_structure.to_markdown(st)
        assert "会话列表" in md and "联系人" in md

    def test_moments_small_window_no_crash(self):
        # 朋友圈 small window: no conversation sidebar — must degrade gracefully, not crash
        d = _fixture("wechat_2.json")
        st = ocr_structure.structure(
            d["texts"], d["boxes"], d["scores"], bundle_id=WECHAT, img_w=d["img_w"]
        )
        # may have zero chats (no list in 朋友圈) — that's correct, not a failure
        assert isinstance(st, dict)
        ocr_structure.to_markdown(st)  # must not raise


# ─── v2: conversation pane sender tagging + title extraction ────────────────────


class TestConversationSenders:
    def _chat(self):
        # title (top-left) + my right bubble + peer left bubble + a centered timestamp
        texts = ["测试联系人", "我在写论文", "记得补上复现实验", "13:27", "已经更新结果"]
        boxes = [
            [352, 20, 392, 36],  # title, top, left
            [806, 60, 875, 76],  # me (right)
            [425, 90, 497, 106],  # peer (left)
            [630, 120, 664, 136],  # timestamp (center)
            [833, 150, 875, 166],  # me (right)
        ]
        scores = [0.97, 0.95, 0.9, 1.0, 0.95]
        return texts, boxes, scores

    def test_title_extracted_and_senders_tagged(self):
        t, b, s = self._chat()
        st = ocr_structure.structure(t, b, s, bundle_id=WECHAT, img_w=960)
        conv = st["conversation"]
        assert conv["name"] == "测试联系人"
        # title must NOT appear as a message line
        assert all(ln["text"] != "测试联系人" for ln in conv["lines"])
        by_text = {ln["text"]: ln["name"] for ln in conv["lines"]}
        assert by_text["我在写论文"] == "我"
        assert by_text["记得补上复现实验"] == "对方"
        assert by_text["已经更新结果"] == "我"
        assert by_text["13:27"] == "timeline"

    def test_conversation_quality_metric(self):
        t, b, s = self._chat()
        st = ocr_structure.structure(t, b, s, bundle_id=WECHAT, img_w=960)
        q = ocr_structure.conversation_quality(st)
        assert q["name_extracted"] is True
        assert q["title_not_in_lines"] is True
        assert q["lines_typed"] == 1.0
        assert q["sender_coverage"] == 1.0  # every non-timeline line tagged 我/对方

    def test_pure_left_all_peer(self):
        # all left bubbles → all 对方
        t = ["标题", "你好", "在吗"]
        b = [[352, 20, 392, 36], [420, 60, 470, 76], [420, 90, 470, 106]]
        s = [0.95, 0.95, 0.95]
        st = ocr_structure.structure(t, b, s, bundle_id=WECHAT, img_w=960)
        names = [ln["name"] for ln in st["conversation"]["lines"]]
        assert names == ["对方", "对方"]
        assert st["conversation"]["name"] == "标题"

    def test_no_title_degrades(self):
        # top row is a right bubble (no left-aligned title) → name None, nothing crashes
        t = ["在吗"]
        b = [[820, 20, 875, 36]]
        s = [0.95]
        st = ocr_structure.structure(t, b, s, bundle_id=WECHAT, img_w=960)
        assert st["conversation"]["name"] is None
        assert st["conversation"]["lines"] == [{"name": "我", "text": "在吗"}]

    def test_empty_conversation_no_crash(self):
        # only a sidebar contact, nothing in the message pane
        t = ["罗", "14:48"]
        b = [[80, 60, 130, 76], [280, 62, 332, 74]]
        s = [0.97, 0.92]
        st = ocr_structure.structure(t, b, s, bundle_id=WECHAT, img_w=960)
        assert st["conversation"]["lines"] == []
        assert st["conversation"]["name"] is None

    def test_synthetic_chat_fixture(self):
        d = _fixture("wechat_chat.json")
        st = ocr_structure.structure(
            d["texts"], d["boxes"], d["scores"], bundle_id=WECHAT, img_w=d["img_w"]
        )
        conv = st["conversation"]
        assert conv["name"] == "测试联系人"
        by_text = {ln["text"]: ln["name"] for ln in conv["lines"]}
        # Spot-check sender tagging against synthetic desktop geometry.
        assert by_text.get("我在写论文") == "我"
        assert by_text.get("记得补上复现实验") == "对方"
        assert by_text.get("13:27") == "timeline"
        # title lifted out of the message stream
        assert all(ln["text"] != "测试联系人" for ln in conv["lines"])
        q = ocr_structure.conversation_quality(st)
        assert q["lines_typed"] == 1.0 and q["name_extracted"] and q["title_not_in_lines"]


# ─── v3: adaptive sidebar↔conversation divider (window/sidebar-width robust) ─────


class TestAdaptiveDivider:
    """The sidebar width is a user-draggable variable. A fixed `330*scale` lost the whole
    chat list when dragged narrow / leaked conversation into the list when dragged wide
    (ablation: baseline 3/10 samples 0-list). `_wechat_divider` adapts per-image; these
    guard the two failure modes against real narrow/wide-sidebar captures.
    """

    def _nav(self, img_w):
        return ocr_structure._WECHAT_NAV * (img_w / 960)

    def test_divider_tracks_narrow_sidebar(self):
        d = _fixture("wechat_narrow_sidebar.json")  # true divider ≈ 220 (dragged narrow)
        items = ocr_structure._items(d["texts"], d["boxes"], d["scores"], 0.5)
        div = ocr_structure._wechat_divider(items, d["img_w"], self._nav(d["img_w"]))
        # adaptive divider lands near truth, NOT at the fixed 330*scale (~335)
        assert abs(div - d["true_divider"]) <= 30, f"divider {div} far from {d['true_divider']}"
        assert div < 300  # would have been ~335 under the old fixed rule

    def test_divider_tracks_wide_sidebar(self):
        d = _fixture("wechat_wide_sidebar.json")  # true divider ≈ 510 (dragged wide)
        items = ocr_structure._items(d["texts"], d["boxes"], d["scores"], 0.5)
        div = ocr_structure._wechat_divider(items, d["img_w"], self._nav(d["img_w"]))
        assert abs(div - d["true_divider"]) <= 30, f"divider {div} far from {d['true_divider']}"
        assert div > 450  # the fixed 330*scale (~335) would split inside the sidebar

    def test_narrow_sidebar_chat_list_not_lost(self):
        # END-TO-END: the bug was 0 chats when the sidebar was dragged narrow.
        d = _fixture("wechat_narrow_sidebar.json")
        st = ocr_structure.structure(
            d["texts"], d["boxes"], d["scores"], bundle_id=WECHAT, img_w=d["img_w"]
        )
        chats = st["sidebar"]["chats"]
        assert len(chats) > 0, "narrow sidebar must still yield a chat list (was 0 under fixed)"
        q = ocr_structure.quality(st)
        assert q["time_ok"] == 1.0 and q["contact_clean"] == 1.0

    def test_wide_sidebar_no_conversation_leak(self):
        # END-TO-END: the bug was conversation lines leaking into the chat list as
        # over-long "contacts" when the sidebar was dragged wide.
        d = _fixture("wechat_wide_sidebar.json")
        st = ocr_structure.structure(
            d["texts"], d["boxes"], d["scores"], bundle_id=WECHAT, img_w=d["img_w"]
        )
        chats = st["sidebar"]["chats"]
        assert len(chats) > 0
        long_contacts = [c for c in chats if len(c.get("contact", "")) > 15]
        assert not long_contacts, f"conversation leaked into chat list: {long_contacts}"

    def test_divider_fail_open_on_single_column(self):
        # no clear gap (single dense column) → fall back to the fixed prior, never crash
        t = ["一", "二", "三"]
        b = [[80, 60, 120, 76], [80, 90, 120, 106], [80, 120, 120, 136]]
        s = [0.9, 0.9, 0.9]
        items = ocr_structure._items(t, b, s, 0.5)
        div = ocr_structure._wechat_divider(items, 960, ocr_structure._WECHAT_NAV)
        assert div == ocr_structure._WECHAT_LIST  # fixed fallback (img_w==960 → scale 1)
