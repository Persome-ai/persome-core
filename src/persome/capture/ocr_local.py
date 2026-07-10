"""On-device OCR via local PaddleOCR (PP-OCRv6).

Replaces the former Baidu AI Studio cloud OCR (``ocr_client``). The focused-window
screenshot is OCR'd **locally** — nothing leaves the machine — for apps that block
the Accessibility API (WeChat, Feishu, NetEase Music, …).

Design notes:
- A single lazily-built ``PaddleOCR`` engine per tier is cached and reused; building it
  is slow (loads two inference graphs) so we never construct per call.
- ``predict`` is serialized behind a module lock: the Paddle predictor is not
  guaranteed reentrant, and OCR fires at most once per window every
  ``ocr_min_gap_seconds`` (default 15s) so serialization costs nothing.
- Doc-orientation / unwarp / textline-orientation are all OFF — matches the cloud
  client's ``optionalPayload`` and keeps latency down on clean UI text.
- Weights are loaded from a local directory (bundled in the app, vendored in-repo for
  dev) via ``text_detection_model_dir`` / ``text_recognition_model_dir`` so the daemon
  **never reaches the network** to fetch a model.
- Everything is fail-open: any failure (missing weights, decode error, predictor
  raise) returns ``None`` and the caller just skips OCR for that capture, exactly like
  the old cloud path degraded on a network error.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
from pathlib import Path
from typing import Any

# Quiet Paddle's glog spew before paddle is imported (daemon logs go to files).
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("FLAGS_call_stack_level", "0")

from ..logger import get

logger = get("persome.capture.ocr")

DEFAULT_TIER = "tiny"
_VALID_TIERS = ("tiny", "small", "medium")

_engines: dict[str, Any] = {}
_lock = threading.Lock()

_runtime_available: bool | None = None


def runtime_available() -> bool:
    """Whether the local-OCR native runtime (``paddleocr`` + ``paddlepaddle``) is installed in
    THIS build — the honest "can we OCR at all" probe (issue #226).

    **False on Intel (x86_64) macOS by design.** PaddlePaddle publishes no macOS-Intel wheel
    (only manylinux x86_64, win_amd64, and macos arm64), so ``pyproject.toml`` gates the paddle
    deps to ``platform_machine == 'arm64'`` and the PyInstaller spec skips collecting them off
    arm64. The x86_64 daemon slice therefore ships WITHOUT local OCR: AX-poor apps (WeChat /
    Feishu) get no OCR text, but AX-based state formation and every other feature
    work normally. This is the intended graceful degrade, not a failure.

    Cheap + side-effect-free: uses ``importlib.util.find_spec`` (no import), so it never triggers
    paddle's slow load or its glog signal handler. Cached after the first call.
    """
    global _runtime_available
    if _runtime_available is None:
        try:
            _runtime_available = (
                importlib.util.find_spec("paddleocr") is not None
                and importlib.util.find_spec("paddle") is not None
            )
        except (
            ImportError,
            ValueError,
        ):  # a missing parent package raises rather than returning None
            _runtime_available = False
    return _runtime_available


def _ocr_disabled() -> bool:
    """Runtime kill-switch for all local OCR inference.

    The bundled PaddlePaddle can SIGSEGV *during* inference (a native memory fault
    inside ``engine.predict`` on certain inputs/arch — see #335/#218). Because OCR
    runs on an in-process daemon thread, such a fault takes the whole daemon down;
    glog's ``FailureSignalHandler`` only *catches* the fault (and #323 already tears
    that handler out at teardown) — it cannot *prevent* the underlying native crash.

    Until OCR is isolated into a crash-domain subprocess (follow-up), this flag is the
    safe, instant stop-gap: set ``PERSOME_DISABLE_OCR=1`` (deploy-time, no config
    rebuild) and no paddle inference runs at all — the daemon degrades to "no OCR text
    for AX-poor apps" instead of crashing. ``warm()``/``recognize*`` all honor it, so
    paddle is never even imported when disabled. Truthy values: 1/true/yes/on.
    """
    val = os.environ.get("PERSOME_DISABLE_OCR") or os.environ.get(
        "MENS_CONTEXT_DISABLE_OCR", ""
    )  # Mens is the legacy name
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _use_isolation() -> bool:
    """Whether OCR inference runs in the isolated crash-domain subprocess (default: yes).

    Paddle can SIGSEGV natively; running it in a separate worker means such a fault kills
    only the worker, never the daemon (see #403 + the subprocess-isolation spec). Isolation
    is ON by default and cannot be turned off from ``config.toml`` — a normal install is
    always crash-safe. Two escape hatches, both env-only:

    - ``PERSOME_OCR_WORKER=1`` — set inside the worker itself, so a routed call there
      resolves in-proc (a worker never spawns a worker).
    - ``PERSOME_OCR_IN_PROCESS=1`` — debug opt-out to the legacy in-daemon path
      (crash-exposed); for local diagnosis only, never a shipped default.
    """
    if os.environ.get("PERSOME_OCR_WORKER") or os.environ.get(
        "MENS_CONTEXT_OCR_WORKER"
    ):  # Mens is the legacy name
        return False
    val = os.environ.get("PERSOME_OCR_IN_PROCESS") or os.environ.get(
        "MENS_CONTEXT_OCR_IN_PROCESS", ""
    )  # Mens is the legacy name
    return val.strip().lower() not in {"1", "true", "yes", "on"}


def _models_root() -> Path | None:
    """Locate the directory holding ``PP-OCRv6_<tier>_<kind>`` weight folders.

    Resolution order: explicit env override → PyInstaller bundle (``sys._MEIPASS``)
    → installed package bundle → vendored repo dir → the paddlex download cache.
    """
    env = os.environ.get("PERSOME_OCR_MODELS_DIR") or os.environ.get(
        "MENS_CONTEXT_OCR_MODELS_DIR"
    )  # Mens is the legacy name
    if env:
        return Path(env)
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "ocr_models"  # type: ignore[attr-defined]
    # Wheel install: persome/capture/ocr_local.py -> persome/_bundled/ocr_models.
    packaged = Path(__file__).resolve().parents[1] / "_bundled" / "ocr_models"
    if packaged.exists():
        return packaged
    # src/persome/capture/ocr_local.py -> persome-core/ocr_models
    vendored = Path(__file__).resolve().parents[3] / "ocr_models"
    if vendored.exists():
        return vendored
    cache = Path.home() / ".paddlex" / "official_models"
    return cache if cache.exists() else None


def _model_dir(tier: str, kind: str) -> str | None:
    """Return the on-disk dir for one model (``kind`` ∈ {det, rec}), or None if absent."""
    root = _models_root()
    if root is None:
        return None
    d = root / f"PP-OCRv6_{tier}_{kind}"
    return str(d) if (d / "inference.json").exists() else None


def _build_engine(tier: str) -> Any | None:
    det = _model_dir(tier, "det")
    rec = _model_dir(tier, "rec")
    if det is None or rec is None:
        logger.warning("local OCR weights missing for tier=%s (det=%s rec=%s)", tier, det, rec)
        return None
    try:
        from paddleocr import PaddleOCR
    except Exception as exc:  # noqa: BLE001
        logger.warning("paddleocr import failed: %s", exc)
        return None
    # Importing paddle installs a glog FailureSignalHandler that hijacks the fatal
    # signals (SIGTERM/SIGSEGV/…): left in place it intercepts the daemon's SIGTERM
    # at app-quit / launchd-bootout, dumps a stack and re-raises, which macOS
    # records as a SIGSEGV crash report on EVERY quit (long misdiagnosed as an
    # OpenSSL-teardown race). Tear it out at the source so SIGTERM reaches the
    # daemon's own handler. Best-effort; the daemon also re-claims the signals
    # after warmup as a backstop.
    try:
        import paddle

        paddle.disable_signal_handler()
    except Exception as exc:  # noqa: BLE001
        logger.warning("paddle.disable_signal_handler failed: %s", exc)
    try:
        # Pass BOTH name and dir: a dir without a name makes PaddleOCR default the
        # name to the version's default tier (PP-OCRv6_medium_*) and then reject our
        # dir on a name/config mismatch.
        engine = PaddleOCR(
            text_detection_model_name=f"PP-OCRv6_{tier}_det",
            text_detection_model_dir=det,
            text_recognition_model_name=f"PP-OCRv6_{tier}_rec",
            text_recognition_model_dir=rec,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device="cpu",
        )
        logger.info("local OCR engine ready (tier=%s)", tier)
        return engine
    except Exception as exc:  # noqa: BLE001
        logger.warning("local OCR engine build failed (tier=%s): %s", tier, exc)
        return None


def _get_engine(tier: str) -> Any | None:
    """Return the cached engine for ``tier``, building it on first use. Caller holds _lock.

    Honors the ``PERSOME_DISABLE_OCR`` kill-switch: when set, returns None without
    importing/building paddle, so every ``recognize*`` path fails open (no inference, no
    native-crash exposure). This is the single chokepoint all OCR entrypoints route through.
    """
    if _ocr_disabled() or not runtime_available():
        return None
    if tier not in _VALID_TIERS:
        tier = DEFAULT_TIER
    engine = _engines.get(tier)
    if engine is None:
        engine = _build_engine(tier)
        if engine is not None:
            _engines[tier] = engine
    return engine


def warm(tier: str = DEFAULT_TIER) -> None:
    """Pre-build the engine so the first real capture doesn't pay graph-load latency.

    No-op when OCR is disabled (``PERSOME_DISABLE_OCR``): paddle is never imported.
    Under isolation (default), warms the WORKER's engine (spawns it, builds its graphs);
    only the in-process fallback builds paddle inside the daemon.
    """
    if _ocr_disabled():
        logger.info("local OCR disabled via PERSOME_DISABLE_OCR; skipping warm")
        return
    if not runtime_available():
        logger.info(
            "local OCR runtime unavailable in this build (no paddlepaddle wheel for this arch, "
            "e.g. x86_64 macOS); AX-poor apps run without OCR text — see #226. Skipping warm"
        )
        return
    if _use_isolation():
        from . import ocr_subprocess

        ocr_subprocess.get_client().warm(tier)
        return
    with _lock:
        _get_engine(tier)


def recognize(image_bytes: bytes, tier: str = DEFAULT_TIER) -> str | None:
    """OCR a JPEG/PNG image. Returns newline-joined text lines, or None on any failure.

    Fail-open: a None return means "no OCR this time", never an exception to the caller.
    Routes to the isolated worker by default (``_use_isolation``); the kill-switch and the
    in-process debug path are handled by ``recognize_detailed``.
    """
    detailed = recognize_detailed(image_bytes, tier)
    if not detailed or not detailed[0]:
        return None
    return "\n".join(detailed[0])


def _recognize_detailed_inproc(
    image_bytes: bytes, tier: str = DEFAULT_TIER
) -> tuple[list[str], list[list[int]], list[float]] | None:
    """The actual paddle predict — imports cv2 + builds/uses the engine IN THIS PROCESS.

    In the daemon this only runs on the in-process fallback (``PERSOME_OCR_IN_PROCESS``);
    in production it runs inside the isolated worker. A native SIGSEGV here is exactly what
    isolation contains.
    """
    if not image_bytes:
        return None
    # Decode via PIL, not cv2: opencv is only a transitive dependency of paddle
    # (arm64-only), so cv2 is absent on hosts without the paddle wheels — while
    # this parse path must still work there under a stubbed engine (tests, and
    # any future non-paddle engine). Paddle expects BGR, so flip the channels.
    try:
        import io as _io

        import numpy as np
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        logger.warning("pillow/numpy import failed: %s", exc)
        return None

    try:
        img = Image.open(_io.BytesIO(image_bytes)).convert("RGB")
        arr = np.asarray(img)[:, :, ::-1].copy()  # RGB → BGR (cv2/paddle convention)
    except Exception as exc:  # noqa: BLE001
        logger.warning("local OCR: image decode failed: %s", exc)
        return None

    with _lock:
        engine = _get_engine(tier)
        if engine is None:
            return None
        try:
            results = engine.predict(arr)
        except Exception as exc:  # noqa: BLE001
            logger.warning("local OCR predict failed: %s", exc)
            return None

    texts, boxes, scores = _extract_detailed(results)
    return (texts, boxes, scores) if texts else None


def recognize_detailed(
    image_bytes: bytes, tier: str = DEFAULT_TIER
) -> tuple[list[str], list[list[int]], list[float]] | None:
    """OCR a JPEG/PNG image, keeping per-line geometry + confidence.

    Returns ``(texts, boxes, scores)`` where ``boxes[i]`` is ``[x0, y0, x1, y1]`` and
    ``scores[i]`` is the recognizer confidence (0..1) for ``texts[i]``; all three lists
    are aligned and the same length. Returns None on any failure (fail-open). The geometry
    is what ``recognize`` throws away — the downstream structurer (``ocr_structure``) needs
    it to reconstruct columns/regions and drop low-confidence fragments.

    Routing: the ``PERSOME_DISABLE_OCR`` kill-switch and the ``runtime_available()`` probe
    (no paddle wheel on this arch, e.g. x86_64 macOS — #226) short-circuit first, so neither the
    isolated worker nor the in-process path is even entered; then by default inference runs in the
    isolated crash-domain worker (``_use_isolation``); the ``PERSOME_OCR_IN_PROCESS`` debug
    hatch forces the legacy in-daemon predict.
    """
    if _ocr_disabled() or not runtime_available():
        return None
    if _use_isolation():
        from . import ocr_subprocess

        return ocr_subprocess.get_client().recognize_detailed(image_bytes, tier)
    return _recognize_detailed_inproc(image_bytes, tier)


def _extract_detailed(results: Any) -> tuple[list[str], list[list[int]], list[float]]:
    """Pull aligned ``rec_texts`` / ``rec_boxes`` / ``rec_scores`` from a PaddleOCR 3.x result.

    Boxes/scores are best-effort: when a result lacks them, each text gets a zero box
    ``[0,0,0,0]`` and score ``0.0`` so the three lists stay index-aligned (a degraded
    sample is still usable as raw text, just not geometrically structurable).
    """
    if not results:
        return [], [], []
    texts: list[str] = []
    boxes: list[list[int]] = []
    scores: list[float] = []

    def _field(r: Any, key: str) -> Any:
        v = r.get(key) if hasattr(r, "get") else None
        return v if v is not None else getattr(r, key, None)

    for r in results:
        r_texts = _field(r, "rec_texts") or []
        r_boxes = _field(r, "rec_boxes")
        r_scores = _field(r, "rec_scores")
        for i, t in enumerate(r_texts):
            texts.append(str(t))
            try:
                b = r_boxes[i] if r_boxes is not None else None
                boxes.append(
                    [int(b[0]), int(b[1]), int(b[2]), int(b[3])] if b is not None else [0, 0, 0, 0]
                )
            except Exception:  # noqa: BLE001
                boxes.append([0, 0, 0, 0])
            try:
                scores.append(float(r_scores[i]) if r_scores is not None else 0.0)
            except Exception:  # noqa: BLE001
                scores.append(0.0)
    return texts, boxes, scores
