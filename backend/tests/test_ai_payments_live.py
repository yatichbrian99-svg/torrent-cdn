"""TCDN_AI_PAYMENTS_LIVE: money-in is off until explicitly opened.

Every case here drives the real webhook handler with a correctly-signed
Paystack body, so what is proven is the gate in front of the ledger and not a
unit test of a helper. The fake provider cases are the regression guard: the
development path the billing tests use must keep working with the flag off.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from app.ai.config import AISettings
from app.ai.errors import (
    AuthorizationError,
    ConflictError,
    LimitExceeded,
    UnavailableError,
    ValidationError,
)
from app.ai.keyring import KeyRing
from app.ai.payments import FakePaymentProvider, PaystackPaymentProvider
from app.ai.service import Coordinator
from app.ai.store import MemoryStore

ALLOWED = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
SECRET = "sk_test_unit_not_a_real_key"


def _paystack_body(
    account_id: str, *, reference: str = "ps-ref-1", amount_minor: int = 5
) -> bytes:
    return json.dumps(
        {
            "event": "charge.success",
            "data": {
                "reference": reference,
                "amount": amount_minor,
                "currency": "USD",
                "metadata": {"account_id": account_id},
            },
        }
    ).encode()


def _paystack_signature(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha512).hexdigest()


async def _harness(**overrides):
    settings = AISettings(
        environment="development",
        api_key_pepper="unit-test-pepper-not-a-real-secret",
        payment_provider="paystack",
        rate_limit_per_minute=10_000,
        rate_limit_burst=10_000,
        **overrides,
    )
    store = MemoryStore(settings)
    await store.init()
    coordinator = Coordinator(
        store=store,
        keyring=KeyRing.generate("payments-live-test"),
        settings=settings,
        payment_provider=PaystackPaymentProvider(SECRET),
    )
    return SimpleNamespace(settings=settings, store=store, coordinator=coordinator)


class TestFlagOff:
    @pytest.mark.asyncio
    async def test_signed_paystack_event_is_refused_by_default(self):
        env = await _harness()
        body = _paystack_body(ALLOWED)
        with pytest.raises(UnavailableError) as exc:
            await env.coordinator.handle_payment_webhook(
                body, _paystack_signature(body)
            )
        assert exc.value.code == "payments_not_live"
        assert await env.store.balance_micro(ALLOWED) == 0

    @pytest.mark.asyncio
    async def test_fake_provider_still_credits_with_the_flag_off(self):
        """The development path must not depend on a live-payments flag."""
        settings = AISettings(
            environment="development",
            api_key_pepper="unit-test-pepper-not-a-real-secret",
            payment_provider="fake",
        )
        store = MemoryStore(settings)
        await store.init()
        provider = FakePaymentProvider(allow=True)
        coordinator = Coordinator(
            store=store,
            keyring=KeyRing.generate("fake-payments-test"),
            settings=settings,
            payment_provider=provider,
        )
        body = json.dumps(
            {
                "event": "fake.charge",
                "data": {
                    "reference": "fake-1",
                    "amount_micro": 2_000_000,
                    "currency": "USD",
                    "metadata": {"account_id": ALLOWED},
                },
            }
        ).encode()
        signature = hmac.new(
            b"dev-only-fake-webhook-secret", body, hashlib.sha256
        ).hexdigest()
        result = await coordinator.handle_payment_webhook(body, signature)
        assert result["status"] == "credited"
        assert await store.balance_micro(ALLOWED) == 2_000_000


class TestAllowlist:
    @pytest.mark.asyncio
    async def test_allowlisted_account_is_credited(self):
        env = await _harness(payments_live=True, payments_live_allowlist=ALLOWED)
        body = _paystack_body(ALLOWED, amount_minor=5)
        result = await env.coordinator.handle_payment_webhook(
            body, _paystack_signature(body)
        )
        assert result["status"] == "credited"
        # 5 minor units (cents) = 5 credits = 5_000_000 micro.
        assert await env.store.balance_micro(ALLOWED) == 5 * 1_000_000

    @pytest.mark.asyncio
    async def test_account_outside_the_allowlist_is_refused(self):
        env = await _harness(payments_live=True, payments_live_allowlist=ALLOWED)
        body = _paystack_body(OTHER)
        with pytest.raises(AuthorizationError) as exc:
            await env.coordinator.handle_payment_webhook(
                body, _paystack_signature(body)
            )
        assert exc.value.code == "payments_account_not_allowlisted"
        assert await env.store.balance_micro(OTHER) == 0

    @pytest.mark.asyncio
    async def test_allowlist_parses_a_comma_separated_env_value(self):
        env = await _harness(
            payments_live=True, payments_live_allowlist=f" {ALLOWED} , {OTHER} "
        )
        assert env.settings.payments_live_accounts == frozenset({ALLOWED, OTHER})


class TestCaps:
    @pytest.mark.asyncio
    async def test_purchase_above_the_transaction_cap_is_refused(self):
        env = await _harness(
            payments_live=True,
            payments_live_allowlist=ALLOWED,
            payments_live_max_purchase_micro=1_000_000,
            payments_live_daily_cap_micro=100_000_000,
        )
        body = _paystack_body(ALLOWED, amount_minor=5)  # 5_000_000 micro
        with pytest.raises(ValidationError) as exc:
            await env.coordinator.handle_payment_webhook(
                body, _paystack_signature(body)
            )
        assert exc.value.code == "payment_above_cap"
        assert await env.store.balance_micro(ALLOWED) == 0

    @pytest.mark.asyncio
    async def test_daily_cap_stops_the_second_purchase(self):
        env = await _harness(
            payments_live=True,
            payments_live_allowlist=ALLOWED,
            payments_live_max_purchase_micro=5_000_000,
            payments_live_daily_cap_micro=8_000_000,
        )
        first = _paystack_body(ALLOWED, reference="ps-1")
        await env.coordinator.handle_payment_webhook(first, _paystack_signature(first))
        second = _paystack_body(ALLOWED, reference="ps-2")
        with pytest.raises(LimitExceeded) as exc:
            await env.coordinator.handle_payment_webhook(
                second, _paystack_signature(second)
            )
        assert exc.value.code == "payment_daily_cap_reached"
        assert await env.store.balance_micro(ALLOWED) == 5_000_000

    @pytest.mark.asyncio
    async def test_a_replayed_event_stays_idempotent_and_does_not_spend_the_cap(self):
        env = await _harness(
            payments_live=True,
            payments_live_allowlist=ALLOWED,
            payments_live_daily_cap_micro=6_000_000,
        )
        body = _paystack_body(ALLOWED, reference="ps-replay")
        signature = _paystack_signature(body)
        await env.coordinator.handle_payment_webhook(body, signature)
        await env.coordinator.handle_payment_webhook(body, signature)
        assert await env.store.balance_micro(ALLOWED) == 5_000_000


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_switch_refuses_purchases_even_when_the_flag_is_on(self):
        env = await _harness(payments_live=True, payments_live_allowlist=ALLOWED)
        await env.coordinator.set_payments_live_kill_switch(
            paused=True, reason="owner paused the live test", actor="owner"
        )
        body = _paystack_body(ALLOWED)
        with pytest.raises(ConflictError) as exc:
            await env.coordinator.handle_payment_webhook(
                body, _paystack_signature(body)
            )
        assert exc.value.code == "payments_live_paused"
        assert await env.store.balance_micro(ALLOWED) == 0

    @pytest.mark.asyncio
    async def test_switch_state_is_shared_and_resumable(self):
        env = await _harness(payments_live=True, payments_live_allowlist=ALLOWED)
        await env.coordinator.set_payments_live_kill_switch(
            paused=True, reason="paused", actor="owner"
        )
        status = await env.coordinator.payments_live_status()
        assert status["paused"] is True
        assert status["state"]["reason"] == "paused"
        await env.coordinator.set_payments_live_kill_switch(
            paused=False, reason="resumed", actor="owner"
        )
        body = _paystack_body(ALLOWED)
        result = await env.coordinator.handle_payment_webhook(
            body, _paystack_signature(body)
        )
        assert result["status"] == "credited"

    @pytest.mark.asyncio
    async def test_the_payouts_switch_does_not_stop_money_in(self):
        env = await _harness(payments_live=True, payments_live_allowlist=ALLOWED)
        await env.coordinator.set_payout_kill_switch(
            paused=True, reason="node review", actor="owner"
        )
        body = _paystack_body(ALLOWED)
        result = await env.coordinator.handle_payment_webhook(
            body, _paystack_signature(body)
        )
        assert result["status"] == "credited"

    @pytest.mark.asyncio
    async def test_a_switch_change_needs_a_reason(self):
        env = await _harness(payments_live=True, payments_live_allowlist=ALLOWED)
        with pytest.raises(ValidationError):
            await env.coordinator.set_payments_live_kill_switch(
                paused=True, reason="  ", actor="owner"
            )


class TestReconciliation:
    """The script is the counterpart of the kill switch: it catches the money
    that moved while the gate was off, or around a failure."""

    def _reconcile(self, tmp_path, provider: list[dict], ledger: list[dict]):
        import json as _json

        from scripts.reconcile_payments import load_ledger, load_provider, reconcile

        provider_path = tmp_path / "provider.json"
        provider_path.write_text(_json.dumps({"data": provider}))
        ledger_path = tmp_path / "ledger.json"
        ledger_path.write_text(_json.dumps(ledger))
        return reconcile(
            load_provider(str(provider_path)), load_ledger(str(ledger_path))
        )

    @staticmethod
    def _paid(reference: str, cents: int = 5) -> dict:
        return {
            "reference": reference,
            "amount": cents,
            "currency": "USD",
            "status": "success",
            "metadata": {"account_id": ALLOWED},
        }

    @staticmethod
    def _credit(reference: str, micro: int = 5_000_000) -> dict:
        return {
            "idempotency_key": f"payment:paystack:{reference}",
            "amount_micro": micro,
            "credit_account": f"account:{ALLOWED}",
            "created_at": 1,
        }

    def test_matched_payment_has_no_findings(self, tmp_path):
        report = self._reconcile(tmp_path, [self._paid("r1")], [self._credit("r1")])
        assert report.findings == 0
        assert report.matched == 1

    def test_paid_but_not_credited_is_a_finding(self, tmp_path):
        report = self._reconcile(tmp_path, [self._paid("r1")], [])
        assert [row["reference"] for row in report.paid_not_credited] == ["r1"]

    def test_credit_without_a_charge_is_a_finding(self, tmp_path):
        report = self._reconcile(tmp_path, [], [self._credit("r1")])
        assert [row["reference"] for row in report.credited_not_paid] == ["r1"]

    def test_amount_mismatch_is_a_finding(self, tmp_path):
        report = self._reconcile(tmp_path, [self._paid("r1")], [self._credit("r1", 1)])
        assert report.amount_mismatch[0]["expected_micro"] == 5_000_000

    def test_duplicate_credit_is_a_finding(self, tmp_path):
        report = self._reconcile(
            tmp_path, [self._paid("r1")], [self._credit("r1"), self._credit("r1")]
        )
        assert report.duplicate_credit[0]["reference"] == "r1"

    def test_fake_provider_rows_are_not_reconciled(self, tmp_path):
        fake = {**self._credit("fake-1"), "idempotency_key": "payment:fake:fake-1"}
        report = self._reconcile(tmp_path, [], [fake])
        assert report.findings == 0
        assert report.ledger_rows == 0

    def test_non_success_provider_rows_are_ignored(self, tmp_path):
        failed = {**self._paid("r2"), "status": "failed"}
        report = self._reconcile(tmp_path, [failed], [])
        assert report.provider_rows == 0
        assert report.findings == 0


class TestProductionValidation:
    def _production(self, **overrides) -> AISettings:
        base = {
            "environment": "production",
            "require_tls": True,
            "allow_simulated_attestation": False,
            "allow_dev_stub_backend": False,
            "api_key_pepper": "a-real-pepper-from-the-secret-store",
            "coordinator_key_file": "/run/secrets/keyring.json",
            "payment_provider": "paystack",
        }
        base.update(overrides)
        return AISettings(**base)

    def test_live_payments_require_an_allowlist(self):
        with pytest.raises(ValueError) as exc:
            self._production(payments_live=True)
        assert "ALLOWLIST" in str(exc.value)

    def test_live_payments_require_the_paystack_provider(self):
        with pytest.raises(ValueError) as exc:
            self._production(
                payments_live=True,
                payments_live_allowlist=ALLOWED,
                payment_provider="fake",
            )
        assert "PAYMENT_PROVIDER" in str(exc.value)

    def test_transaction_cap_cannot_exceed_the_daily_cap(self):
        with pytest.raises(ValueError) as exc:
            self._production(
                payments_live=True,
                payments_live_allowlist=ALLOWED,
                payments_live_max_purchase_micro=9_000_000,
                payments_live_daily_cap_micro=1_000_000,
            )
        assert "DAILY_CAP" in str(exc.value)

    def test_a_valid_live_config_boots(self):
        settings = self._production(payments_live=True, payments_live_allowlist=ALLOWED)
        assert settings.payments_live_accounts == frozenset({ALLOWED})
