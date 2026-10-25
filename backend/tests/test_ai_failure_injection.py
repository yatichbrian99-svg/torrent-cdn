"""Failure-injection tests for money-correctness gaps found in the Part 4 gate.

GAP-1 (HIGH) — abandoned placement held client credits forever — is fixed: the
coordinator now expires stale jobs and releases their hold. The test that used
to be a strict xfail is a normal assertion again.

Keep this file small and honest. A passing xfail is a recorded bug, not a test
that was weakened to go green.
"""

from __future__ import annotations

import pytest

from app.ai.models import PlacementRequest


@pytest.mark.asyncio
async def test_abandoned_placement_does_not_hold_credits_forever(
    ai_env, ai_worker_factory
):
    await ai_worker_factory()
    job = await ai_env.coordinator.place(
        ai_env.auth,
        PlacementRequest(model=ai_env.model.model_id, max_tokens=8),
    )
    held = await ai_env.store.held_micro(ai_env.account_id)
    assert held > 0

    # Simulate the ticket expiring with no receipt and no ack ever arriving:
    # age the stored job past the TTL and run the reaper.
    stored = await ai_env.store.get_job(job.job_id)
    stored.created_at = 0
    expired = await ai_env.coordinator.expire_stale_jobs()
    assert job.job_id in expired
    assert await ai_env.store.held_micro(ai_env.account_id) == 0
    stored = await ai_env.store.get_job(job.job_id)
    assert stored is not None and stored.status == "expired"
