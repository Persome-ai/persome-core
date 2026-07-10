"""Unit tests for capture/window_screenshot.py."""

from __future__ import annotations

import io

import pytest
from PIL import Image


class TestGetFocusedWindowBounds:
    def test_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from persome.capture import window_screenshot as ws

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))

            class Proc:
                returncode = 0
                stdout = "10,20,300,400\n"

            return Proc()

        monkeypatch.setattr(ws.subprocess, "run", fake_run)
        bounds = ws.get_focused_window_bounds()
        assert bounds is not None
        assert bounds.x == 10
        assert bounds.y == 20
        assert bounds.w == 300
        assert bounds.h == 400
        assert calls[0][0] == ["osascript", "-e", ws._BOUNDS_SCRIPT]
        assert calls[0][1]["capture_output"] is True
        assert calls[0][1]["text"] is True
        assert calls[0][1]["timeout"] == 5

    def test_empty_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from persome.capture import window_screenshot as ws

        def fake_run(cmd, **kwargs):
            class Proc:
                returncode = 0
                stdout = "\n"

            return Proc()

        monkeypatch.setattr(ws.subprocess, "run", fake_run)
        assert ws.get_focused_window_bounds() is None

    def test_bad_returncode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from persome.capture import window_screenshot as ws

        def fake_run(cmd, **kwargs):
            class Proc:
                returncode = 1
                stdout = "10,20,300,400\n"

            return Proc()

        monkeypatch.setattr(ws.subprocess, "run", fake_run)
        assert ws.get_focused_window_bounds() is None

    def test_wrong_part_count(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from persome.capture import window_screenshot as ws

        def fake_run(cmd, **kwargs):
            class Proc:
                returncode = 0
                stdout = "10,20,300\n"

            return Proc()

        monkeypatch.setattr(ws.subprocess, "run", fake_run)
        assert ws.get_focused_window_bounds() is None

    def test_non_numeric_parts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from persome.capture import window_screenshot as ws

        def fake_run(cmd, **kwargs):
            class Proc:
                returncode = 0
                stdout = "a,b,c,d\n"

            return Proc()

        monkeypatch.setattr(ws.subprocess, "run", fake_run)
        assert ws.get_focused_window_bounds() is None

    def test_subprocess_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from persome.capture import window_screenshot as ws

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError()

        monkeypatch.setattr(ws.subprocess, "run", fake_run)
        assert ws.get_focused_window_bounds() is None


class TestGrabFocusedWindow:
    def test_no_mss(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from persome.capture import window_screenshot as ws

        monkeypatch.setattr(ws, "mss", None)
        assert ws.grab_focused_window() is None

    def test_no_bounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from persome.capture import window_screenshot as ws

        monkeypatch.setattr(ws, "get_focused_window_bounds", lambda: None)
        assert ws.grab_focused_window() is None

    def test_grab_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from persome.capture import window_screenshot as ws

        fake_img = Image.new("RGB", (10, 10), color="red")
        grab_calls = []

        class FakeGrab:
            size = (10, 10)
            rgb = fake_img.tobytes()

        class FakeSCT:
            def grab(self, monitor):
                grab_calls.append(monitor)
                return FakeGrab()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        class FakeMss:
            @staticmethod
            def mss():
                return FakeSCT()

        monkeypatch.setattr(ws, "mss", FakeMss())
        monkeypatch.setattr(
            ws, "get_focused_window_bounds", lambda: ws.WindowBounds(5, 10, 100, 200)
        )

        result = ws.grab_focused_window()
        assert result is not None
        assert result.size == (10, 10)
        assert grab_calls == [{"left": 5, "top": 10, "width": 100, "height": 200}]

    def test_grab_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from persome.capture import window_screenshot as ws

        def boom(*a, **k):
            raise RuntimeError("mss error")

        class FakeMss:
            def mss(self, *a, **k):
                return boom()

        monkeypatch.setattr(ws, "mss", FakeMss())
        monkeypatch.setattr(ws, "get_focused_window_bounds", lambda: ws.WindowBounds(0, 0, 10, 10))
        assert ws.grab_focused_window() is None


class TestPilToJpegBytes:
    def test_roundtrip(self) -> None:
        from persome.capture import window_screenshot as ws

        img = Image.new("RGB", (10, 10), color="blue")
        data = ws.pil_to_jpeg_bytes(img, quality=85)
        assert isinstance(data, bytes)
        assert len(data) > 0
        # Verify it is valid JPEG
        reloaded = Image.open(io.BytesIO(data))
        assert reloaded.format == "JPEG"

    def test_default_quality(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from persome.capture import window_screenshot as ws

        save_calls = []

        def fake_save(self, buf, **kwargs):
            save_calls.append(kwargs)

        monkeypatch.setattr(Image.Image, "save", fake_save)
        img = Image.new("RGB", (10, 10), color="blue")
        ws.pil_to_jpeg_bytes(img)
        assert save_calls[0]["quality"] == 85
        assert save_calls[0]["format"] == "JPEG"
        assert save_calls[0]["optimize"] is True
