"""SDK crypto: vetted primitives only, plus a TLS 1.3-only SSL context.

The SDK intentionally does not import the backend package — it is a standalone
client. The primitives and domain strings are identical (see
``docs/tcdn-ai/SDK-PORT-SPEC.md``).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import ssl

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .protocol import ProtocolError, b64d, b64e, canonical_json

KEY_BYTES = 32
NONCE_BYTES = 12


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ticket_hash(document: dict) -> str:
    return sha256_hex(canonical_json(document))


def verify_ed25519(public_b64: str, message: bytes, signature_b64: str) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(b64d(public_b64))
        key.verify(b64d(signature_b64), message)
        return True
    except (InvalidSignature, ProtocolError, ValueError, TypeError):
        return False


def domain_message(domain: str, payload: dict) -> bytes:
    if not domain or "\x00" in domain:
        raise ProtocolError("invalid signature domain")
    return domain.encode("ascii") + b"\x00" + canonical_json(payload)


def new_x25519() -> tuple[str, str]:
    private = X25519PrivateKey.generate()
    return b64e(private.private_bytes_raw()), b64e(
        private.public_key().public_bytes_raw()
    )


def shared_secret(private_b64: str, peer_public_b64: str) -> bytes:
    private = X25519PrivateKey.from_private_bytes(b64d(private_b64))
    peer = X25519PublicKey.from_public_bytes(b64d(peer_public_b64))
    secret = private.exchange(peer)
    if not secret or secret == b"\x00" * KEY_BYTES:
        raise ProtocolError("degenerate key agreement result")
    return secret


def derive_keys(shared: bytes, *, salt: bytes, context: str) -> tuple[bytes, bytes]:
    def derive(direction: str) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=KEY_BYTES,
            salt=salt,
            info=f"{context}/{direction}".encode(),
        ).derive(shared)

    return derive("c2w"), derive("w2c")


def aead_encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def aead_decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if len(blob) < NONCE_BYTES + 16:
        raise ProtocolError("ciphertext truncated")
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except (InvalidTag, ValueError) as exc:
        raise ProtocolError("AEAD authentication failed") from exc


def constant_time_eq(left: bytes | str, right: bytes | str) -> bool:
    left_b = left.encode() if isinstance(left, str) else left
    right_b = right.encode() if isinstance(right, str) else right
    return hmac.compare_digest(left_b, right_b)


def tls_context() -> ssl.SSLContext:
    """TLS 1.3 only, certificate validation mandatory, compression disabled.

    There is no code path that disables verification: the SDK never accepts an
    ``insecure`` flag, so a misconfigured private CA must be trusted properly
    rather than skipped.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.options |= ssl.OP_NO_COMPRESSION
    return context
