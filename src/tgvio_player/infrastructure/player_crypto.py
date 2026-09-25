from __future__ import annotations

import base64
import binascii
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_MAGIC = b"TGVIO\x01"
_NONCE_BYTES = 12
_AAD_PREFIX = b"tgvio-player-state-envelope-v1\x00"
_KDF_SALT = b"tgvio-player-recovery-key-v1"


def decode_recovery_key(value: str) -> bytes:
    """Decode an unpadded URL-safe base64 encoding of exactly 32 random bytes."""
    if not isinstance(value, str) or not _KEY_RE.fullmatch(value):
        raise ValueError("TGVIO_PLAYER_RECOVERY_KEY must be a URL-safe 256-bit key")
    try:
        key = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("TGVIO_PLAYER_RECOVERY_KEY must be a URL-safe 256-bit key") from exc
    if len(key) != 32:
        raise ValueError("TGVIO_PLAYER_RECOVERY_KEY must be a URL-safe 256-bit key")
    return key


class PlayerStateCipher:
    """Authenticated encryption for durable Player state snapshots."""

    def __init__(self, recovery_key: str) -> None:
        key = decode_recovery_key(recovery_key)
        self._key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=_KDF_SALT,
            info=b"tgvio-player-state-aes-256-gcm-v1",
        ).derive(key)

    def encrypt(self, payload: bytes, *, context: bytes) -> bytes:
        if not isinstance(payload, bytes) or not isinstance(context, bytes) or not context:
            raise ValueError("payload and non-empty byte context are required")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = AESGCM(self._key).encrypt(nonce, payload, _AAD_PREFIX + context)
        return _MAGIC + nonce + ciphertext

    def decrypt(self, envelope: bytes, *, context: bytes) -> bytes:
        if not isinstance(envelope, bytes) or not isinstance(context, bytes) or not context:
            raise ValueError("invalid encrypted Player state")
        minimum = len(_MAGIC) + _NONCE_BYTES + 16
        if len(envelope) < minimum or not envelope.startswith(_MAGIC):
            raise ValueError("invalid encrypted Player state")
        nonce_start = len(_MAGIC)
        nonce = envelope[nonce_start:nonce_start + _NONCE_BYTES]
        ciphertext = envelope[nonce_start + _NONCE_BYTES:]
        try:
            return AESGCM(self._key).decrypt(nonce, ciphertext, _AAD_PREFIX + context)
        except InvalidTag as exc:
            raise ValueError("encrypted Player state authentication failed") from exc
