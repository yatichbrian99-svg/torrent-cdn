"""Billing: signed webhooks, idempotent credit grants, signup-credit abuse limits."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.ai.config import AISettings
from app.ai.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    ValidationError,
)
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
    async def test_unverified_email_gets_no_free_credit(self, ai_env):
        account = "66666666-6666-4666-8666-666666666666"
        with pytest.raises(AuthorizationError) as excinfo:
            await ai_env.coordinator.grant_signup_credits(account, "ip-hash-1")
        assert excinfo.value.code == "email_not_verified"
        assert ai_env.store.ledger.balance(account_credits(account)) == 0
        assert await ai_env.store.count_grants_for_ip("ip-hash-1") == 0

    @pytest.mark.asyncio
    async def test_monthly_budget_caps_total_free_credit(self, ai_env, monkeypatch):
        # The budget is enforced before any grant is recorded, so the cap is a
        # ceiling on the platform's cost — not a per-account rate limit.
        monkeypatch.setattr(
            ai_env.settings, "signup_monthly_cap_micro", 600 * 1_000_000
        )
        first = "77777777-7777-4777-8777-777777777777"
        second = "88888888-8888-4888-8888-888888888888"
        ai_env.store.verified_accounts.update({first, second})
        assert await ai_env.coordinator.grant_signup_credits(first, "ip-a") == 500_000_000
        with pytest.raises(ConflictError) as excinfo:
            await ai_env.coordinator.grant_signup_credits(second, "ip-b")
        assert excinfo.value.code == "signup_monthly_cap"
        # The refused grant left nothing behind: no row, no credit.
        assert await ai_env.store.count_grants_for_ip("ip-b") == 0
        assert ai_env.store.ledger.balance(account_credits(second)) == 0

    @pytest.mark.asyncio
    async def test_granted_once_per_account_from_platform_pool(self, ai_env):
        account = "33333333-3333-4333-8333-333333333333"
        ai_env.store.verified_accounts.add(account)
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
        accounts = [
            f"4444444{index}-4444-4444-8444-444444444444" for index in range(3)
        ]
        ai_env.store.verified_accounts.update(accounts)
        for account in accounts:
            await ai_env.coordinator.grant_signup_credits(account, "shared-ip")
        fourth = "55555555-5555-4555-8555-555555555555"
        ai_env.store.verified_accounts.add(fourth)
        with pytest.raises(ConflictError) as excinfo:
            await ai_env.coordinator.grant_signup_credits(fourth, "shared-ip")
        assert excinfo.value.code == "signup_credit_limit"



def _ai_surface_enabled() -> bool:
    from app.ai.config import get_ai_settings
    from app.config import get_settings

    try:
        return get_ai_settings().is_ai_enabled_for_host(get_settings().app_env)
    except Exception:
        return False

class TestWebhookInputHardening:
    """Row 5.6: the webhook is an unauthenticated endpoint with a JSON parser.

    A valid signature proves *who* sent it, not that the payload is sane, so
    every field the coordinator later depends on is checked before it is used.
    """

    @pytest.mark.asyncio
    async def test_signed_event_with_a_bad_account_id_is_rejected(self, ai_env):
        body = _fake_body("not-a-uuid")
        signature = hmac.new(
            b"dev-only-fake-webhook-secret", body, hashlib.sha256
        ).hexdigest()
        with pytest.raises(ValidationError) as excinfo:
            await ai_env.coordinator.handle_payment_webhook(body, signature)
        assert excinfo.value.code == "invalid_payment_event"
        # Nothing was credited, and no orphan ledger row was written.
        assert await ai_env.store.balance_micro(ai_env.account_id) == 1_000_000_000
        assert not [e for e in ai_env.store.ledger.entries if e.kind == "credit_purchase"
                    and e.job_id is None and e.idempotency_key != "seed-credits"]

    def test_paystack_malformed_json_with_a_valid_signature(self):
        provider = PaystackPaymentProvider("sk_test_key")
        body = b"{not json at all"
        signature = hmac.new(b"sk_test_key", body, hashlib.sha512).hexdigest()
        with pytest.raises(ValidationError) as excinfo:
            provider.verify_webhook(body, signature)
        assert excinfo.value.code == "invalid_payment_event"

    @pytest.mark.skipif(
        not _ai_surface_enabled(),
        reason="ENABLE_TCDN_AI is off: /api/ai/v1 is not mounted",
    )
    def test_oversized_webhook_is_refused_before_parsing(self, ai_env, client):
        from app.ai import routes as ai_routes

        ai_routes.reset_for_tests()
        ai_routes.set_coordinator_for_tests(ai_env.coordinator)
        try:
            response = client.post(
                "/api/ai/v1/billing/webhook",
                content=b"x" * (ai_env.settings.max_body_bytes + 1024),
                headers={
                    "content-type": "application/json",
                    "x-tcdn-signature": "0" * 64,
                },
            )
        finally:
            ai_routes.set_coordinator_for_tests(None)
            ai_routes.reset_for_tests()
        assert response.status_code == 413, response.text
        assert response.json()["error"]["code"] == "body_too_large"
