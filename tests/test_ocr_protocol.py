"""Tests for the daemon↔OCR-worker wire protocol (capture/ocr_protocol.py).

Pure, hermetic — no subprocess, no paddle. Covers frame + request/response round-trips
and the EOF/short-read fail-open behavior the parent relies on to detect a dead worker.
"""

from __future__ import annotations

import io

from persome.capture import ocr_protocol as p


class TestFrame:
    def test_round_trip(self) -> None:
        buf = io.BytesIO()
        p.write_frame(buf, b"hello world")
        buf.seek(0)
        assert p.read_frame(buf) == b"hello world"

    def test_empty_body(self) -> None:
        buf = io.BytesIO()
        p.write_frame(buf, b"")
        buf.seek(0)
        assert p.read_frame(buf) == b""

    def test_eof_returns_none(self) -> None:
        assert p.read_frame(io.BytesIO(b"")) is None

    def test_truncated_header_returns_none(self) -> None:
        assert p.read_frame(io.BytesIO(b"\x00\x00")) is None

    def test_truncated_body_returns_none(self) -> None:
        # length says 10 bytes, only 3 present → short read → None (worker died mid-frame)
        assert p.read_frame(io.BytesIO(b"\x00\x00\x00\x0aabc")) is None

    def test_two_frames_back_to_back(self) -> None:
        buf = io.BytesIO()
        p.write_frame(buf, b"one")
        p.write_frame(buf, b"two")
        buf.seek(0)
        assert p.read_frame(buf) == b"one"
        assert p.read_frame(buf) == b"two"
        assert p.read_frame(buf) is None


class TestRequest:
    def test_round_trip(self) -> None:
        body = p.encode_request("tiny", b"\xff\xd8\xff\x00image")
        tier, image = p.decode_request(body)
        assert tier == "tiny"
        assert image == b"\xff\xd8\xff\x00image"

    def test_warm_request_empty_image(self) -> None:
        tier, image = p.decode_request(p.encode_request("small", b""))
        assert tier == "small"
        assert image == b""

    def test_unicode_tier(self) -> None:
        tier, image = p.decode_request(p.encode_request("tiny", b"x"))
        assert tier == "tiny"


class TestResponse:
    def test_round_trip(self) -> None:
        result = (["你好", "world"], [[1, 2, 3, 4], [5, 6, 7, 8]], [0.9, 0.8])
        decoded = p.decode_response(p.encode_response(result))
        assert decoded == result

    def test_none_result(self) -> None:
        assert p.decode_response(p.encode_response(None)) is None

    def test_empty_body_is_none(self) -> None:
        assert p.decode_response(b"") is None
        assert p.decode_response(None) is None

    def test_garbage_is_none(self) -> None:
        assert p.decode_response(b"not json") is None

    def test_ok_false_is_none(self) -> None:
        assert p.decode_response(b'{"ok": false}') is None

    def test_degraded_boxes_scores_are_coerced(self) -> None:
        # A worker that returns texts but malformed geometry still yields aligned lists.
        body = b'{"ok": true, "texts": ["a"], "boxes": [[0]], "scores": ["x"]}'
        texts, boxes, scores = p.decode_response(body)
        assert texts == ["a"]
        assert boxes == [[0, 0, 0, 0]]
        assert scores == [0.0]
