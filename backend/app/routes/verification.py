import logging
from fastapi import APIRouter, Depends, HTTPException
from uuid import UUID

from ..database import get_db_pool
from ..auth import get_current_user_id
from ..models import EmailVerifyRequest
from ..email_service import decode_verification_token, send_verification_email

# Simple in-memory rate limiting for resend (Redis not required)
_resend_cooldowns: dict[str, float] = {}
from time import time

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
    # Rate limit: check cooldown
    now = time()
    last_send = _resend_cooldowns.get(str(user_id), 0)
    if now - last_send < 60:
        remaining = int(60 - (now - last_send))
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
    return {"detail": "Email verified automatically (email service not configured)"}


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
