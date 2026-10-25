"""Credit purchases through a payment provider.

Two adapters, same interface:

* ``FakePaymentProvider`` — development only, refused in production by config and
  by its own constructor. Its events are labelled ``"fake"``.
* ``PaystackPaymentProvider`` — the repo already uses Paystack for KES billing,
  so no new vendor is introduced. Webhooks are authenticated with
  HMAC-SHA-512 over the **raw body** and handled idempotently by provider
  reference.

No card data ever reaches this module: the provider hosts the checkout, and we
only consume a signed event with a reference, an amount and metadata.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Protocol

from .config import AISettings
from .errors import AuthenticationError, ValidationError

MICRO_PER_USD_CENT = 1_000_000  # $0.01 = 1 credit = 1e6 micro-credits


@dataclass(frozen=True)
class PaymentEvent:
    provider: str
    reference: str
    account_id: str
    amount_micro: int
    currency: str
    raw_event: str


class PaymentProvider(Protocol):
    name: str

    def verify_webhook(self, body: bytes, signature: str) -> PaymentEvent: ...


class FakePaymentProvider:
    name = "fake"

    def __init__(
        self, *, allow: bool, secret: str = "dev-only-fake-webhook-secret"
    ) -> None:
        if not allow:
            raise RuntimeError("FakePaymentProvider is disabled in this environment")
        self._secret = secret.encode()

    def verify_webhook(self, body: bytes, signature: str) -> PaymentEvent:
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature or ""):
            raise AuthenticationError(
                "invalid webhook signature", code="bad_webhook_signature"
            )
        try:
            payload = json.loads(body)
            account_id = payload["data"]["metadata"]["account_id"]
            reference = payload["data"]["reference"]
            amount_micro = int(payload["data"]["amount_micro"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(
                "malformed payment event", code="invalid_payment_event"
            ) from exc
        if amount_micro <= 0:
            raise ValidationError(
                "payment amount must be positive", code="invalid_payment_event"
            )
        return PaymentEvent(
            provider=self.name,
            reference=reference,
            account_id=account_id,
            amount_micro=amount_micro,
            currency=str(payload["data"].get("currency", "USD")),
            raw_event=str(payload.get("event", "fake.charge")),
        )


class PaystackPaymentProvider:
    name = "paystack"

    def __init__(self, secret_key: str, *, webhook_secret: str | None = None) -> None:
        if not secret_key:
            raise ValidationError(
                "Paystack secret key is required", code="missing_provider_key"
            )
        # Paystack signs webhooks with the account's secret key.
        self._webhook_secret = (webhook_secret or secret_key).encode()

    def verify_webhook(self, body: bytes, signature: str) -> PaymentEvent:
        expected = hmac.new(self._webhook_secret, body, hashlib.sha512).hexdigest()
        if not hmac.compare_digest(expected, signature or ""):
            raise AuthenticationError(
                "invalid webhook signature", code="bad_webhook_signature"
            )
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise ValidationError(
                "malformed payment event", code="invalid_payment_event"
            ) from exc

        if payload.get("event") != "charge.success":
            raise ValidationError(
                "unsupported payment event", code="unsupported_payment_event"
            )
        data = payload.get("data") or {}
        metadata = data.get("metadata") or {}
        account_id = metadata.get("account_id")
        reference = data.get("reference")
        currency = str(data.get("currency", "")).upper()
        amount_minor = data.get("amount")
        if not account_id or not reference or not isinstance(amount_minor, int):
            raise ValidationError(
                "payment event is missing required fields", code="invalid_payment_event"
            )
        if currency != "USD":
            # Credits are USD-pegged by definition; accepting another currency
            # would need an FX rate and is deliberately refused.
            raise ValidationError(
                "only USD payments can be converted to credits",
                code="unsupported_currency",
            )
        amount_micro = amount_minor * MICRO_PER_USD_CENT
        if amount_micro <= 0:
            raise ValidationError(
                "payment amount must be positive", code="invalid_payment_event"
            )
        return PaymentEvent(
            provider=self.name,
            reference=str(reference),
            account_id=str(account_id),
            amount_micro=amount_micro,
            currency=currency,
            raw_event="charge.success",
        )


def get_payment_provider(
    settings: AISettings, *, secret_key: str = ""
) -> PaymentProvider:
    if settings.payment_provider == "fake":
        return FakePaymentProvider(allow=not settings.is_production)
    return PaystackPaymentProvider(secret_key)
