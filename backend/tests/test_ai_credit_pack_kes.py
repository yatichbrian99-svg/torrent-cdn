"""KES credit packs: a signed Kenyan charge must buy the pack it paid for.

The console sells credit packs priced in KES (``console.PACKS``), but the
Paystack adapter used to convert every ``charge.success`` with a USD-only rate
and refuse any other currency. A customer who paid KES 650 for the "starter"
pack was therefore charged and received nothing ("unsupported_currency").

The fix does **not** relabel KES as USD (that would over-credit ~130x): for
events whose ``metadata.purpose`` is ``ai_credits`` the amount is looked up from
the server-side pack table, the currency and the minor amount must match that
pack exactly, the transaction is re-checked against Paystack's verify API by
reference, and the ledger is credited with the pack's own ``credit_micro`` —
never a number taken from the event metadata.

Every case drives the real webhook handler with a correctly-signed body, so the
thing under test is the money path, not a helper.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from app.ai.config import AISettings
from app.ai.console import PACKS
from app.ai.console import pack as get_pack
from app.ai.errors import AuthenticationError, ValidationError
from app.ai.keyring import KeyRing
from app.ai.payments import PaystackPaymentProvider
from app.ai.service import Coordinator
from app.ai.store import MemoryStore

ACCOUNT = "11111111-1111-4111-8111-111111111111"
SECRET = "sk_test_unit_not_a_real_key"
STARTER = get_pack("starter")
assert STARTER is not None  # the pack table is server-side, not fixture data


def _body(
    *,
    reference: str = "tcdnai-ps-1",
    pack_id: str | None = "starter",
    amount: int = STARTER["kes_minor"],
    currency: str = "KES",
    purpose: str = "ai_credits",
    account_id: str = ACCOUNT,
    credit_micro: int | None = None,
) -> bytes:
    """A Paystack charge.success exactly as the checkout initialises it."""
    metadata: dict[str, object] = {"account_id": account_id}
    if purpose:
        metadata["purpose"] = purpose
    if pack_id is not None:
        metadata["pack_id"] = pack_id
    if credit_micro is not None:
        # Client-visible metadata the server must ignore when crediting.
        metadata["credit_micro"] = credit_micro
    return json.dumps(
        {
            "event": "charge.success",
            "data": {
                "id": 9000,
                "status": "success",
                "reference": reference,
                "amount": amount,
                "currency": currency,
                "metadata": metadata,
            },
        }
    ).encode()


def _sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha512).hexdigest()


def _verified(
    *,
    amount: int = STARTER["kes_minor"],
    currency: str = "KES",
    status: str = "success",
    pack_id: str | None = "starter",
    account_id: str = ACCOUNT,
) -> dict:
    """The ``data`` object Paystack's /transaction/verify returns for a charge.

    ``PaystackPaymentProvider.verify_transaction`` returns ``payload["data"]``,
    so the stub returns the same shape.
    """
    return {
        "status": status,
        "reference": "tcdnai-ps-1",
        "amount": amount,
        "currency": currency,
        "metadata": {
            "purpose": "ai_credits",
            "pack_id": pack_id,
            "account_id": account_id,
        },
    }


class _StubProvider(PaystackPaymentProvider):
    """Real signature checking, stubbed verify API (no network in unit tests)."""

    def __init__(self, verified: dict | Exception | None = None) -> None:
        super().__init__(SECRET)
        self._verified = verified if verified is not None else _verified()
        self.verify_calls: list[str] = []

    async def verify_transaction(self, reference: str) -> dict:
        self.verify_calls.append(reference)
        if isinstance(self._verified, Exception):
            raise self._verified
        return self._verified


async def _harness(provider: _StubProvider | None = None):
    settings = AISettings(
        environment="development",
        api_key_pepper="unit-test-pepper-not-a-real-secret",
        payment_provider="paystack",
        payments_live=True,
        payments_live_allowlist=ACCOUNT,
        rate_limit_per_minute=10_000,
        rate_limit_burst=10_000,
    )
    store = MemoryStore(settings)
    await store.init()
    provider = provider or _StubProvider()
    coordinator = Coordinator(
        store=store,
        keyring=KeyRing.generate("credit-pack-test"),
        settings=settings,
        payment_provider=provider,
    )
    await store.credit(
        ACCOUNT,
        1_000_000_000,
        source="gateway:test",
        idempotency_key="seed-credits",
        kind="credit_purchase",
    )
    return SimpleNamespace(
        settings=settings, store=store, coordinator=coordinator, provider=provider
    )


def _purchase_entries(store) -> list:
    return [
        entry
        for entry in store.ledger.entries
        if entry.kind == "credit_purchase" and entry.idempotency_key != "seed-credits"
    ]


class TestKesPackPurchase:
    @pytest.mark.asyncio
    async def test_signed_kes_charge_credits_exactly_the_pack(self):
        """The headline bug: KES was refused, so a paid pack credited nothing."""
        env = await _harness()
        body = _body()
        result = await env.coordinator.handle_payment_webhook(body, _sign(body))

        assert result["status"] == "credited"
        assert result["amount_micro"] == STARTER["credit_micro"] == 5_000_000
        assert await env.store.balance_micro(ACCOUNT) == 1_005_000_000
        entries = _purchase_entries(env.store)
        assert len(entries) == 1
        assert entries[0].idempotency_key == "payment:paystack:tcdnai-ps-1"
        # The transaction was re-checked with the provider by reference.
        assert env.provider.verify_calls == ["tcdnai-ps-1"]
        assert env.store.ledger.invariant_ok()

    @pytest.mark.asyncio
    async def test_credit_comes_from_the_pack_never_from_metadata(self):
        """Metadata is client-visible; a 100x credit_micro must be ignored."""
        env = await _harness()
        body = _body(credit_micro=999_000_000_000)
        result = await env.coordinator.handle_payment_webhook(body, _sign(body))
        assert result["amount_micro"] == STARTER["credit_micro"]
        assert await env.store.balance_micro(ACCOUNT) == 1_005_000_000

    @pytest.mark.asyncio
    async def test_audit_entry_records_pack_currency_and_amount(self):
        env = await _harness()
        body = _body()
        await env.coordinator.handle_payment_webhook(body, _sign(body))
        event = next(
            entry
            for entry in env.store.audit_log
            if entry["event"] == "billing.credit_purchase"
        )
        assert event["subject"] == "tcdnai-ps-1"
        assert event["detail"]["pack_id"] == "starter"
        assert event["detail"]["currency"] == "KES"
        assert event["detail"]["amount_minor"] == STARTER["kes_minor"]
        assert event["detail"]["micro"] == STARTER["credit_micro"]

    @pytest.mark.asyncio
    async def test_replay_credits_once(self):
        env = await _harness()
        body = _body()
        await env.coordinator.handle_payment_webhook(body, _sign(body))
        first = await env.store.balance_micro(ACCOUNT)
        replay = await env.coordinator.handle_payment_webhook(body, _sign(body))
        assert replay["duplicate"] is True
        assert replay["amount_micro"] == STARTER["credit_micro"]
        assert await env.store.balance_micro(ACCOUNT) == first
        assert len(_purchase_entries(env.store)) == 1
        # A replay is answered from the ledger, so it never re-hits the provider.
        assert len(env.provider.verify_calls) == 1


class TestKesPackRefusals:
    @pytest.mark.asyncio
    async def test_wrong_amount_refused(self):
        """KES 64.99 for the starter pack must not credit 5 USD of usage."""
        env = await _harness()
        body = _body(amount=STARTER["kes_minor"] - 1)
        with pytest.raises(ValidationError) as excinfo:
            await env.coordinator.handle_payment_webhook(body, _sign(body))
        assert excinfo.value.code == "payment_amount_mismatch"
        assert await env.store.balance_micro(ACCOUNT) == 1_000_000_000
        assert _purchase_entries(env.store) == []
        assert env.provider.verify_calls == []

    @pytest.mark.asyncio
    async def test_wrong_currency_refused(self):
        env = await _harness()
        body = _body(currency="USD")
        with pytest.raises(ValidationError) as excinfo:
            await env.coordinator.handle_payment_webhook(body, _sign(body))
        assert excinfo.value.code == "payment_currency_mismatch"
        assert await env.store.balance_micro(ACCOUNT) == 1_000_000_000

    @pytest.mark.asyncio
    async def test_unknown_pack_id_refused(self):
        env = await _harness()
        body = _body(pack_id="platinum")
        with pytest.raises(ValidationError) as excinfo:
            await env.coordinator.handle_payment_webhook(body, _sign(body))
        assert excinfo.value.code == "unknown_credit_pack"
        assert await env.store.balance_micro(ACCOUNT) == 1_000_000_000

    @pytest.mark.asyncio
    async def test_missing_pack_id_refused(self):
        env = await _harness()
        body = _body(pack_id=None)
        with pytest.raises(ValidationError) as excinfo:
            await env.coordinator.handle_payment_webhook(body, _sign(body))
        assert excinfo.value.code in {"invalid_payment_event", "unknown_credit_pack"}
        assert await env.store.balance_micro(ACCOUNT) == 1_000_000_000

    @pytest.mark.asyncio
    async def test_bad_signature_never_reaches_the_ledger(self):
        env = await _harness()
        body = _body()
        with pytest.raises(AuthenticationError):
            await env.coordinator.handle_payment_webhook(body, "0" * 128)
        assert await env.store.balance_micro(ACCOUNT) == 1_000_000_000


class TestVerifyApiGate:
    @pytest.mark.asyncio
    async def test_verify_api_amount_mismatch_needs_review_and_credits_nothing(self):
        """A signed event is authentic, not necessarily true: ask Paystack."""
        env = await _harness(_StubProvider(_verified(amount=STARTER["kes_minor"] - 1)))
        body = _body()
        with pytest.raises(ValidationError) as excinfo:
            await env.coordinator.handle_payment_webhook(body, _sign(body))
        assert excinfo.value.code == "payment_needs_review"
        assert await env.store.balance_micro(ACCOUNT) == 1_000_000_000
        assert _purchase_entries(env.store) == []
        review = [
            entry
            for entry in env.store.audit_log
            if entry["event"] == "billing.credit_purchase_needs_review"
        ]
        assert len(review) == 1
        assert review[0]["subject"] == "tcdnai-ps-1"
        assert review[0]["detail"]["reason"] == "verify_amount_mismatch"

    @pytest.mark.asyncio
    async def test_verify_api_status_not_success_needs_review(self):
        env = await _harness(_StubProvider(_verified(status="failed")))
        body = _body()
        with pytest.raises(ValidationError) as excinfo:
            await env.coordinator.handle_payment_webhook(body, _sign(body))
        assert excinfo.value.code == "payment_needs_review"
        assert await env.store.balance_micro(ACCOUNT) == 1_000_000_000

    @pytest.mark.asyncio
    async def test_verify_api_error_fails_closed(self):
        env = await _harness(_StubProvider(RuntimeError("paystack unreachable")))
        body = _body()
        with pytest.raises(ValidationError) as excinfo:
            await env.coordinator.handle_payment_webhook(body, _sign(body))
        assert excinfo.value.code == "payment_needs_review"
        assert await env.store.balance_micro(ACCOUNT) == 1_000_000_000

    @pytest.mark.asyncio
    async def test_verify_api_pack_mismatch_needs_review(self):
        env = await _harness(_StubProvider(_verified(pack_id="builder")))
        body = _body()
        with pytest.raises(ValidationError) as excinfo:
            await env.coordinator.handle_payment_webhook(body, _sign(body))
        assert excinfo.value.code == "payment_needs_review"
        assert await env.store.balance_micro(ACCOUNT) == 1_000_000_000


class TestPackTableStaysConsistent:
    def test_every_pack_is_priced_in_kes_and_credits_usd(self):
        for entry in PACKS:
            assert entry["kes_minor"] > 0
            assert entry["credits_usd"] in {5, 20, 50, 100}
            resolved = get_pack(entry["id"])
            assert resolved is not None
            assert resolved["currency"] == "KES"
            assert resolved["credit_micro"] == entry["credits_usd"] * 1_000_000
