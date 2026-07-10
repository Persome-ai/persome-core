"""AES-256-GCM at-rest encryption for capture-buffer screenshots (spec E5 / TODO #6).

Capture screenshots land in ``~/.persome/capture-buffer/<ts>.json`` as a
base64 string under ``screenshot.image_base64`` and are stripped after 24h. This
module lets the scheduler encrypt that base64 payload at rest with an AES-256-GCM
envelope whose key comes **only from the environment** (``PERSOME_SCREENSHOT_KEY``,
64 hex chars = 32 bytes).

The standalone installer generates the key into the owner-only Runtime env file;
an embedding product may instead mirror it from its secure store. This module is
the Python consumer only: it never reads a Keychain or writes the key itself and
only consumes ``os.environ``.

Envelope layout (string, JSON-safe so it drops straight into ``image_base64``):

    "PSOMEGCM1:" + base64( iv(12) || ciphertext || tag(16) )

``is_encrypted`` recognises the ``PSOMEGCM1:`` magic so old plaintext captures stay
readable; ``read_screenshot`` is the one decode chokepoint every reader routes
through (decrypts a sealed payload when a key is present, else returns the
plaintext bytes verbatim).
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..logger import get

logger = get("persome.capture")

#: Key env var — 64 hex chars (32 raw bytes, AES-256). The standalone installer
#: provisions it; embedding products may inject it from their own secure store.
KEY_ENV = "PERSOME_SCREENSHOT_KEY"

#: Magic prefix marking a sealed envelope. Versioned so the format can evolve
#: without mistaking a future scheme for this one (or for raw base64 plaintext).
MAGIC = "PSOMEGCM1:"

_KEY_BYTES = 32  # AES-256
_IV_BYTES = 12  # GCM standard nonce length


def load_key() -> bytes | None:
    """Read the 32-byte AES key from ``PERSOME_SCREENSHOT_KEY`` (64 hex chars).

    Returns ``None`` when the var is absent, blank, or not exactly 32 bytes of
    valid hex — the caller treats ``None`` as "encryption unavailable" and falls
    back to plaintext. Never raises.
    """
    raw = os.environ.get(KEY_ENV)
    if not raw:
        return None
    raw = raw.strip()
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        logger.warning("%s is set but is not valid hex; ignoring", KEY_ENV)
        return None
    if len(key) != _KEY_BYTES:
        logger.warning(
            "%s must be %d hex chars (%d bytes); got %d bytes; ignoring",
            KEY_ENV,
            _KEY_BYTES * 2,
            _KEY_BYTES,
            len(key),
        )
        return None
    return key


def is_encrypted(value: Any) -> bool:
    """True iff ``value`` is a sealed envelope (starts with the magic prefix).

    Plaintext base64 (old captures) and non-strings return False, so readers can
    branch cheaply without a key.
    """
    return isinstance(value, str) and value.startswith(MAGIC)


def encrypt(b64_or_bytes: str | bytes, key: bytes) -> str:
    """Seal a screenshot payload into a ``PSOMEGCM1:`` AES-256-GCM envelope.

    Accepts either the raw image bytes or a base64 string (what the scheduler
    already holds in ``image_base64``); either way the *original* bytes are what
    get sealed, so ``decrypt`` round-trips to exactly the input. The output is a
    JSON-safe string suitable for the ``image_base64`` field.
    """
    if isinstance(b64_or_bytes, str):
        plaintext = b64_or_bytes.encode("ascii")
    else:
        plaintext = bytes(b64_or_bytes)
    iv = os.urandom(_IV_BYTES)
    # AESGCM appends the 16-byte tag to the ciphertext.
    sealed = AESGCM(key).encrypt(iv, plaintext, None)
    return MAGIC + base64.b64encode(iv + sealed).decode("ascii")


def decrypt(envelope: str, key: bytes) -> bytes:
    """Open a ``PSOMEGCM1:`` envelope, returning the original payload bytes.

    Raises ``ValueError`` on a malformed/short envelope or a wrong key /
    tampered ciphertext (GCM auth failure) — callers decide whether to fail open.
    """
    if not is_encrypted(envelope):
        raise ValueError("not a PSOMEGCM1 envelope")
    body = envelope[len(MAGIC) :]
    try:
        blob = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"screenshot envelope base64 invalid: {exc}") from exc
    if len(blob) < _IV_BYTES + 16:  # iv + at least the GCM tag
        raise ValueError("screenshot envelope too short")
    iv, sealed = blob[:_IV_BYTES], blob[_IV_BYTES:]
    try:
        return AESGCM(key).decrypt(iv, sealed, None)
    except InvalidTag as exc:
        raise ValueError("screenshot envelope auth failed (wrong key/tampered)") from exc


def read_screenshot(capture_dict: dict[str, Any]) -> bytes | None:
    """Unified read chokepoint: return a capture's screenshot as raw image bytes.

    - sealed payload + key present  → decrypted bytes
    - sealed payload + no/bad key   → ``None`` (can't read; never raises)
    - plaintext base64 (old/unenc.) → decoded bytes
    - no screenshot / stripped      → ``None``

    The ``image_base64`` value stays whatever it was on disk (a base64 string,
    encrypted or not); only the decode path differs. Readers that previously did
    ``shot["image_base64"]`` route here so encryption is transparent to them while
    plaintext and key-less reads behave exactly as before.
    """
    shot = capture_dict.get("screenshot") or {}
    if not isinstance(shot, dict):
        return None
    value = shot.get("image_base64")
    if not value or not isinstance(value, str):
        return None
    if is_encrypted(value):
        key = load_key()
        if key is None:
            logger.warning("screenshot is encrypted but %s is unavailable; cannot read", KEY_ENV)
            return None
        try:
            # The envelope seals the base64 *string* the scheduler held (so the
            # dict shape is unchanged); decrypt yields that base64 text, which we
            # then decode to the raw image bytes — matching the plaintext branch.
            inner = decrypt(value, key)
        except ValueError as exc:
            logger.warning("screenshot decrypt failed: %s", exc)
            return None
        return _b64_to_bytes(inner.decode("ascii", "replace"))
    # Plaintext base64 (legacy / encryption off) — decode as the readers expect.
    return _b64_to_bytes(value)


def _b64_to_bytes(b64: str) -> bytes | None:
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        logger.warning("screenshot base64 decode failed: %s", exc)
        return None
