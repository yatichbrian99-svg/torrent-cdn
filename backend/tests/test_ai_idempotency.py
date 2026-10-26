"""Idempotent creation requests (checklist row 2.5).

The ledger already made *credits* idempotent, but a placement could be
retried into a second job with a second hold — the classic double-charge on a
dropped response. These tests pin the HTTP contract:

* the same ``Idempotency-Key`` with the same body replays the original ticket
  (same ``job_id``, one job, one hold);
* the same key with a *different* body is refused (409) instead of silently
  answering a question the client did not ask;
* a different key places a second job, so the header does not accidentally
  collapse legitimate requests;
* a concurrent retry while the first is still in flight gets 409, never a
  duplicate;
* a failed placement releases the key, so a retry can succeed;
* no header keeps the old behaviour (two calls, two jobs).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from app.ai import routes as ai_routes
from app.ai.errors import ValidationError
from app.main import app


def _ai_surface_enabled() -> bool:
    from app.ai.config import get_ai_settings
    from app.config import get_settings

    try:
        return get_ai_settings().is_ai_enabled_for_host(get_settings().app_env)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _ai_surface_enabled(),
    reason="ENABLE_TCDN_AI is off: /api/ai/v1 is not mounted",
)


@asynccontextmanager
async def ai_api(env):
    ai_routes.reset_for_tests()
    ai_routes.set_coordinator_for_tests(env.coordinator)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    ai_routes.set_coordinator_for_tests(None)
    ai_routes.reset_for_tests()


def _headers(env, key: str | None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {env.api_key}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _body(env, *, max_tokens: int = 16) -> dict:
    return {
        "model": env.model.model_id,
        "max_tokens": max_tokens,
        "prompt_tokens_estimate": 10,
    }


async def _jobs(env) -> list:
    return await env.store.list_jobs(env.account_id)


class TestPlacementReplay:
    @pytest.mark.asyncio
    async def test_same_key_replays_the_same_job_and_one_hold(
        self, ai_env, ai_worker_factory
    ):
        await ai_worker_factory()
        async with ai_api(ai_env) as client:
            first = await client.post(
                "/api/ai/v1/placements",
                json=_body(ai_env),
                headers=_headers(ai_env, "retry-after-drop"),
            )
            second = await client.post(
                "/api/ai/v1/placements",
                json=_body(ai_env),
                headers=_headers(ai_env, "retry-after-drop"),
            )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.json()["job_id"] == first.json()["job_id"]
        assert second.json()["ticket"] == first.json()["ticket"]
        jobs = await _jobs(ai_env)
        assert len(jobs) == 1
        # One hold, not two: the ledger holds exactly the job's reservation
        # (a second placement would double it).
        from app.ai.ledger import account_credits

        assert (
            ai_env.store.ledger.held(account_credits(ai_env.account_id))
            == jobs[0].hold_micro
        )

    @pytest.mark.asyncio
    async def test_same_key_with_a_different_body_is_refused(
        self, ai_env, ai_worker_factory
    ):
        await ai_worker_factory()
        async with ai_api(ai_env) as client:
            first = await client.post(
                "/api/ai/v1/placements",
                json=_body(ai_env, max_tokens=16),
                headers=_headers(ai_env, "body-changed"),
            )
            second = await client.post(
                "/api/ai/v1/placements",
                json=_body(ai_env, max_tokens=32),
                headers=_headers(ai_env, "body-changed"),
            )

        assert first.status_code == 200, first.text
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == "idempotency_key_reused"
        assert len(await _jobs(ai_env)) == 1

    @pytest.mark.asyncio
    async def test_a_different_key_places_a_second_job(
        self, ai_env, ai_worker_factory
    ):
        await ai_worker_factory()
        async with ai_api(ai_env) as client:
            first = await client.post(
                "/api/ai/v1/placements",
                json=_body(ai_env),
                headers=_headers(ai_env, "key-one"),
            )
            second = await client.post(
                "/api/ai/v1/placements",
                json=_body(ai_env),
                headers=_headers(ai_env, "key-two"),
            )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.json()["job_id"] != first.json()["job_id"]
        assert len(await _jobs(ai_env)) == 2

    @pytest.mark.asyncio
    async def test_without_a_key_the_old_behaviour_holds(
        self, ai_env, ai_worker_factory
    ):
        await ai_worker_factory()
        async with ai_api(ai_env) as client:
            first = await client.post(
                "/api/ai/v1/placements", json=_body(ai_env), headers=_headers(ai_env, None)
            )
            second = await client.post(
                "/api/ai/v1/placements", json=_body(ai_env), headers=_headers(ai_env, None)
            )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.json()["job_id"] != first.json()["job_id"]

    @pytest.mark.asyncio
    async def test_concurrent_retry_cannot_create_a_second_job(
        self, ai_env, ai_worker_factory
    ):
        await ai_worker_factory()
        async with ai_api(ai_env) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/api/ai/v1/placements",
                        json=_body(ai_env),
                        headers=_headers(ai_env, "concurrent-retry"),
                    )
                    for _ in range(4)
                )
            )

        # Either the retries replayed the completed response (200, same job) or
        # they arrived while the first was in flight (409). Both are correct;
        # what must never happen is a second job or a second hold.
        assert all(response.status_code in {200, 409} for response in responses), [
            response.status_code for response in responses
        ]
        job_ids = {
            response.json()["job_id"]
            for response in responses
            if response.status_code == 200
        }
        assert len(job_ids) == 1, job_ids
        jobs = await _jobs(ai_env)
        assert len(jobs) == 1
        from app.ai.ledger import account_credits

        assert (
            ai_env.store.ledger.held(account_credits(ai_env.account_id))
            == jobs[0].hold_micro
        )

    @pytest.mark.asyncio
    async def test_a_failed_placement_releases_the_key(
        self, ai_env, ai_worker_factory
    ):
        # No worker is registered yet, so placement fails *after* the key is
        # reserved — a transient failure the client is expected to retry.
        async with ai_api(ai_env) as client:
            failed = await client.post(
                "/api/ai/v1/placements",
                json=_body(ai_env),
                headers=_headers(ai_env, "release-me"),
            )
            await ai_worker_factory()
            # Retry with the SAME key and body: it must not be stuck at 409.
            retried = await client.post(
                "/api/ai/v1/placements",
                json=_body(ai_env),
                headers=_headers(ai_env, "release-me"),
            )

        assert failed.status_code != 200, failed.text
        assert retried.status_code == 200, retried.text

    @pytest.mark.asyncio
    async def test_key_length_is_bounded(self, ai_env):
        from app.ai.service import Coordinator

        settings = ai_env.settings
        coordinator = Coordinator(
            store=ai_env.store, keyring=ai_env.keyring, settings=settings
        )
        with pytest.raises(ValidationError) as excinfo:
            await coordinator.place(
                ai_env.auth,
                # Bypasses HTTP to test the service-level guard directly.
                _placement_request(ai_env),
                idempotency_key="x" * 129,
            )
        assert excinfo.value.code == "invalid_idempotency_key"


def _placement_request(env):
    from app.ai.models import PlacementRequest

    return PlacementRequest(
        model=env.model.model_id, max_tokens=16, prompt_tokens_estimate=10
    )
