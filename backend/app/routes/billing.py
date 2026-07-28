import stripe
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from uuid import UUID

from ..config import get_settings
from ..database import get_db_pool
from ..auth import get_current_user_id
from ..models import BillingStatus, CheckoutRequest, CheckoutResponse

settings = get_settings()
stripe.api_key = settings.stripe_secret_key

router = APIRouter(prefix="/billing", tags=["billing"])

PLANS = {
    "free": {"name": "Free", "limit_gb": 100},
    "pro": {"name": "Pro", "limit_gb": 1024},
    "business": {"name": "Business", "limit_gb": float("inf")},
}

STRIPE_PRICES = {
    "pro": settings.stripe_price_pro,
    "business": settings.stripe_price_business,
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
    """Create a Stripe checkout session."""
    plan = data.plan
    if plan not in STRIPE_PRICES:
        raise HTTPException(status_code=400, detail="Invalid plan")

    price_id = STRIPE_PRICES[plan]

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT email, stripe_customer_id FROM users WHERE id = $1", user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        customer_id = user["stripe_customer_id"]

        if not customer_id:
            customer = stripe.Customer.create(email=user["email"])
            customer_id = customer.id
            await conn.execute(
                "UPDATE users SET stripe_customer_id = $1 WHERE id = $2",
                customer_id, user_id,
            )

    try:
        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=f"{settings.app_url}/billing?success=true",
            cancel_url=f"{settings.app_url}/billing?canceled=true",
            metadata={"user_id": str(user_id), "plan": plan},
        )
        return CheckoutResponse(url=checkout_session.url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhook events."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session["metadata"]["user_id"]
        plan = session["metadata"]["plan"]

        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET plan = $1 WHERE id = $2",
                plan, UUID(user_id),
            )

    return {"detail": "Webhook received"}
