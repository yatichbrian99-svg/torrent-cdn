import re
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from uuid import UUID

from ..database import get_db_pool, get_redis
from ..auth import hash_password, verify_password, create_access_token, get_current_user_id
from ..email_service import send_verification_email
from ..models import UserRegister, UserLogin, TokenResponse, ForgotPasswordRequest, ResetPasswordRequest
from ..email_service import send_verification_email, send_password_reset_email, decode_password_reset_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse)
async def register(data: UserRegister):
    # Validate email format
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', data.email):
        raise HTTPException(status_code=400, detail="Invalid email address")

    # Validate minimum password length
    if len(data.password) < 8:
        raise HTTPException(
            status_code=400, detail="Password must be at least 8 characters"
        )

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Ensure tables exist (lazy creation on first request)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                email VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                plan VARCHAR(50) DEFAULT 'free',
                verified BOOLEAN DEFAULT FALSE,
                is_admin BOOLEAN DEFAULT FALSE,
                stripe_customer_id VARCHAR(255),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS uploads (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                filename VARCHAR(255) NOT NULL,
                minio_url TEXT NOT NULL,
                torrent_magnet TEXT NOT NULL,
                file_size_mb FLOAT NOT NULL DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS analytics (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                file_id UUID NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
                event_type VARCHAR(50) NOT NULL,
                bytes_p2p BIGINT DEFAULT 0,
                bytes_origin BIGINT DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS usage_records (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                month VARCHAR(7) NOT NULL,
                total_gb_used FLOAT DEFAULT 0,
                UNIQUE(user_id, month)
            );

            CREATE INDEX IF NOT EXISTS idx_uploads_user_id ON uploads(user_id);
            CREATE INDEX IF NOT EXISTS idx_analytics_file_id ON analytics(file_id);
            CREATE INDEX IF NOT EXISTS idx_usage_user_month ON usage_records(user_id, month);
        """)

        existing = await conn.fetchrow(
            "SELECT id FROM users WHERE email = $1", data.email
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )

        password_hash = hash_password(data.password)
        try:
            # First user to register becomes admin
            existing_count = await conn.fetchval("SELECT COUNT(*) FROM users")
            is_admin = existing_count == 0

            user = await conn.fetchrow(
                """INSERT INTO users (email, password_hash, is_admin)
                   VALUES ($1, $2, $3)
                   RETURNING id""",
                data.email,
                password_hash,
                is_admin,
            )
        except Exception as e:
            # Catch unique constraint violation (race condition between SELECT and INSERT)
            err_str = str(e)
            if "duplicate key" in err_str or "unique" in err_str.lower():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email already registered",
                )
            logger.error("Registration INSERT failed: %s", e)
            raise HTTPException(
                status_code=500, detail="Registration failed. Please try again."
            )

        # Fallback: ensure at least one admin exists
        # This handles the edge case where the startup migration ran before
        # any users existed, and existing users' is_admin wasn't set correctly
        if not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM users WHERE is_admin = TRUE)"):
            first_id = await conn.fetchval(
                "SELECT id FROM users ORDER BY created_at ASC LIMIT 1"
            )
            if first_id:
                await conn.execute(
                    "UPDATE users SET is_admin = TRUE WHERE id = $1", first_id
                )
                logger.info("Promoted user %s to admin (first registered)", first_id)

        token = create_access_token({"sub": str(user["id"])})

        # Send verification email after successful registration
        # Wrap in try/except so registration succeeds even if email delivery fails
        try:
            await send_verification_email(data.email, user["id"])
        except Exception as e:
            logger.warning("Verification email failed after registration: %s", e)

        return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
async def login(data: UserLogin):
    pool = await get_db_pool()
    redis = await get_redis()

    # Rate limiting: check login attempts (skip if Redis unavailable)
    if redis is not None:
        rate_key = f"login_attempts:{data.email}"
        attempts = await redis.get(rate_key)
        if attempts and int(attempts) >= 5:
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Try again in 15 minutes.",
            )

    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, password_hash FROM users WHERE email = $1",
            data.email,
        )
        if not user or not verify_password(data.password, user["password_hash"]):
            # Increment failed attempt counter (skip if Redis unavailable)
            if redis is not None:
                await redis.incr(rate_key)
                await redis.expire(rate_key, 900)  # 15 minutes
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )

    # Reset rate limit on success
    if redis is not None:
        await redis.delete(rate_key)

    token = create_access_token({"sub": str(user["id"])})
    return TokenResponse(access_token=token)


@router.post("/forgot-password")
async def forgot_password(data: ForgotPasswordRequest):
    """
    Request a password reset email.
    Always returns success to avoid email enumeration.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email FROM users WHERE email = $1", data.email
        )

    if user:
        await send_password_reset_email(user["email"], user["id"])

    return {"detail": "If that email is registered, a password reset link has been sent."}


@router.post("/reset-password")
async def reset_password(data: ResetPasswordRequest):
    """Reset password using a valid reset token."""
    if len(data.password) < 8:
        raise HTTPException(
            status_code=400, detail="Password must be at least 8 characters"
        )

    user_id_str = decode_password_reset_token(data.token)
    if not user_id_str:
        raise HTTPException(
            status_code=400, detail="Invalid or expired reset token"
        )

    # Verify token hasn't been revoked (via Redis)
    redis = await get_redis()
    if redis is not None:
        token_key = f"reset_token:{data.token}"
        stored = await redis.get(token_key)
        if not stored:
            raise HTTPException(
                status_code=400, detail="Invalid or expired reset token"
            )
        # Consume the token (one-time use)
        await redis.delete(token_key)

    pool = await get_db_pool()
    password_hash = hash_password(data.password)
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET password_hash = $1 WHERE id = $2",
            password_hash,
            UUID(user_id_str),
        )
        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="User not found")

    logger.info("Password reset successful for user %s", user_id_str)
    return {"detail": "Password has been reset successfully."}


@router.post("/setup")
async def setup_first_admin(email: str = None):
    """
    One-time setup: promote a user to admin.
    - If ?email=user@example.com is provided, promote that specific user.
    - If called without email, promote the first registered user if no admin exists.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # If email is specified, promote that user regardless
        if email:
            user = await conn.fetchrow(
                "SELECT id, email, is_admin FROM users WHERE email = $1", email
            )
            if not user:
                return {"detail": f"No user found with email {email}"}
            if user["is_admin"]:
                return {"detail": f"User {email} is already an admin"}

            await conn.execute(
                "UPDATE users SET is_admin = TRUE WHERE email = $1", email
            )
            logger.info("Promoted user %s (%s) to admin via /auth/setup", user["id"], email)
            return {"detail": f"User {email} promoted to admin. Log in again."}

        # No email — promote first user if no admin exists
        admin_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM users WHERE is_admin = TRUE)"
        )
        if admin_exists:
            return {"detail": "Admin already exists — no action needed"}

        first_user = await conn.fetchval(
            "SELECT id FROM users ORDER BY created_at ASC LIMIT 1"
        )
        if not first_user:
            return {"detail": "No users found — register first"}

        await conn.execute(
            "UPDATE users SET is_admin = TRUE WHERE id = $1", first_user
        )
        logger.info("Promoted user %s to admin via /auth/setup", first_user)

    return {"detail": "First user promoted to admin. Log in again for the changes to take effect."}


@router.get("/me")
async def get_me(user_id: UUID = Depends(get_current_user_id)):
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, plan, verified, is_admin, created_at FROM users WHERE id = $1",
            user_id,
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return dict(user)
