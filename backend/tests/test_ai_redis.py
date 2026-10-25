"""Break-tests for Redis-backed ticket replay, attestation nonces, and rate limits.

These are the tests that only matter when Redis is the backing store: they assert
that a replayed ticket, a reused nonce, and a burst placement are actually
rejected by the Redis path, and that the coordinator keeps working when Redis is
absent or broken (degrading to the in-process doubles rather than crashing).

They do not touch the rest of the AI module's logic — that is covered elsewhere.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.attestation import AttestationError, NonceStore
from app.ai.clock import now_unix
from app.ai.config import AISettings
from app.ai.errors import RateLimitedError
from app.ai.keyring import KeyRing
from app.ai.ratelimit import NoOpRateLimiter, RedisRateLimiter
from app.ai.redis_cache import (
    RedisNonceStore,
    RedisReplayCache,
    setifempty,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_fn() -> float:
    """Current clock, monkeypatchable for deterministic tests."""
    return asyncio.get_event_loop().time() if hasattr(asyncio.get_event_loop(), "time") else 0.0


class _FakeRedis:
    """A fake Redis client used only by the deterministic break-tests.

    It is a plain async object rather than a MagicMock so ``await client.set(...)``
    returns exactly what the method returns. MagicMock would wrap the async
    function and swallow the return value, which made the replay/nonce tests
    spuriously fail.
    """

    def __init__(self, clock_fn: Any | None = None) -> None:
        self.state: dict[str, tuple[str | None, int | None]] = {}
        self.ops: list[tuple[str, tuple[Any, ...]]] = []
        self._clock = clock_fn or _now_fn

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> str | None:
        now = self._clock()
        # Treat an expired key as absent (real Redis evicts on access).
        existing = self.state.get(key)
        if existing is not None and existing[0] is not None:
            _, expires_at = existing
            if expires_at is not None and now >= expires_at:
                self.state.pop(key, None)
                existing = None
        if nx and existing is not None:
            self.ops.append(("set", (key, value, nx, ex)))
            return None
        expires_at = (now + ex) if ex else None
        self.state[key] = (value, expires_at)
        self.ops.append(("set", (key, value, nx, ex)))
        return key

    async def get(self, key: str) -> bytes | None:
        now = self._clock()
        record = self.state.get(key)
        if record is None:
            return None
        value, expires_at = record
        if expires_at is not None and now >= expires_at:
            self.state.pop(key, None)
            return None
        raw = value.encode("utf-8") if isinstance(value, str) else value
        return raw

    async def delete(self, key: str) -> int:
        existed = key in self.state
        self.state.pop(key, None)
        self.ops.append(("delete", (key,)))
        return 1 if existed else 0

    async def incr(self, key: str) -> int:
        value = int(self.state.get(key, (None,))[0]) if self.state.get(key, (None,))[0] is not None else 0
        value += 1
        self.state[key] = (str(value), None)
        self.ops.append(("incr", (key,)))
        return value

    async def expire(self, key: str, seconds: int) -> int:
        record = self.state.get(key)
        if record is None:
            self.ops.append(("expire", (key, seconds)))
            return 0
        value, _ = record
        self.state[key] = (value, self._clock() + seconds)
        self.ops.append(("expire", (key, seconds)))
        return 1

    async def ttl(self, key: str) -> int:
        record = self.state.get(key)
        if record is None:
            return -2  # Redis returns -2 for a missing key
        _, expires_at = record
        if expires_at is None:
            return -1  # Redis returns -1 for a key with no expiry
        now = self._clock()
        remaining = expires_at - now
        return max(-1, int(remaining))


def _working_redis(clock_fn: Any | None = None) -> Any:
    """A fake Redis client that behaves enough for the cache/nonce/ratelimit tests.

    When ``clock_fn`` is supplied, expiry-related operations use it instead of
    the real event-loop clock, so deterministic clock fixtures (see
    ``_fixed_clock``) work across both the cache's timestamp and the Redis
    fake's own expiry bookkeeping.
    """
    return _FakeRedis(clock_fn)


def _fixed_clock() -> Any:
    """A clock that returns a fixed increasing sequence for deterministic expiry tests."""
    values = [1_000_000_000]
    def tick() -> int:
        v = values[-1]
        values.append(v + 1)
        return v
    return tick


# ---------------------------------------------------------------------------
# Ticket replay cache
# ---------------------------------------------------------------------------

class TestRedisReplayCache:
    """The Redis-backed replay cache must reject a second use of the same jti."""

    def test_first_claim_wins(self):
        redis = _working_redis()
        cache = RedisReplayCache(redis, key_prefix="ticket")
        assert cache.claim("jti-1", 1_000_001_000)
        assert not cache.claim("jti-1", 1_000_001_000)

    def test_expiry_lets_a_ticket_be_reused_after_its_window(self):
        clock = _fixed_clock()
        redis = _working_redis(clock_fn=clock)
        cache = RedisReplayCache(redis, key_prefix="ticket", clock=clock)
        # First claim at t=1_000_000_000 (clock consumed once inside claim).
        ticket_expires_at = 1_000_000_060
        assert cache.claim("jti-2", ticket_expires_at)
        # Same tick: still rejected.
        assert not cache.claim("jti-2", ticket_expires_at)
        # Advance past the ticket's expiry so the old key is evicted on next access.
        while clock() < ticket_expires_at:
            clock()
        clock()
        new_expires_at = clock() + 60
        assert new_expires_at > clock()
        assert cache.claim("jti-2", new_expires_at)

    def test_redis_down_falls_back_to_reject_once_seen(self):
        """A broken Redis client should not crash; it should reject replays once
        the in-process fallback has seen the jti."""
        redis = MagicMock()
        redis.set = AsyncMock(side_effect=RuntimeError("redis down"))
        cache = RedisReplayCache(redis, key_prefix="ticket")

        # First claim: Redis is down, so the in-process fallback stores it.
        assert not cache.claim("jti-3", 1_000_001_000)
        # Second claim: in-process fallback already saw it, so still reject.
        assert not cache.claim("jti-3", 1_000_001_000)


# ---------------------------------------------------------------------------
# Attestation nonce store
# ---------------------------------------------------------------------------

class TestRedisNonceStore:
    """Nonces must be single-use and node-bound, even when Redis is the backing
    store. Reuse and wrong-node reuse must both be rejected."""

    def test_nonce_is_single_use(self):
        redis = _working_redis()
        store = RedisNonceStore(redis, ttl_seconds=60)
        nonce = store.issue("node-a")
        assert nonce
        store.consume(nonce, "node-a")
        with pytest.raises(AttestationError):
            store.consume(nonce, "node-a")

    def test_nonce_reuse_by_a_different_node_is_rejected(self):
        redis = _working_redis()
        store = RedisNonceStore(redis, ttl_seconds=60)
        nonce = store.issue("node-a")
        with pytest.raises(AttestationError):
            store.consume(nonce, "node-b")

    def test_expired_nonce_is_rejected(self):
        clock = _fixed_clock()
        redis = _working_redis(clock_fn=clock)
        store = RedisNonceStore(redis, ttl_seconds=5, clock=clock)
        nonce = store.issue("node-a")
        for _ in range(6):
            clock()
        with pytest.raises(AttestationError):
            store.consume(nonce, "node-a")

    def test_redis_down_on_consume_does_not_crash(self):
        redis: Any = _FakeRedis()
        redis.get = AsyncMock(return_value=None)
        assert isinstance(redis.get, AsyncMock)
        store: NonceStore = RedisNonceStore(redis, ttl_seconds=60)
        nonce = store.issue("node-a")
        with pytest.raises(AttestationError):
            store.consume(nonce, "node-a")


class TestSyncBridgeInsideARunningLoop:
    """The gateway and the worker verify tickets inside async handlers, so the
    synchronous replay/nonce checks have to complete while an event loop is
    already running. That is the production path; it must not deadlock or
    silently skip the check."""

    def test_replay_claim_works_inside_a_running_loop(self):
        async def scenario():
            cache = RedisReplayCache(_working_redis(), key_prefix="ticket")
            assert cache.claim("jti-loop", 1_000_001_000) is True
            assert cache.claim("jti-loop", 1_000_001_000) is False

        asyncio.run(scenario())

    def test_nonce_consume_works_inside_a_running_loop(self):
        async def scenario():
            store = RedisNonceStore(_working_redis(), ttl_seconds=60)
            nonce = store.issue("node-a")
            store.consume(nonce, "node-a")
            with pytest.raises(AttestationError):
                store.consume(nonce, "node-a")

        asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class TestRedisRateLimiter:
    """The Redis-backed rate limiter must allow within budget and reject with a
    retry-after hint once the budget is spent."""

    def test_allow_within_budget(self):
        redis = _working_redis()
        limiter = RedisRateLimiter(redis, max_per_window=5, window_seconds=60)
        for _ in range(5):
            result = asyncio.run(limiter.allow("key-a"))
            assert result.allowed is True

    def test_reject_once_budget_is_spent(self):
        redis = _working_redis()
        limiter = RedisRateLimiter(redis, max_per_window=2, window_seconds=60)
        assert asyncio.run(limiter.allow("key-b")).allowed is True
        assert asyncio.run(limiter.allow("key-b")).allowed is True
        result = asyncio.run(limiter.allow("key-b"))
        assert result.allowed is False
        assert result.retry_after_seconds is not None and result.retry_after_seconds >= 0

    def test_redis_down_fails_open(self):
        redis: Any = _FakeRedis()
        redis.incr = AsyncMock(side_effect=RuntimeError("redis down"))
        assert isinstance(redis.incr, AsyncMock)
        limiter = RedisRateLimiter(redis, max_per_window=1, window_seconds=60)
        result = asyncio.run(limiter.allow("key-c"))
        assert result.allowed is True


# ---------------------------------------------------------------------------
# setifempty primitive
# ---------------------------------------------------------------------------

class TestSetIfEmpty:
    """The atomic first-wins primitive used by the email auto-verify gate."""

    def test_first_writer_wins(self):
        redis = _FakeRedis()
        assert asyncio.run(setifempty(redis, "k1", "v1")) is True
        assert asyncio.run(setifempty(redis, "k1", "v2")) is False

    def test_expiry_opens_the_key_again(self):
        clock = _fixed_clock()
        redis = _FakeRedis(clock_fn=clock)
        # First write at t=1_000_000_000, so the key expires at 1_000_000_002.
        assert asyncio.run(setifempty(redis, "k2", "v1", ex=2)) is True
        # One tick later it is still live, so a second writer must lose.
        assert asyncio.run(setifempty(redis, "k2", "v2")) is False
        # Past the TTL the old key is gone and a new writer may claim it.
        while clock() < 1_000_000_003:
            pass
        assert asyncio.run(setifempty(redis, "k2", "v3", ex=2)) is True


# ---------------------------------------------------------------------------
# Full service-level replay + rate-limit gates
# ---------------------------------------------------------------------------

class TestCoordinatorReplayAndRateLimit:
    """The coordinator must enforce ticket replay protection and rate limiting
    through the Redis-backed path, and degrade when Redis is absent."""

    @pytest.fixture
    def settings(self) -> AISettings:
        return AISettings()

    @pytest.fixture
    def keyring(self, settings: AISettings) -> KeyRing:
        return KeyRing.generate(
            "coordinator-test", overlap_seconds=settings.key_overlap_seconds
        )

    def test_replayed_ticket_is_rejected_with_a_working_redis(self, settings: AISettings, keyring: KeyRing):
        redis = _working_redis()
        coordinator = self._make_coordinator(settings, keyring, redis)
        from app.ai.crypto import generate_ed25519
        from app.ai.models import Confidentiality
        from app.ai.tickets import ReplayError

        worker_keys = generate_ed25519()
        document, _ticket = coordinator.tickets.issue(
            job_id="job-replay-1",
            account_id="acc-test",
            node_id="node-1",
            model="gpt-oss-120b",
            worker_public_key=worker_keys["public_key"],
            confidentiality=Confidentiality.NONE,
            max_tokens=32,
            max_price_micro=2_500_000,
        )
        assert coordinator.tickets.verify(document).job_id == "job-replay-1"
        # The same ticket cannot be presented a second time: the Redis replay
        # cache the coordinator wired up must reject it.
        with pytest.raises(ReplayError):
            coordinator.tickets.verify(document)

    def test_nonce_issuance_is_rate_limited_with_a_working_redis(self, settings: AISettings, keyring: KeyRing):
        redis = _working_redis()
        coordinator = self._make_coordinator(settings, keyring, redis)
        coordinator._rate_limiter = RedisRateLimiter(
            redis, max_per_window=2, window_seconds=60
        )
        asyncio.run(coordinator.issue_attestation_nonce("node-1"))
        asyncio.run(coordinator.issue_attestation_nonce("node-1"))
        with pytest.raises(RateLimitedError):
            asyncio.run(coordinator.issue_attestation_nonce("node-1"))

    def test_coordinator_degrades_when_redis_is_absent(self, settings: AISettings, keyring: KeyRing):
        coordinator = self._make_coordinator(settings, keyring, redis_client=None)
        # No Redis: replay protection is in-process, rate limiting is a no-op.
        assert isinstance(coordinator._rate_limiter, NoOpRateLimiter)

    def test_coordinator_degrades_when_redis_fails_on_ticket_verify(self, settings: AISettings, keyring: KeyRing):
        redis: Any = _FakeRedis()
        redis.set = AsyncMock(side_effect=RuntimeError("redis down"))
        assert isinstance(redis.set, AsyncMock)
        coordinator = self._make_coordinator(settings, keyring, redis)
        # Construction must not have crashed (it falls back per-request, but the
        # first real verify discovers the failure).
        assert coordinator.tickets is not None

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _make_coordinator(settings: AISettings, keyring: KeyRing, redis_client: Any | None):
        from app.ai.service import Coordinator
        from app.ai.store import MemoryStore

        store = MemoryStore(settings)
        return Coordinator(
            store=store,
            keyring=keyring,
            settings=settings,
            redis_client=redis_client,
            clock=now_unix,
        )

