"""bge-small-zh semantic discriminator for the slow-path pre-gate (#547 follow-up).

The slow pre-gate's lexical SLOW_ANCHOR_RE starves info_need / search / un-clocked
reminder / assignment intents (it was tuned for meeting/commitment cues). This
module is the optional smarter discriminator: a vendored **bge-small-zh** ONNX
encoder + a tiny trained logistic head scoring ``P(this block is worth an LLM
recognition call)``. ``recognizer.block_passes_gate`` consults it when
``pregate_mode`` is ``"bge"`` / ``"hybrid"``.

FAIL-OPEN BY DESIGN — mirrors ``capture/ocr_local``: any missing piece
(onnxruntime / tokenizers not installed, model dir absent, a bad file, a runtime
error) makes :func:`available` return False and :func:`score` return None, and
the caller falls back to the regex gate. The daemon never breaks because the
model isn't there; it just behaves like today.

Bundled like the OCR weights: ``gate_models/bge-small-zh/`` (model.int8.onnx +
tokenizer.json + head.npz), resolved via :func:`_models_root` (env →
PyInstaller ``sys._MEIPASS`` → vendored repo dir). The offline export+train
tooling that produces those files lives in ``scripts/`` (not shipped).
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

from ..logger import get

logger = get("persome.intent.gate_model")

_MODEL_SUBDIR = "bge-small-zh"
_ONNX_NAME = "model.int8.onnx"
_TOKENIZER_NAME = "tokenizer.json"
_HEAD_NAME = "head.npz"  # {"w": (d,), "b": (), "max_len": ()} logistic head over pooled emb

_MAX_LEN_FALLBACK = 256

_lock = threading.Lock()
_engine: _Engine | None = None
_load_failed = False  # cache a failed load so we don't retry every block


def _models_root() -> Path | None:
    """Locate the dir holding ``<_MODEL_SUBDIR>/`` (env → bundle → vendored repo)."""
    env = (os.environ.get("PERSOME_GATE_MODEL_DIR") or os.environ.get("MENS_CONTEXT_GATE_MODEL_DIR"))  # Mens is the legacy name
    if env:
        return Path(env)
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "gate_models"  # type: ignore[attr-defined]
    # src/persome/intent/gate_model.py -> persome-core/gate_models
    vendored = Path(__file__).resolve().parents[3] / "gate_models"
    return vendored if vendored.exists() else None


def _model_dir() -> Path | None:
    root = _models_root()
    if root is None:
        return None
    d = root / _MODEL_SUBDIR
    needed = [d / _ONNX_NAME, d / _TOKENIZER_NAME, d / _HEAD_NAME]
    return d if all(p.exists() for p in needed) else None


class _Engine:
    """Lazily-built ONNX session + tokenizer + logistic head. Built once, reused."""

    def __init__(self, model_dir: Path) -> None:
        import numpy as np  # noqa: PLC0415
        import onnxruntime as ort  # noqa: PLC0415
        from tokenizers import Tokenizer  # noqa: PLC0415

        self._np = np
        self.tokenizer = Tokenizer.from_file(str(model_dir / _TOKENIZER_NAME))
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_dir / _ONNX_NAME), sess_options=so, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self.session.get_inputs()}
        head = np.load(model_dir / _HEAD_NAME)
        self.w = head["w"].astype("float32").reshape(-1)
        self.b = float(head["b"])
        self.max_len = int(head["max_len"]) if "max_len" in head else _MAX_LEN_FALLBACK

    def embed_cls(self, text: str):
        """The L2-normalized CLS sentence embedding for ``text`` (np float32 vector).

        This is the shared encode path: the pre-gate's :meth:`score` runs the
        logistic head on top of it, and :mod:`intent.embeddings` reuses it raw for
        semantic-dedup cosine similarity — one ONNX session, no second load.
        """
        np = self._np
        enc = self.tokenizer.encode(text)
        ids = enc.ids[: self.max_len]
        mask = [1] * len(ids)
        feed: dict[str, Any] = {
            "input_ids": np.asarray([ids], dtype="int64"),
            "attention_mask": np.asarray([mask], dtype="int64"),
        }
        if "token_type_ids" in self._input_names:
            feed["token_type_ids"] = np.zeros((1, len(ids)), dtype="int64")
        feed = {k: v for k, v in feed.items() if k in self._input_names}
        out = self.session.run(None, feed)[0]  # (1, seq, dim) last_hidden_state
        cls = np.asarray(out)[0, 0]  # bge uses the CLS token as the sentence embedding
        norm = np.linalg.norm(cls)
        if norm > 0:
            cls = cls / norm
        return cls

    def score(self, text: str) -> float:
        np = self._np
        cls = self.embed_cls(text)
        logit = float(cls @ self.w + self.b)
        return 1.0 / (1.0 + np.exp(-logit))


def _get_engine() -> _Engine | None:
    global _engine, _load_failed
    if _engine is not None:
        return _engine
    if _load_failed:
        return None
    with _lock:
        if _engine is not None:
            return _engine
        if _load_failed:
            return None
        md = _model_dir()
        if md is None:
            _load_failed = True
            return None
        try:
            _engine = _Engine(md)
            logger.info("bge pre-gate model loaded from %s", md)
            return _engine
        except Exception as exc:  # noqa: BLE001 — fail-open: any error → regex fallback
            logger.warning("bge pre-gate model load failed (%s); falling back to regex", exc)
            _load_failed = True
            return None


def available() -> bool:
    """True iff the bge gate can score (model vendored + runtime importable)."""
    return _get_engine() is not None


def score(text: str) -> float | None:
    """P(block worth an LLM call) in [0,1], or None when the model is unavailable
    or scoring raised (caller then falls back to the regex gate)."""
    eng = _get_engine()
    if eng is None:
        return None
    try:
        return eng.score(text)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("bge pre-gate score failed (%s); regex fallback", exc)
        return None


def default_threshold() -> float:
    """The configured pass threshold (``intent_recognizer.pregate_bge_threshold``)."""
    try:
        from .. import config as config_mod  # noqa: PLC0415

        return float(config_mod.load().intent_recognizer.pregate_bge_threshold)
    except Exception:
        return 0.5
