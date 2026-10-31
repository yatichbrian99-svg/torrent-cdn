"""The public status surface: what it reports, and what it must never leak.

The endpoint exists so the owner can tell a staged deployment from a dead one
without a shell. It is unauthenticated, so the tests that matter most are the
negative ones: no node ids, no endpoints, no keys, no prompts.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import httpx
import pytest

from app.ai import routes as ai_routes
from app.ai import status as status_mod
from app.main import app


def _ai_surface_enabled() -> bool:
    from app.ai.config import get_ai_settings
    from app.config import get_settings

    try:
        return get_ai_settings().is_ai_enabled_for_host(get_settings().app_env)
    except Exception:  # no AI config at all → the routes are not mounted
        return False


pytestmark = pytest.mark.skipif(
    not _ai_surface_enabled(),
    reason="ENABLE_TCDN_AI is off: /api/ai/v1/status is not mounted",
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


@pytest.fixture
def probes_ok(monkeypatch):
    async def ok() -> dict:
        return {"status": "ok", "latency_ms": 1}

    monkeypatch.setattr(status_mod, "database_probe", ok)
    monkeypatch.setattr(status_mod, "redis_probe", ok)


class TestStatusEndpoint:
    @pytest.mark.asyncio
    async def test_is_public_and_reports_a_live_deployment(
        self, ai_env, ai_worker_factory, monkeypatch, probes_ok
    ):
        agent = await ai_worker_factory()
        await ai_env.store.update_node_heartbeat(
            agent.identity.node_id, load=0.0, healthy=True, at=int(time.time())
        )
        monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567890abcdef")

        async with ai_api(ai_env) as client:
            response = await client.get("/api/ai/v1/status")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["service"] == "tcdn-ai-coordinator"
        assert body["commit"] == "abcdef123456"  # full SHA is truncated, not echoed
        assert body["feature_flag"]["on"] is True
        assert body["database"]["status"] == "ok"
        assert body["redis"]["status"] == "ok"
        workers = body["workers"]
        assert workers["registered"] == 1
        assert workers["healthy"] == 1
        assert workers["models_served"] == [ai_env.model.model_id]
        assert workers["newest_heartbeat_age_seconds"] is not None
        assert (
            workers["newest_heartbeat_age_seconds"]
            < workers["heartbeat_timeout_seconds"]
        )

    @pytest.mark.asyncio
    async def test_leaks_no_identities_or_secrets(
        self, ai_env, ai_worker_factory, probes_ok
    ):
        await ai_worker_factory()
        async with ai_api(ai_env) as client:
            response = await client.get("/api/ai/v1/status")
        text = response.text
        assert "node_" not in text, "node ids are not public"
        assert "127.0.0.1" not in text, "worker endpoints are not public"
        assert ai_env.api_key not in text
        assert ai_env.account_id not in text
        assert ai_env.model.manifest_hash not in text
        assert "key_id" not in text

    @pytest.mark.asyncio
    async def test_a_failing_dependency_is_reported_not_raised(
        self, ai_env, monkeypatch
    ):
        async def broken() -> dict:
            return {"status": "error", "latency_ms": 0, "detail": "ConnectionRefused"}

        monkeypatch.setattr(status_mod, "database_probe", broken)
        monkeypatch.setattr(status_mod, "redis_probe", broken)

        async with ai_api(ai_env) as client:
            response = await client.get("/api/ai/v1/status")

        # A status page that 500s when the database is down is useless exactly
        # when it is needed.
        assert response.status_code == 200
        body = response.json()
        assert body["database"]["status"] == "error"
        assert body["redis"]["status"] == "error"


class TestWorkerSummary:
    @pytest.mark.asyncio
    async def test_stale_heartbeat_is_registered_but_not_healthy(
        self, ai_env, ai_worker_factory
    ):
        agent = await ai_worker_factory()
        now = 1_700_000_000
        await ai_env.store.update_node_heartbeat(
            agent.identity.node_id, load=0.0, healthy=True, at=now - 500
        )
        summary = await status_mod.worker_summary(ai_env.coordinator, now=now)
        assert summary["registered"] == 1
        assert summary["healthy"] == 0, "a beat older than the gate must not count"
        assert summary["oldest_heartbeat_age_seconds"] == 500

    @pytest.mark.asyncio
    async def test_no_workers_is_reported_as_empty(self, ai_env):
        summary = await status_mod.worker_summary(ai_env.coordinator, now=1_700_000_000)
        assert summary["registered"] == 0
        assert summary["healthy"] == 0
        assert summary["newest_heartbeat_age_seconds"] is None
        assert summary["models_served"] == []


class TestBuildCommit:
    def test_commit_is_short_or_absent(self, monkeypatch):
        for name in ("RENDER_GIT_COMMIT", "GIT_COMMIT", "SOURCE_VERSION"):
            monkeypatch.delenv(name, raising=False)
        assert status_mod.build_commit() is None
        monkeypatch.setenv("RENDER_GIT_COMMIT", "0123456789abcdef0123")
        assert status_mod.build_commit() == "0123456789ab"
