"""Two coordinator instances over a **real** Redis, not an in-process fake.

``test_ai_two_instances.py`` proves the sharing logic against a small shared
fake. That fake has the same methods but not the same failure modes — argument
shapes, ``SET NX EX`` return values, connection-to-event-loop binding — so a
green fake run does not prove the Redis client is wired correctly. This file
repeats the five cross-instance assertions against a live server, and it found
one: an ``redis.asyncio`` client used through the sync bridge raised ``got
Future attached to a different loop``, which the fail-closed branch turned into
"every ticket is a replay" whenever Redis was configured.

It skips unless ``TEST_REDIS_URL`` or ``REDIS_URL`` is set, and uses database 15
so it can never collide with the keys the suite's other Redis tests leave in
database 0.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.ai.attestation import AttestationError
from app.ai.errors import ConflictError, RateLimitedError, ReplayError
from app.ai.models import Confidentiality, PlacementRequest
from app.ai.service import Coordinator

pytest.importorskip("redis.asyncio")

REDIS_URL = os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")

pytestmark = pytest.mark.skipif(
    not REDIS_URL,
    reason="TEST_REDIS_URL/REDIS_URL not set — real-Redis lane only",
)

#: Database 15, so this lane can never collide with the keys the suite's other
#: Redis tests leave in database 0. Rewritten from the URL rather than passed as
#: a kwarg: ``Redis.from_url`` takes the database from the path, so a ``db=15``
#: argument is silently ignored.
BASE_URL = (REDIS_URL or "").rstrip("/").rpartition("/")[0]
TEST_REDIS_URL = f"{BASE_URL}/15"


@asynccontextmanager
async def live_redis():
    """A real client on database 15, flushed before and after.

    Created inside each test, not in a fixture: pytest-asyncio gives every test
    its own loop, and a client whose connections were opened in another loop
    fails on use (which is the bug this file exists to catch).
    """
    from redis.asyncio import Redis

    client = Redis.from_url(TEST_REDIS_URL, decode_responses=False)
    await client.flushdb()
    try:
        # The coordinator treats a client whose ping fails as absent; assert the
        # server is really there so a skip never masquerades as a pass.
        assert await client.ping() is True
        yield client
    finally:
        try:
            await client.flushdb()
        finally:
            await client.aclose()


def instance(ai_env: Any, redis: Any, **settings_overrides: Any) -> Coordinator:
    settings = ai_env.settings
    if settings_overrides:
        settings = settings.model_copy(update=settings_overrides)
    return Coordinator(
        store=ai_env.store,
        keyring=ai_env.keyring,
        settings=settings,
        redis_client=redis,
    )


class TestLiveSharedState:
    @pytest.mark.asyncio
    async def test_a_nonce_issued_by_one_instance_is_consumed_once_by_the_other(
        self, ai_env
    ):
        async with live_redis() as redis:
            first = instance(ai_env, redis)
            second = instance(ai_env, redis)
            nonce = await first.issue_attestation_nonce("node_live")

            second.attestation.nonces.consume(nonce, "node_live")
            with pytest.raises(AttestationError) as excinfo:
                first.attestation.nonces.consume(nonce, "node_live")
            assert excinfo.value.code == "stale_nonce"

    @pytest.mark.asyncio
    async def test_a_nonce_is_node_bound_across_live_instances(self, ai_env):
        async with live_redis() as redis:
            first = instance(ai_env, redis)
            second = instance(ai_env, redis)
            nonce = await first.issue_attestation_nonce("node_owner")
            with pytest.raises(AttestationError) as excinfo:
                second.attestation.nonces.consume(nonce, "node_other")
            assert excinfo.value.code == "nonce_node_mismatch"

    @pytest.mark.asyncio
    async def test_a_ticket_claimed_by_one_live_instance_is_a_replay_at_the_other(
        self, ai_env, ai_worker_factory
    ):
        async with live_redis() as redis:
            first = instance(ai_env, redis)
            second = instance(ai_env, redis)
            worker = await ai_worker_factory()
            document, ticket = first.tickets.issue(
                job_id="job_live_ticket",
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
                expected_job_id="job_live_ticket",
                expected_node_id=worker.identity.node_id,
                claim_replay=True,
            )
            with pytest.raises(ReplayError):
                second.tickets.verify(
                    document,
                    expected_job_id="job_live_ticket",
                    expected_node_id=worker.identity.node_id,
                    claim_replay=True,
                )
            assert ticket.jti

    @pytest.mark.asyncio
    async def test_the_rate_limit_window_is_shared_through_redis(
        self, ai_env, ai_worker_factory
    ):
        async with live_redis() as redis:
            first = instance(ai_env, redis, rate_limit_per_minute=2)
            second = instance(ai_env, redis, rate_limit_per_minute=2)
            await ai_worker_factory()
            request = PlacementRequest(
                model=ai_env.model.model_id, max_tokens=8, prompt_tokens_estimate=10
            )
            for _ in range(2):
                await first.place(ai_env.auth, request)
            with pytest.raises(RateLimitedError):
                await second.place(ai_env.auth, request)

    @pytest.mark.asyncio
    async def test_a_live_idempotency_reservation_replays_on_the_other_instance(
        self, ai_env, ai_worker_factory
    ):
        async with live_redis() as redis:
            first = instance(ai_env, redis)
            second = instance(ai_env, redis)
            await ai_worker_factory()
            request = PlacementRequest(
                model=ai_env.model.model_id, max_tokens=8, prompt_tokens_estimate=10
            )
            placed = await first.place(ai_env.auth, request, idempotency_key="live-key")
            replay = await second.place(
                ai_env.auth, request, idempotency_key="live-key"
            )
            assert replay.job_id == placed.job_id
            assert len(ai_env.store.jobs) == 1

            other = PlacementRequest(
                model=ai_env.model.model_id, max_tokens=9, prompt_tokens_estimate=10
            )
            with pytest.raises(ConflictError) as excinfo:
                await second.place(ai_env.auth, other, idempotency_key="live-key")
            assert excinfo.value.code == "idempotency_key_reused"

    @pytest.mark.asyncio
    async def test_the_daily_verification_budget_is_shared_through_redis(
        self, ai_env, ai_worker_factory
    ):
        async with live_redis() as redis:
            first = instance(
                ai_env,
                redis,
                verification_sample_pct=100,
                verification_daily_cap=1,
            )
            second = instance(
                ai_env,
                redis,
                verification_sample_pct=100,
                verification_daily_cap=1,
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
            assert sorted(record.status for record in records) == [
                "daily_cap_reached",
                "scheduled",
            ]
