"""Per-key rate limiting for the AI coordinator.

Used on the two endpoints whose abuse has real money impact: placement
(``/api/ai/v1/jobs``) and attestation evidence submission. Both mint or spend
a short-lived, money-affecting resource, so a per-key token bucket is a better
fit than the app's generic global/per-IP budget (which already exempts
/api/ai/v1 anyway).

Two backends ship behind the same ``RateLimiter`` protocol:

* ``RedisRateLimiter`` — production. One Redis counter per key, sliding window
  approximated by a fixed expiry. Good enough for gating bursts; a full
  sliding-window log is documented as a future hardening step in
  LIMITATIONS.md.
* ``NoOpRateLimiter`` — tests and deployments without Redis. Allowed-by-default
  so the coordinator keeps working; tests that care about rate limits use the
  memory-based ``MemoryRateLimiter`` instead.

The coordinator only calls ``allow(key, *, cost=1)`` and treats a ``False``
return as a hard 429 with a recoverable ``retry_after`` hint.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol

from .clock import now_unix
from .redis_cache import note_redis_failure


class RateLimitResult(Protocol):
    allowed: bool
    retry_after_seconds: int | None


class TokenBucketLimiter:
    """In-process token-bucket limiter used by the coordinator's HTTP surface.

    **One bucket per key.** The first version kept a single bucket and ignored
    the ``key`` argument, so every API key and every client address shared one
    allowance: one busy key could 429 the whole deployment, and "per-key" was
    only true when a test used exactly one key. The capacity run (row 10.2)
    found it — four independent keys saturated a 2 req/s allowance. Buckets are
    dropped once they have refilled to the burst cap, which is indistinguishable
    from having no bucket, so pruning cannot change a decision.

    Memory is bounded by ``max_buckets``; the full-bucket prune runs when that
    bound is crossed. A deployment with Redis uses the shared limiter for
    cross-instance limits; this one is per process by construction.
    """

    def __init__(
        self,
        *,
        rate_per_minute: float = 1200.0,
        burst: int = 40,
        clock=None,
        max_buckets: int = 50_000,
    ) -> None:
        # Configured per minute (AISettings.rate_limit_per_minute / _burst); the
        # bucket refills per second.
        self._max_per_second = float(rate_per_minute) / 60.0
        self._max_burst = int(burst)
        self._clock = clock or time.monotonic
        self._max_buckets = max(1, int(max_buckets))
        # key -> (tokens, last_refill)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    async def allow(self, key: str, *, cost: int = 1) -> RateLimitResult:
        with self._lock:
            now = self._clock()
            tokens, last = self._buckets.get(key, (float(self._max_burst), now))
            tokens = min(
                float(self._max_burst),
                tokens + max(0.0, now - last) * self._max_per_second,
            )
            if tokens >= cost:
                self._buckets[key] = (tokens - cost, now)
                if len(self._buckets) > self._max_buckets:
                    self._prune(now)
                return RateLimitResultValue(allowed=True, retry_after_seconds=None)
            self._buckets[key] = (tokens, now)
            delay = max(0.0, (cost - tokens) / self._max_per_second)
            return RateLimitResultValue(
                allowed=False, retry_after_seconds=int(delay) + 1
            )

    def _prune(self, now: float) -> None:
        """Drop buckets that are back at (or above) the burst cap."""
        full = float(self._max_burst)
        self._buckets = {
            key: (tokens, last)
            for key, (tokens, last) in self._buckets.items()
            if tokens + max(0.0, now - last) * self._max_per_second < full
        }

    allowed: bool
    retry_after_seconds: int | None


class RateLimiter(Protocol):
    async def allow(self, key: str, *, cost: int = 1) -> RateLimitResult: ...


class RateLimitResultValue:
    def __init__(self, allowed: bool, retry_after_seconds: int | None = None) -> None:
        self.allowed = allowed
        self.retry_after_seconds = retry_after_seconds


class NoOpRateLimiter:
    """Always allows. Used when Redis is unavailable and tests do not care."""

    async def allow(self, key: str, *, cost: int = 1) -> RateLimitResult:
        return RateLimitResultValue(allowed=True, retry_after_seconds=None)


class MemoryRateLimiter:
    """In-process fixed-window counter. Adequate for the single-instance dev
    deployment and for tests that assert rate-limit behaviour without Redis."""

    def __init__(
        self, *, max_per_window: int = 120, window_seconds: int = 60, clock=now_unix
    ) -> None:
        self._max = max_per_window
        self._window = window_seconds
        self._clock = clock
        self._counts: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    async def allow(self, key: str, *, cost: int = 1) -> RateLimitResult:
        now = int(self._clock())
        with self._lock:
            count, window_start = self._counts.get(key, (0, 0))
            if now - window_start >= self._window:
                count, window_start = 0, now
            count += cost
            self._counts[key] = (count, window_start)
            if count > self._max:
                remaining = max(0, self._window - (now - window_start))
                return RateLimitResultValue(
                    allowed=False, retry_after_seconds=remaining
                )
            return RateLimitResultValue(allowed=True, retry_after_seconds=None)


class RedisRateLimiter:
    """Redis-backed fixed-window counter per key.

    Window is approximated by a Redis key expiry; the coordinator uses short
    windows (60s) so the approximation is acceptable for gating bursts. When
    Redis is down the limiter fails open (allows) rather than blocking honest
    traffic; that is intentional — rate limiting is defence-in-depth, not the
    only guard.
    """

    def __init__(
        self,
        redis_client: Any,
        *,
        max_per_window: int = 120,
        window_seconds: int = 60,
        key_prefix: str = "ratelimit",
    ) -> None:
        self._redis = redis_client
        self._max = max_per_window
        self._window = window_seconds
        self._key_prefix = key_prefix

    def _key(self, key: str) -> str:
        return f"tcdn-ai:{self._key_prefix}:{key}"

    async def allow(self, key: str, *, cost: int = 1) -> RateLimitResult:
        redis_key = self._key(key)
        now = int(time.time())
        window_key = f"{redis_key}:{now // self._window}"
        try:
            new_val = await self._redis.incr(window_key)
            if new_val == 1:
                await self._redis.expire(window_key, self._window)
            if new_val > self._max:
                ttl = await self._redis.ttl(window_key)
                retry_after = ttl if ttl and ttl > 0 else self._window
                return RateLimitResultValue(
                    allowed=False, retry_after_seconds=retry_after
                )
            return RateLimitResultValue(allowed=True, retry_after_seconds=None)
        except Exception as exc:
            # Fail OPEN here (documented: a Redis blip must not stop honest
            # traffic) — but never silently. This is the one Redis path where a
            # failure weakens the control instead of refusing, so it must be
            # visible in logs and counters rather than looking like `allowed`.
            note_redis_failure("rate_limit", key, exc)
            return RateLimitResultValue(allowed=True, retry_after_seconds=None)
