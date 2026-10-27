"""Shared nonces and replay primitives the Redis and in-process stores both implement.

Extracted here so neither attestation.py nor redis_cache.py has to import each
other — the in-process double lives here, and the Redis-backed version subclasses
it without creating a cycle.
"""

from __future__ import annotations

import threading

from . import clock, crypto
from .errors import AttestationError


class NonceStore:
    """Single-use nonces with expiry (in-process; see LIMITATIONS.md)."""

    def __init__(self, ttl_seconds: int = 300, clock=clock.now_unix) -> None:
        self.ttl_seconds = ttl_seconds
        self._nonces: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def issue(self, node_id: str) -> str:
        nonce = crypto.new_session_nonce()
        now = int(self._clock())
        with self._lock:
            self._nonces[nonce] = (node_id, now + self.ttl_seconds)
        return nonce

    def consume(self, nonce: str, node_id: str) -> None:
        """Spend the nonce exactly once, even under concurrent callers.

        The pop happens under the lock and *before* validation, so a loser of a
        race sees a missing key instead of the record and cannot be attested
        with the same nonce. ``test_ai_redis.py::TestNonceConsumptionIsAtomic``
        races eight threads at one nonce and requires exactly one winner; a
        get-without-pop implementation passes every sequential test and fails
        that one.
        """
        now = int(self._clock())
        with self._lock:
            record = self._nonces.pop(nonce, None)
        if record is None:
            raise AttestationError(
                "unknown or already-used attestation nonce", code="stale_nonce"
            )
        expected_node, expires_at = record
        if expected_node != node_id:
            raise AttestationError(
                "nonce was issued for a different node", code="nonce_node_mismatch"
            )
        if now > expires_at:
            raise AttestationError("attestation nonce has expired", code="stale_nonce")
