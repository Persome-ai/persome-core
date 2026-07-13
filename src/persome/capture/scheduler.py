"""Capture scheduler: event-driven + heartbeat. Writes one JSON per tick to capture-buffer/."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import queue
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import paths
from ..config import CaptureConfig, Config
from ..logger import get
from ..store import fts as fts_store
from . import (
    ax_capture,
    cmux_source,
    ocr_local,
    ocr_structure,
    placeholder,
    s1_parser,
    screen_state,
    screenshot,
    screenshot_crypto,
    window_meta,
    window_screenshot,
)
from .event_dispatcher import EventDispatcher
from .timestamps import (
    capture_path_has_ambiguous_local_time,
    parse_capture_path_timestamp,
    parse_capture_timestamp,
)
from .watcher import AXWatcherProcess

logger = get("persome.capture")

# Frequency-limit cache for OCR fallback: key="bundle_id::title" -> last submit timestamp.
_last_ocr_ts: dict[str, float] = {}

# A far-future client timestamp sorts after the reducer watermark and used to
# evade retention indefinitely. Allow ordinary clock skew, not arbitrary time.
_MAX_INGEST_FUTURE_SKEW = timedelta(minutes=5)


def _should_trigger_ocr(cfg: CaptureConfig, meta: dict[str, str]) -> bool:
    if not cfg.enable_ocr_fallback:
        return False
    key = f"{meta.get('bundle_id', '')}::{meta.get('title', '')}"
    now = time.time()
    last = _last_ocr_ts.get(key, 0)
    if now - last < cfg.ocr_min_gap_seconds:
        return False
    _last_ocr_ts[key] = now
    return True


# The live capture runner created by `run_forever`, exposed so the
# `POST /captures/ingest` route (Swift-owned capture, `capture.source="ingest"`)
# funnels pushed payloads through the same content-dedup and session hook as the
# in-daemon capture loop.
# None when no capture task is running (CLI one-shots / tests / capture disabled).
_active_runner: _CaptureRunner | None = None


def _set_active_runner(runner: _CaptureRunner | None) -> None:
    global _active_runner
    _active_runner = runner


def capture_now() -> Path | None:
    """Force one capture through the live daemon runner for onboarding proof."""
    runner = _active_runner
    if runner is None:
        return None
    return runner.capture_now()


def active_runner_state(cfg: CaptureConfig) -> str:
    """Return a side-effect-free onboarding state for the live capture runner."""
    runner = _active_runner
    if runner is None:
        return "not-ready"
    gate = capture_gate_reason(cfg)
    if gate is not None:
        return gate
    if cfg.source == "ingest":
        return "ingest-ready"
    return "ready"


def _submit_ocr_async(
    image_bytes: bytes,
    capture_id: str,
    tier: str,
    window_meta: dict[str, str] | None = None,
    structured: bool = False,
    placeholder_values: tuple[str, ...] = (),
) -> None:
    """Fire-and-forget local OCR + geometry structuring. Runs on a daemon thread.

    Local PP-OCRv6 inference is synchronous (~0.5s) and runs entirely on-device, so
    there is no job table / polling loop — we recognize and backfill in one shot.
    Called from `_write_capture` AFTER the capture row is indexed, so the backfill
    `UPDATE … WHERE id=?` always finds its row.

    When `structured`, the raw OCR lines are reconstructed into field-labeled text via
    the per-app geometry structurer (`ocr_structure`) before backfill — fail-open to the
    raw join if structuring yields nothing.
    """
    from ..store import fts as fts_store

    meta = window_meta or {}
    detailed = ocr_local.recognize_detailed(image_bytes, tier)
    if not detailed:
        return
    texts, boxes, scores = detailed
    raw = "\n".join(texts)

    struct: dict = {}
    if structured:
        struct = ocr_structure.structure(
            texts,
            boxes,
            scores,
            bundle_id=meta.get("bundle_id", ""),
            app_name=meta.get("app_name", ""),
            img_w=_image_width(image_bytes),
        )
    backfill_text = ocr_structure.to_markdown(struct) if struct else ""
    if not backfill_text:
        backfill_text = raw
    backfill_text = placeholder.sanitize_ocr_text(backfill_text, placeholder_values)
    if not backfill_text:
        return

    try:
        with fts_store.cursor() as conn:
            fts_store.backfill_capture_ocr_text(conn, capture_id, backfill_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ocr backfill failed for %s: %s", capture_id, exc)


def _image_width(image_bytes: bytes) -> int:
    """Decode just the image width for the structurer's column scaling. 960 on failure."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            return im.width
    except Exception:  # noqa: BLE001
        return 960


def _now_iso() -> str:
    # Current filenames remain lexically monotonic in UTC; fixed-width
    # microseconds preserve the capture ID when multiple frames land per second.
    # Upgrade-time mixed formats are always compared through timestamps.py.
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _safe_filename(ts: str) -> str:
    return ts.replace(":", "-").replace("+", "p")


def _should_skip_capture(cfg: CaptureConfig) -> bool:
    """Pause / privacy gate shared by the daemon capture loop and the ingest path.

    Returns True (skip this capture) when the user paused capture or the screen is
    locked / asleep. Lock detection is fail-closed: an unknown state skips one
    capture rather than risking collection behind the login screen. Both checks
    default on.
    """
    return capture_gate_reason(cfg) is not None


def capture_gate_reason(cfg: CaptureConfig) -> str | None:
    """Return the active privacy gate, if any, without attempting a capture."""
    paths.ensure_dirs()
    if paths.paused_flag().exists():
        logger.info("capture skipped (paused)")
        return "paused"
    # Privacy guardrail (spec E7): don't collect behind the lock screen / asleep.
    if cfg.pause_on_lock and screen_state.is_screen_locked():
        logger.info("capture skipped (screen locked / asleep)")
        return "locked"
    return None


def _attach_screenshot(
    cfg: CaptureConfig,
    out: dict[str, Any],
    image_base64: str,
    mime_type: str,
    width: int,
    height: int,
) -> None:
    """Encrypt-at-rest (if enabled) then attach the screenshot to ``out``.

    Shared by the daemon grab path and the ingest path: Swift sends plaintext
    base64 and the at-rest encryption — when configured (spec E5 / TODO #6) —
    happens HERE, so the "screenshots are encrypted on disk" invariant holds
    regardless of who captured the pixels. Fail-closed: when encryption is
    required but no valid key is available, omit the persisted screenshot while
    preserving the capture's AX text and metadata.
    """
    screenshot_enc = False
    if cfg.encrypt_screenshots:
        key = screenshot_crypto.load_key()
        if key is not None:
            image_base64 = screenshot_crypto.encrypt(image_base64, key)
            screenshot_enc = True
        else:
            logger.error(
                "encrypt_screenshots is on but %s is unavailable; omitting screenshot",
                screenshot_crypto.KEY_ENV,
            )
            return
    out["screenshot"] = {
        "image_base64": image_base64,
        "mime_type": mime_type,
        "width": width,
        "height": height,
    }
    if screenshot_enc:
        out["screenshot"]["screenshot_enc"] = True


def _finalize_capture(
    cfg: CaptureConfig,
    out: dict[str, Any],
    *,
    ocr_jpeg_provider: Callable[[], bytes | None],
) -> dict[str, Any]:
    """Enrich + OCR-decision tail shared by the daemon capture loop and ingest.

    Runs on a capture dict whose header / window_meta / ax_tree / screenshot are
    already populated (by the daemon's OS grab, or by an ingest payload). Applies
    the secure-input privacy guard, S1 enrichment, cmux text injection, and the
    OCR fallback decision — sourcing the focused-window JPEG via
    ``ocr_jpeg_provider`` (the daemon grabs it on the spot; ingest decodes it from
    the pushed payload). Returns the (possibly secure-suppressed) capture dict.
    """
    # Privacy guardrail (spec E7): when the focused element is a secure text
    # field (a password box), this window must NOT produce a screenshot or AX
    # snapshot for this tick — drop both before any downstream enrichment / OCR
    # fallback runs. Read from the already-captured AX info (`out["ax_tree"]`),
    # so this is read-only. Fail-conservative — a positive secure signal wins.
    # Default on.
    if cfg.suppress_secure_input and screen_state.is_secure_input_active(out):
        meta_for_log = out.get("window_meta") or {}
        logger.info(
            "capture suppressed screenshot+AX (secure input focused): app=%r",
            meta_for_log.get("app_name"),
        )
        out.pop("screenshot", None)
        out.pop("ax_tree", None)
        out.pop("ax_metadata", None)
        out["secure_input_suppressed"] = True
        # No AX tree left to enrich from; mark it explicitly absent and skip the
        # OCR fallback below (which would re-grab a screenshot of this window).
        out["ax_unavailable"] = True
        out["focused_element"] = s1_parser.FocusedElement().to_dict()
        out["visible_text"] = ""
        out["url"] = None
        return out

    s1_parser.enrich(out)

    # cmux signal source (issue #558): GPU-rendered terminals expose ~no AX
    # text; when the frontmost app is cmux, append the real terminal text
    # read over its local socket RPC. Bounded + silent-degrade inside.
    cmux_injected = cmux_source.maybe_inject(out, cfg)

    # OCR fallback for AX-poor apps (WeChat, Feishu, etc.) — see the daemon-path
    # note in `_daemon_ocr_jpeg_provider`. The screenshot bytes come from the
    # provider (daemon grabs the focused window now; ingest decodes the JPEG the
    # Swift app already grabbed). Deferred OCR inference happens in `_write_capture`.
    visible_text = out.get("visible_text") or ""
    meta = out.get("window_meta") or {}
    has_ax_content = any(line.startswith("  ") for line in visible_text.split("\n"))
    if not cmux_injected and not has_ax_content and _should_trigger_ocr(cfg, meta):
        try:
            jpeg = ocr_jpeg_provider()
            if jpeg is not None:
                out["_ocr_pending_jpeg"] = jpeg
                out["_ocr_tier"] = cfg.ocr_tier
                out["_ocr_structured"] = cfg.ocr_structured
                # Clear the no-content AX header so the deferred OCR backfill
                # (writes only when visible_text is empty) takes over.
                out["visible_text"] = ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("ocr fallback screenshot failed: %s", exc)

    return out


def _daemon_ocr_jpeg_provider(cfg: CaptureConfig) -> Callable[[], bytes | None]:
    """OCR JPEG source for the daemon capture path: grab the focused window now.

    The screenshot is grabbed synchronously (while the window is still frontmost);
    the OCR inference + backfill are deferred until after the capture row is
    persisted (see `_write_capture`). Returns None when no focused window grab is
    possible (no Screen Recording grant / no window).
    """

    def _provide() -> bytes | None:
        pil_img = window_screenshot.grab_focused_window()
        if pil_img is None:
            return None
        return window_screenshot.pil_to_jpeg_bytes(pil_img, quality=cfg.screenshot_jpeg_quality)

    return _provide


def _build_capture(
    cfg: CaptureConfig,
    provider: ax_capture.AXProvider,
    trigger: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build an enriched capture dict in memory. Returns None if capturing is paused."""
    if _should_skip_capture(cfg):
        return None

    out: dict[str, Any] = {
        "timestamp": _now_iso(),
        "schema_version": 2,
        "trigger": trigger or {"event_type": "heartbeat"},
    }

    meta = window_meta.active_window()
    out["window_meta"] = {
        "app_name": meta.app_name,
        "title": meta.title,
        "bundle_id": meta.bundle_id,
    }

    if provider.available:
        result = provider.capture_frontmost(focused_window_only=True)
        if result is not None:
            out["ax_tree"] = result.raw_json
            out["ax_metadata"] = result.metadata
    else:
        out["ax_unavailable"] = True

    if cfg.include_screenshot:
        shot = screenshot.grab(
            max_width=cfg.screenshot_max_width, jpeg_quality=cfg.screenshot_jpeg_quality
        )
        if shot is not None:
            _attach_screenshot(cfg, out, shot.image_base64, shot.mime_type, shot.width, shot.height)

    return _finalize_capture(cfg, out, ocr_jpeg_provider=_daemon_ocr_jpeg_provider(cfg))


def _ingest_ocr_jpeg_provider(payload: dict[str, Any]) -> Callable[[], bytes | None]:
    """OCR JPEG source for the ingest path: decode the JPEG the Swift app pushed.

    The Swift "Persome" process grabs the focused-window screenshot (it holds Screen
    Recording) and sends it as base64 in ``ocr_jpeg_b64`` when the window looks
    AX-poor. The daemon's `_finalize_capture` makes the authoritative OCR decision
    (via S1's `has_ax_content`) and, if it needs pixels, decodes them here. Returns
    None when no JPEG was provided (daemon then skips OCR for this capture).
    """

    def _provide() -> bytes | None:
        b64 = payload.get("ocr_jpeg_b64")
        if not b64:
            return None
        try:
            return base64.b64decode(b64)
        except (ValueError, binascii.Error) as exc:
            logger.warning("ingest ocr_jpeg_b64 decode failed: %s", exc)
            return None

    return _provide


def _sanitize_ingest_timestamp(raw: Any) -> str:
    """Return a path-safe ISO8601 timestamp for an ingested capture.

    The ingest payload is UNTRUSTED and its ``timestamp`` flows into the capture-buffer
    filename (`_write_capture` → `_safe_filename`, which only rewrites ``:``/``+``), so a
    value containing path separators (``/``, ``..``) could escape the buffer or clobber an
    arbitrary ``.json``. Accept the client timestamp only when it parses as a real ISO8601
    datetime (which then cannot carry a path separator); otherwise fall back to the server
    clock — the same format the daemon's own capture path uses.
    """
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw)
            server_now = datetime.now(UTC)
            if parsed.tzinfo is None:
                parsed = parsed.astimezone()
            if parsed.astimezone(UTC) > server_now + _MAX_INGEST_FUTURE_SKEW:
                return _now_iso()
            # Normalize new IDs to fixed-width UTC. Readers still parse legacy
            # local-offset IDs before ordering or retention decisions.
            return parsed.astimezone(UTC).isoformat(timespec="microseconds")
        except (OverflowError, ValueError):
            pass
    return _now_iso()


def build_ingest_capture(cfg: CaptureConfig, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Build a capture dict from a Swift-pushed ingest payload (no OS capture).

    The header / window_meta / ax_tree / screenshot come from ``payload`` (the
    Swift "Persome" process captured them); the daemon runs the SAME enrich → OCR
    tail as `_build_capture`. Returns None when capture is paused / screen locked.
    """
    if _should_skip_capture(cfg):
        return None

    out: dict[str, Any] = {
        "timestamp": _sanitize_ingest_timestamp(payload.get("timestamp")),
        "schema_version": 2,
        "trigger": payload.get("trigger") or {"event_type": "ingest"},
        "capture_source": "ingest",
    }
    wm = payload.get("window_meta") or {}
    out["window_meta"] = {
        "app_name": wm.get("app_name", ""),
        "title": wm.get("title", ""),
        "bundle_id": wm.get("bundle_id", ""),
    }
    ax_tree = payload.get("ax_tree")
    if isinstance(ax_tree, dict):
        out["ax_tree"] = ax_tree
        if isinstance(payload.get("ax_metadata"), dict):
            out["ax_metadata"] = payload["ax_metadata"]
    else:
        out["ax_unavailable"] = True

    # Honor the screenshot opt-out exactly like the daemon path: `include_screenshot=false`
    # means no screen image is persisted, even if a (stale/buggy) client still sends one.
    shot = payload.get("screenshot")
    if cfg.include_screenshot and isinstance(shot, dict) and shot.get("image_base64"):
        _attach_screenshot(
            cfg,
            out,
            shot["image_base64"],
            shot.get("mime_type", "image/jpeg"),
            int(shot.get("width") or 0),
            int(shot.get("height") or 0),
        )

    return _finalize_capture(cfg, out, ocr_jpeg_provider=_ingest_ocr_jpeg_provider(payload))


def ingest_capture(cfg: Config, payload: dict[str, Any]) -> dict[str, Any]:
    """Ingest one trusted local capture: enrich, persist, and update sessions.

    Routes through the live `_active_runner` when a capture task is running (so the
    content-dedup and session hook behave exactly as for in-daemon captures).
    Falls back to a direct, hookless write when no
    capture task is up (CLI one-shots / tests). Returns ``{id, deduped, skipped}``.
    """
    out = build_ingest_capture(cfg.capture, payload)
    if out is None:
        return {"id": None, "deduped": False, "skipped": True}
    runner = _active_runner
    if runner is not None:
        stem = runner.commit_prebuilt(out)
        return {"id": stem, "deduped": stem is None, "skipped": False}
    path = _write_capture(out)
    return {"id": path.stem, "deduped": False, "skipped": False}


def _write_capture(out: dict[str, Any]) -> Path:
    """Persist a built capture dict to the buffer, index it for search, and log."""
    ts = out["timestamp"]
    # Pop the private OCR payload BEFORE serializing — it's raw JPEG bytes (not
    # JSON-serializable, and not something we want on disk). It's consumed below,
    # after the capture row is indexed, so the OCR backfill can't race the insert.
    ocr_jpeg = out.pop("_ocr_pending_jpeg", None)
    ocr_tier = out.pop("_ocr_tier", "tiny")
    ocr_structured = out.pop("_ocr_structured", False)
    ocr_placeholder_values = s1_parser.ocr_placeholder_values(out) if ocr_jpeg is not None else ()
    if ocr_jpeg is not None:
        out["ocr_submitted"] = True

    path = paths.capture_buffer_dir() / f"{_safe_filename(ts)}.json"
    paths.atomic_write_private_text(path, json.dumps(out, ensure_ascii=False))
    _index_capture(path.stem, out)
    meta = out.get("window_meta") or {}
    logger.info(
        "capture ok: %s trigger=%s app=%r title=%r ax=%s screenshot=%s",
        path.name,
        (out.get("trigger") or {}).get("event_type"),
        meta.get("app_name"),
        (meta.get("title") or "")[:60],
        "ax_tree" in out,
        "screenshot" in out,
    )

    # Now that the capture row exists in the DB, kick off OCR. The backfill
    # (`UPDATE captures SET visible_text … WHERE id=?`) will find the row.
    if ocr_jpeg is not None:
        thread = threading.Thread(
            target=_submit_ocr_async,
            args=(
                ocr_jpeg,
                path.stem,
                ocr_tier,
                meta,
                ocr_structured,
                ocr_placeholder_values,
            ),
            name=f"ocr-submit-{path.stem}",
            daemon=True,
        )
        thread.start()

    return path


def _index_capture(file_stem: str, out: dict[str, Any]) -> bool:
    """Insert/upsert the capture's S1 fields into the FTS5 index.

    Failures here are non-fatal — a missed FTS row is recoverable via
    ``persome rebuild-captures-index``; killing the capture worker
    over an indexing hiccup would lose the JSON too.
    """
    # Rollback/recovery paths can feed historical JSON that predates native
    # placeholder filtering. Index the repaired S1 projection without
    # rewriting the forensic raw AX tree on disk.
    indexed_out = s1_parser.sanitize_capture(out)
    meta = indexed_out.get("window_meta") or {}
    focused = indexed_out.get("focused_element") or {}
    try:
        with fts_store.cursor() as conn:
            fts_store.insert_capture(
                conn,
                id=file_stem,
                timestamp=indexed_out.get("timestamp", ""),
                app_name=meta.get("app_name") or "",
                bundle_id=meta.get("bundle_id") or "",
                window_title=meta.get("title") or "",
                focused_role=focused.get("role") or "",
                focused_value=focused.get("value") or "",
                visible_text=indexed_out.get("visible_text") or "",
                url=indexed_out.get("url") or "",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("captures FTS insert failed for %s: %s", file_stem, exc)
        return False
    return True


def _content_fingerprint(out: dict[str, Any]) -> str:
    """Hash the content-bearing fields of a capture for consecutive-duplicate detection.

    Excludes timestamp, trigger metadata, screenshots, and the raw ax_tree (which
    contains coordinate noise). Focuses on what actually drives downstream stages:
    the window identity + what the user can see + what they've typed.
    """
    meta = out.get("window_meta") or {}
    focused = out.get("focused_element") or {}
    payload = "\x1f".join(
        [
            meta.get("bundle_id") or "",
            meta.get("title") or "",
            focused.get("role") or "",
            focused.get("value") or "",
            out.get("visible_text") or "",
            out.get("url") or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def capture_once(
    cfg: CaptureConfig,
    provider: ax_capture.AXProvider,
    *,
    trigger: dict[str, Any] | None = None,
) -> Path | None:
    """Perform one capture and write it to the buffer. Returns the file path on success.

    ``trigger`` (optional) carries the watcher event metadata that caused this
    capture. When absent the capture is treated as a heartbeat / manual tick.

    This helper always writes — content-dedup lives in ``_CaptureRunner`` so the
    CLI ``capture-once`` smoke test still produces a fresh file on demand.
    """
    out = _build_capture(cfg, provider, trigger)
    if out is None:
        return None
    return _write_capture(out)


class _CaptureRunner:
    """Serializes capture_once calls from the watcher thread + heartbeat task.

    Captures execute on a single dedicated worker thread fed by a bounded
    queue, so the watcher reader thread never blocks on AX / screenshot I/O
    and a runaway burst of events can never spawn unbounded threads.

    Also enforces *consecutive-duplicate dedup*: if the content fingerprint
    (bundle+title+focused value+visible_text+url) matches the previously
    written capture, the new one is dropped. Time-based dedup in the
    dispatcher handles rapid-fire bursts; this handles a static screen
    (e.g. the lock screen overnight) that keeps generating identical
    captures. When deduped, the ``pre_capture_hook`` is NOT fired, so the
    session manager's idle timer isn't reset by meaningless repetition.
    """

    # Bounded queue for backpressure. Captures are de-duplicated by the
    # dispatcher upstream and again by content-fingerprint here, so a
    # backlog past this size is a sign the worker is stuck or LLM/AX
    # calls are slow — drop with a warning rather than build an
    # unbounded thread/memory backlog.
    _MAX_PENDING = 16
    _SENTINEL: Any = object()

    def __init__(
        self,
        cfg: CaptureConfig,
        provider: ax_capture.AXProvider | None,
        *,
        pre_capture_hook: Callable[[dict[str, Any]], None] | None = None,
        capture_receipt_hook: Callable[[Path | None, str], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._provider = provider
        self._pre_capture_hook = pre_capture_hook
        self._capture_receipt_hook = capture_receipt_hook
        self._lock = threading.Lock()
        self._last_fingerprint: str | None = None
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self._MAX_PENDING)
        self._worker: threading.Thread | None = None

    def start_worker(self) -> None:
        """Spawn the dedicated worker thread. Idempotent."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="capture-worker",
            daemon=True,
        )
        self._worker.start()

    def stop_worker(self, *, timeout: float = 5.0) -> None:
        """Drain the queue and join the worker thread."""
        if self._worker is None:
            return
        with contextlib.suppress(queue.Full):
            self._queue.put(self._SENTINEL, timeout=1.0)
        self._worker.join(timeout=timeout)
        if self._worker.is_alive():
            logger.warning("capture worker did not exit within %.1fs", timeout)
        self._worker = None

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._SENTINEL:
                return
            self.run(item)

    def run(self, trigger: dict[str, Any] | None) -> None:
        # Serialize so two near-simultaneous triggers don't double-capture.
        with self._lock:
            try:
                if self._provider is None:
                    logger.debug("OS capture skipped: runner is ingest-only")
                    self._publish_receipt(None, capture_gate_reason(self._cfg) or "ingest-ready")
                    return
                out = _build_capture(self._cfg, self._provider, trigger)
                if out is None:
                    self._publish_receipt(None, capture_gate_reason(self._cfg) or "capture-skipped")
                    return
                self._commit(out, trigger)
            except Exception as exc:  # noqa: BLE001
                logger.error("capture failed: %s", exc, exc_info=True)

    def capture_now(self) -> Path | None:
        """Synchronously force a fresh record through the daemon-owned runner."""
        with self._lock:
            try:
                if self._provider is None:
                    logger.debug("forced OS capture skipped: runner is ingest-only")
                    self._publish_receipt(None, capture_gate_reason(self._cfg) or "ingest-ready")
                    return None
                out = _build_capture(
                    self._cfg,
                    self._provider,
                    {"event_type": "OnboardingProbe"},
                )
                if out is None:
                    self._publish_receipt(None, capture_gate_reason(self._cfg) or "capture-skipped")
                    return None
                return self._commit(out, None, force=True)
            except Exception as exc:  # noqa: BLE001
                logger.error("forced capture failed: %s", exc, exc_info=True)
                self._publish_receipt(None, "capture-failed")
                return None

    def _publish_receipt(self, path: Path | None, reason: str) -> None:
        if self._capture_receipt_hook is None:
            return
        try:
            self._capture_receipt_hook(path, reason)
        except Exception as exc:  # noqa: BLE001
            logger.warning("capture_receipt_hook failed: %s", exc)

    def commit_prebuilt(self, out: dict[str, Any]) -> str | None:
        """Persist a capture built elsewhere (the ingest path) through this runner.

        Same content-dedup + session hook as `run`, minus the OS
        `_build_capture` step. Returns the written capture stem, or None **only** when
        the push was a genuine content-duplicate of the previous capture (no-op). A real
        write/index failure (full disk, serialization error, …) PROPAGATES — it must not
        be reported to the caller as a dedup, so the ingest route turns it into a non-2xx
        and the client can drop/retry the frame. Synchronous (runs on the route's
        threadpool).
        """
        with self._lock:
            path = self._commit(out, out.get("trigger"))
            return path.stem if path is not None else None

    def _commit(
        self,
        out: dict[str, Any],
        trigger: dict[str, Any] | None,
        *,
        force: bool = False,
    ) -> Path | None:
        """Content-dedup → write → fire hooks. Caller must hold ``self._lock``.

        Returns the written capture path, or None when content-deduped against the
        previous capture (the session hook does not fire for a duplicate, so a
        static screen does not refresh the session idle timer).
        """
        fingerprint = _content_fingerprint(out)
        if not force and fingerprint == self._last_fingerprint:
            meta = out.get("window_meta") or {}
            logger.debug(
                "capture skipped (content dedup): trigger=%s app=%r title=%r",
                (trigger or {}).get("event_type"),
                meta.get("app_name"),
                (meta.get("title") or "")[:60],
            )
            return None
        self._last_fingerprint = fingerprint
        path = _write_capture(out)
        reason = str((out.get("trigger") or {}).get("event_type") or "capture")
        self._publish_receipt(path, reason)
        if self._pre_capture_hook is not None and trigger is not None:
            try:
                self._pre_capture_hook(trigger)
            except Exception as exc:  # noqa: BLE001
                logger.warning("pre_capture_hook failed: %s", exc)
        return path

    def run_threaded(self, trigger: dict[str, Any] | None) -> None:
        """Enqueue a capture for the worker thread; drop with a warning if full."""
        try:
            self._queue.put_nowait(trigger)
        except queue.Full:
            logger.warning(
                "capture queue full (%d pending); dropping trigger=%s",
                self._queue.qsize(),
                (trigger or {}).get("event_type") if trigger else "heartbeat",
            )


async def run_forever(
    cfg: CaptureConfig,
    *,
    pre_capture_hook: Callable[[dict[str, Any]], None] | None = None,
    capture_receipt_hook: Callable[[Path | None, str], None] | None = None,
) -> None:
    """Run the capture pipeline until cancelled.

    If ``cfg.event_driven`` is true, starts the watcher subprocess and routes
    events through the dispatcher. A heartbeat timer also runs so long idle
    periods (no window changes, no typing) still get periodic snapshots.

    ``pre_capture_hook`` (optional) fires with the trigger dict for every
    capture that actually wrote new content to the buffer — duplicates
    collapsed by content-dedup do NOT fire it, so the session manager's idle
    timer isn't refreshed by a screen that isn't changing (e.g. the lock
    screen overnight).

    """
    # capture.source = "ingest": the Swift "Persome" app owns OS capture and pushes
    # frames via POST /captures/ingest; the daemon spawns no watcher and grabs no
    # screenshots, so it needs NO Accessibility / Screen-Recording permission. We
    # still build the runner so ingested captures dedup + update session state
    # identically; the ingest route reaches it via `_active_runner`.
    ingest_only = cfg.source == "ingest"

    provider: ax_capture.AXProvider | None = None
    if not ingest_only:
        provider = ax_capture.create_provider(depth=cfg.ax_depth, timeout=cfg.ax_timeout_seconds)
        if not provider.available:
            logger.warning(
                "AX capture unavailable: %s", getattr(provider, "reason", "unknown reason")
            )

    runner = _CaptureRunner(
        cfg,
        provider,
        pre_capture_hook=pre_capture_hook,
        capture_receipt_hook=capture_receipt_hook,
    )
    runner.start_worker()
    _set_active_runner(runner)
    watcher: AXWatcherProcess | None = None
    dispatcher: EventDispatcher | None = None

    try:
        if ingest_only:
            runner._publish_receipt(None, capture_gate_reason(cfg) or "ingest-ready")
            logger.info(
                "capture source=ingest — OS capture disabled (no watcher / no screenshot "
                "grab); serving POST /captures/ingest"
            )
            # Park until cancelled; the ingest route drives commits via _active_runner.
            await asyncio.Event().wait()
            return

        def _on_capture(trigger: dict[str, Any] | None) -> None:
            # Hook firing is deferred into the runner so content-deduped captures
            # (e.g. overnight lock-screen repeats) don't refresh the session timer.
            runner.run_threaded(trigger)

        if cfg.event_driven:
            watcher = AXWatcherProcess()
            if watcher.available:
                dispatcher = EventDispatcher(
                    _on_capture,
                    debounce_seconds=cfg.debounce_seconds,
                    min_capture_gap_seconds=cfg.min_capture_gap_seconds,
                    dedup_interval_seconds=cfg.dedup_interval_seconds,
                    same_window_dedup_seconds=cfg.same_window_dedup_seconds,
                )
                watcher.on_event(dispatcher.on_event)
                watcher.start()
                logger.info("event-driven capture started")
            else:
                logger.warning("AX watcher unavailable — falling back to heartbeat-only captures")

        # One capture immediately so the user sees something in the buffer right away.
        runner.run_threaded(None)

        if cfg.heartbeat_minutes > 0:
            heartbeat_interval = max(60.0, cfg.heartbeat_minutes * 60.0)
            logger.info(
                "heartbeat capture every %.0fs (event_driven=%s)",
                heartbeat_interval,
                cfg.event_driven,
            )
            while True:
                await asyncio.sleep(heartbeat_interval)
                try:
                    await asyncio.to_thread(runner.run, None)
                except Exception as exc:  # noqa: BLE001
                    logger.error("heartbeat capture failed: %s", exc, exc_info=True)
        else:
            logger.info(
                "heartbeat disabled (heartbeat_minutes=%d); event-driven only",
                cfg.heartbeat_minutes,
            )
            # Park until the task is cancelled so the watcher keeps streaming.
            await asyncio.Event().wait()
    finally:
        # Stop in producer→consumer order so no new work piles up after we've
        # told the worker to drain: watcher (no new events) → dispatcher
        # (cancel debounce) → runner worker (drain + join).
        _set_active_runner(None)
        if watcher is not None:
            watcher.stop()
        if dispatcher is not None:
            dispatcher.shutdown()
        runner.stop_worker()


# Capture trigger event type treated as an "Enter-anchored" frame (spec E5): a
# keyboard-committed input is a strong evidence signal, so it earns extended
# retention. There is no distinct
# "Return key" watcher event today — UserTextInput is the keyboard-commit
# trigger the dispatcher fires — so it is the marker. Read from the capture's
# own ``trigger`` metadata, so this needs no DB.
_ENTER_ANCHOR_EVENT_TYPES = frozenset({"UserTextInput"})
_ATOMIC_CAPTURE_TEMP_GRACE_SECONDS = 5 * 60


def _is_enter_anchored(path: Path) -> bool:
    """Does this capture's ``trigger.event_type`` mark it Enter-anchored (#7)?"""
    try:
        data = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return False
    trigger = data.get("trigger") if isinstance(data, dict) else None
    if not isinstance(trigger, dict):
        return False
    return str(trigger.get("event_type") or "") in _ENTER_ANCHOR_EVENT_TYPES


def cleanup_buffer(
    retention_hours: int,
    processed_before_ts: str | None = None,
    *,
    screenshot_retention_hours: int | None = None,
    screenshot_thumbnail_hours: int | None = None,
    max_mb: int = 0,
    extended_retention_enabled: bool = False,
    actionable_retention_days: int = 7,
) -> dict[str, int]:
    buf = paths.capture_buffer_dir()
    if not buf.exists():
        _prune_missing_capture_rows(buf)
        return {"deleted": 0, "stripped": 0, "thumbnailed": 0, "evicted": 0}

    now = time.time()
    delete_cutoff = now - retention_hours * 3600
    strip_cutoff = (
        now - screenshot_retention_hours * 3600
        if screenshot_retention_hours and screenshot_retention_hours > 0
        else None
    )
    thumb_cutoff = (
        now - screenshot_thumbnail_hours * 3600
        if screenshot_thumbnail_hours and screenshot_thumbnail_hours > 0
        else None
    )
    # User-input-anchored captures keep their screenshot until this longer cutoff.
    extended_cutoff = (
        now - actionable_retention_days * 86400 if extended_retention_enabled else None
    )
    has_watermark = processed_before_ts is not None
    absorbed_before = (
        parse_capture_timestamp(processed_before_ts) if processed_before_ts is not None else None
    )
    if has_watermark and absorbed_before is None:
        logger.warning("invalid capture cleanup watermark; treating every frame as unabsorbed")

    deleted = stripped = thumbnailed = evicted = 0
    surviving: list[tuple[float, Path, int]] = []  # (mtime, path, size_after_pass)
    expired: list[tuple[float, Path, int]] = []

    for p in sorted(buf.iterdir()):
        # A SIGKILL can strand the private inode used by atomic capture writes.
        # It has no recovery contract; after a short grace period (so cleanup
        # cannot race an active rename), remove it regardless of retention.
        if p.name.startswith(".") and ".json." in p.name:
            try:
                temp_stat = p.lstat()
                if temp_stat.st_mtime <= now - _ATOMIC_CAPTURE_TEMP_GRACE_SECONDS:
                    p.unlink(missing_ok=True)
                    deleted += 1
            except OSError:
                pass
            continue
        if not p.is_file() or p.suffix != ".json":
            continue
        captured_at = parse_capture_path_timestamp(p)
        is_absorbed = not has_watermark or (
            absorbed_before is not None
            and captured_at is not None
            # A legacy naive timestamp inside the repeated DST hour has two
            # possible instants. Never age-delete/strip it on an assumption;
            # the hard disk cap remains the deterministic final boundary.
            and not capture_path_has_ambiguous_local_time(p)
            and captured_at < absorbed_before
        )
        try:
            st = p.stat()
        except OSError:
            continue

        if is_absorbed and st.st_mtime <= delete_cutoff:
            # Remove the searchable copy first. If SQLite is unavailable, keep
            # the JSON for retry instead of leaving an invisible FTS orphan.
            expired.append((st.st_mtime, p, st.st_size))
            continue

        if (
            is_absorbed
            and strip_cutoff is not None
            and st.st_mtime <= strip_cutoff
            and not _retain_screenshot_extended(
                p,
                mtime=st.st_mtime,
                extended_cutoff=extended_cutoff,
            )
            and _strip_screenshot_inplace(p)
        ):
            stripped += 1
            with contextlib.suppress(OSError):
                st = p.stat()
        elif (
            # §2.1 pixel tier 2 — only for captures the strip pass didn't (yet)
            # reach: older than the thumbnail cutoff, still younger than strip
            # (or strip-deferred by extended retention: an actionable frame
            # keeps FULL resolution — its screenshot exists for grounding, a
            # thumbnail would defeat the deferral).
            is_absorbed
            and thumb_cutoff is not None
            and st.st_mtime <= thumb_cutoff
            and not _retain_screenshot_extended(
                p,
                mtime=st.st_mtime,
                extended_cutoff=extended_cutoff,
            )
            and _thumbnail_screenshot_inplace(p)
        ):
            thumbnailed += 1
            with contextlib.suppress(OSError):
                st = p.stat()

        surviving.append((st.st_mtime, p, st.st_size))

    if expired:
        # Disk erasure remains authoritative if SQLite is temporarily down;
        # the reconciliation pass below removes any stale searchable rows once
        # the database is available again.
        index_removed = _delete_captures_from_fts([p.stem for _, p, _ in expired])
        for record in expired:
            try:
                record[1].unlink()
                deleted += 1
            except OSError:
                if index_removed:
                    _restore_capture_index(record[1])
                surviving.append(record)

    if max_mb > 0:
        limit = max_mb * 1024 * 1024
        total = sum(sz for _, _, sz in surviving)
        if total > limit:
            surviving.sort()  # oldest first by mtime
            for _mtime, path, size in surviving:
                if total <= limit:
                    break
                index_removed = _delete_captures_from_fts([path.stem])
                captured_at = parse_capture_path_timestamp(path)
                is_absorbed = not has_watermark or (
                    absorbed_before is not None
                    and captured_at is not None
                    and not capture_path_has_ambiguous_local_time(path)
                    and captured_at < absorbed_before
                )
                if has_watermark and not is_absorbed:
                    logger.warning(
                        "capture buffer hard cap evicting unabsorbed frame: %s", path.name
                    )
                try:
                    path.unlink()
                    total -= size
                    evicted += 1
                except OSError:
                    if index_removed:
                        _restore_capture_index(path)

    # A transient DB failure must not defeat the hard disk cap. Files still win
    # that boundary; once SQLite recovers, reconcile any stale searchable rows.
    _prune_missing_capture_rows(buf)

    return {
        "deleted": deleted,
        "stripped": stripped,
        "thumbnailed": thumbnailed,
        "evicted": evicted,
    }


def _delete_captures_from_fts(stems: list[str]) -> bool:
    """Atomically drop matching searchable copies before deleting JSON files."""
    if not stems:
        return True
    try:
        with fts_store.cursor() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for stem in stems:
                    fts_store.delete_capture(conn, stem)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("captures FTS delete failed for %d stems: %s", len(stems), exc)
        return False
    return True


def _restore_capture_index(path: Path) -> None:
    """Best-effort rollback when FTS deletion succeeded but unlink did not."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.error("cannot restore capture index after unlink failure: %s", path.name)
        return
    if not isinstance(data, dict) or not _index_capture(path.stem, data):
        logger.error("capture remained on disk without a restored index row: %s", path.name)


def _prune_missing_capture_rows(buf: Path) -> bool:
    """Reconcile stale FTS rows left by a prior filesystem-first hard eviction."""
    try:
        with fts_store.cursor() as conn:
            # Freeze DB writers before establishing the candidate set and then
            # scan the filesystem. A concurrent capture writes JSON before its
            # row; it is therefore either visible in ``present`` or inserted
            # after this transaction commits. In neither case can reconciliation
            # delete a brand-new searchable row based on an older directory scan.
            conn.execute("BEGIN IMMEDIATE")
            try:
                candidates = {
                    str(row[0]) for row in conn.execute("SELECT id FROM captures").fetchall()
                }
                present = (
                    {p.stem for p in buf.iterdir() if p.is_file() and p.suffix == ".json"}
                    if buf.is_dir()
                    else set()
                )
                for stem in candidates - present:
                    fts_store.delete_capture(conn, stem)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("capture index reconciliation failed: %s", exc)
        return False
    return True


def _retain_screenshot_extended(
    path: Path,
    *,
    mtime: float,
    extended_cutoff: float | None,
) -> bool:
    """Keep a user-input-anchored screenshot past the normal strip cutoff.

    True iff extended retention is active (``extended_cutoff`` not None), the
    capture is still within the extended window (mtime newer than
    ``extended_cutoff``), and it is an Enter-anchored frame. Past the cutoff it
    strips normally. When extended retention is
    off this is always False, so the legacy unconditional strip runs.

    Note: this never reads or decrypts the screenshot field; the decision uses
    the capture's ``trigger`` only, so an
    encrypted (#6) retained frame stays ciphertext on disk.
    """
    if extended_cutoff is None:
        return False
    if mtime <= extended_cutoff:
        return False  # past the actionable cap — strip normally
    return _is_enter_anchored(path)


# Pixel-axis graded forgetting (memory-rebuild spec §2.1 / §1.5-5): the
# thumbnail tier between full-res and strip. Small enough to shed most of the
# bytes, large enough that "what was on screen" survives a human glance.
_THUMBNAIL_MAX_WIDTH = 480
_THUMBNAIL_JPEG_QUALITY = 50


def _thumbnail_screenshot_inplace(path: Path) -> bool:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    shot = data.get("screenshot")
    if not isinstance(shot, dict) or shot.get("thumbnail"):
        return False
    payload = shot.get("image_base64") or ""
    if not payload:
        return False

    was_encrypted = bool(shot.get("screenshot_enc"))
    key = None
    if was_encrypted:
        key = screenshot_crypto.load_key()
        if key is None:
            return False  # can't read it → can't downscale it; leave ciphertext intact
        try:
            raw = screenshot_crypto.decrypt(payload, key)
        except Exception:  # noqa: BLE001
            return False
        # the capture path seals the BASE64 TEXT (encrypt(image_base64)), so
        # the opened envelope is b64 bytes, not JPEG bytes — decode one more
        # layer; a raw-bytes-sealed payload falls through untouched
        with contextlib.suppress(ValueError, binascii.Error):
            raw = base64.b64decode(raw, validate=True)
    else:
        try:
            raw = base64.b64decode(payload)
        except (ValueError, binascii.Error):
            return False

    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        width, height = img.size
        if width > _THUMBNAIL_MAX_WIDTH:
            new_h = max(1, round(height * _THUMBNAIL_MAX_WIDTH / width))
            resized = img.convert("RGB").resize((_THUMBNAIL_MAX_WIDTH, new_h))
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=_THUMBNAIL_JPEG_QUALITY)
            shot["width"], shot["height"] = _THUMBNAIL_MAX_WIDTH, new_h
            shot["mime_type"] = "image/jpeg"
            out_b64 = base64.b64encode(buf.getvalue()).decode()
            if was_encrypted:
                assert key is not None
                shot["image_base64"] = screenshot_crypto.encrypt(out_b64, key)
            else:
                shot["image_base64"] = out_b64
        # already ≤ max width: no re-encode — just mark, so the nightly pass
        # never re-reads (and never re-decrypts) this capture again
    except Exception:  # noqa: BLE001 — a corrupt image never breaks buffer hygiene
        return False

    shot["thumbnail"] = True
    try:
        paths.atomic_write_private_text(path, json.dumps(data, ensure_ascii=False))
        return True
    except (OSError, RuntimeError):
        return False


def _strip_screenshot_inplace(path: Path) -> bool:
    """Rewrite a capture JSON without its ``screenshot`` field. Returns True if stripped."""
    try:
        raw = path.read_text()
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if "screenshot" not in data:
        return False
    data.pop("screenshot", None)
    data["screenshot_stripped"] = True
    try:
        paths.atomic_write_private_text(path, json.dumps(data, ensure_ascii=False))
        return True
    except (OSError, RuntimeError):
        return False
