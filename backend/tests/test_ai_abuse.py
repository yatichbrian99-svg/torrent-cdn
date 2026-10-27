"""Business-logic abuse tests and the money-out kill switch (checklist 5.9).

The arithmetic (negative, zero, overflow, double-spend) is covered elsewhere;
what this file adds is the operator control that the checklist named as missing —
a switch that stops refunds and payouts — and the abuse shapes that only appear
once credits can arrive through a refund: replaying the reference, refunding while
a hold is open, and spending refunded credit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import pytest

from app.ai.errors import ConflictError
from app.ai.ledger import PLATFORM_REVENUE
from app.main import app


async def _refund(coordinator, account_id: str, *, amount: int, reference: str):
    return await coordinator.refund_credits(
        account_id=account_id,
        amount_micro=amount,
        reason="operator adjustment",
        reference=reference,
    )


class TestPayoutKillSwitch:
    @pytest.mark.asyncio
    async def test_the_switch_stops_refunds_and_payouts_until_resumed(self, ai_env):
        coordinator = ai_env.coordinator
        state = await coordinator.set_payout_kill_switch(
            paused=True, reason="chargeback investigation", actor="admin-1"
        )
        assert state["paused"] is True

        with pytest.raises(ConflictError) as excinfo:
            await _refund(coordinator, ai_env.account_id, amount=1_000, reference="r-1")
        assert excinfo.value.code == "payouts_paused"

        with pytest.raises(ConflictError) as excinfo:
            await coordinator.record_payout(
                {
                    "period_start": 0,
                    "period_end": 86_400,
                    "node_id": "node_x",
                    "component": "compute",
                    "amount_micro": 1_000,
                }
            )
        assert excinfo.value.code == "payouts_paused"
        assert ai_env.store.payouts == []
        # The fixture seeds credit once; nothing the paused switch refused may
        # have posted an entry.
        assert [e for e in ai_env.store.ledger.entries if e.kind == "refund"] == []

        # The switch is state, not a one-shot: a second instance reads the same row.
        assert (await coordinator.payout_status())["paused"] is True

        await coordinator.set_payout_kill_switch(
            paused=False, reason="investigation closed", actor="admin-1"
        )
        await _refund(coordinator, ai_env.account_id, amount=1_000, reference="r-1")
        await coordinator.record_payout(
            {
                "period_start": 0,
                "period_end": 86_400,
                "node_id": "node_x",
                "component": "compute",
                "amount_micro": 1_000,
            }
        )
        assert len(ai_env.store.payouts) == 1
        events = [entry["event"] for entry in ai_env.store.audit_log]
        assert events.count("admin.payout_kill_switch") == 2
        assert "payout.recorded" in events

    @pytest.mark.asyncio
    async def test_the_switch_needs_a_reason(self, ai_env):
        with pytest.raises(Exception) as excinfo:
            await ai_env.coordinator.set_payout_kill_switch(
                paused=True, reason="   ", actor="admin-1"
            )
        assert getattr(excinfo.value, "code", "") == "missing_reason"

    @pytest.mark.asyncio
    async def test_the_admin_route_requires_an_admin(self, ai_env):
        from app.ai import routes as ai_routes
        from app.routes.admin import require_admin

        @asynccontextmanager
        async def client():
            ai_routes.set_coordinator_for_tests(ai_env.coordinator)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as c:
                yield c
            ai_routes.set_coordinator_for_tests(None)

        # No admin override: conftest's DB mock says "not an admin".
        async with client() as c:
            refused = await c.post(
                "/api/ai/v1/admin/kill-switch",
                json={"paused": True, "reason": "test"},
            )
        assert refused.status_code in (401, 403)

        app.dependency_overrides[require_admin] = lambda: UUID(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        try:
            async with client() as c:
                accepted = await c.post(
                    "/api/ai/v1/admin/kill-switch",
                    json={"paused": True, "reason": "test"},
                )
                read = await c.get("/api/ai/v1/admin/kill-switch")
        finally:
            app.dependency_overrides.pop(require_admin, None)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["paused"] is True
        assert read.status_code == 200
        assert read.json()["state"]["reason"] == "test"


class TestRefundAbuse:
    @pytest.mark.asyncio
    async def test_the_reference_is_the_idempotency_key(self, ai_env):
        before = ai_env.store.ledger.balance(f"account:{ai_env.account_id}:credits")
        await _refund(
            ai_env.coordinator, ai_env.account_id, amount=2_500, reference="tkt-9"
        )
        await _refund(
            ai_env.coordinator, ai_env.account_id, amount=2_500, reference="tkt-9"
        )
        after = ai_env.store.ledger.balance(f"account:{ai_env.account_id}:credits")
        assert after == before + 2_500
        refunds = [
            entry for entry in ai_env.store.ledger.entries if entry.kind == "refund"
        ]
        assert len(refunds) == 1
        assert refunds[0].debit == PLATFORM_REVENUE
        assert ai_env.store.ledger.invariant_ok()

    @pytest.mark.asyncio
    async def test_a_refund_does_not_release_an_open_hold(
        self, ai_env, ai_worker_factory
    ):
        from app.ai.models import PlacementRequest

        await ai_worker_factory()
        placed = await ai_env.coordinator.place(
            ai_env.auth,
            PlacementRequest(
                model=ai_env.model.model_id, max_tokens=8, prompt_tokens_estimate=10
            ),
        )
        job = await ai_env.store.get_job(placed.job_id)
        assert job is not None and job.hold_micro > 0

        await _refund(
            ai_env.coordinator, ai_env.account_id, amount=1_000, reference="tkt-10"
        )
        assert await ai_env.store.held_micro(ai_env.account_id) == job.hold_micro

        # Refunded credit is spendable like any other, and the ledger still nets.
        await ai_env.coordinator.place(
            ai_env.auth,
            PlacementRequest(
                model=ai_env.model.model_id, max_tokens=8, prompt_tokens_estimate=10
            ),
        )
        assert ai_env.store.ledger.invariant_ok()
        balance = ai_env.store.ledger.balance(f"account:{ai_env.account_id}:credits")
        held = ai_env.store.ledger.held(f"account:{ai_env.account_id}:credits")
        assert balance - held >= 0, (
            "refunding while a hold was open over-spent the account"
        )
