"""Verification voucher signing (Part 3 milestone 1).

The Part 3 build spec names ``verifier_sign::ed25519_sign`` as the single
signing helper every verification document goes through, and
:class:`VerificationVoucher` as the envelope those documents are signed into.

This module is that seam. It does not invent crypto: the Ed25519 operation is
:func:`app.ai.crypto.sign`, which signs ``domain || 0x00 || canonical_json`` —
the same construction tickets, receipts and verdict documents use, with a
domain that cannot be replayed as any of them.

Only the three Part 3 domains are accepted. A caller cannot pass an arbitrary
domain string and get a signature over it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import crypto


class VerificationVoucherDomain:
    """Allowed signing domains for verification vouchers.

    Domains are explicit and disjoint from the receipt / ticket domains so a
    voucher can never be misused as a coordinator receipt, and vice versa.
    """

    CHALLENGE_RESPONSE = "tcdn-ai/verification2/challenge-response/v1"
    VERIFIER_VERDICT = "tcdn-ai/verification2/verifier-verdict/v1"
    VERIFIER_RECEIPT = "tcdn-ai/verification2/verifier-receipt/v1"


ALLOWED_DOMAINS = frozenset(
    {
        VerificationVoucherDomain.CHALLENGE_RESPONSE,
        VerificationVoucherDomain.VERIFIER_VERDICT,
        VerificationVoucherDomain.VERIFIER_RECEIPT,
    }
)


class VoucherVerificationError(ValueError):
    """A voucher is malformed, has the wrong domain, or fails its signature."""


@dataclass(frozen=True)
class VerificationVoucher:
    """Signed envelope for verification documents.

    The field names and semantics are stable so the generated protobuf message
    from the Part 3 ``.proto`` can replace this dataclass without touching
    callers. The signature covers the ``payload`` under ``domain``; it does not
    cover ``key_id`` or ``alg``, so both are checked separately during
    verification (``key_id`` must be the id of the verifying key).
    """

    domain: str
    payload: dict[str, Any]
    key_id: str
    alg: str
    sig: str

    def payload_bytes(self) -> bytes:
        """Deterministic serialisation of the payload."""
        return _canonical_payload_bytes(self.payload)

    def document(self) -> dict[str, Any]:
        """JSON-safe document form (what goes on the wire)."""
        return {
            "domain": self.domain,
            "payload": self.payload,
            "key_id": self.key_id,
            "alg": self.alg,
            "sig": self.sig,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> VerificationVoucher:
        if not isinstance(document, dict):
            raise VoucherVerificationError("voucher must be an object")
        try:
            domain = document["domain"]
            payload = document["payload"]
            key_id = document["key_id"]
            alg = document["alg"]
            sig = document["sig"]
        except KeyError as exc:
            raise VoucherVerificationError(
                f"voucher is missing {exc.args[0]!r}"
            ) from exc
        if not all(
            isinstance(value, str) and value for value in (domain, key_id, alg, sig)
        ):
            raise VoucherVerificationError("voucher fields must be non-empty strings")
        if not isinstance(payload, dict):
            raise VoucherVerificationError("voucher payload must be an object")
        return cls(domain=domain, payload=payload, key_id=key_id, alg=alg, sig=sig)


def _canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    """Deterministic canonical bytes for a verification payload."""
    if not isinstance(payload, dict):
        raise TypeError("verification payload must be a dict")
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _domain_value(domain: VerificationVoucherDomain | str) -> str:
    value = domain.value if isinstance(domain, VerificationVoucherDomain) else domain
    if not isinstance(value, str) or value not in ALLOWED_DOMAINS:
        raise ValueError(f"unsupported verification voucher domain: {value!r}")
    return value


def ed25519_sign(
    domain: VerificationVoucherDomain | str,
    payload: dict[str, Any],
    *,
    key: str,
) -> VerificationVoucher:
    """Sign a verification payload and return a :class:`VerificationVoucher`.

    Args:
        domain: one of the three :class:`VerificationVoucherDomain` values.
            Arbitrary strings are rejected — the domain is a security boundary,
            not a label.
        payload: the structured payload to sign. It must be JSON-serialisable
            and must not contain prompt or output plaintext.
        key: the signer's Ed25519 private key, base64url (unpadded) — the same
            representation :mod:`app.ai.crypto` uses everywhere else.

    Raises:
        ValueError: unknown domain.
        TypeError: payload is not a dict or the key is not a string.
        CryptoError: malformed private key (from :mod:`app.ai.crypto`).
    """
    domain_str = _domain_value(domain)
    if not isinstance(key, str):
        raise TypeError("Ed25519 private key must be a base64url string")
    _canonical_payload_bytes(payload)

    return VerificationVoucher(
        domain=domain_str,
        payload=payload,
        key_id=crypto.key_id_for(crypto.ed25519_public_from_private(key)),
        alg="Ed25519",
        sig=crypto.sign(domain_str, payload, key),
    )


def verify_voucher(
    voucher: VerificationVoucher | dict[str, Any],
    *,
    public_key_b64: str,
) -> VerificationVoucher:
    """Verify a voucher's signature and key binding.

    Checks, in order:

    * the document is well-formed and uses ``alg = Ed25519``;
    * the domain is one of the three Part 3 domains;
    * ``key_id`` is the id of the supplied public key (so a valid signature by
      some other key cannot be relabelled);
    * the Ed25519 signature over ``domain || 0x00 || canonical_json(payload)``
      verifies.

    Returns the parsed voucher, or raises :class:`VoucherVerificationError`.
    """
    parsed = (
        voucher
        if isinstance(voucher, VerificationVoucher)
        else VerificationVoucher.from_document(voucher)
    )
    if parsed.alg != "Ed25519":
        raise VoucherVerificationError(f"unsupported voucher algorithm: {parsed.alg!r}")
    if parsed.domain not in ALLOWED_DOMAINS:
        raise VoucherVerificationError(f"unsupported voucher domain: {parsed.domain!r}")
    if not isinstance(public_key_b64, str) or not public_key_b64:
        raise VoucherVerificationError("verifying key is missing")
    try:
        expected_key_id = crypto.key_id_for(public_key_b64)
    except crypto.CryptoError as exc:
        raise VoucherVerificationError("verifying key is malformed") from exc
    if parsed.key_id != expected_key_id:
        raise VoucherVerificationError(
            "voucher key_id does not match the verifying key"
        )
    if not crypto.verify(parsed.domain, parsed.payload, parsed.sig, public_key_b64):
        raise VoucherVerificationError("voucher signature is invalid")
    return parsed
