"""Billing: signed webhooks, idempotent credit grants, signup-credit abuse limits."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.ai.config import AISettings
from app.ai.errors import AuthenticationError, ConflictError, ValidationError
from app.ai.ledger import SIGNUP_GRANTS, account_credits
from app.ai.payments import (
    FakePaymentProvider,
    PaystackPaymentProvider,
    get_payment_provider,
)

FAKE_SECRET = "dev-only-fake-webhook-secret"


def _fake_body(account_id: str, *, reference: str = "ref-1", micro: int = 5_000_000) -> bytes:
    return json.dumps(
        {
            "event": "fake.charge",
            "data": {
                "reference": reference,
                "amount_micro": micro,
                "currency": "USD",
                "metadata": {"account_id": account_id},
            },
        }
    ).encode()


def _fake_signature(body: bytes) -> str:
    return hmac.new(FAKE_SECRET.encode(), body, hashlib.sha256).hexdigest()


class TestFakeProvider:
    @pytest.mark.asyncio
    async def test_signed_webhook_credits_account(self, ai_env):
        body = _fake_body(ai_env.account_id)
        result = await ai_env.coordinator.handle_payment_webhook(body, _fake_signature(body))
        assert result["status"] == "credited"
        assert result["amount_micro"] == 5_000_000
        balance = await ai_env.store.balance_micro(ai_env.account_id)
        assert balance == 1_000_000_000 + 5_000_000

    @pytest.mark.asyncio
    async def test_replayed_webhook_is_idempotent(self, ai_env):
        body = _fake_body(ai_env.account_id)
        signature = _fake_signature(body)
        await ai_env.coordinator.handle_payment_webhook(body, signature)
        first = await ai_env.store.balance_micro(ai_env.account_id)
        await ai_env.coordinator.handle_payment_webhook(body, signature)
        assert await ai_env.store.balance_micro(ai_env.account_id) == first
        entries = [
            entry for entry in ai_env.store.ledger.entries
            if entry.kind == "credit_purchase" and entry.job_id is None
        ]
        assert len(entries) == 2  # seed credit + exactly one purchase

    @pytest.mark.asyncio
    async def test_bad_signature_rejected(self, ai_env):
        body = _fake_body(ai_env.account_id)
        with pytest.raises(AuthenticationError):
            await ai_env.coordinator.handle_payment_webhook(body, "0" * 64)
        assert await ai_env.store.balance_micro(ai_env.account_id) == 1_000_000_000

    def test_fake_provider_not_allowed_when_disabled(self):
        with pytest.raises(RuntimeError):
            FakePaymentProvider(allow=False)

    def test_production_selects_paystack(self):
        settings = AISettings(
            environment="production",
            require_tls=True,
            allow_simulated_attestation=False,
            allow_dev_stub_backend=False,
            payment_provider="paystack",
            api_key_pepper="p",
            coordinator_key_file="/run/secrets/keys.json",
        )
        assert get_payment_provider(settings, secret_key="sk_live_x").name == "paystack"


class TestPaystackProvider:
    def test_charge_success_converts_cents_to_micro_credits(self):
        provider = PaystackPaymentProvider("sk_test_key")
        body = json.dumps(
            {
                "event": "charge.success",
                "data": {
                    "reference": "ps-ref-1",
                    "amount": 2_500,  # $25.00
                    "currency": "USD",
                    "metadata": {"account_id": "11111111-1111-4111-8111-111111111111"},
                },
            }
        ).encode()
        signature = hmac.new(b"sk_test_key", body, hashlib.sha512).hexdigest()
        event = provider.verify_webhook(body, signature)
        assert event.amount_micro == 2_500 * 1_000_000
        assert event.account_id == "11111111-1111-4111-8111-111111111111"

    def test_wrong_signature_rejected(self):
        provider = PaystackPaymentProvider("sk_test_key")
        with pytest.raises(AuthenticationError):
            provider.verify_webhook(b"{}", "0" * 128)

    def test_non_usd_currency_refused(self):
        provider = PaystackPaymentProvider("sk_test_key")
        body = json.dumps(
            {
                "event": "charge.success",
                "data": {
                    "reference": "r",
                    "amount": 100,
                    "currency": "KES",
                    "metadata": {"account_id": "a"},
                },
            }
        ).encode()
        signature = hmac.new(b"sk_test_key", body, hashlib.sha512).hexdigest()
        with pytest.raises(ValidationError):
            provider.verify_webhook(body, signature)

    def test_non_payment_event_refused(self):
        provider = PaystackPaymentProvider("sk_test_key")
        body = json.dumps({"event": "transfer.success", "data": {}}).encode()
        signature = hmac.new(b"sk_test_key", body, hashlib.sha512).hexdigest()
        with pytest.raises(ValidationError):
            provider.verify_webhook(body, signature)


class TestSignupCredits:
    @pytest.mark.asyncio
    async def test_granted_once_per_account_from_platform_pool(self, ai_env):
        account = "33333333-3333-4333-8333-333333333333"
        granted = await ai_env.coordinator.grant_signup_credits(account, "ip-hash-1")
        assert granted == 500 * 1_000_000
        again = await ai_env.coordinator.grant_signup_credits(account, "ip-hash-1")
        assert again == 0
        assert ai_env.store.ledger.balance(account_credits(account)) == 500_000_000
        # Funded by the grant pool, never by node earnings.
        assert ai_env.store.ledger.balance(SIGNUP_GRANTS) == -500_000_000
        assert ai_env.store.ledger.invariant_ok()

    @pytest.mark.asyncio
    async def test_per_ip_cap(self, ai_env):
        for index in range(3):
            await ai_env.coordinator.grant_signup_credits(
                f"4444444{index}-4444-4444-8444-444444444444", "shared-ip"
            )
        with pytest.raises(ConflictError):
            await ai_env.coordinator.grant_signup_credits(
                "55555555-5555-4555-8555-555555555555", "shared-ip"
            )
