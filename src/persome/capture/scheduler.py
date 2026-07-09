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
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
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
    s1_parser,
    screen_state,
    screenshot,
    screenshot_crypto,
    window_meta,
    window_screenshot,
)
from .event_dispatcher import EventDispatcher
from .watcher import AXWatcherProcess

logger = get("persome.capture")

# Frequency-limit cache for OCR fallback: key="bundle_id::title" -> last submit timestamp.
_last_ocr_ts: dict[str, float] = {}


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


# Injected fast-path entry for AX-poor apps whose only structured signal is OCR
# (WeChat). Their fast-path recognition can't run at capture time — OCR runs on a
# background thread and isn't ready yet — so `_submit_ocr_async` re-triggers it here
# once `ocr_structure` has produced a conversation. Wired by daemon.py to the same
# post pool + on_capture every other app uses; None disables it.
_ocr_ready_hook: Callable[[dict[str, Any]], None] | None = None


def set_ocr_ready_hook(hook: Callable[[dict[str, Any]], None] | None) -> None:
    """Inject (or clear) the OCR-ready fast-path entry. See `_ocr_ready_hook`."""
    global _ocr_ready_hook
    _ocr_ready_hook = hook


# The live capture runner created by `run_forever`, exposed so the
# `POST /captures/ingest` route (Swift-owned capture, `capture.source="ingest"`)
# funnels pushed payloads through the SAME runner — identical content-dedup and
# pre/post-capture hooks (the intent fast path) as the in-daemon capture loop.
# None when no capture task is running (CLI one-shots / tests / capture disabled).
_active_runner: _CaptureRunner | None = None


def _set_active_runner(runner: _CaptureRunner | None) -> None:
    global _active_runner
    _active_runner = runner


def _submit_ocr_async(
    image_bytes: bytes,
    capture_id: str,
    tier: str,
    window_meta: dict[str, str] | None = None,
    structured: bool = False,
    collect_training_data: bool = False,
    capture_out: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget local OCR + geometry structuring. Runs on a daemon thread.

    Local PP-OCRv6 inference is synchronous (~0.5s) and runs entirely on-device, so
    there is no job table / polling loop — we recognize and backfill in one shot.
    Called from `_write_capture` AFTER the capture row is indexed, so the backfill
    `UPDATE … WHERE id=?` always finds its row.

    When `structured`, the raw OCR lines are reconstructed into field-labeled text via
    the per-app geometry structurer (`ocr_structure`) before backfill — fail-open to the
    raw join if structuring yields nothing. When `collect_training_data`, a local-only
    JSON sample (OCR geometry + structured result, NEVER the screenshot) is written for
    future model training.
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
    if not backfill_text:
        return

    try:
        with fts_store.cursor() as conn:
            fts_store.backfill_capture_ocr_text(conn, capture_id, backfill_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ocr backfill failed for %s: %s", capture_id, exc)

    if collect_training_data:
        _write_ocr_training_sample(capture_id, meta, texts, boxes, scores, struct)

    # WeChat (AX-poor): OCR is its only structured signal, so the fast path could not
    # run at capture time. Now that structuring is ready, re-trigger it with a
    # capture-like dict (original fields + the structured result) — routed to the same
    # post pool + on_capture as every other app. Only for a real WeChat conversation.
    if (
        struct.get("layout") == "wechat-desktop"
        and capture_out is not None
        and _ocr_ready_hook is not None
    ):
        try:
            _ocr_ready_hook({**capture_out, "_ocr_structured_result": struct})
        except Exception as exc:  # noqa: BLE001
            logger.warning("ocr_ready_hook failed for %s: %s", capture_id, exc)


def _image_width(image_bytes: bytes) -> int:
    """Decode just the image width for the structurer's column scaling. 960 on failure."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as im:
            return im.width
    except Exception:  # noqa: BLE001
        return 960


def _write_ocr_training_sample(
    capture_id: str,
    meta: dict[str, str],
    texts: list[str],
    boxes: list[list[int]],
    scores: list[float],
    struct: dict,
) -> None:
    """Write one local-only OCR training sample to `paths.ocr_samples_dir()`.

    PRIVACY: stores ONLY the OCR geometry (boxes/scores), the recognized text (which is
    already what lands in `visible_text`), and the geometry-structured result — NEVER the
    screenshot or any raw image. This keeps the #119 invariant ("screenshots never leave
    the machine"); the samples also never leave the machine (no upload path exists).
    Best-effort: a write failure never perturbs capture.
    """
    try:
        d = paths.ocr_samples_dir()
        d.mkdir(parents=True, exist_ok=True)
        sample = {
            "capture_id": capture_id,
            "bundle_id": meta.get("bundle_id", ""),
            "app_name": meta.get("app_name", ""),
            "window_title": meta.get("title", ""),
            "ocr": {"texts": texts, "boxes": boxes, "scores": scores},
            "structured": struct,
            "geom_version": ocr_structure.GEOM_VERSION,
        }
        (d / f"{capture_id}.json").write_text(json.dumps(sample, ensure_ascii=False))
        _prune_ocr_samples(d)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ocr training sample write failed for %s: %s", capture_id, exc)


def _prune_ocr_samples(d: Path, keep: int = 5000) -> None:
    """Bound the local sample dir: keep the newest `keep` JSON files, delete older."""
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[keep:]:
        with contextlib.suppress(Exception):
            p.unlink()


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().replace(microsecond=0).isoformat()


def _safe_filename(ts: str) -> str:
    return ts.replace(":", "-").replace("+", "p")


def _should_skip_capture(cfg: CaptureConfig) -> bool:
    """Pause / privacy gate shared by the daemon capture loop and the ingest path.

    Returns True (skip this capture) when the user paused capture or the screen is
    locked / asleep. Fail-open inside ``is_screen_locked`` — a broken probe reads
    "unlocked" so capture is never wedged. Both checks default on.
    """
    paths.ensure_dirs()
    if paths.paused_flag().exists():
        logger.info("capture skipped (paused)")
        return True
    # Privacy guardrail (spec E7): don't collect behind the lock screen / asleep.
    if getattr(cfg, "capture_pause_on_lock", True) and screen_state.is_screen_locked():
        logger.info("capture skipped (screen locked / asleep)")
        return True
    return False


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
    regardless of who captured the pixels. Fail-open: flag on but no key in
    ``PERSOME_SCREENSHOT_KEY`` ⇒ warn + store plaintext, never drop the screenshot.
    """
    screenshot_enc = False
    if getattr(cfg, "capture_encrypt_screenshots", False):
        key = screenshot_crypto.load_key()
        if key is not None:
            image_base64 = screenshot_crypto.encrypt(image_base64, key)
            screenshot_enc = True
        else:
            logger.warning(
                "capture_encrypt_screenshots is on but %s is unavailable; "
                "writing plaintext screenshot",
                screenshot_crypto.KEY_ENV,
            )
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
    if getattr(cfg, "capture_suppress_secure_input", True) and screen_state.is_secure_input_active(
        out
    ):
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
                out["_ocr_collect_training_data"] = cfg.ocr_collect_training_data
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
            datetime.fromisoformat(raw)
            return raw
        except ValueError:
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
    """Ingest one Swift-pushed capture: enrich → persist → fire hooks.

    Routes through the live `_active_runner` when a capture task is running (so the
    content-dedup and pre/post-capture hooks — the intent fast path — fire exactly
    as for in-daemon captures). Falls back to a direct, hookless write when no
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
    ocr_collect = out.pop("_ocr_collect_training_data", False)
    if ocr_jpeg is not None:
        out["ocr_submitted"] = True

    path = paths.capture_buffer_dir() / f"{_safe_filename(ts)}.json"
    path.write_text(json.dumps(out, ensure_ascii=False))
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
            args=(ocr_jpeg, path.stem, ocr_tier, meta, ocr_structured, ocr_collect, dict(out)),
            name=f"ocr-submit-{path.stem}",
            daemon=True,
        )
        thread.start()

    return path


def _index_capture(file_stem: str, out: dict[str, Any]) -> None:
    """Insert/upsert the capture's S1 fields into the FTS5 index.

    Failures here are non-fatal — a missed FTS row is recoverable via
    ``persome rebuild-captures-index``; killing the capture worker
    over an indexing hiccup would lose the JSON too.
    """
    meta = out.get("window_meta") or {}
    focused = out.get("focused_element") or {}
    try:
        with fts_store.cursor() as conn:
            fts_store.insert_capture(
                conn,
                id=file_stem,
                timestamp=out.get("timestamp", ""),
                app_name=meta.get("app_name") or "",
                bundle_id=meta.get("bundle_id") or "",
                window_title=meta.get("title") or "",
                focused_role=focused.get("role") or "",
                focused_value=focused.get("value") or "",
                visible_text=out.get("visible_text") or "",
                url=out.get("url") or "",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("captures FTS insert failed for %s: %s", file_stem, exc)


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
        provider: ax_capture.AXProvider,
        *,
        pre_capture_hook: Callable[[dict[str, Any]], None] | None = None,
        post_capture_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._provider = provider
        self._pre_capture_hook = pre_capture_hook
        # ``post_capture_hook`` fires with the *enriched capture dict* after a
        # new-content write, on a separate bounded single-worker pool so the
        # capture worker never blocks on the fast path's parse/LLM I/O. Like
        # ``pre_capture_hook`` it is NOT fired for content-deduped captures.
        self._post_capture_hook = post_capture_hook
        self._post_pool: ThreadPoolExecutor | None = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="post-capture")
            if post_capture_hook is not None
            else None
        )
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
        if self._post_pool is not None:
            # Don't block daemon shutdown on an in-flight fast-path LLM call.
            self._post_pool.shutdown(wait=False, cancel_futures=True)

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
                out = _build_capture(self._cfg, self._provider, trigger)
                if out is None:
                    return
                self._commit(out, trigger)
            except Exception as exc:  # noqa: BLE001
                logger.error("capture failed: %s", exc, exc_info=True)

    def commit_prebuilt(self, out: dict[str, Any]) -> str | None:
        """Persist a capture built elsewhere (the ingest path) through this runner.

        Same content-dedup + pre/post-capture hooks as `run`, minus the OS
        `_build_capture` step. Returns the written capture stem, or None **only** when
        the push was a genuine content-duplicate of the previous capture (no-op). A real
        write/index failure (full disk, serialization error, …) PROPAGATES — it must not
        be reported to the caller as a dedup, so the ingest route turns it into a non-2xx
        and the client can drop/retry the frame. Synchronous (runs on the route's
        threadpool); the post-capture hook dispatches to the bounded post pool, so it
        never blocks the HTTP response.
        """
        with self._lock:
            path = self._commit(out, out.get("trigger"))
            return path.stem if path is not None else None

    def _commit(self, out: dict[str, Any], trigger: dict[str, Any] | None) -> Path | None:
        """Content-dedup → write → fire hooks. Caller must hold ``self._lock``.

        Returns the written capture path, or None when content-deduped against the
        previous capture (the pre/post hooks do NOT fire for a dedup, so a static
        screen doesn't refresh the session idle timer or re-run the fast path).
        """
        fingerprint = _content_fingerprint(out)
        if fingerprint == self._last_fingerprint:
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
        if self._pre_capture_hook is not None and trigger is not None:
            try:
                self._pre_capture_hook(trigger)
            except Exception as exc:  # noqa: BLE001
                logger.warning("pre_capture_hook failed: %s", exc)
        self._fire_post_capture_hook(out)
        return path

    def _fire_post_capture_hook(self, out: dict[str, Any]) -> None:
        """Submit the enriched capture to the post-capture pool (non-blocking).

        Errors inside the hook are swallowed with a warning so a fast-path
        hiccup never kills the capture worker. A full/refused submission (pool
        shutting down) is logged and dropped, never raised.
        """
        if self._post_capture_hook is None or self._post_pool is None:
            return

        def _run() -> None:
            try:
                self._post_capture_hook(out)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                logger.warning("post_capture_hook failed: %s", exc)

        try:
            self._post_pool.submit(_run)
        except RuntimeError:
            # Pool already shut down (daemon stopping) — drop silently.
            logger.debug("post_capture_hook skipped: pool shut down")

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
    post_capture_hook: Callable[[dict[str, Any]], None] | None = None,
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

    ``post_capture_hook`` (optional) fires with the *enriched capture dict*
    (window_meta / ax_tree / visible_text / trigger) for the same new-content
    writes, but on a separate bounded single-worker pool so the capture worker
    never blocks on the hook's work. This is the mount point for the
    event-driven intent fast path (Phase A / K1).
    """
    # capture.source = "ingest": the Swift "Persome" app owns OS capture and pushes
    # frames via POST /captures/ingest; the daemon spawns no watcher and grabs no
    # screenshots, so it needs NO Accessibility / Screen-Recording permission. We
    # still build the runner (+ hooks) so ingested captures dedup + fire the intent
    # fast path identically; the ingest route reaches it via `_active_runner`.
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
        post_capture_hook=post_capture_hook,
    )
    runner.start_worker()
    # WeChat (OCR-only) fast-path: route the OCR-ready re-trigger through the SAME
    # bounded post pool + hook as every other app, so it shares the fast path's
    # concurrency cap and on_capture logic (see `_ocr_ready_hook`).
    set_ocr_ready_hook(runner._fire_post_capture_hook if post_capture_hook is not None else None)
    _set_active_runner(runner)
    watcher: AXWatcherProcess | None = None
    dispatcher: EventDispatcher | None = None

    try:
        if ingest_only:
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


def _actionable_stems() -> set[str]:
    """Capture stems that earn extended screenshot retention (#7 / spec E5).

    The actionable subset = captures that PRODUCED an intent (provenance, the
    primary selector via the ``intents.source_capture`` column). A bare 24h strip
    is too short for these — a recognized to-do may only run later, and the
    screenshot is the grounding the proposal writer / a human review wants.

    Best-effort: any DB error (table absent on a fresh install, locked, …) yields
    an empty set, so the scanner degrades to the unconditional strip — never
    crashes buffer hygiene over a provenance lookup. Enter-anchored frames are
    detected per-file from the capture's own ``trigger`` (no DB needed) in
    :func:`_is_enter_anchored`.
    """
    try:
        from ..intent import store as intent_store
        from ..store import fts as fts_store

        with fts_store.cursor() as conn:
            return intent_store.actionable_capture_stems(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("actionable-stem lookup failed; extended retention skipped: %s", exc)
        return set()


# Capture trigger event type treated as an "Enter-anchored" frame (spec E5): a
# keyboard-committed input is a strong actionable signal even before the
# recognizer has run, so it earns extended retention too. There is no distinct
# "Return key" watcher event today — UserTextInput is the keyboard-commit
# trigger the dispatcher fires — so it is the marker. Read from the capture's
# own ``trigger`` metadata, so this needs no DB.
_ENTER_ANCHOR_EVENT_TYPES = frozenset({"UserTextInput"})


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
    """Tiered buffer hygiene. Returns {deleted, stripped, thumbnailed, evicted}.

    Passes, all gated on ``processed_before_ts`` so an unprocessed
    trailing capture is never evicted:

    1. **Delete whole file** when mtime is older than ``retention_hours``.
    2. **Strip screenshot** when mtime is older than
       ``screenshot_retention_hours`` (if provided and smaller than
       ``retention_hours``). The screenshot field is 77% of the payload
       and nothing downstream consumes it, so stripping keeps AX+text
       queryable for much longer at ~20% of the original size.
    2b. **Thumbnail screenshot** (memory-rebuild spec §2.1 pixel-axis graded
       forgetting, ``screenshot_thumbnail_hours``; 0/None = off, byte-identical
       legacy) — the tier BETWEEN full-res and strip: a capture older than the
       thumbnail cutoff but younger than the strip cutoff has its screenshot
       downscaled in place (≤480px JPEG). 全分辨率 → 缩略 → 仅存文本化（strip；
       OCR/AX text 长留 ``captures``）→ 删除 — pixels degrade first and
       hardest, the text projection outlives them, and the evidence chain
       degrades without breaking. Fail-open per file (encrypted without a key
       / undecodable image ⇒ untouched).
    3. **Evict by size** once total buffer size exceeds ``max_mb`` MB.
       Oldest already-absorbed files go first. ``max_mb=0`` disables this.

    **Actionable-subset extended retention (#7 / spec E5).** When
    ``extended_retention_enabled`` (default off → byte-for-byte the legacy
    behaviour), the strip pass SKIPS a capture that is *actionable* — it produced
    an intent (``intents.source_capture`` provenance) or is an Enter-anchored
    frame — until it ages past ``actionable_retention_days`` (then it strips
    normally). A retained capture keeps its screenshot exactly as stored: if #6
    encryption is on the field is ciphertext and STAYS ciphertext (extended
    retention never reads or decrypts it). Whole-file delete + size eviction are
    unchanged — only the (reversible, downstream-unused) screenshot strip is
    deferred for the actionable subset.
    """
    buf = paths.capture_buffer_dir()
    if not buf.exists():
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
    # Actionable captures keep their screenshot until THIS (longer) cutoff. Built
    # only when the feature is on, so the legacy path pays no DB / per-file cost.
    extended_cutoff = (
        now - actionable_retention_days * 86400 if extended_retention_enabled else None
    )
    actionable_stems = _actionable_stems() if extended_retention_enabled else set()
    absorbed_before = (
        _safe_filename(processed_before_ts) if processed_before_ts is not None else None
    )

    deleted = stripped = thumbnailed = evicted = 0
    surviving: list[tuple[float, Path, int]] = []  # (mtime, path, size_after_pass)
    removed_stems: list[str] = []  # for FTS delete-through

    for p in sorted(buf.iterdir()):
        if not p.is_file() or p.suffix != ".json":
            continue
        is_absorbed = absorbed_before is None or p.stem < absorbed_before
        try:
            st = p.stat()
        except OSError:
            continue

        if is_absorbed and st.st_mtime <= delete_cutoff:
            try:
                p.unlink()
                deleted += 1
                removed_stems.append(p.stem)
            except OSError:
                pass
            continue

        if (
            is_absorbed
            and strip_cutoff is not None
            and st.st_mtime <= strip_cutoff
            and not _retain_screenshot_extended(
                p,
                mtime=st.st_mtime,
                extended_cutoff=extended_cutoff,
                actionable_stems=actionable_stems,
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
                actionable_stems=actionable_stems,
            )
            and _thumbnail_screenshot_inplace(p)
        ):
            thumbnailed += 1
            with contextlib.suppress(OSError):
                st = p.stat()

        surviving.append((st.st_mtime, p, st.st_size))

    if max_mb > 0:
        limit = max_mb * 1024 * 1024
        total = sum(sz for _, _, sz in surviving)
        if total > limit:
            surviving.sort()  # oldest first by mtime
            for _mtime, path, size in surviving:
                if total <= limit:
                    break
                if absorbed_before is not None and path.stem >= absorbed_before:
                    continue  # don't evict un-absorbed captures
                try:
                    path.unlink()
                    total -= size
                    evicted += 1
                    removed_stems.append(path.stem)
                except OSError:
                    pass

    if removed_stems:
        _delete_captures_from_fts(removed_stems)

    return {
        "deleted": deleted,
        "stripped": stripped,
        "thumbnailed": thumbnailed,
        "evicted": evicted,
    }


def _delete_captures_from_fts(stems: list[str]) -> None:
    """Drop matching rows from the captures index. Non-fatal on failure."""
    try:
        with fts_store.cursor() as conn:
            for stem in stems:
                fts_store.delete_capture(conn, stem)
    except Exception as exc:  # noqa: BLE001
        logger.warning("captures FTS delete failed for %d stems: %s", len(stems), exc)


def _retain_screenshot_extended(
    path: Path,
    *,
    mtime: float,
    extended_cutoff: float | None,
    actionable_stems: set[str],
) -> bool:
    """Should this capture's screenshot be KEPT past the normal strip cutoff (#7)?

    True iff extended retention is active (``extended_cutoff`` not None), the
    capture is still within the actionable retention window (mtime newer than
    ``extended_cutoff``), AND it is actionable — its stem produced an intent
    (provenance) or it is an Enter-anchored frame. Past ``extended_cutoff`` even
    an actionable capture strips normally (the cap). When extended retention is
    off this is always False, so the legacy unconditional strip runs.

    Note: this NEVER reads or decrypts the screenshot field — actionability is
    decided from provenance (the stem) + the capture's ``trigger`` only — so an
    encrypted (#6) retained frame stays ciphertext on disk.
    """
    if extended_cutoff is None:
        return False
    if mtime <= extended_cutoff:
        return False  # past the actionable cap — strip normally
    if path.stem in actionable_stems:
        return True
    return _is_enter_anchored(path)


# Pixel-axis graded forgetting (memory-rebuild spec §2.1 / §1.5-5): the
# thumbnail tier between full-res and strip. Small enough to shed most of the
# bytes, large enough that "what was on screen" survives a human glance.
_THUMBNAIL_MAX_WIDTH = 480
_THUMBNAIL_JPEG_QUALITY = 50


def _thumbnail_screenshot_inplace(path: Path) -> bool:
    """Downscale a capture's screenshot in place (§2.1 pixel tier 2: 缩略).

    Returns True when the file was rewritten (downscaled, or just marked when
    the image is already small enough). Fail-open everywhere: an encrypted
    screenshot whose key is unavailable, an undecodable image, or any I/O
    error leaves the file untouched — graded forgetting must never DESTROY
    pixels it cannot faithfully re-encode (the strip tier will still reap them
    later). An encrypted screenshot is decrypted, downscaled, and
    RE-encrypted, so the encrypted-at-rest invariant survives the tier
    transition.
    """
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
            img = img.convert("RGB").resize((_THUMBNAIL_MAX_WIDTH, new_h))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=_THUMBNAIL_JPEG_QUALITY)
            shot["width"], shot["height"] = _THUMBNAIL_MAX_WIDTH, new_h
            shot["mime_type"] = "image/jpeg"
            out_b64 = base64.b64encode(buf.getvalue()).decode()
            shot["image_base64"] = (
                screenshot_crypto.encrypt(out_b64, key) if was_encrypted else out_b64
            )
        # already ≤ max width: no re-encode — just mark, so the nightly pass
        # never re-reads (and never re-decrypts) this capture again
    except Exception:  # noqa: BLE001 — a corrupt image never breaks buffer hygiene
        return False

    shot["thumbnail"] = True
    try:
        path.write_text(json.dumps(data, ensure_ascii=False))
        return True
    except OSError:
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
        path.write_text(json.dumps(data, ensure_ascii=False))
        return True
    except OSError:
        return False
