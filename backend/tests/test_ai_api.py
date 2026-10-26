"""HTTP surface tests for /api/ai/v1."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest

from app.ai import routes as ai_routes
from app.ai.ratelimit import TokenBucketLimiter
from app.main import app


def _ai_surface_enabled() -> bool:
    """main.py mounts /api/ai/v1 only when ENABLE_TCDN_AI is on."""
    from app.ai.config import get_ai_settings
    from app.config import get_settings

    try:
        return get_ai_settings().is_ai_enabled_for_host(get_settings().app_env)
    except Exception:  # no AI config at all → the app cannot mount the routes
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


def _auth(env) -> dict[str, str]:
    return {"Authorization": f"Bearer {env.api_key}"}


class TestAuthAndCatalogue:
    @pytest.mark.asyncio
    async def test_models_requires_api_key(self, ai_env):
        async with ai_api(ai_env) as client:
            assert (await client.get("/api/ai/v1/models")).status_code == 401
            assert (
                await client.get(
                    "/api/ai/v1/models", headers={"Authorization": "Bearer tcdn_ai_wrong"}
                )
            ).status_code == 401

    @pytest.mark.asyncio
    async def test_models_lists_prices(self, ai_env):
        async with ai_api(ai_env) as client:
            response = await client.get("/api/ai/v1/models", headers=_auth(ai_env))
            assert response.status_code == 200
            models = response.json()
            assert models[0]["model_id"] == ai_env.model.model_id
            assert models[0]["input_micro_per_mtok"] == 3_000_000
            assert models[0]["confidential_input_micro_per_mtok"] == 3_750_000

    @pytest.mark.asyncio
    async def test_keys_endpoint_is_public_and_signed(self, ai_env):
        async with ai_api(ai_env) as client:
            response = await client.get("/api/ai/v1/keys/coordinator")
            assert response.status_code == 200
            body = response.json()
            assert body["bundle"]["domain"] == "tcdn-ai/keys/v1"
            assert body["revocations"]["domain"] == "tcdn-ai/revocations/v1"


class TestPlacement:
    @pytest.mark.asyncio
    async def test_placement_holds_credits_and_returns_ticket(self, ai_env, ai_worker_factory):
        await ai_worker_factory()
        before = await ai_env.store.balance_micro(ai_env.account_id)
        async with ai_api(ai_env) as client:
            response = await client.post(
                "/api/ai/v1/placements",
                headers=_auth(ai_env),
                json={"model": ai_env.model.model_id, "max_tokens": 8,
                      "prompt_tokens_estimate": 2},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ticket"]["domain"] == "tcdn-ai/ticket/v1"
        assert body["confidentiality"] == "none"
        assert "Standard tier" in body["confidentiality_notice"]
        held = await ai_env.store.held_micro(ai_env.account_id)
        assert held > 0
        assert held <= before

    @pytest.mark.asyncio
    async def test_no_worker_is_503(self, ai_env):
        async with ai_api(ai_env) as client:
            response = await client.post(
                "/api/ai/v1/placements",
                headers=_auth(ai_env),
                json={"model": ai_env.model.model_id, "max_tokens": 8},
            )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "no_eligible_worker"

    @pytest.mark.asyncio
    async def test_injection_shaped_model_rejected(self, ai_env):
        async with ai_api(ai_env) as client:
            response = await client.post(
                "/api/ai/v1/placements",
                headers=_auth(ai_env),
                json={"model": "llama'; DROP TABLE ai_jobs; --", "max_tokens": 8},
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_token_limit_enforced(self, ai_env, ai_worker_factory):
        await ai_worker_factory()
        async with ai_api(ai_env) as client:
            response = await client.post(
                "/api/ai/v1/placements",
                headers=_auth(ai_env),
                json={"model": ai_env.model.model_id, "max_tokens": 10**6},
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_tokens_are_bounded(self, ai_env):
        async with ai_api(ai_env) as client:
            for value in (0, -1, 999_999):
                response = await client.post(
                    "/api/ai/v1/placements",
                    headers=_auth(ai_env),
                    json={"model": ai_env.model.model_id, "max_tokens": value},
                )
                assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_body_size_limit(self, ai_env):
        async with ai_api(ai_env) as client:
            response = await client.post(
                "/api/ai/v1/placements",
                headers={**_auth(ai_env), "Content-Length": str(10 * 1024 * 1024)},
                json={"model": ai_env.model.model_id, "max_tokens": 8},
            )
        # 413, not 429: "too large" is not "retry later" (see PayloadTooLarge).
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "body_too_large"


class TestAccountIsolation:
    @pytest.mark.asyncio
    async def test_usage_is_scoped_to_the_key_owner(self, ai_env, ai_worker_factory):
        await ai_worker_factory()
        async with ai_api(ai_env) as client:
            placed = await client.post(
                "/api/ai/v1/placements",
                headers=_auth(ai_env),
                json={"model": ai_env.model.model_id, "max_tokens": 8},
            )
            assert placed.status_code == 200
            other_key = await ai_env.coordinator.provision_api_key(
                "22222222-2222-4222-8222-222222222222"
            )
            response = await client.get(
                "/api/ai/v1/usage", headers={"Authorization": f"Bearer {other_key}"}
            )
        assert response.status_code == 200
        assert response.json()["jobs"] == []

    @pytest.mark.asyncio
    async def test_balance_is_scoped(self, ai_env):
        async with ai_api(ai_env) as client:
            response = await client.get("/api/ai/v1/balance", headers=_auth(ai_env))
        assert response.status_code == 200
        body = response.json()
        assert body["account_id"] == ai_env.account_id
        assert body["micro_credits"] == 1_000_000_000
        assert body["credits"] == 1000.0

    @pytest.mark.asyncio
    async def test_ack_unknown_job_is_404_not_403(self, ai_env):
        async with ai_api(ai_env) as client:
            response = await client.post(
                "/api/ai/v1/jobs/does-not-exist/ack",
                headers=_auth(ai_env),
                json={
                    "job_id": "does-not-exist",
                    "tokens_received": 1,
                    "output_sha256": "a" * 64,
                    "acked_at": 1,
                },
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_api_key_cannot_reach_admin(self, ai_env):
        async with ai_api(ai_env) as client:
            response = await client.post(
                "/api/ai/v1/admin/api-keys",
                headers=_auth(ai_env),
                params={"account_id": ai_env.account_id},
            )
        assert response.status_code in (401, 403)


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_api_key_bucket_enforced(self, ai_env):
        async with ai_api(ai_env) as client:
            ai_routes._key_limiter = TokenBucketLimiter(rate_per_minute=1, burst=1)
            first = await client.get("/api/ai/v1/models", headers=_auth(ai_env))
            second = await client.get("/api/ai/v1/models", headers=_auth(ai_env))
            ai_routes._key_limiter = None
        assert first.status_code == 200
        assert second.status_code == 429
        assert second.headers.get("Retry-After")
