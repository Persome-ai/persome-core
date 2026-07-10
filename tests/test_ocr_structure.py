"""Tests for geometry-based OCR structuring (capture/ocr_structure.py).

Pure functions, zero LLM, offline — driven by constructed boxes and one
committed synthetic desktop fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

from persome.capture import ocr_structure

FIXTURES = Path(__file__).parent / "fixtures" / "ocr"
WECHAT = "com.tencent.xinWeChat"


def _fixture(name: str) -> dict:
    path = FIXTURES / name
    return json.loads(path.read_text(encoding="utf-8"))


# ─── constructed-input unit tests ──────────────────────────────────────────────


class TestWeChatStructuring:
    def _two_col(self):
        # left sidebar: contact + time on one row, preview below; right pane: a message
        texts = [
            "\u7f57",
            "14:48",
            "\u4ed6\u600e\u4e48\u6837\u4e86",
            "\u591a\u4ee3\u7406\u7f16\u6392\u795e\u5e93",
        ]
        boxes = [[80, 60, 130, 76], [280, 62, 332, 74], [80, 90, 200, 106], [800, 50, 900, 66]]
        scores = [0.97, 0.92, 0.95, 0.9]
        return texts, boxes, scores

    def test_extracts_contact_time_preview(self):
        t, b, s = self._two_col()
        st = ocr_structure.structure(t, b, s, bundle_id=WECHAT, img_w=960)
        chats = st["sidebar"]["chats"]
        assert chats == [
            {"contact": "\u7f57", "time": "14:48", "preview": "\u4ed6\u600e\u4e48\u6837\u4e86"}
        ]
        # right pane lands in conversation (v2: typed {name,text} lines), not sidebar
        conv_text = " ".join(ln["text"] for ln in st["conversation"]["lines"])
        assert "\u591a\u4ee3\u7406\u7f16\u6392\u795e\u5e93" in conv_text

    def test_drops_low_confidence_fragments(self):
        # a 'X' fragment at conf 0.3 must not appear anywhere
        t = ["\u7f57", "14:48", "X"]
        b = [[80, 60, 130, 76], [280, 62, 332, 74], [120, 60, 130, 72]]
        s = [0.97, 0.92, 0.30]
        st = ocr_structure.structure(t, b, s, bundle_id=WECHAT, img_w=960)
        md = ocr_structure.to_markdown(st)
        assert "X" not in md


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
        assert "Region 1" in md and "hello" in md


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


# ─── v2: conversation pane sender tagging + title extraction ────────────────────


class TestConversationSenders:
    def _chat(self):
        # title (top-left) + my right bubble + peer left bubble + a centered timestamp
        texts = [
            "\u6d4b\u8bd5\u8054\u7cfb\u4eba",
            "\u6211\u5728\u5199\u6587\u6863",
            "\u8bb0\u5f97\u8865\u4e0a\u9a8c\u8bc1\u6b65\u9aa4",
            "13:27",
            "\u5df2\u7ecf\u66f4\u65b0\u7ed3\u679c",
        ]
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
        assert conv["name"] == "\u6d4b\u8bd5\u8054\u7cfb\u4eba"
        # title must NOT appear as a message line
        assert all(ln["text"] != "\u6d4b\u8bd5\u8054\u7cfb\u4eba" for ln in conv["lines"])
        by_text = {ln["text"]: ln["name"] for ln in conv["lines"]}
        assert by_text["\u6211\u5728\u5199\u6587\u6863"] == "self"
        assert by_text["\u8bb0\u5f97\u8865\u4e0a\u9a8c\u8bc1\u6b65\u9aa4"] == "counterpart"
        assert by_text["\u5df2\u7ecf\u66f4\u65b0\u7ed3\u679c"] == "self"
        assert by_text["13:27"] == "timeline"

    def test_pure_left_all_peer(self):

        t = ["\u6807\u9898", "\u4f60\u597d", "\u5728\u5417"]
        b = [[352, 20, 392, 36], [420, 60, 470, 76], [420, 90, 470, 106]]
        s = [0.95, 0.95, 0.95]
        st = ocr_structure.structure(t, b, s, bundle_id=WECHAT, img_w=960)
        names = [ln["name"] for ln in st["conversation"]["lines"]]
        assert names == ["counterpart", "counterpart"]
        assert st["conversation"]["name"] == "\u6807\u9898"

    def test_no_title_degrades(self):
        # top row is a right bubble (no left-aligned title) → name None, nothing crashes
        t = ["\u5728\u5417"]
        b = [[820, 20, 875, 36]]
        s = [0.95]
        st = ocr_structure.structure(t, b, s, bundle_id=WECHAT, img_w=960)
        assert st["conversation"]["name"] is None
        assert st["conversation"]["lines"] == [{"name": "self", "text": "\u5728\u5417"}]

    def test_empty_conversation_no_crash(self):
        # only a sidebar contact, nothing in the message pane
        t = ["\u7f57", "14:48"]
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
        assert conv["name"] == "\u6d4b\u8bd5\u8054\u7cfb\u4eba"
        by_text = {ln["text"]: ln["name"] for ln in conv["lines"]}
        # Spot-check sender tagging against synthetic desktop geometry.
        assert by_text.get("\u6211\u5728\u5199\u6587\u6863") == "self"
        assert by_text.get("\u8bb0\u5f97\u8865\u4e0a\u9a8c\u8bc1\u6b65\u9aa4") == "counterpart"
        assert by_text.get("13:27") == "timeline"
        # title lifted out of the message stream
        assert all(ln["text"] != "\u6d4b\u8bd5\u8054\u7cfb\u4eba" for ln in conv["lines"])
        assert all(isinstance(line, dict) for line in conv["lines"])


# ─── v3: adaptive sidebar↔conversation divider (window/sidebar-width robust) ─────


class TestAdaptiveDivider:
    """The sidebar width is a user-draggable variable. A fixed `330*scale` lost the whole
    chat list when dragged narrow / leaked conversation into the list when dragged wide
    in earlier builds. `_wechat_divider` adapts per image; these synthetic
    geometries guard both boundary directions.
    """

    def _nav(self, img_w):
        return ocr_structure._WECHAT_NAV * (img_w / 960)

    @staticmethod
    def _items_for_divider(divider: int) -> list[dict]:
        texts = [
            "\u4fa7\u680f\u4e00",
            "\u4fa7\u680f\u4e8c",
            "\u4fa7\u680f\u4e09",
            "\u4fa7\u680f\u56db",
            "\u5bf9\u8bdd\u5185\u5bb9",
        ]
        boxes = [[80, 40 + row * 30, divider - 20, 58 + row * 30] for row in range(4)] + [
            [divider + 20, 60, divider + 160, 80]
        ]
        return ocr_structure._items(texts, boxes, [0.99] * len(texts), 0.5)

    def test_divider_tracks_narrow_sidebar(self):
        div = ocr_structure._wechat_divider(self._items_for_divider(220), 960, self._nav(960))
        assert abs(div - 220) <= 2
        assert div < 300  # would have been ~335 under the old fixed rule

    def test_divider_tracks_wide_sidebar(self):
        div = ocr_structure._wechat_divider(self._items_for_divider(510), 960, self._nav(960))
        assert abs(div - 510) <= 2
        assert div > 450  # the fixed 330*scale (~335) would split inside the sidebar

    def test_divider_fail_open_on_single_column(self):
        # no clear gap (single dense column) → fall back to the fixed prior, never crash
        t = ["\u4e00", "\u4e8c", "\u4e09"]
        b = [[80, 60, 120, 76], [80, 90, 120, 106], [80, 120, 120, 136]]
        s = [0.9, 0.9, 0.9]
        items = ocr_structure._items(t, b, s, 0.5)
        div = ocr_structure._wechat_divider(items, 960, ocr_structure._WECHAT_NAV)
        assert div == ocr_structure._WECHAT_LIST  # fixed fallback (img_w==960 → scale 1)
