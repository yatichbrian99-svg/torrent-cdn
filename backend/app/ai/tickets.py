"""Placement tickets: the coordinator's signed permission for one job on one node.

Properties enforced on every verification (all fail closed):

* Ed25519 signature by a key the caller's keyring knows and has not revoked.
* Correct domain — a receipt signature cannot be replayed as a ticket.
* ``expires_at`` in the future within a bounded skew tolerance.
* The ticket is **single use**: its ``jti`` is claimed in the replay cache.
* Bindings match: job id, node id, and the confidentiality tier. A standard
  ticket cannot be upgraded at the worker, and a confidential ticket cannot be
  used for a different model than the one priced.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any, Protocol

from pydantic import ValidationError as PydanticValidationError

from . import crypto
from .clock import now_unix
from .errors import ExpiredError, ReplayError, TicketError
from .keyring import KeyRing
from .models import Confidentiality, PlacementTicket
from .redis_cache import RedisReplayCache

TICKET_DOMAIN = "tcdn-ai/ticket/v1"


class ReplayCache(Protocol):
    def claim(self, key: str, expires_at: int) -> bool:
        """Return True the first time ``key`` is seen, False afterwards."""
        ...


class MemoryReplayCache:
    """Process-local seen-set with expiry.

    Adequate for the current single-coordinator deployment; a multi-instance
    deployment must use the Redis-backed implementation documented in
    LIMITATIONS.md (the protocol is identical).
    """

    def __init__(self, clock=now_unix) -> None:
        self._seen: dict[str, int] = {}
        self._lock = threading.Lock()
        self._clock = clock

    def claim(self, key: str, expires_at: int) -> bool:
        now = int(self._clock())
        with self._lock:
            if len(self._seen) > 10_000:
                self._seen = {k: v for k, v in self._seen.items() if v >= now}
            seen_until = self._seen.get(key)
            if seen_until is not None and seen_until >= now:
                return False
            self._seen[key] = expires_at
            return True


def ticket_hash(document: dict[str, Any]) -> str:
    return hashlib.sha256(crypto.canonical_json(document)).hexdigest()


class TicketService:
    def __init__(
        self,
        keyring: KeyRing,
        *,
        ttl_seconds: int = 120,
        skew_seconds: int = 5,
        replay: ReplayCache | None = None,
        clock=now_unix,
    ) -> None:
        self.keyring = keyring
        self.ttl_seconds = ttl_seconds
        self.skew_seconds = skew_seconds
        self.replay = replay or MemoryReplayCache(clock=clock)
        self._clock = clock

    @staticmethod
    def from_redis(
        keyring: KeyRing,
        redis_client: Any,
        *,
        ttl_seconds: int = 120,
        skew_seconds: int = 5,
        clock=now_unix,
    ) -> TicketService:
        """Build a TicketService backed by Redis when Redis is available.

        ``redis_client`` may be ``None`` (or any object lacking the expected
        methods) in which case the service falls back to the in-process
        ``MemoryReplayCache`` rather than crashing.

        The probe is deliberately cheap and non-blocking at import time: we
        construct the ``RedisReplayCache`` eagerly, but we only discover a
        broken Redis client when the first real ``verify`` runs, at which point
        ``claim`` falls back to ``False`` and the in-process cache is swapped
        in for the remainder of that request. That is enough to keep the
        coordinator live during a Redis blip; a persistent outage will leave
        replay protection in-process (per-instance), which is the documented
        limitation.
        """
        try:
            replay: ReplayCache = RedisReplayCache(
                redis_client, key_prefix="ticket", clock=clock
            )
        except Exception:
            replay = MemoryReplayCache(clock=clock)

        # Redis-backed: verification claims the jti atomically in Redis so two
        # coordinator instances cannot both accept the same ticket. The rest of
        # the verify path is identical; only replay protection is backed by Redis.
        return TicketService(
            keyring,
            ttl_seconds=ttl_seconds,
            skew_seconds=skew_seconds,
            replay=replay,
            clock=clock,
        )

    def issue(
        self,
        *,
        job_id: str,
        account_id: str,
        node_id: str,
        model: str,
        worker_public_key: str,
        confidentiality: Confidentiality,
        max_tokens: int,
        max_price_micro: int,
        now: int | None = None,
    ) -> tuple[dict[str, Any], PlacementTicket]:
        issued_at = int(now if now is not None else self._clock())
        payload = PlacementTicket(
            job_id=job_id,
            account_id=account_id,
            node_id=node_id,
            model=model,
            confidentiality=confidentiality,
            worker_public_key=worker_public_key,
            max_tokens=max_tokens,
            max_price_micro=max_price_micro,
            tier="confidential"
            if confidentiality is Confidentiality.ATTESTED_TEE
            else "standard",
            jti=crypto.new_session_nonce(),
            issued_at=issued_at,
            expires_at=issued_at + self.ttl_seconds,
        ).model_dump(mode="json")
        document = self.keyring.sign(TICKET_DOMAIN, payload)
        return document, PlacementTicket(**payload)

    def verify(
        self,
        document: dict[str, Any],
        *,
        expected_job_id: str | None = None,
        expected_node_id: str | None = None,
        expected_confidentiality: Confidentiality | None = None,
        now: int | None = None,
        claim_replay: bool = True,
    ) -> PlacementTicket:
        now_ts = int(now if now is not None else self._clock())
        payload = self.keyring.verify(document, TICKET_DOMAIN, at=now_ts)
        try:
            ticket = PlacementTicket(**payload)
        except PydanticValidationError as exc:
            raise TicketError(
                "ticket payload is malformed", code="malformed_ticket"
            ) from exc

        if ticket.issued_at > now_ts + self.skew_seconds:
            raise TicketError(
                "ticket is issued in the future", code="ticket_not_yet_valid"
            )
        if now_ts > ticket.expires_at + self.skew_seconds:
            raise ExpiredError("ticket has expired")
        if expected_job_id is not None and ticket.job_id != expected_job_id:
            raise TicketError(
                "ticket is for a different job", code="ticket_job_mismatch"
            )
        if expected_node_id is not None and ticket.node_id != expected_node_id:
            raise TicketError(
                "ticket is for a different node", code="ticket_node_mismatch"
            )
        if (
            expected_confidentiality is not None
            and ticket.confidentiality is not expected_confidentiality
        ):
            raise TicketError(
                "ticket confidentiality tier does not match the request",
                code="ticket_tier_mismatch",
            )
        if claim_replay and not self.replay.claim(
            ticket.jti, ticket.expires_at + self.skew_seconds
        ):
            raise ReplayError(
                "ticket has already been used", details={"jti": ticket.jti}
            )
        return ticket
