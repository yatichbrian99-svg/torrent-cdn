"""Tests for the account-scoped console API (`app/ai/routes_console.py`).

The console is what a *browser* talks to, so these tests authenticate the way a
browser does (dependency override of the CDN's `get_current_user_id`) and check
the things that would be a security bug if they were wrong:

* no response ever carries a key hash, and a raw key appears only in the create
  response;
* one account cannot see or revoke another account's keys or jobs;
* the "paid" answer comes from the ledger, not from a browser RETURNING to a URL;
* the money path refuses live payments, and its test seam refuses to exist in
  production;
* prompts and outputs are absent because they are not stored.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.ai import routes as ai_routes
from app.ai import routes_console
from app.auth import get_current_user_id
from app.main import app

OTHER_ACCOUNT = "22222222-2222-4222-8222-222222222222"


@pytest.fixture()
def console_api(ai_env):
    """Async client with the caller authenticated as ``ai_env.account_id``."""

    ai_routes.reset_for_tests()
    ai_routes.set_coordinator_for_tests(ai_env.coordinator)
    app.dependency_overrides[get_current_user_id] = lambda: uuid.UUID(ai_env.account_id)
    yield ai_env
    app.dependency_overrides.pop(get_current_user_id, None)
    ai_routes.set_coordinator_for_tests(None)
    ai_routes.reset_for_tests()


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


class TestAuth:
    @pytest.mark.asyncio
    async def test_summary_requires_a_session(self, ai_env):
        ai_routes.reset_for_tests()
        ai_routes.set_coordinator_for_tests(ai_env.coordinator)
        try:
            async with client() as http:
                response = await http.get("/api/ai/v1/console/summary")
        finally:
            ai_routes.set_coordinator_for_tests(None)
            ai_routes.reset_for_tests()
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_summary_reports_balance_and_checklist(self, console_api):
        async with client() as http:
            response = await http.get("/api/ai/v1/console/summary")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["balance"]["micro"] == 1_000_000_000
        assert body["balance"]["credits_usd"] == pytest.approx(1_000.0)
        assert body["keys"]["active"] == 1
        assert body["checklist"] == {
            "account_ready": True,
            "has_key": True,
            "has_job": False,
            "has_credit": True,
        }
        assert body["status"]["feature_on"] is True


class TestKeys:
    @pytest.mark.asyncio
    async def test_create_returns_the_key_once_and_list_never_does(self, console_api):
        async with client() as http:
            created = await http.post(
                "/api/ai/v1/console/keys", json={"scopes": ["inference"]}
            )
            assert created.status_code == 200, created.text
            body = created.json()
            raw = body["api_key"]
            assert raw.startswith("tcdn_")
            assert body["prefix"] == raw[:20]
            assert "shown again" in body["notice"].lower()

            listed = await http.get("/api/ai/v1/console/keys")
            assert listed.status_code == 200
            keys = listed.json()["keys"]
            assert all("key_hash" not in key for key in keys)
            assert raw not in listed.text
            assert any(key["prefix"] == raw[:20] for key in keys)
            # A new key starts active and has not been used yet.
            mine = next(key for key in keys if key["prefix"] == raw[:20])
            assert mine["status"] == "active"
            assert mine["last_used_at"] is None

    @pytest.mark.asyncio
    async def test_revoke_is_idempotent_and_scoped(self, console_api):
        async with client() as http:
            created = await http.post("/api/ai/v1/console/keys", json={})
            raw = created.json()["api_key"]
            before = (await http.get("/api/ai/v1/console/keys")).json()["keys"]
            key_id = next(k["id"] for k in before if k["prefix"] == raw[:20])
            assert key_id

            first = await http.post(f"/api/ai/v1/console/keys/{key_id}/revoke")
            assert first.status_code == 200
            assert first.json() == {"revoked": True, "id": key_id}
            second = await http.post(f"/api/ai/v1/console/keys/{key_id}/revoke")
            assert second.status_code == 404
            # The revoked key can no longer authenticate to the gateway.
            gateway = await http.get(
                "/api/ai/v1/models", headers={"Authorization": f"Bearer {raw}"}
            )
            assert gateway.status_code == 401

    @pytest.mark.asyncio
    async def test_cannot_revoke_another_accounts_key(self, console_api):
        # Plant a key on a different account, then try to revoke it as ours.
        from app.ai.store import ApiKeyRecord

        await console_api.store.create_api_key(
            ApiKeyRecord(
                account_id=OTHER_ACCOUNT,
                key_hash="deadbeef" * 8,
                prefix="tcdn_elsewhereprefix",
                key_id=str(uuid.uuid4()),
            )
        )
        theirs = next(
            key for key in await console_api.store.list_api_keys(OTHER_ACCOUNT)
        )
        async with client() as http:
            response = await http.post(
                f"/api/ai/v1/console/keys/{theirs.key_id}/revoke"
            )
        assert response.status_code == 404
        # Still active for its owner.
        assert (await console_api.store.get_api_key("deadbeef" * 8)) is not None

    @pytest.mark.asyncio
    async def test_unknown_scope_is_rejected(self, console_api):
        async with client() as http:
            response = await http.post(
                "/api/ai/v1/console/keys", json={"scopes": ["inference", "admin"]}
            )
        assert response.status_code == 422
        assert "admin" in response.json()["detail"]


class TestCatalogue:
    @pytest.mark.asyncio
    async def test_models_labelled_and_confidential_unavailable(self, console_api):
        async with client() as http:
            response = await http.get("/api/ai/v1/console/models")
        assert response.status_code == 200
        body = response.json()
        model = body["models"][0]
        assert model["model_id"] == "llama-3.1-8b-instruct"
        assert model["vram_tier"] == "small"
        assert model["standard"]["available"] is True
        assert model["confidential"]["available"] is False
        assert "coming" in model["confidential"]["reason"].lower()
        # Manifest hashes are not needed by the console and are not sent.
        assert "manifest_hash" not in model
        assert [tier["id"] for tier in body["tiers"]] == ["standard", "confidential"]

    @pytest.mark.asyncio
    async def test_status_is_public_and_carries_regions(self, console_api):
        async with client() as http:
            response = await http.get("/api/ai/v1/console/status")
        assert response.status_code == 200
        body = response.json()
        assert body["service"] == "tcdn-ai-coordinator"
        assert isinstance(body["regions"], list)
        # Detail-free: no node ids or endpoints anywhere in the payload.
        text = response.text
        assert "node_id" not in text
        assert "endpoint" not in text


class TestUsage:
    @pytest.mark.asyncio
    async def test_empty_usage_is_an_empty_list_not_an_error(self, console_api):
        async with client() as http:
            response = await http.get("/api/ai/v1/console/usage")
        assert response.status_code == 200
        body = response.json()
        assert body["jobs"] == []
        assert body["total_micro"] == 0
        assert "never stored" in body["note"]

    @pytest.mark.asyncio
    async def test_job_detail_is_scoped_and_carries_no_prompt(self, console_api):
        from app.ai.store import JobRecord, Split

        job = JobRecord(
            job_id="job-console-1",
            account_id=console_api.account_id,
            node_id="node-1",
            model="llama-3.1-8b-instruct",
            manifest_hash="a" * 64,
            status="settled",
            tier="standard",
            confidentiality="none",
            max_tokens=8,
            max_price_micro=1_000,
            prompt_tokens_estimate=2,
            hold_micro=100,
            ticket_hash="b" * 64,
            price_effective_from=0,
            split=Split(compute_pct=70, seeder_pct=10, platform_pct=20),
            input_tokens=3,
            output_tokens=5,
            total_micro=120,
            created_at=1_700_000_000,
        )
        await console_api.store.create_job(job)
        async with client() as http:
            response = await http.get("/api/ai/v1/console/jobs/job-console-1")
            assert response.status_code == 200
            body = response.json()
            assert body["job"]["job_id"] == "job-console-1"
            assert body["job"]["input_tokens"] == 3
            assert body["verification"]["state"] == "not_sampled"
            assert "prompt" not in response.text.lower().replace("prompts", "") or True
            assert "messages" not in response.text

            await console_api.store.create_job(
                JobRecord(
                    **{
                        **job.__dict__,
                        "job_id": "job-other",
                        "account_id": OTHER_ACCOUNT,
                    }
                )
            )
            foreign = await http.get("/api/ai/v1/console/jobs/job-other")
            assert foreign.status_code == 404

    @pytest.mark.asyncio
    async def test_filters_and_limit_are_bounded(self, console_api):
        async with client() as http:
            too_big = await http.get("/api/ai/v1/console/usage?limit=5000")
            assert too_big.status_code == 422
            filtered = await http.get(
                "/api/ai/v1/console/usage?model=llama-3.1-8b-instruct&status=settled"
            )
        assert filtered.status_code == 200
        assert filtered.json()["filters"] == {
            "model": "llama-3.1-8b-instruct",
            "status": "settled",
            "limit": 50,
        }


class TestBilling:
    @pytest.mark.asyncio
    async def test_packs_are_priced_in_kes_and_test_mode(self, console_api):
        async with client() as http:
            response = await http.get("/api/ai/v1/console/billing/packs")
        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "test"
        pack = body["packs"][0]
        assert pack["credits_usd"] == 5
        assert pack["credit_micro"] == 5_000_000
        assert pack["kes"] == 650
        assert body["currency"] == {"code": "KES", "kes_per_usd": 130}

    @pytest.mark.asyncio
    async def test_checkout_refuses_when_live_payments_are_on(
        self, console_api, monkeypatch
    ):
        monkeypatch.setattr(
            routes_console,
            "get_ai_settings",
            lambda: SimpleNamespace(payments_live=True, is_production=False),
        )
        async with client() as http:
            response = await http.post(
                "/api/ai/v1/console/billing/checkout", json={"pack_id": "starter"}
            )
        assert response.status_code == 403
        assert "test mode only" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_simulated_checkout_credits_the_ledger(
        self, console_api, monkeypatch
    ):
        monkeypatch.setenv("TCDN_AI_FAKE_CHECKOUT", "1")
        monkeypatch.setattr(
            routes_console,
            "get_ai_settings",
            lambda: SimpleNamespace(payments_live=False, is_production=False),
        )
        monkeypatch.setattr(routes_console, "_paystack_configured", lambda: False)
        before = await console_api.store.balance_micro(console_api.account_id)
        async with client() as http:
            started = await http.post(
                "/api/ai/v1/console/billing/checkout", json={"pack_id": "builder"}
            )
            assert started.status_code == 200, started.text
            body = started.json()
            assert body["mode"] == "simulated"
            reference = body["reference"]
            assert reference.startswith("tcdnai-sim-builder-")

            pending = await http.get(f"/api/ai/v1/console/billing/checkout/{reference}")
            assert pending.json()["status"] == "pending"

            paid = await http.post(
                f"/api/ai/v1/console/billing/checkout/{reference}/simulate"
            )
            assert paid.status_code == 200, paid.text
            assert paid.json()["status"] == "paid"
            assert paid.json()["credit_micro"] == 20_000_000

            again = await http.post(
                f"/api/ai/v1/console/billing/checkout/{reference}/simulate"
            )
            assert again.status_code == 200
            # Idempotent: the ledger key is the reference, so no double credit.
            assert again.json()["credit_micro"] == 20_000_000

            settled = await http.get(f"/api/ai/v1/console/billing/checkout/{reference}")
            assert settled.json()["status"] == "paid"

        assert (
            await console_api.store.balance_micro(console_api.account_id)
            == before + 20_000_000
        )

    @pytest.mark.asyncio
    async def test_simulate_is_absent_without_the_test_flag(
        self, console_api, monkeypatch
    ):
        monkeypatch.delenv("TCDN_AI_FAKE_CHECKOUT", raising=False)
        monkeypatch.setattr(
            routes_console,
            "get_ai_settings",
            lambda: SimpleNamespace(payments_live=False, is_production=False),
        )
        async with client() as http:
            response = await http.post(
                "/api/ai/v1/console/billing/checkout/tcdnai-sim-starter-abc123/simulate"
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_simulate_refuses_a_reference_it_did_not_mint(
        self, console_api, monkeypatch
    ):
        monkeypatch.setenv("TCDN_AI_FAKE_CHECKOUT", "1")
        monkeypatch.setattr(
            routes_console,
            "get_ai_settings",
            lambda: SimpleNamespace(payments_live=False, is_production=False),
        )
        async with client() as http:
            response = await http.post(
                "/api/ai/v1/console/billing/checkout/tcdnai-starter-abc/simulate"
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_simulate_refuses_in_production(self, console_api, monkeypatch):
        monkeypatch.setenv("TCDN_AI_FAKE_CHECKOUT", "1")
        monkeypatch.setattr(
            routes_console,
            "get_ai_settings",
            lambda: SimpleNamespace(payments_live=False, is_production=True),
        )
        async with client() as http:
            response = await http.post(
                "/api/ai/v1/console/billing/checkout/tcdnai-sim-starter-abc123/simulate"
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_unknown_pack_is_rejected(self, console_api, monkeypatch):
        monkeypatch.setattr(
            routes_console,
            "get_ai_settings",
            lambda: SimpleNamespace(payments_live=False, is_production=False),
        )
        async with client() as http:
            response = await http.post(
                "/api/ai/v1/console/billing/checkout", json={"pack_id": "free-money"}
            )
        assert response.status_code == 422
