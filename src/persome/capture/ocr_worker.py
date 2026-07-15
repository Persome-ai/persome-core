"""Isolated OCR worker — the child process that owns native OCR inference.

Spawned by the daemon as ``Persome Backend _ocr-worker`` (frozen) / ``python -m persome.cli
_ocr-worker`` (dev). On Apple Silicon this is the only process that imports Paddle; on
Intel it owns the Apple Vision helper. A native fault kills just this worker, and the
parent (``ocr_subprocess.OCRWorkerClient``) fails open + respawns.

The worker reads length-prefixed request frames on **stdin** and writes response frames on
**stdout** (``ocr_protocol``). stdout is the data channel and MUST stay clean — all logging
goes to the rotating file sinks / stderr, never stdout.
"""

from __future__ import annotations

import os
import sys

from ..logger import get
from . import ocr_local, ocr_protocol

logger = get("persome.capture.ocr.worker")


def serve() -> int:
    """Run the request→response loop until stdin closes. Returns a process exit code.

    Marks the environment so any routed OCR call inside this process resolves in-proc
    (a worker must never spawn another worker). Engine build happens lazily on the first
    request via ``ocr_local._recognize_detailed_inproc`` (an empty image = warm-only).
    """
    os.environ["PERSOME_OCR_WORKER"] = "1"
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    logger.info("ocr worker started (pid=%s)", os.getpid())

    while True:
        body = ocr_protocol.read_frame(stdin)
        if body is None:
            logger.info("ocr worker: stdin closed, exiting")
            return 0
        try:
            tier, image = ocr_protocol.decode_request(body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ocr worker: bad request frame: %s", exc)
            _reply(stdout, None)
            continue

        result: ocr_protocol.Detailed | None
        if not image:
            # Warm request: build the engine now so the first real capture is fast.
            result = ([], [], []) if ocr_local.warm(tier) else None
        else:
            # A native fault inside here takes down ONLY this process (by design).
            try:
                result = ocr_local._recognize_detailed_inproc(image, tier)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ocr worker: recognize failed: %s", exc)
                result = None
        _reply(stdout, result)


def _reply(stdout, result: ocr_protocol.Detailed | None) -> None:
    try:
        ocr_protocol.write_frame(stdout, ocr_protocol.encode_response(result))
    except Exception as exc:  # noqa: BLE001
        # The parent went away mid-reply; nothing else to do but exit the loop caller.
        logger.info("ocr worker: reply failed (parent gone?): %s", exc)
        raise
