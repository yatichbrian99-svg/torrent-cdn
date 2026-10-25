"""Thin, auditable wrappers over ``cryptography`` primitives.

No algorithm is implemented here. This module exists to make the *safe* call the
easy one and to remove the two mistakes that hand-written call sites keep making:

1. **No domain separation** — a signature over one kind of object replayable as
   another. Every signature covers ``domain || 0x00 || canonical_json(payload)``
   with a distinct domain per document type (see ASSUMPTIONS.md A5).
2. **Nonce/AAD drift** — AEAD always uses a fresh OS-random 96-bit nonce, and
   callers must pass associated data (job id, sequence, direction) so a
   ciphertext cannot be moved between jobs or reordered.

Deliberate choices, documented because the prompt allowed alternatives:
* AES-256-GCM, not XChaCha20-Poly1305: ``cryptography`` ships the former; adding
  libsodium for the latter would add a second native dependency for no gain at
  our message volumes (random 96-bit nonces are safe far below 2^32 messages
  per key, and each job key is single-use).
* API keys are stored as HMAC-SHA-256 with a server-side pepper, never plain
  SHA-256 — an unkeyed hash of a low-entropy secret is offline-guessable.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import hmac
import json
import os
import secrets
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import CryptoError

#: 32-byte symmetric keys, 12-byte GCM nonces, 32-byte raw public keys.
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
API_KEY_PREFIX = "tcdn_ai_"
API_KEY_RANDOM_BYTES = 32


# ── encoding ────────────────────────────────────────────────────────────────


def b64e(data: bytes) -> str:
    """URL-safe base64 without padding (safe in JSON, URLs, and env vars)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(value: str) -> bytes:
    """Strict decode. Invalid base64 is a crypto failure, never silently empty."""
    if not isinstance(value, str) or not value:
        raise CryptoError("malformed key material", code="malformed_key")
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise CryptoError("malformed key material", code="malformed_key") from exc


def canonical_json(payload: dict[str, Any]) -> bytes:
    """Deterministic JSON used as the signature input.

    Sorted keys, no whitespace, UTF-8, no ASCII escaping: two processes signing
    the same logical payload produce identical bytes.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def constant_time_eq(left: bytes | str, right: bytes | str) -> bool:
    left_b = left.encode() if isinstance(left, str) else left
    right_b = right.encode() if isinstance(right, str) else right
    return hmac.compare_digest(left_b, right_b)


def domain_message(domain: str, payload: dict[str, Any]) -> bytes:
    """``domain || 0x00 || canonical_json`` — one signature cannot be reused
    as a signature over a different document type."""
    if not domain or "\x00" in domain:
        raise CryptoError("invalid signature domain", code="invalid_domain")
    return domain.encode("ascii") + b"\x00" + canonical_json(payload)


def zeroise(buf: bytearray) -> None:
    """Best-effort wipe of a mutable buffer.

    CPython may have copied the value elsewhere (and the GC may already have
    moved it), so this is documented as best-effort, never as a guarantee —
    see SECURITY.md "What is not claimed".
    """
    if not isinstance(buf, bytearray) or not buf:
        return
    try:
        ctypes.memset((ctypes.c_char * len(buf)).from_buffer(buf), 0, len(buf))
    except (TypeError, ValueError):  # pragma: no cover - defensive
        for i in range(len(buf)):
            buf[i] = 0


# ── Ed25519 signatures ──────────────────────────────────────────────────────


def generate_ed25519() -> dict[str, str]:
    private = Ed25519PrivateKey.generate()
    return {
        "private_key": b64e(private.private_bytes_raw()),
        "public_key": b64e(private.public_key().public_bytes_raw()),
    }


def ed25519_public_from_private(private_b64: str) -> str:
    try:
        key = Ed25519PrivateKey.from_private_bytes(b64d(private_b64))
    except (ValueError, TypeError) as exc:
        raise CryptoError("malformed Ed25519 private key", code="malformed_key") from exc
    return b64e(key.public_key().public_bytes_raw())


def key_id_for(public_b64: str) -> str:
    """Stable, non-secret key id derived from the public key."""
    return sha256_hex(b64d(public_b64))[:16]


def sign(domain: str, payload: dict[str, Any], private_b64: str) -> str:
    try:
        key = Ed25519PrivateKey.from_private_bytes(b64d(private_b64))
    except (ValueError, TypeError) as exc:
        raise CryptoError("malformed Ed25519 private key", code="malformed_key") from exc
    return b64e(key.sign(domain_message(domain, payload)))


def verify(domain: str, payload: dict[str, Any], signature_b64: str, public_b64: str) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(b64d(public_b64))
        key.verify(b64d(signature_b64), domain_message(domain, payload))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# ── X25519 key agreement + HKDF ─────────────────────────────────────────────


def generate_x25519() -> dict[str, str]:
    private = X25519PrivateKey.generate()
    return {
        "private_key": b64e(private.private_bytes_raw()),
        "public_key": b64e(private.public_key().public_bytes_raw()),
    }


def x25519_shared_secret(private_b64: str, peer_public_b64: str) -> bytes:
    try:
        private = X25519PrivateKey.from_private_bytes(b64d(private_b64))
        peer = X25519PublicKey.from_public_bytes(b64d(peer_public_b64))
        secret = private.exchange(peer)
    except (ValueError, TypeError) as exc:
        raise CryptoError("key agreement failed", code="key_agreement_failed") from exc
    if not secret or secret == b"\x00" * KEY_BYTES:
        # Low-order / all-zero shared secret: reject rather than derive a key
        # an attacker also knows.
        raise CryptoError("key agreement produced a degenerate secret", code="key_agreement_failed")
    return secret


def hkdf_sha256(
    secret: bytes, *, salt: bytes, info: bytes, length: int = KEY_BYTES
) -> bytes:
    if not salt:
        raise CryptoError("HKDF salt must be supplied", code="bad_kdf_parameters")
    if not info:
        raise CryptoError("HKDF info must be supplied (domain separation)", code="bad_kdf_parameters")
    return HKDF(
        algorithm=hashes.SHA256(), length=length, salt=salt, info=info
    ).derive(secret)


def derive_session_keys(
    shared_secret: bytes, *, salt: bytes, context: str
) -> tuple[bytes, bytes]:
    """Derive independent client→worker and worker→client keys.

    Separate info strings mean a compromised direction key cannot decrypt the
    other direction, and ``context`` (normally the job id) binds the keys to one
    job.
    """
    client_to_worker = hkdf_sha256(
        shared_secret, salt=salt, info=f"{context}/c2w".encode()
    )
    worker_to_client = hkdf_sha256(
        shared_secret, salt=salt, info=f"{context}/w2c".encode()
    )
    return client_to_worker, worker_to_client


# ── AES-256-GCM ─────────────────────────────────────────────────────────────


def aead_encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    if len(key) != KEY_BYTES:
        raise CryptoError("AEAD key must be 32 bytes", code="bad_key_length")
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def aead_decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(key) != KEY_BYTES:
        raise CryptoError("AEAD key must be 32 bytes", code="bad_key_length")
    if len(blob) < NONCE_BYTES + TAG_BYTES:
        raise CryptoError("ciphertext truncated", code="aead_auth_failed")
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except (InvalidTag, ValueError) as exc:
        # Do not distinguish tampering from a wrong key: both are rejections.
        raise CryptoError("AEAD authentication failed", code="aead_auth_failed") from exc


# ── API keys ────────────────────────────────────────────────────────────────


def hash_api_key(raw: str, pepper: bytes) -> str:
    if not pepper:
        raise CryptoError("API key pepper must be set", code="missing_pepper")
    return hmac.new(pepper, raw.encode("utf-8"), hashlib.sha256).hexdigest()


def new_api_key(pepper: bytes) -> tuple[str, str, str]:
    """Return ``(raw, prefix, hmac_sha256_hex)``.

    The caller stores only the prefix and hash. The raw value is shown to the
    developer exactly once and never logged.
    """
    if not pepper:
        raise CryptoError("API key pepper must be set", code="missing_pepper")
    raw = API_KEY_PREFIX + secrets.token_urlsafe(API_KEY_RANDOM_BYTES)
    return raw, raw[:20], hash_api_key(raw, pepper)


def new_session_nonce() -> str:
    return secrets.token_urlsafe(24)
