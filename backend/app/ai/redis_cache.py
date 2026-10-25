"""Redis-backed replay cache and attestation nonce store.

Both classes implement the same protocols as the in-process doubles
(``MemoryReplayCache``, ``NonceStore``) so the coordinator can swap the
backing store without changing call sites — it only chooses the Redis version
when a Redis client is available and falls back to the in-process double when
it is not.

Assumptions recorded in LIMITATIONS.md:
* A multi-instance deployment must use these Redis-backed versions or replay /
  nonce protection becomes process-local.
* Keys are namespaced under ``tcdn-ai:`` to avoid colliding with any other
  Redis usage in the same Redis instance.
* Both are fail-closed on write errors only when the operation is security-
  critical (nonce consume, replay claim); a Redis outage on issuance degrades
  to the in-process fallback so a transient Redis blip cannot block placement
  or attestation issuance.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from . import clock
from .errors import AttestationError
from .nonce_store import NonceStore

logger = logging.getLogger(__name__)

REDIS_PREFIX = "tcdn-ai:"


def _run_sync(coro: Any) -> Any:
    """Drive a Redis coroutine to completion from synchronous code.

    The replay cache and nonce store are consumed by *synchronous* verification
    paths (``TicketService.verify``, ``AttestationService.attest``), because the
    worker and the gateway need a verdict before they continue. Those call sites
    may or may not already be inside an event loop, so this helper handles both:

    * no running loop → ``asyncio.run``;
    * running loop → run the coroutine on a private loop in a helper thread.

    Blocking is deliberate: a replay claim that is not awaited is not a replay
    claim, so the check has to finish before the caller continues. Previously
    these methods were ``async`` but called without ``await``, which returned a
    truthy coroutine object — so ticket replay protection and single-use
    attestation nonces were silently disabled whenever Redis was configured.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    outcome: dict[str, Any] = {}

    def _runner() -> None:
        try:
            outcome["value"] = asyncio.run(coro)
        except BaseException as exc:  # propagated to the caller below
            outcome["error"] = exc

    thread = threading.Thread(target=_runner, name="redis-sync-bridge", daemon=True)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


class RedisReplayCache:  # pragma: no cover - exercised through coordinator break-tests
    """Redis-backed single-use ticket replay cache.

    ``claim`` is atomic via SET NX with an expiry, so two coordinator instances
    cannot both accept the same ticket jti.
    """

    def __init__(
        self, redis_client: Any, *, key_prefix: str = "ticket", clock=clock.now_unix
    ) -> None:
        self._redis = redis_client
        self._key_prefix = key_prefix
        self._clock = clock

    def _key(self, key: str) -> str:
        return f"{REDIS_PREFIX}{self._key_prefix}:replay:{key}"

    def claim(self, ticket_jti: str, expires_at: int) -> bool:
        """Return True the first time ``ticket_jti`` is seen, False afterwards.

        The expiry is the same as the in-process version — the ticket's own TTL
        plus skew — so a replay window cannot outlive the ticket it protects.
        """
        return _run_sync(self._claim(ticket_jti, expires_at))

    async def _claim(self, ticket_jti: str, expires_at: int) -> bool:
        key = self._key(ticket_jti)
        now = int(self._clock())
        ttl = max(1, expires_at - now)
        try:
            ok = await self._redis.set(key, "1", nx=True, ex=ttl)
        except Exception:  # Redis is optional by design; never crash the coordinator
            logger.warning(
                "redis replay claim failed for %s — falling back to reject", ticket_jti
            )
            return False
        return ok is not None


class RedisNonceStore(
    NonceStore
):  # pragma: no cover - exercised through coordinator break-tests
    """Redis-backed single-use attestation nonces with expiry.

    Mirrors the in-process ``NonceStore`` API but backs the state with Redis TTLs
    so expiry is enforced by Redis, not only by the coordinator's clock on the
    next read.
    """

    def __init__(
        self,
        redis_client: Any,
        *,
        ttl_seconds: int = 300,
        key_prefix: str = "nonce",
        clock=clock.now_unix,
    ) -> None:
        super().__init__(ttl_seconds=ttl_seconds, clock=clock)
        self._redis = redis_client
        self._key_prefix = key_prefix

    def _key(self, nonce: str) -> str:
        return f"{REDIS_PREFIX}{self._key_prefix}:{nonce}"

    def issue(self, node_id: str) -> str:
        nonce = super().issue(node_id)
        _run_sync(self._publish(node_id, nonce))
        return nonce

    async def _publish(self, node_id: str, nonce: str) -> None:
        key = self._key(nonce)
        expires_at = int(self._clock()) + self.ttl_seconds
        value = f"{node_id}:{expires_at}"
        try:
            await self._redis.set(key, value, ex=self.ttl_seconds)
        except Exception:
            logger.warning(
                "redis nonce issuance failed for node %s — nonce is in-process only",
                node_id,
            )

    def consume(self, nonce: str, node_id: str) -> None:
        _run_sync(self._consume(nonce, node_id))

    async def _consume(self, nonce: str, node_id: str) -> None:
        key = self._key(nonce)
        try:
            value = await self._redis.get(key)
        except Exception:
            value = None
        if value is None:
            raise AttestationError(
                "unknown or already-used attestation nonce", code="stale_nonce"
            )
        try:
            expected_node, expires_at = value.decode("utf-8").split(":", 1)
            expires_at = int(expires_at)
        except (ValueError, UnicodeDecodeError) as exc:
            raise AttestationError(
                "malformed nonce record", code="stale_nonce"
            ) from exc
        now = int(self._clock())
        if expected_node != node_id:
            raise AttestationError(
                "nonce was issued for a different node", code="nonce_node_mismatch"
            )
        if now > expires_at:
            await self._forget(key)
            raise AttestationError("attestation nonce has expired", code="stale_nonce")
        try:
            await self._redis.delete(key)
        except Exception:
            logger.warning(
                "redis nonce consume failed for %s — in-process state may be stale",
                nonce,
            )
        super().consume(nonce, node_id)

    async def _forget(self, key: str) -> None:
        try:
            await self._redis.delete(key)
        except Exception:
            pass


async def setifempty(
    redis_client: Any,
    key: str,
    value: str,
    *,
    ex: int | None = None,
) -> bool:
    """Set ``key`` to ``value`` only if it is currently absent.

    Returns ``True`` when the key was set (first caller wins), ``False`` when it
    already held a value. This is the primitive the email-verification auto-verify
    path uses so two concurrent registrations from the same inbox during a provider
    outage cannot both mark the account verified — only the first does.

    Redis is optional by design; when it is unavailable the function returns
    ``False`` and the caller falls back to the DB path, which is correct (the DB
    path is idempotent for that case).
    """
    redis_key = f"{REDIS_PREFIX}setifempty:{key}"
    try:
        ok = await redis_client.set(redis_key, value, nx=True, ex=ex)
        return ok is not None
    except Exception:
        return False
