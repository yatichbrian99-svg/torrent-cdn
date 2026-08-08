"""Billing routes — Paystack (Kenya).

Paystack is used because Stripe does not support merchant accounts registered
in Kenya. The flows mirror the old Stripe integration one-for-one:

  * POST /billing/create-checkout — server-side call to Paystack
    ``transaction/initialize`` against a recurring plan; returns the hosted
    ``authorization_url`` the browser redirects to (Paystack's hosted checkout
    page, which presents cards and M-Pesa to the customer).
  * POST /billing/webhook — HMAC-SHA512 signed Paystack webhook
    (``x-paystack-signature`` header). Only a *successful subscription charge*
    (``charge.success`` whose payload carries a ``plan`` object) upgrades the
    user's plan — a failed charge or a one-time payment never grants a plan.

Paystack amounts are in the currency's smallest unit (KES cents), so a KES
4,000/month plan is ``amount=400000``.
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import get_current_user_id
from ..config import get_settings
from ..database import get_db_pool, get_redis
from ..models import BillingStatus, CheckoutRequest, CheckoutResponse

VALID_PLANS = {"free", "pro", "business"}

settings = get_settings()
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

PAYSTACK_API_BASE = "https://api.paystack.co"

PLANS = {
    "free": {"name": "Free", "limit_gb": 100, "plan_code": None, "amount": 0},
    "pro": {
        "name": "Pro",
        "limit_gb": 1024,
        "plan_code": settings.paystack_plan_pro,
        "amount": settings.paystack_amount_pro,
    },
    "business": {
        "name": "Business",
        "limit_gb": float("inf"),
        "plan_code": settings.paystack_plan_business,
        "amount": settings.paystack_amount_business,
    },
}

# plan_code -> tier. The webhook trusts the plan_code on Paystack's own charge
# object (a server-side field, never client-supplied) as authoritative for the
# subscription the customer actually paid for.
PLAN_CODE_TO_TIER = {
    p["plan_code"]: tier for tier, p in PLANS.items() if p["plan_code"]
}

_PAYSTACK_HEADERS = {
    "Authorization": f"Bearer {settings.paystack_secret_key}",
    "Content-Type": "application/json",
}


@router.get("/status", response_model=BillingStatus)
async def get_billing_status(user_id: UUID = Depends(get_current_user_id)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT plan FROM users WHERE id = $1", user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        month = datetime.utcnow().strftime("%Y-%m")
        usage = await conn.fetchrow(
            "SELECT total_gb_used FROM usage_records WHERE user_id = $1 AND month = $2",
            user_id, month,
        )
        usage_gb = round(usage["total_gb_used"], 2) if usage else 0

        plan = user["plan"]
        limit_gb = PLANS.get(plan, PLANS["free"])["limit_gb"]

    return BillingStatus(
        plan=plan,
        usage_gb=usage_gb,
        limit_gb=limit_gb,
        can_upgrade=plan != "business",
    )


@router.post("/create-checkout", response_model=CheckoutResponse)
async def create_checkout(
    data: CheckoutRequest,
    user_id: UUID = Depends(get_current_user_id),
):
    """Create a Paystack checkout session against a recurring plan.

    Paystack identifies the customer by email and starts the subscription when
    the plan code is included on ``transaction/initialize``. The response's
    ``authorization_url`` is the hosted checkout page the browser redirects to.
    """
    plan = data.plan
    plan_config = PLANS.get(plan)
    if plan_config is None or plan == "free" or not plan_config["plan_code"]:
        raise HTTPException(status_code=400, detail="Invalid plan")

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT email FROM users WHERE id = $1", user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

    try:
        async with httpx.AsyncClient(
            base_url=PAYSTACK_API_BASE, headers=_PAYSTACK_HEADERS, timeout=15
        ) as client:
            resp = await client.post(
                "/transaction/initialize",
                json={
                    "email": user["email"],
                    "amount": plan_config["amount"],
                    "plan": plan_config["plan_code"],
                    "currency": settings.paystack_currency,
                    "callback_url": f"{settings.app_url}/billing?success=true",
                    "metadata": {"user_id": str(user_id), "plan": plan},
                },
            )
        if resp.status_code != 200:
            logger.error(
                "Paystack initialize failed for user %s (HTTP %s): %s",
                user_id, resp.status_code, resp.text[:500],
            )
            raise HTTPException(
                status_code=502, detail="Checkout failed. Please try again."
            )
        try:
            body = resp.json()
        except ValueError:
            logger.error(
                "Paystack initialize returned non-JSON for user %s: %s",
                user_id, resp.text[:500],
            )
            raise HTTPException(
                status_code=502, detail="Checkout failed. Please try again."
            )
        if not body.get("status"):
            logger.error(
                "Paystack initialize returned status=false for user %s: %s",
                user_id, resp.text[:500],
            )
            raise HTTPException(
                status_code=502, detail="Checkout failed. Please try again."
            )
        return CheckoutResponse(url=body["data"]["authorization_url"])
    except httpx.HTTPError as e:
        logger.error("Paystack checkout session failed for user %s: %s", user_id, e)
        raise HTTPException(
            status_code=502, detail="Checkout failed. Please try again."
        )


@router.post("/webhook")
async def paystack_webhook(request: Request):
    """Handle Paystack webhook events.

    Signature: HMAC-SHA512 hex of the raw request body keyed by the Paystack
    SECRET key (Paystack has no separate webhook secret), delivered in the
    ``x-paystack-signature`` header.
    """
    payload = await request.body()
    sig_header = request.headers.get("x-paystack-signature", "")

    expected = hmac.new(
        settings.paystack_secret_key.encode(), payload, hashlib.sha512
    ).hexdigest()
    if not sig_header or not hmac.compare_digest(sig_header, expected):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    try:
        event = json.loads(payload)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    data = event.get("data") or {}
    event_id = data.get("id") or event.get("id")

    # Idempotency check — skip if already processed (skip if Redis unavailable)
    redis = await get_redis()
    if redis is not None and event_id:
        already_processed = await redis.get(f"webhook:{event_id}")
        if already_processed:
            return {"detail": "Already processed"}
        await redis.setex(f"webhook:{event_id}", 86400, "1")  # 24h TTL

    event_type = event.get("event")

    if event_type == "charge.success":
        # Business-logic gate: only a settled, successful charge upgrades a
        # plan — a failed charge must never be a free-upgrade bypass.
        if data.get("status") != "success":
            logger.warning(
                "Paystack charge.success with status=%r — ignoring",
                data.get("status"),
            )
            return {"detail": "Charge not successful, ignoring"}

        # One-time charges carry no `plan` — only subscription charges do.
        # The plan_code is Paystack's own server-side field (never supplied by
        # the client), so it is authoritative for what was actually paid for.
        plan_code = (data.get("plan") or {}).get("plan_code")
        tier = PLAN_CODE_TO_TIER.get(plan_code)
        if tier is None:
            return {"detail": "Not a subscription charge, ignoring"}

        metadata = data.get("metadata") or {}
        user_id = metadata.get("user_id")

        # Validate metadata before writing, and never trust malformed values
        # (a bad UUID would otherwise 500 the webhook and trigger Paystack
        # retries).
        try:
            parsed_user_id = UUID(user_id) if user_id else None
        except (ValueError, TypeError, AttributeError):
            parsed_user_id = None
        if parsed_user_id is None:
            return {"detail": "Missing/invalid user_id metadata, ignoring"}

        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET plan = $1 WHERE id = $2",
                tier, parsed_user_id,
            )

    # Paystack Transfers events (admin payouts to M-PESA) — keep the local
    # payout status in sync and notify the admin who initiated the transfer.
    # These never grant a plan (only the charge.success branch above does).
    transfer_status_map = {
        "transfer.success": "success",
        "transfer.failed": "failed",
        "transfer.reversed": "reversed",
    }
    if event_type in transfer_status_map:
        transfer_code = data.get("transfer_code")
        if not transfer_code:
            return {"detail": "Missing transfer_code, ignoring"}

        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT amount_cents, created_by FROM payouts WHERE transfer_code = $1",
                transfer_code,
            )
            if not row:
                logger.warning(
                    "Paystack %s for unknown transfer %s — ignoring",
                    event_type, transfer_code,
                )
                return {"detail": "Unknown transfer, ignoring"}

            new_status = transfer_status_map[event_type]
            await conn.execute(
                "UPDATE payouts SET status = $1, updated_at = NOW() WHERE transfer_code = $2",
                new_status, transfer_code,
            )
            if row.get("created_by"):
                amount_kes = (row["amount_cents"] or 0) / 100
                await conn.execute(
                    "INSERT INTO notifications (user_id, type, message) VALUES ($1, 'payout', $2)",
                    row["created_by"],
                    f"Payout {new_status}: {transfer_code} (KES {amount_kes:,.2f})",
                )
        return {"detail": "Webhook received"}

    return {"detail": "Webhook received"}
