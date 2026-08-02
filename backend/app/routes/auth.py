import re
import time
import logging
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from uuid import UUID

from ..database import get_db_pool, get_redis, USERS_TABLE_DDL
from ..auth import hash_password, verify_password, create_access_token, get_current_user_id
from ..email_service import send_verification_email, send_password_reset_email, decode_password_reset_token
from ..models import UserRegister, UserLogin, TokenResponse, ForgotPasswordRequest, ResetPasswordRequest
from ..config import get_settings
from ..security import get_client_ip
from ..services.email import send_welcome_email

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# In-memory rate-limiting fallbacks (used only when Redis is unavailable)
# {key: [epoch_seconds, ...]} — failed login attempts per IP+email
_login_attempts: dict[str, list[float]] = {}
# {ip: last_request_epoch} — forgot-password requests per IP
_forgot_requests: dict[str, float] = {}
_LOGIN_WINDOW = 900  # 15 minutes
_FORGOT_WINDOW = 60  # 60 seconds


def _prune_login_attempts(now: float) -> None:
    """Drop login-attempt keys whose newest entry has expired (keeps dict bounded)."""
    expired = [k for k, v in _login_attempts.items() if not v or now - v[-1] > _LOGIN_WINDOW]
    for k in expired:
        del _login_attempts[k]


def _prune_forgot_requests(now: float) -> None:
    """Drop forgot-password keys older than the window (keeps dict bounded)."""
    expired = [k for k, v in _forgot_requests.items() if now - v > _FORGOT_WINDOW]
    for k in expired:
        del _forgot_requests[k]


@router.post("/register", response_model=TokenResponse)
async def register(data: UserRegister, background_tasks: BackgroundTasks):
    # Validate email format
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', data.email):
        raise HTTPException(status_code=400, detail="Invalid email address")

    # Validate password: min 8 chars, must have letter + number
    pwd = data.password
    if len(pwd) < 8:
        raise HTTPException(
            status_code=400, detail="Password must be at least 8 characters"
        )
    if not re.search(r'[A-Za-z]', pwd):
        raise HTTPException(
            status_code=400, detail="Password must contain at least one letter"
        )
    if not re.search(r'[0-9]', pwd):
        raise HTTPException(
            status_code=400, detail="Password must contain at least one number"
        )

    # Require acceptance of Terms & Privacy
    if not data.terms_accepted:
        raise HTTPException(
            status_code=400,
            detail="You must accept the Terms of Service and Privacy Policy to create an account.",
        )

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Ensure tables exist (lazy creation on first request). The users table
        # uses the shared USERS_TABLE_DDL (single source of truth) so a fresh DB
        # gets the full schema — including terms_accepted / terms_accepted_at
        # referenced by the INSERT below — even if startup migrations were skipped.
        await conn.execute(USERS_TABLE_DDL)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS uploads (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                filename VARCHAR(255) NOT NULL,
                minio_url TEXT NOT NULL,
                torrent_magnet TEXT NOT NULL,
                file_size_mb FLOAT NOT NULL DEFAULT 0,
                thumbnail_url TEXT,
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
            CREATE TABLE IF NOT EXISTS notifications (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                type VARCHAR(50) NOT NULL,
                message TEXT NOT NULL,
                read BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_uploads_user_id ON uploads(user_id);
            CREATE INDEX IF NOT EXISTS idx_analytics_file_id ON analytics(file_id);
            CREATE INDEX IF NOT EXISTS idx_usage_user_month ON usage_records(user_id, month);
            CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at DESC);
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
            # No auto-admin on registration — admins are promoted only via
            # /auth/setup (gated by ADMIN_SETUP_ENABLED) or by an existing admin.
            # Generate the user UUID up front so we can derive a referral code from it.
            user_uuid = uuid.uuid4()
            referral_code = str(user_uuid).replace("-", "")[:8].upper()
            user = await conn.fetchrow(
                """INSERT INTO users (id, email, password_hash, is_admin, terms_accepted, terms_accepted_at, referral_code)
                   VALUES ($1, $2, $3, FALSE, TRUE, NOW(), $4)
                   RETURNING id""",
                user_uuid,
                data.email,
                password_hash,
                referral_code,
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

        # --- Referral handling ---
        # If the request included a referral code, credit the referrer (+10GB) and
        # the new user (+5GB). All failures are non-fatal — registration always succeeds.
        if data.ref_code:
            try:
                ref = data.ref_code.strip().upper()
                referrer = await conn.fetchrow(
                    "SELECT id FROM users WHERE UPPER(referral_code) = $1", ref
                )
                if referrer and str(referrer["id"]) != str(user_uuid):
                    await conn.execute(
                        "UPDATE users SET referral_bonus_gb = referral_bonus_gb + 10 WHERE id = $1",
                        referrer["id"],
                    )
                    await conn.execute(
                        "UPDATE users SET referred_by = $1, referral_bonus_gb = 5 WHERE id = $2",
                        referrer["id"],
                        user_uuid,
                    )
                    logger.info(
                        "Referral applied: user %s referred by %s", user_uuid, referrer["id"]
                    )
            except Exception as e:
                logger.warning("Referral processing failed for %s: %s", data.email, e)

        token = create_access_token({"sub": str(user["id"])})

        # Send verification email after successful registration
        # Wrap in try/except so registration succeeds even if email delivery fails
        try:
            await send_verification_email(data.email, user["id"])
        except Exception as e:
            logger.warning("Verification email failed after registration: %s", e)

        # Welcome email runs as a background task so it never slows down registration.
        background_tasks.add_task(send_welcome_email, data.email, data.email.split("@")[0])

        return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
async def login(data: UserLogin, request: Request):
    pool = await get_db_pool()
    redis = await get_redis()

    # Rate limiting: keyed by IP + email so an attacker can't lock out a victim's
    # account from their own IP. Redis primary, in-memory fallback when down.
    ip = get_client_ip(request)
    rate_key = f"login_attempts:{ip}:{data.email}"
    if redis is not None:
        attempts = await redis.get(rate_key)
        if attempts and int(attempts) >= 5:
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Try again in 15 minutes.",
            )
    else:
        now = time.time()
        _prune_login_attempts(now)
        recent = [t for t in _login_attempts.get(rate_key, []) if now - t < _LOGIN_WINDOW]
        if len(recent) >= 5:
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Try again in 15 minutes.",
            )
        _login_attempts[rate_key] = recent

    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, password_hash, verified FROM users WHERE email = $1",
            data.email,
        )
        if not user or not verify_password(data.password, user["password_hash"]):
            # Increment failed attempt counter (Redis primary, in-memory fallback)
            if redis is not None:
                await redis.incr(rate_key)
                await redis.expire(rate_key, 900)  # 15 minutes
            else:
                _login_attempts.setdefault(rate_key, []).append(time.time())
                _prune_login_attempts(time.time())
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )

        # Block login if email verification is required and user hasn't verified
        settings = get_settings()
        if settings.email_verification_required and not user["verified"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Email not verified. Please check your inbox or request a new verification link.",
            )

    # Reset rate limit on success
    if redis is not None:
        await redis.delete(rate_key)
    else:
        _login_attempts.pop(rate_key, None)

    token = create_access_token({"sub": str(user["id"])})
    return TokenResponse(access_token=token)


@router.post("/forgot-password")
async def forgot_password(data: ForgotPasswordRequest, request: Request):
    """
    Request a password reset email.
    Always returns success to avoid email enumeration.
    Rate limited to 1 request per 60 seconds per IP.
    """
    # Rate limiting: 1 request per 60 seconds per IP
    if request:
        redis = await get_redis()
        ip = get_client_ip(request)
        rate_key = f"forgot_pw_rate:{ip}"
        if redis is not None:
            count = await redis.get(rate_key)
            if count and int(count) >= 1:
                raise HTTPException(
                    status_code=429,
                    detail="Please wait before requesting another reset email.",
                )
            await redis.incr(rate_key)
            await redis.expire(rate_key, 60)
        else:
            # In-memory fallback: 1 request per 60 seconds per IP
            now = time.time()
            _prune_forgot_requests(now)
            if now - _forgot_requests.get(ip, 0) < _FORGOT_WINDOW:
                raise HTTPException(
                    status_code=429,
                    detail="Please wait before requesting another reset email.",
                )
            _forgot_requests[ip] = now

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email FROM users WHERE email = $1", data.email
        )

    if user:
        # Wrap in try/except so email delivery failures can never 500 the
        # endpoint — the anti-enumeration response below must always be
        # returned (a 500 vs 200 split would leak which emails are registered).
        # Registration follows the same pattern for its verification email.
        try:
            await send_password_reset_email(user["email"], user["id"])
        except Exception as e:
            logger.warning("Password reset email failed for %s: %s", user["email"], e)

    return {"detail": "If that email is registered, a password reset link has been sent."}


@router.post("/reset-password")
async def reset_password(data: ResetPasswordRequest):
    """Reset password using a valid reset token."""
    if len(data.password) < 8:
        raise HTTPException(
            status_code=400, detail="Password must be at least 8 characters"
        )
    if not re.search(r'[A-Za-z]', data.password):
        raise HTTPException(
            status_code=400, detail="Password must contain at least one letter"
        )
    if not re.search(r'[0-9]', data.password):
        raise HTTPException(
            status_code=400, detail="Password must contain at least one number"
        )

    user_id_str = decode_password_reset_token(data.token)
    if not user_id_str:
        raise HTTPException(
            status_code=400, detail="Invalid or expired reset token"
        )

    # Verify the token hasn't been revoked (via Redis). The one-time key is
    # only stored when Redis is up at SEND time — if Redis was down then, a
    # valid, unexpired token legitimately has no key, so fall back to
    # JWT-expiry-only validation (matching the Redis-down-at-click path) rather
    # than rejecting a valid token. When the key IS present it is consumed into
    # a "CONSUMED" tombstone (not deleted), so a replayed link is still rejected
    # for the rest of the JWT window (and across Redis restarts, if persistence
    # is enabled).
    redis = await get_redis()
    if redis is not None:
        token_key = f"reset_token:{data.token}"
        stored = await redis.get(token_key)
        if stored == "CONSUMED":
            # Already used — reject the replay even though the JWT is still valid.
            raise HTTPException(
                status_code=400, detail="Invalid or expired reset token"
            )
        if stored:
            # Consume the token (one-time use): replace with a tombstone so a
            # replayed link within the 1h JWT window is still blocked.
            await redis.set(token_key, "CONSUMED", ex=3600)
        else:
            logger.warning(
                "Reset token one-time key missing — falling back to JWT-expiry-only "
                "validation (Redis likely down when the reset email was sent)"
            )

    pool = await get_db_pool()
    password_hash = hash_password(data.password)
    async with pool.acquire() as conn:
        # Also mark the email verified: presenting a valid reset token IS proof
        # of email ownership (it was only delivered to that inbox), so a user
        # who lost/never got their verification email can unlock login by
        # resetting instead of being permanently locked out (production gates
        # login on verified).
        result = await conn.execute(
            "UPDATE users SET password_hash = $1, verified = TRUE WHERE id = $2",
            password_hash,
            UUID(user_id_str),
        )
        if result == "UPDATE 0":
            raise HTTPException(status_code=404, detail="User not found")

    logger.info("Password reset successful for user %s", user_id_str)
    return {"detail": "Password has been reset successfully."}


@router.post("/setup")
async def setup_first_admin(
    email: str = None,
    requester_id: UUID = Depends(get_current_user_id),
):
    """
    Promote a user to admin.
    - Only available while ADMIN_SETUP_ENABLED=true (bootstrap mode).
    - If an admin already exists, the requester must be an admin.
    - If called without email and no admin exists, promotes the first registered user.
    """
    _settings = get_settings()
    if not _settings.admin_setup_enabled:
        raise HTTPException(
            status_code=403,
            detail="Admin setup is disabled. Set ADMIN_SETUP_ENABLED=true to bootstrap the first admin.",
        )

    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Check if any admin exists
        admin_exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM users WHERE is_admin = TRUE)"
        )

        # If admin already exists, verify the requester is an admin
        if admin_exists:
            requester = await conn.fetchrow(
                "SELECT is_admin FROM users WHERE id = $1", requester_id
            )
            if not requester or not requester["is_admin"]:
                raise HTTPException(
                    status_code=403,
                    detail="Admin access required. An admin account already exists.",
                )

        # If email is specified, promote that user
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


@router.get("/referral")
async def get_referral(user_id: UUID = Depends(get_current_user_id)):
    """Return the current user's referral code and referral stats."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT referral_code, referral_bonus_gb FROM users WHERE id = $1",
            user_id,
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        code = user["referral_code"]
        # Backfill referral code for users created before the feature shipped.
        if not code:
            code = str(user_id).replace("-", "")[:8].upper()
            await conn.execute(
                "UPDATE users SET referral_code = $1 WHERE id = $2", code, user_id
            )

        total_referrals = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE referred_by = $1", user_id
        )

    _settings = get_settings()
    return {
        "referral_code": code,
        # Use the canonical app domain (www.torrentcdn.store in prod) rather than
        # the apex, which has historically been flaky.
        "referral_url": f"{_settings.app_url.rstrip('/')}?ref={code}",
        "total_referrals": total_referrals or 0,
        "bonus_gb_earned": user["referral_bonus_gb"] or 0,
    }


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


@router.delete("/me")
async def delete_own_account(user_id: UUID = Depends(get_current_user_id)):
    """
    Delete the current user's own account and all associated data.
    Admin accounts cannot be deleted through this endpoint.
    """
    from ..routes.upload import get_minio_client

    _settings = get_settings()
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Check if the user is an admin — prevent self-deletion of admin accounts
        user = await conn.fetchrow(
            "SELECT id, email, is_admin FROM users WHERE id = $1", user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if user["is_admin"]:
            raise HTTPException(
                status_code=403,
                detail="Admin accounts cannot be deleted. Contact another admin or use the admin panel.",
            )

        # Delete upload files from S3 first
        files = await conn.fetch(
            "SELECT id, minio_url FROM uploads WHERE user_id = $1", user_id
        )
        try:
            s3 = get_minio_client()
            for f in files:
                try:
                    key = "/".join(f["minio_url"].split("/")[-3:])
                    s3.delete_object(Bucket=_settings.minio_bucket, Key=key)
                except Exception as e:
                    logger.warning("Failed to delete S3 object for file %s: %s", f["id"], e)
        except Exception:
            logger.warning("MinIO unavailable — skipping file deletion")

        # Delete user (cascades to uploads, analytics, usage_records via FK)
        result = await conn.execute(
            "DELETE FROM users WHERE id = $1", user_id
        )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="User not found")

    logger.info("User %s (%s) deleted their own account", user_id, user["email"])
    return {"detail": "Account and all associated data deleted successfully."}
