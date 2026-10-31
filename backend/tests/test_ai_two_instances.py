"""Two coordinator instances over one Redis and one database (checklist 5.4).

The single-instance suite proves the *mechanisms*; this file proves they are
actually shared. Every assertion here fails if the state lives in one process:
a nonce minted by instance A, a ticket claimed by A, a rate-limit window counted
by A, an idempotency key reserved by A, and the daily verification budget.

The fake Redis is deliberately small (SET NX/EX, GET, DEL, INCR, EXPIRE, TTL) and
shared by both instances, which is the only thing Redis does for us here.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.ai.attestation import AttestationError
from app.ai.config import AISettings
from app.ai.errors import ConflictError, RateLimitedError, ReplayError
from app.ai.models import Confidentiality, PlacementRequest
from app.ai.service import Coordinator


class SharedRedis:
    """The subset of Redis these paths use, in memory, shared by both instances."""

    def __init__(self) -> None:
        self.state: dict[str, tuple[str, float | None]] = {}

    def _live(self, key: str) -> tuple[str, float | None] | None:
        record = self.state.get(key)
        if record is None:
            return None
        _value, expires_at = record
        if expires_at is not None and time.time() >= expires_at:
            self.state.pop(key, None)
            return None
        return record

    async def set(
        self, key: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> str | None:
        if nx and self._live(key) is not None:
            return None
        self.state[key] = (value, (time.time() + ex) if ex else None)
        return key

    async def get(self, key: str) -> bytes | None:
        record = self._live(key)
        return record[0].encode() if record else None

    async def delete(self, key: str) -> int:
        return 1 if self.state.pop(key, None) is not None else 0

    async def getdel(self, key: str) -> bytes | None:
        """Atomic fetch-and-delete (Redis 6.2+), used by the nonce store."""
        record = self._live(key)
        self.state.pop(key, None)
        return record[0].encode() if record else None

    async def incr(self, key: str) -> int:
        record = self._live(key)
        value = int(record[0]) + 1 if record else 1
        self.state[key] = (str(value), record[1] if record else None)
        return value

    async def expire(self, key: str, seconds: int) -> int:
        record = self._live(key)
        if record is None:
            return 0
        self.state[key] = (record[0], time.time() + seconds)
        return 1

    async def ttl(self, key: str) -> int:
        record = self._live(key)
        if record is None:
            return -2
        if record[1] is None:
            return -1
        return max(-1, int(record[1] - time.time()))


def instance(ai_env: Any, redis: Any, **settings_overrides: Any) -> Coordinator:
    settings: AISettings = ai_env.settings
    if settings_overrides:
        settings = settings.model_copy(update=settings_overrides)
    return Coordinator(
        store=ai_env.store,
        keyring=ai_env.keyring,
        settings=settings,
        redis_client=redis,
    )


@pytest.fixture()
def shared_redis() -> SharedRedis:
    return SharedRedis()


class TestSharedStateAcrossInstances:
    @pytest.mark.asyncio
    async def test_a_nonce_issued_by_one_instance_is_accepted_by_the_other(
        self, ai_env, shared_redis
    ):
        first = instance(ai_env, shared_redis)
        second = instance(ai_env, shared_redis)
        node_id = "node_two_instances"

        nonce = await first.issue_attestation_nonce(node_id)
        # The instance that did NOT issue it must accept it: this failed closed
        # before the Redis nonce store stopped requiring process-local state.
        second.attestation.nonces.consume(nonce, node_id)

        # ...and it is single use across the pair.
        with pytest.raises(AttestationError) as excinfo:
            first.attestation.nonces.consume(nonce, node_id)
        assert excinfo.value.code == "stale_nonce"

    @pytest.mark.asyncio
    async def test_a_nonce_is_bound_to_its_node_across_instances(
        self, ai_env, shared_redis
    ):
        first = instance(ai_env, shared_redis)
        second = instance(ai_env, shared_redis)
        nonce = await first.issue_attestation_nonce("node_owner")
        with pytest.raises(AttestationError) as excinfo:
            second.attestation.nonces.consume(nonce, "node_other")
        assert excinfo.value.code == "nonce_node_mismatch"

    @pytest.mark.asyncio
    async def test_a_ticket_claimed_by_one_instance_is_used_up(
        self, ai_env, ai_worker_factory, shared_redis
    ):
        first = instance(ai_env, shared_redis)
        second = instance(ai_env, shared_redis)
        worker = await ai_worker_factory()
        document, ticket = first.tickets.issue(
            job_id="job_ticket_once",
            account_id=ai_env.account_id,
            node_id=worker.identity.node_id,
            model=ai_env.model.model_id,
            worker_public_key=worker.identity.session_public_key,
            confidentiality=Confidentiality.NONE,
            max_tokens=8,
            max_price_micro=1_000,
        )
        first.tickets.verify(
            document,
            expected_job_id="job_ticket_once",
            expected_node_id=worker.identity.node_id,
            claim_replay=True,
        )
        with pytest.raises(ReplayError):
            second.tickets.verify(
                document,
                expected_job_id="job_ticket_once",
                expected_node_id=worker.identity.node_id,
                claim_replay=True,
            )
        assert ticket.jti

    @pytest.mark.asyncio
    async def test_the_rate_limit_window_is_shared(
        self, ai_env, ai_worker_factory, shared_redis
    ):
        # Two placements per minute: the first instance uses the window up and
        # the second must refuse, which only happens if the counter is in Redis.
        first = instance(ai_env, shared_redis, rate_limit_per_minute=2)
        second = instance(ai_env, shared_redis, rate_limit_per_minute=2)
        await ai_worker_factory()
        request = PlacementRequest(
            model=ai_env.model.model_id, max_tokens=8, prompt_tokens_estimate=10
        )
        for _ in range(2):
            await first.place(ai_env.auth, request)
        with pytest.raises(RateLimitedError):
            await second.place(ai_env.auth, request)

    @pytest.mark.asyncio
    async def test_an_idempotency_key_reserved_by_one_instance_replays_on_the_other(
        self, ai_env, ai_worker_factory, shared_redis
    ):
        first = instance(ai_env, shared_redis)
        second = instance(ai_env, shared_redis)
        await ai_worker_factory()
        request = PlacementRequest(
            model=ai_env.model.model_id, max_tokens=8, prompt_tokens_estimate=10
        )

        placed = await first.place(ai_env.auth, request, idempotency_key="shared-key")
        replay = await second.place(ai_env.auth, request, idempotency_key="shared-key")
        assert replay.job_id == placed.job_id
        assert len(ai_env.store.jobs) == 1

        # A different body under the same key is still refused, across instances.
        other = PlacementRequest(
            model=ai_env.model.model_id, max_tokens=9, prompt_tokens_estimate=10
        )
        with pytest.raises(ConflictError) as excinfo:
            await second.place(ai_env.auth, other, idempotency_key="shared-key")
        assert excinfo.value.code == "idempotency_key_reused"

    @pytest.mark.asyncio
    async def test_the_daily_verification_budget_is_shared(
        self, ai_env, ai_worker_factory, shared_redis
    ):
        # 100 % sampling and a cap of one: without a shared counter each instance
        # would spend its own cap and the platform would pay for twice the work.
        first = instance(
            ai_env, shared_redis, verification_sample_pct=100, verification_daily_cap=1
        )
        second = instance(
            ai_env, shared_redis, verification_sample_pct=100, verification_daily_cap=1
        )
        await ai_worker_factory()
        await ai_worker_factory()
        request = PlacementRequest(
            model=ai_env.model.model_id, max_tokens=8, prompt_tokens_estimate=10
        )

        await first.place(ai_env.auth, request)
        await second.place(ai_env.auth, request)

        records = [
            record
            for record in ai_env.store.verifications.values()
            if record.job_id in ai_env.store.jobs
        ]
        statuses = sorted(record.status for record in records)
        assert statuses == ["daily_cap_reached", "scheduled"], statuses
