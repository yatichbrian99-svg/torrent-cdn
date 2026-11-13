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
from urllib.parse import quote

import httpx

from .config import AISettings
from .errors import AuthenticationError, ValidationError

MICRO_PER_USD_CENT = 1_000_000  # $0.01 = 1 credit = 1e6 micro-credits

#: ``metadata.purpose`` the console sets on a credit-pack checkout. It is the
#: only thing that selects the pack-priced path below; every other charge keeps
#: the legacy USD conversion.
CREDIT_PACK_PURPOSE = "ai_credits"

PAYSTACK_API_BASE = "https://api.paystack.co"


@dataclass(frozen=True)
class PaymentEvent:
    provider: str
    reference: str
    account_id: str
    #: Micro-credits implied by a USD charge. Zero for a pack purchase: there the
    #: credit is the pack's own ``credit_micro``, never a conversion of the KES
    #: amount (relabelling KES as USD would over-credit by KES_PER_USD).
    amount_micro: int
    currency: str
    raw_event: str
    #: ``"ai_credits"`` for a console credit-pack checkout, else empty.
    purpose: str = ""
    #: Pack the customer paid for (console.PACKS id), from metadata.
    pack_id: str | None = None
    #: Provider amount in the currency's smallest unit (KES cents on Paystack).
    amount_minor: int = 0


class PaymentProvider(Protocol):
    name: str

    def verify_webhook(self, body: bytes, signature: str) -> PaymentEvent: ...

    async def verify_transaction(self, reference: str) -> dict: ...


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

    async def verify_transaction(self, reference: str) -> dict:
        """The fake provider settles nothing, so there is nothing to verify."""
        raise ValidationError(
            f"the development fake provider cannot verify {reference}",
            code="payment_verification_failed",
        )


class PaystackPaymentProvider:
    name = "paystack"

    def __init__(self, secret_key: str, *, webhook_secret: str | None = None) -> None:
        if not secret_key:
            raise ValidationError(
                "Paystack secret key is required", code="missing_provider_key"
            )
        # The same key authenticates the verify API and signs webhooks.
        self._secret_key = secret_key
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
        purpose = str(metadata.get("purpose") or "")
        pack_id = metadata.get("pack_id")
        if not account_id or not reference or not isinstance(amount_minor, int):
            raise ValidationError(
                "payment event is missing required fields", code="invalid_payment_event"
            )
        if amount_minor <= 0:
            raise ValidationError(
                "payment amount must be positive", code="invalid_payment_event"
            )
        if purpose == CREDIT_PACK_PURPOSE:
            # A pack is priced in KES; the credit it buys is fixed in USD cents
            # by the server-side pack table. The currency and amount are NOT
            # converted here — the coordinator checks them against that table
            # and credits the pack's own credit_micro, so a wrong currency or
            # amount is refused instead of silently mis-priced.
            if not isinstance(pack_id, str) or not pack_id:
                raise ValidationError(
                    "credit-pack payment is missing its pack id",
                    code="invalid_payment_event",
                )
            return PaymentEvent(
                provider=self.name,
                reference=str(reference),
                account_id=str(account_id),
                amount_micro=0,
                currency=currency,
                raw_event="charge.success",
                purpose=purpose,
                pack_id=pack_id,
                amount_minor=amount_minor,
            )
        if currency != "USD":
            # Legacy (non-pack) charges are USD-pegged by definition; accepting
            # another currency there would need an FX rate and is refused.
            raise ValidationError(
                "only USD payments can be converted to credits",
                code="unsupported_currency",
            )
        amount_micro = amount_minor * MICRO_PER_USD_CENT
        return PaymentEvent(
            provider=self.name,
            reference=str(reference),
            account_id=str(account_id),
            amount_micro=amount_micro,
            currency=currency,
            raw_event="charge.success",
            amount_minor=amount_minor,
        )

    async def verify_transaction(self, reference: str) -> dict:
        """Ask Paystack what it actually settled for this reference.

        A valid signature proves the event came from Paystack; it does not prove
        the amount in it. The coordinator calls this before crediting a pack and
        fails closed (a "needs review" audit entry, no credit) on any mismatch
        or error, so a forged-but-signed body with a replayed reference cannot
        buy credits.
        """
        try:
            async with httpx.AsyncClient(
                base_url=PAYSTACK_API_BASE,
                headers={"Authorization": f"Bearer {self._secret_key}"},
                timeout=15,
            ) as client:
                response = await client.get(
                    f"/transaction/verify/{quote(reference, safe='')}"
                )
        except httpx.HTTPError as exc:
            raise ValidationError(
                "could not verify the transaction with Paystack",
                code="payment_verification_failed",
            ) from exc
        if response.status_code != 200:
            raise ValidationError(
                "could not verify the transaction with Paystack",
                code="payment_verification_failed",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValidationError(
                "could not verify the transaction with Paystack",
                code="payment_verification_failed",
            ) from exc
        if not payload.get("status"):
            raise ValidationError(
                "Paystack did not confirm the transaction",
                code="payment_verification_failed",
            )
        return payload.get("data") or {}


def get_payment_provider(
    settings: AISettings, *, secret_key: str = ""
) -> PaymentProvider:
    if settings.payment_provider == "fake":
        return FakePaymentProvider(allow=not settings.is_production)
    return PaystackPaymentProvider(secret_key)
