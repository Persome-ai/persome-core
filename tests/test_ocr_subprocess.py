"""Tests for the isolated OCR worker client (capture/ocr_subprocess.py).

These are the crash-containment oracle. They drive REAL subprocesses (via ``python -c``
fake workers that speak ``ocr_protocol``) — including one that raises a genuine SIGSEGV —
and assert the parent (this test process) survives, fails open (``None``), and respawns.
No paddle, hermetic, default gate.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from persome.capture import ocr_local, ocr_subprocess


@pytest.fixture(autouse=True)
def _fake_paddle_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the paddle runtime probe to True (routing tests stub the worker/client;
    hosts without paddle wheels would short-circuit before the routing under test)."""
    monkeypatch.setattr(ocr_local, "_runtime_available", True)


# ─── fake workers (real subprocesses speaking the protocol) ──────────────────

# Echoes a deterministic result derived from the request tier; loops until stdin EOF.
_ECHO = """
import sys
from persome.capture import ocr_protocol as p
si, so = sys.stdin.buffer, sys.stdout.buffer
while True:
    body = p.read_frame(si)
    if body is None:
        break
    tier, image = p.decode_request(body)
    if not image:
        p.write_frame(so, p.encode_response(([], [], [])))
    else:
        p.write_frame(so, p.encode_response(([f"ok:{tier}"], [[1, 2, 3, 4]], [0.9])))
"""

# Reads one request, then dies with a genuine SIGSEGV (native fault, like paddle).
_CRASH = """
import os, signal, sys
from persome.capture import ocr_protocol as p
p.read_frame(sys.stdin.buffer)
os.kill(os.getpid(), signal.SIGSEGV)
"""

# Reads one request, then hangs forever (exercises the recv deadline).
_HANG = """
import sys, time
from persome.capture import ocr_protocol as p
p.read_frame(sys.stdin.buffer)
time.sleep(3600)
"""

_JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def _spawner(script: str):
    def _spawn() -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            close_fds=True,
        )

    return _spawn


class TestRoundTrip:
    def test_recognize_returns_worker_result(self) -> None:
        client = ocr_subprocess.OCRWorkerClient(spawn=_spawner(_ECHO), timeout=10)
        try:
            assert client.recognize_detailed(_JPEG, "tiny") == (["ok:tiny"], [[1, 2, 3, 4]], [0.9])
        finally:
            client.shutdown()

    def test_worker_is_reused_across_calls(self) -> None:
        client = ocr_subprocess.OCRWorkerClient(spawn=_spawner(_ECHO), timeout=10)
        try:
            client.recognize_detailed(_JPEG, "tiny")
            pid1 = client._proc.pid
            client.recognize_detailed(_JPEG, "small")
            pid2 = client._proc.pid
            assert pid1 == pid2  # one persistent worker, not respawned per call
        finally:
            client.shutdown()

    def test_empty_image_returns_none_without_spawn(self) -> None:
        client = ocr_subprocess.OCRWorkerClient(spawn=_spawner(_ECHO), timeout=10)
        try:
            assert client.recognize_detailed(b"", "tiny") is None
            assert client._proc is None  # no worker spawned for an empty image
        finally:
            client.shutdown()


class TestCrashContainment:
    def test_worker_sigsegv_fails_open_and_respawns(self) -> None:
        """The load-bearing scenario: a worker SIGSEGV must not touch the parent.

        First call hits a worker that segfaults → returns None (fail-open). The parent
        (this test process) is obviously still alive to run the assertions. The next call
        respawns a fresh worker and succeeds.
        """
        scripts = [_CRASH, _ECHO]

        def _spawn() -> subprocess.Popen:
            script = scripts.pop(0) if scripts else _ECHO
            return _spawner(script)()

        client = ocr_subprocess.OCRWorkerClient(spawn=_spawn, timeout=10)
        try:
            assert client.recognize_detailed(_JPEG, "tiny") is None  # crash → fail open
            # respawn: fresh worker handles the next request normally
            assert client.recognize_detailed(_JPEG, "tiny") == (["ok:tiny"], [[1, 2, 3, 4]], [0.9])
        finally:
            client.shutdown()

    def test_hang_times_out_then_respawns(self) -> None:
        scripts = [_HANG, _ECHO]

        def _spawn() -> subprocess.Popen:
            script = scripts.pop(0) if scripts else _ECHO
            return _spawner(script)()

        client = ocr_subprocess.OCRWorkerClient(spawn=_spawn, timeout=0.5)
        try:
            assert client.recognize_detailed(_JPEG, "tiny") is None  # deadline → fail open
            assert client.recognize_detailed(_JPEG, "tiny") == (["ok:tiny"], [[1, 2, 3, 4]], [0.9])
        finally:
            client.shutdown()

    def test_spawn_failure_returns_none(self) -> None:
        def _boom() -> subprocess.Popen:
            raise OSError("cannot spawn")

        client = ocr_subprocess.OCRWorkerClient(spawn=_boom, timeout=1)
        assert client.recognize_detailed(_JPEG, "tiny") is None
        assert client.warm("tiny") is None  # warm also fails open, never raises


class TestWarm:
    def test_warm_spawns_worker(self) -> None:
        client = ocr_subprocess.OCRWorkerClient(spawn=_spawner(_ECHO), timeout=10)
        try:
            client.warm("tiny")
            assert client._proc is not None and client._proc.poll() is None
        finally:
            client.shutdown()

    def test_cold_start_has_a_separate_timeout(self) -> None:
        client = ocr_subprocess.OCRWorkerClient(timeout=20, startup_timeout=120)
        assert client._timeout == 20
        assert client._startup_timeout == 120


class TestRouting:
    """`ocr_local` routes to the isolated client by default; env hatches change that."""

    def test_default_routes_to_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERSOME_OCR_IN_PROCESS", raising=False)
        monkeypatch.delenv("PERSOME_OCR_WORKER", raising=False)
        monkeypatch.delenv("PERSOME_DISABLE_OCR", raising=False)
        calls: list[tuple[bytes, str]] = []

        class _FakeClient:
            def recognize_detailed(self, image, tier):
                calls.append((image, tier))
                return (["routed"], [[0, 0, 0, 0]], [1.0])

        monkeypatch.setattr(ocr_subprocess, "get_client", lambda: _FakeClient())
        assert ocr_local.recognize_detailed(_JPEG, "tiny") == (["routed"], [[0, 0, 0, 0]], [1.0])
        assert calls == [(_JPEG, "tiny")]

    def test_disabled_short_circuits_before_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSOME_DISABLE_OCR", "1")

        def _boom():
            raise AssertionError("client must not be built when OCR is disabled")

        monkeypatch.setattr(ocr_subprocess, "get_client", _boom)
        assert ocr_local.recognize_detailed(_JPEG, "tiny") is None

    def test_in_process_hatch_bypasses_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERSOME_DISABLE_OCR", raising=False)
        monkeypatch.setenv("PERSOME_OCR_IN_PROCESS", "1")

        def _boom():
            raise AssertionError("client must not be used on the in-process hatch")

        monkeypatch.setattr(ocr_subprocess, "get_client", _boom)
        monkeypatch.setattr(
            ocr_local, "_recognize_detailed_inproc", lambda img, tier: (["inproc"], [], [])
        )
        assert ocr_local.recognize_detailed(_JPEG, "tiny") == (["inproc"], [], [])

    def test_worker_env_forces_in_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Inside the worker, a routed call must resolve in-proc (no recursive spawn).
        monkeypatch.delenv("PERSOME_OCR_IN_PROCESS", raising=False)
        monkeypatch.setenv("PERSOME_OCR_WORKER", "1")
        assert ocr_local._use_isolation() is False


@pytest.mark.integration
def test_end_to_end_isolated_recognition() -> None:
    """Real chain: default spawn (`python -m persome.cli _ocr-worker`) → paddle IN the worker.

    Exercises the CLI subcommand + worker serve loop + protocol + real PP-OCRv6 inference,
    proving the daemon never touches paddle yet still gets OCR text back. Deselected from
    the default gate (needs the bundled model weights); runs in the nightly eval / manually.
    """
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (240, 80), (255, 255, 255))
    ImageDraw.Draw(img).text((10, 25), "HELLO WORLD 12345", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    client = ocr_subprocess.OCRWorkerClient(timeout=120)  # default spawn = real worker
    try:
        result = client.recognize_detailed(buf.getvalue(), "tiny")
        assert result is not None and result[0], "isolated worker returned no text"
        assert any("HELLO" in t.upper() for t in result[0])
    finally:
        client.shutdown()
