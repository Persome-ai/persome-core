"""Parent-side client for the isolated OCR worker.

Owns one long-lived worker subprocess (``ocr_worker``) and drives it request→response
under a lock. This is where the crash containment lives: a worker that SIGSEGVs closes its
stdout, the client reads EOF, **fails open (returns ``None``)**, reaps the corpse, and
respawns on the next call. The daemon process itself never imports paddle/cv2, so a native
OCR fault can never take it down.

The subprocess isolates native OCR failures from the daemon on both architectures.
"""

from __future__ import annotations

import contextlib
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Literal

from ..logger import get
from . import ocr_protocol

logger = get("persome.capture.ocr.client")

Spawn = Callable[[], subprocess.Popen]
WorkerState = Literal["not_started", "warming", "ready", "failed"]
StateListener = Callable[[WorkerState], None]

_LEN = struct.Struct(">I")
_KILL_WAIT = 2.0  # seconds to wait for a killed worker to reap


def _default_spawn() -> subprocess.Popen:
    """Spawn the OCR worker: the same frozen binary re-invoked with the hidden subcommand.

    Frozen (shipped ``Persome Backend``): ``Persome Backend _ocr-worker``. Dev (source): ``python
    -m persome.cli _ocr-worker``. The worker env carries ``PERSOME_OCR_WORKER=1`` so any
    routed OCR call inside it resolves in-proc (a worker never spawns a worker).
    """
    if getattr(sys, "frozen", False):
        argv = [sys.executable, "_ocr-worker"]
    else:
        argv = [sys.executable, "-m", "persome.cli", "_ocr-worker"]
    env = {**os.environ, "PERSOME_OCR_WORKER": "1"}
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # worker logs go to file sinks; keep stdout the clean data channel
        env=env,
        bufsize=0,
        close_fds=True,
        # A worker may compile or invoke a native OCR helper. Give it a private
        # process group so timeout teardown also reaps those descendants.
        start_new_session=os.name == "posix",
    )


class OCRWorkerClient:
    """Manages one reused OCR worker subprocess. All methods are fail-open."""

    def __init__(
        self,
        spawn: Spawn | None = None,
        timeout: float = 20.0,
        startup_timeout: float | None = None,
    ) -> None:
        self._spawn = spawn or _default_spawn
        self._timeout = timeout
        self._startup_timeout = timeout if startup_timeout is None else startup_timeout
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        # The default worker owns a private POSIX process group. Record that
        # group while the leader is alive so cleanup can still kill native
        # descendants after the worker itself has crashed or exited.
        self._proc_group: int | None = None
        self._state: WorkerState = "not_started"
        self._state_lock = threading.Lock()

    # ─── public API (mirrors ocr_local) ──────────────────────────────────────

    def recognize_detailed(self, image_bytes: bytes, tier: str) -> ocr_protocol.Detailed | None:
        """OCR one image in the worker. Returns ``(texts, boxes, scores)`` or ``None``."""
        if not image_bytes:
            return None
        result = self._request(tier, image_bytes)
        if result is not None:
            self._set_state("ready")
        return result

    def warm(self, tier: str) -> bool:
        """Pre-build the worker engine and report whether it became ready."""
        self._set_state("warming")
        ready = self._request(tier, b"") is not None
        self._set_state("ready" if ready else "failed")
        return ready

    def state(self) -> WorkerState:
        """Return current process liveness without blocking an in-flight inference."""
        with self._state_lock:
            state = self._state
        proc = self._proc
        if state in {"warming", "ready"} and proc is not None and proc.poll() is not None:
            self._set_state("failed")
            return "failed"
        return state

    def shutdown(self) -> None:
        """Terminate the worker (best-effort). The worker also exits on stdin EOF."""
        with self._lock:
            self._kill_worker_locked()
            self._set_state("not_started")

    def _set_state(self, state: WorkerState) -> None:
        with self._state_lock:
            self._state = state
        with _state_listener_lock:
            listener = _state_listener
        if listener is not None:
            try:
                listener(state)
            except Exception as exc:  # noqa: BLE001 - health publication cannot break OCR
                logger.warning("OCR worker state listener failed: %s", exc)

    # ─── internals ────────────────────────────────────────────────────────────

    def _request(self, tier: str, image_bytes: bytes) -> ocr_protocol.Detailed | None:
        with self._lock:
            proc, just_spawned = self._ensure_worker_locked()
            if proc is None:
                return None
            try:
                self._send(proc, tier, image_bytes)
                timeout = self._startup_timeout if just_spawned else self._timeout
                body = self._recv_deadline(proc, time.monotonic() + timeout)
            except _WorkerGone as exc:
                logger.warning("ocr worker gone (%s); failing open, will respawn", exc)
                self._kill_worker_locked()
                self._set_state("failed")
                return None
            if body is None:  # EOF — worker died (e.g. SIGSEGV) mid-request
                logger.warning("ocr worker closed stdout mid-request; failing open, will respawn")
                self._kill_worker_locked()
                self._set_state("failed")
                return None
            # A complete framed response proves that the current worker process
            # is responsive. ``warm`` still converts an application-level
            # {ok:false} response back to ``failed`` below.
            self._set_state("ready")
            return ocr_protocol.decode_response(body)

    def _ensure_worker_locked(self) -> tuple[subprocess.Popen | None, bool]:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return proc, False
        if proc is not None:  # dead worker lingering — reap it
            self._kill_worker_locked()
        try:
            self._proc = self._spawn()
            self._proc_group = self._owned_process_group(self._proc)
            self._set_state("warming")
            logger.info("spawned ocr worker (pid=%s)", self._proc.pid)
            return self._proc, True
        except Exception as exc:  # noqa: BLE001
            logger.warning("ocr worker spawn failed: %s; OCR degrades to none", exc)
            self._proc = None
            self._proc_group = None
            self._set_state("failed")
            return None, False

    @staticmethod
    def _owned_process_group(proc: subprocess.Popen) -> int | None:
        """Return the worker's private POSIX process group, if it owns one."""
        if os.name != "posix":
            return None
        try:
            group = os.getpgid(proc.pid)
        except (AttributeError, OSError):
            return None
        return group if group == proc.pid else None

    def _send(self, proc: subprocess.Popen, tier: str, image_bytes: bytes) -> None:
        req = ocr_protocol.encode_request(tier, image_bytes)
        try:
            assert proc.stdin is not None
            proc.stdin.write(_LEN.pack(len(req)) + req)
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise _WorkerGone(f"send failed: {exc}") from exc

    def _recv_deadline(self, proc: subprocess.Popen, deadline: float) -> bytes | None:
        """Read one response frame from the worker's stdout, bounded by ``deadline``.

        Reads the raw fd (bypassing Python buffering) so ``select`` reflects real
        readiness. ``None`` = EOF (worker died); raises ``_WorkerGone`` on timeout.
        """
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        hdr = self._read_exact(fd, _LEN.size, deadline)
        if hdr is None:
            return None
        (n,) = _LEN.unpack(hdr)
        if n < 0 or n > ocr_protocol.MAX_FRAME:
            raise _WorkerGone(f"bad frame length {n}")
        if n == 0:
            return b""
        return self._read_exact(fd, n, deadline)

    @staticmethod
    def _read_exact(fd: int, n: int, deadline: float) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _WorkerGone("read timeout")
            r, _, _ = select.select([fd], [], [], remaining)
            if not r:
                raise _WorkerGone("read timeout")
            try:
                chunk = os.read(fd, n - len(buf))
            except OSError as exc:
                raise _WorkerGone(f"read error: {exc}") from exc
            if not chunk:  # EOF: worker's stdout closed (crash / exit)
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _kill_worker_locked(self) -> None:
        proc = self._proc
        group = self._proc_group
        self._proc = None
        self._proc_group = None
        if proc is None:
            return
        killed_group = False
        if os.name == "posix" and group is not None:
            try:
                # Do this even when the worker leader has already exited: its
                # compiler/Vision child may still be alive in the owned group.
                os.killpg(group, signal.SIGKILL)
                killed_group = True
            except ProcessLookupError:
                pass
            except OSError as exc:
                logger.warning("failed to kill OCR worker process group %s: %s", group, exc)
        if proc.poll() is None and not killed_group:
            with contextlib.suppress(Exception):
                proc.kill()
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
        with contextlib.suppress(Exception):
            proc.wait(timeout=_KILL_WAIT)


class _WorkerGone(Exception):
    """Raised internally when the worker pipe fails or times out."""


# Module singleton — one worker per daemon.
_client: OCRWorkerClient | None = None
_client_lock = threading.Lock()
_state_listener: StateListener | None = None
_state_listener_lock = threading.Lock()


def get_client() -> OCRWorkerClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = OCRWorkerClient(
                timeout=_timeout_from_env(),
                startup_timeout=_startup_timeout_from_env(),
            )
        return _client


def current_worker_state() -> WorkerState:
    """Inspect the existing singleton without creating an OCR subprocess."""
    with _client_lock:
        client = _client
    return "not_started" if client is None else client.state()


def set_state_listener(listener: StateListener | None) -> None:
    """Publish every worker transition to the daemon generation receipt."""

    global _state_listener
    with _state_listener_lock:
        _state_listener = listener
    if listener is not None:
        listener(current_worker_state())


def _timeout_from_env() -> float:
    try:
        return float(os.environ.get("PERSOME_OCR_WORKER_TIMEOUT", "20"))
    except ValueError:
        return 20.0


def _startup_timeout_from_env() -> float:
    try:
        # First-run Intel warm-up may compile the Swift helper (120s bound) and
        # then probe Vision (15s bound). Keep margin for process startup and I/O.
        return float(os.environ.get("PERSOME_OCR_WORKER_STARTUP_TIMEOUT", "180"))
    except ValueError:
        return 180.0
