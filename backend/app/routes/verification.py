import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from ..ai.redis_cache import setifempty
from ..auth import get_current_user_id
from ..database import get_db_pool, get_redis
from ..email_service import (
    decode_verification_token,
    is_email_service_configured,
    send_verification_email,
)
from ..models import EmailVerifyRequest

# Simple in-memory rate limiting for resend (Redis not required)
# Bounded: entries older than the 60s cooldown window are dropped on every
# write, so a flood of distinct authenticated user_ids can't grow the dict
# without bound (the global per-IP limiter still paces request volume).
_resend_cooldowns: dict[str, float] = {}
_RESEND_COOLDOWN_SECONDS = 60
from time import time


def _prune_resend_cooldowns(now: float) -> None:
    expired = [k for k, ts in _resend_cooldowns.items() if now - ts >= _RESEND_COOLDOWN_SECONDS]
    for k in expired:
        del _resend_cooldowns[k]

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["verification"])


@router.post("/verify-email")
async def verify_email(data: EmailVerifyRequest):
    """Verify a user's email using a token sent via email."""
    user_id_str = decode_verification_token(data.token)
    if not user_id_str:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, verified FROM users WHERE id = $1",
            UUID(user_id_str),
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user["verified"]:
            return {"detail": "Email already verified"}

        await conn.execute(
            "UPDATE users SET verified = TRUE WHERE id = $1",
            UUID(user_id_str),
        )

    logger.info("Email verified for user %s", user_id_str)
    return {"detail": "Email verified successfully"}


@router.post("/resend-verification")
async def resend_verification(user_id: UUID = Depends(get_current_user_id)):
    """Resend the verification email to the current user (max once per 60 seconds)."""
    # Rate limit: check cooldown (bounded store — expired entries pruned first)
    now = time()
    _prune_resend_cooldowns(now)
    last_send = _resend_cooldowns.get(str(user_id), 0)
    if now - last_send < _RESEND_COOLDOWN_SECONDS:
        remaining = int(_RESEND_COOLDOWN_SECONDS - (now - last_send))
        raise HTTPException(
            status_code=429,
            detail=f"Please wait {remaining}s before requesting another email",
        )

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT email, verified FROM users WHERE id = $1", user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user["verified"]:
            return {"detail": "Email already verified"}

    _resend_cooldowns[str(user_id)] = now
    sent = await send_verification_email(user["email"], user_id)
    if sent:
        return {"detail": "Verification email sent"}
    # No email provider at all → verification is impossible; mark the account
    # verified so it isn't locked out forever (the session gate restricts
    # unverified sessions to this allowlist). Keyed on the provider being
    # ABSENT, not on a flaky send: a configured-but-failing provider keeps the
    # user unverified and the gate engaged.
    if not is_email_service_configured():
        redis = await get_redis()
        auto_verified = False
        if redis is not None:
            # Atomic first-wins gate: two concurrent registrations from the same
            # inbox during a provider outage cannot both mark the account verified.
            auto_verified = await setifempty(
                redis,
                f"email-verify:auto:{user_id}",
                "1",
                ex=600,
            )
        if auto_verified or redis is None:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET verified = TRUE WHERE id = $1", user_id
                )
            return {"detail": "Email verified automatically (email service not configured)"}
        # Another concurrent caller won the atomic race and already verified this
        # account; nothing more to do.
    return {"detail": "Verification email could not be sent — please try again later."}


@router.get("/verification-status")
async def verification_status(user_id: UUID = Depends(get_current_user_id)):
    """Check if the current user's email is verified."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT verified FROM users WHERE id = $1", user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {"verified": user["verified"]}
