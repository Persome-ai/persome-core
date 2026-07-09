"""Sentence embeddings for semantic intent dedup.

Reuses the bundled **bge-small-zh** int8 ONNX encoder that :mod:`intent.gate_model`
already loads for the slow pre-gate — same singleton session, no second model load,
no new dependency (onnxruntime + tokenizers are hard deps).

Purpose: the sink's content fold (``intent.sink._find_content_fold_target``) matches
re-statements of the same commitment by **char-bigram Jaccard**, which captures only
surface overlap — a paraphrase ("修复Mens bug：把动画去掉…" vs "去掉动画，修复…导航bug")
sits at ~0.3 and never folds, so the SAME fact fans into N open rows across sessions.
A dense sentence embedding sees the shared *meaning*; cosine over the two bodies folds
the paraphrases the lexical layer misses.

**Fail-open**: if the model / onnxruntime is unavailable (``available()`` is False) or
encoding raises, :func:`embed` returns ``None`` and the caller falls back to the
char-bigram path — exactly like the pre-gate falls back to the regex gate.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import TYPE_CHECKING

from . import gate_model

if TYPE_CHECKING:
    import numpy as np

# Bounded LRU so repeated candidate bodies within one persist (and across nearby
# persists) are embedded once. Keyed on the EXACT (already-normalized) body string
# the sink passes in. None results are cached too — a non-embeddable body shouldn't
# re-run the encoder every comparison.
_CACHE_MAX = 512
_MISS = object()
_cache: OrderedDict[str, object] = OrderedDict()
_cache_lock = threading.Lock()


def available() -> bool:
    """True iff the bge encoder can produce embeddings (model vendored + runtime importable)."""
    return gate_model.available()


def embed(text: str) -> np.ndarray | None:
    """L2-normalized 512-d CLS embedding for ``text``, or ``None`` when the model is
    unavailable / encoding raised. Bounded-LRU cached on the exact string."""
    if not text:
        return None
    with _cache_lock:
        cached = _cache.get(text, _MISS)
        if cached is not _MISS:
            _cache.move_to_end(text)
            return cached  # type: ignore[return-value]
    eng = gate_model._get_engine()
    vec: np.ndarray | None = None
    if eng is not None:
        try:
            vec = eng.embed_cls(text)
        except Exception:  # noqa: BLE001 — fail-open; caller falls back to Jaccard
            vec = None
    with _cache_lock:
        _cache[text] = vec
        _cache.move_to_end(text)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return vec


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """Cosine similarity of two embeddings. Inputs are already L2-normalized, so this
    is just their dot product; ``None`` on either side yields 0.0 (no fold)."""
    if a is None or b is None:
        return 0.0
    return float((a * b).sum())


def similarity(a: str, b: str) -> float:
    """Convenience: cosine similarity of two raw strings' embeddings, in [−1, 1]
    (≈[0, 1] for these short work strings). 0.0 when the model is unavailable."""
    return cosine(embed(a), embed(b))


def _reset_cache_for_tests() -> None:
    """Test hook: clear the LRU so a threshold sweep doesn't see stale vectors."""
    with _cache_lock:
        _cache.clear()
