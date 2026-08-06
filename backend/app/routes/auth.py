import hashlib
import json
import re
import secrets
import time
import logging
import unicodedata
import uuid
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from uuid import UUID

from ..database import get_db_pool, get_redis, USERS_TABLE_DDL, API_KEYS_TABLE_DDL
from ..auth import (
    API_KEY_PREFIX,
    hash_api_key,
    hash_password,
    verify_password,
    create_access_token,
    get_current_user_id,
    revoke_user_sessions,
)
from ..email_service import send_verification_email, send_password_reset_email, decode_password_reset_token
from ..mfa import (
    backup_code_matches,
    create_mfa_token,
    decode_mfa_token,
    generate_backup_codes,
    generate_totp_secret,
    hash_backup_codes,
    otpauth_uri,
    verify_totp,
)
from ..models import (
    ApiKeyResponse,
    UserRegister,
    UserLogin,
    TokenResponse,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    MfaStatusResponse,
    MfaSetupResponse,
    MfaCodeRequest,
    MfaConfirmRequest,
)
from ..config import get_settings
from ..security import get_client_ip
from ..services.email import send_welcome_email
from ..security_events import record as record_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# In-memory rate-limiting fallbacks (used only when Redis is unavailable)
# {key: [epoch_seconds, ...]} — failed login attempts per IP+email
_login_attempts: dict[str, list[float]] = {}
# {ip: last_request_epoch} — forgot-password requests per IP
_forgot_requests: dict[str, float] = {}
# {ip: [epoch_seconds, ...]} — registration attempts per IP (account/email spam)
_register_requests: dict[str, list[float]] = {}
_LOGIN_WINDOW = 900  # 15 minutes
_FORGOT_WINDOW = 60  # 60 seconds
# Registration throttling: per-IP, not per-email, so an attacker can't burn the
# transactional-email quota (every registration sends a verification email) or
# bloat the users table. Redis primary, in-memory fallback.
_REGISTER_WINDOW = 3600  # 1 hour
_REGISTER_LIMIT = 10

# Failed MFA confirm attempts per IP (brute-force a 6-digit TOTP or a 10-char
# backup code). Same 5/15-min lockout shape as login. Redis primary,
# in-memory fallback.
_mfa_attempts: dict[str, list[float]] = {}
_MFA_WINDOW = 900  # 15 minutes
_MFA_LIMIT = 5


def _prune_mfa_attempts(now: float) -> None:
    """Drop MFA-attempt keys whose newest entry has expired (keeps dict bounded)."""
    expired = [k for k, v in _mfa_attempts.items() if not v or now - v[-1] > _MFA_WINDOW]
    for k in expired:
        del _mfa_attempts[k]


async def _check_mfa_rate_limit(ip: str) -> None:
    """Reject early when this IP already has _MFA_LIMIT failed MFA attempts."""
    redis = await get_redis()
    if redis is not None:
        count = await redis.get(f"mfa_attempts:{ip}")
        if count and int(count) >= _MFA_LIMIT:
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Try again in 15 minutes.",
            )
        return
    now = time.time()
    _prune_mfa_attempts(now)
    recent = [t for t in _mfa_attempts.get(ip, []) if now - t < _MFA_WINDOW]
    if len(recent) >= _MFA_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Try again in 15 minutes.",
        )


async def _record_mfa_failure(ip: str) -> None:
    """Record a failed MFA attempt (Redis primary, in-memory fallback)."""
    redis = await get_redis()
    if redis is not None:
        await redis.incr(f"mfa_attempts:{ip}")
        await redis.expire(f"mfa_attempts:{ip}", _MFA_WINDOW)
    else:
        _mfa_attempts.setdefault(ip, []).append(time.time())
        _prune_mfa_attempts(time.time())


def _mfa_code_valid(row, code: str) -> bool:
    """True when `code` is the user's current TOTP code OR an unused backup code."""
    secret = row.get("mfa_secret")
    if secret and verify_totp(secret, code):
        return True
    stored_raw = row.get("mfa_backup_codes")
    if stored_raw:
        try:
            stored = json.loads(stored_raw)
        except (ValueError, TypeError):
            stored = []
        if backup_code_matches(stored, code):
            return True
    return False


async def _revoke_sessions(user_id) -> None:
    """Rotate every pre-existing session token (MFA enable/disable).

    Delegates to revoke_user_sessions, which writes BOTH the DB security_stamp
    (authoritative — works without Redis) and the Redis pw_change marker (fast
    path). A security-posture change must rotate sessions: any token minted
    before the change lacks/retains the old posture."""
    await revoke_user_sessions(user_id)


def _normalize_email(email: str) -> str:
    """NFC-normalize + trim an email for storage/lookup.

    Two visually identical addresses in different Unicode normalization forms
    (e.g. composed vs decomposed accented characters) would otherwise be
    distinct rows — a confusable-account vector. NFC makes the canonical form
    the stored form. Done at the API boundary so every handler (and the rate
    limiter key) sees one shape."""
    return unicodedata.normalize("NFC", email.strip())


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


def _prune_register_requests(now: float) -> None:
    """Drop registration keys whose newest entry has expired (keeps dict bounded)."""
    expired = [k for k, v in _register_requests.items() if not v or now - v[-1] > _REGISTER_WINDOW]
    for k in expired:
        del _register_requests[k]


async def _check_register_rate_limit(request: Request) -> None:
    """Per-IP registration throttle — Redis primary, bounded in-memory fallback."""
    ip = get_client_ip(request)
    redis = await get_redis()
    if redis is not None:
        rate_key = f"register_rate:{ip}"
        count = await redis.get(rate_key)
        if count and int(count) >= _REGISTER_LIMIT:
            record_event("register_throttled", detail="per-IP limit", ip=ip)
            raise HTTPException(
                status_code=429,
                detail="Too many registrations from this IP. Please try again later.",
            )
        await redis.incr(rate_key)
        await redis.expire(rate_key, _REGISTER_WINDOW)
    else:
        now = time.time()
        _prune_register_requests(now)
        recent = [t for t in _register_requests.get(ip, []) if now - t < _REGISTER_WINDOW]
        if len(recent) >= _REGISTER_LIMIT:
            record_event("register_throttled", detail="per-IP limit", ip=ip)
            raise HTTPException(
                status_code=429,
                detail="Too many registrations from this IP. Please try again later.",
            )
        recent.append(now)
        _register_requests[ip] = recent


@router.post("/register", response_model=TokenResponse)
async def register(data: UserRegister, background_tasks: BackgroundTasks, request: Request):
    # Throttle registrations per IP BEFORE doing any work — a registration costs
    # a bcrypt hash + a verification email, both of which an attacker would love
    # to trigger for free in bulk.
    await _check_register_rate_limit(request)

    # NFC-normalize the email at the boundary (confusable-account protection).
    data.email = _normalize_email(data.email)

    # Validate email format (length already capped at 255 by the model — a
    # >255-char email would otherwise 500 the users.email VARCHAR(255) insert).
    if not re.match(r'^[^@]+@[^@]+\.[^@]+$', data.email):
        raise HTTPException(status_code=400, detail="Invalid email address")

    # Validate password: min 8 chars, must have letter + number.
    # The pydantic model already caps the CHARACTER length at 72; bcrypt's hard
    # limit is 72 BYTES, so reject multi-byte passwords that would silently
    # truncate (two distinct long passwords could otherwise collide).
    pwd = data.password
    if len(pwd) < 8:
        raise HTTPException(
            status_code=400, detail="Password must be at least 8 characters"
        )
    if len(pwd.encode("utf-8")) > 72:
        raise HTTPException(
            status_code=400, detail="Password must be at most 72 bytes (UTF-8)"
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
                idempotency_key TEXT,
                content_hash TEXT,
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


@router.post("/login")
async def login(data: UserLogin, request: Request):
    data.email = _normalize_email(data.email)
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
        # Look up the RAW email first (a pre-normalization account may be stored
        # in a non-NFC form — normalizing their input would silently lock them
        # out), then the NFC form (covers newly-normalized accounts where the
        # user typed the other canonical form). mfa_enabled drives the second-
        # factor gate below.
        user = await conn.fetchrow(
            "SELECT id, email, password_hash, verified, mfa_enabled FROM users WHERE email = $1",
            data.email,
        )
        if not user:
            user = await conn.fetchrow(
                "SELECT id, email, password_hash, verified, mfa_enabled FROM users WHERE email = $1",
                _normalize_email(data.email),
            )
        # Constant-ish-time anti-enumeration: when the email is unknown there is
        # no stored hash to verify against, so a bare early return would let an
        # attacker distinguish registered emails by response timing (a bcrypt
        # check costs ~100-250ms). Verify against a fixed dummy bcrypt hash so
        # the "unknown user" path does the same work as a wrong-password path.
        _DUMMY_HASH = "$2b$12$C6UzMDM.H6dfI/f/IKcEeO7Dkz9nWlG5vXqgLbB5fQzY3S0YvX9nS"
        if not user:
            verify_password(data.password, _DUMMY_HASH)
        if not user or not verify_password(data.password, user["password_hash"]):
            # Increment failed attempt counter (Redis primary, in-memory fallback)
            if redis is not None:
                await redis.incr(rate_key)
                await redis.expire(rate_key, 900)  # 15 minutes
            else:
                _login_attempts.setdefault(rate_key, []).append(time.time())
                _prune_login_attempts(time.time())
            # Authentication-event log: failed login (email + client IP, never
            # the password) so brute-force / credential-stuffing campaigns are
            # detectable in the logs even before the lockout counter trips.
            logger.warning("Login failed for %s from IP %s", data.email, ip)
            record_event("login_failure", detail=data.email, ip=ip)
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

    # Second-factor gate: MFA-enabled accounts get a SHORT-LIVED mfa_token
    # instead of a session token. The real access token is only minted at
    # /auth/mfa/confirm after a valid TOTP/backup code is presented — a stolen
    # password alone can no longer complete the login.
    if user.get("mfa_enabled"):
        logger.info("Login for user %s requires second factor", user["id"])
        return {
            "mfa_required": True,
            "mfa_token": create_mfa_token(str(user["id"])),
            "token_type": "bearer",
        }

    # Authentication-event log: successful login (user id only — no token,
    # no password, no email beyond what the failed-path already logs).
    # Not recorded to the security feed: success is high-volume noise that
    # would drown out the failures/admin actions the feed exists to surface.
    logger.info("Login succeeded for user %s", user["id"])
    token = create_access_token({"sub": str(user["id"])})
    return {"access_token": token, "token_type": "bearer"}


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
        # Raw first (pre-normalization accounts), NFC fallback (new accounts).
        user = await conn.fetchrow(
            "SELECT id, email FROM users WHERE email = $1", data.email
        )
        if not user:
            user = await conn.fetchrow(
                "SELECT id, email FROM users WHERE email = $1", _normalize_email(data.email)
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
    if len(data.password.encode("utf-8")) > 72:
        raise HTTPException(
            status_code=400, detail="Password must be at most 72 bytes (UTF-8)"
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

    # Record the password change so EVERY pre-existing session token (issued
    # before this instant) is rejected by get_current_user_id — "change my
    # password" must kick out sessions on all other devices. The DB security
    # stamp is authoritative (works without Redis); Redis adds a fast path.
    await revoke_user_sessions(UUID(user_id_str))

    logger.info("Password reset successful for user %s", user_id_str)
    record_event("password_reset", detail=str(user_id_str))
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


@router.post("/api-key", response_model=ApiKeyResponse)
async def create_api_key(user_id: UUID = Depends(get_current_user_id)):
    """Generate (or rotate) the current user's API key.

    One active key per user: generating revokes any previous key. The plaintext
    token is returned exactly ONCE — only its SHA-256 hash is stored, so it can
    never be recovered again (generate again to rotate). Keys authenticate as
    ``Authorization: Bearer tcdn_…`` and intentionally survive password changes
    (they are revoked explicitly from the dashboard)."""
    token = API_KEY_PREFIX + secrets.token_urlsafe(32)
    key_hash = hash_api_key(token)
    prefix = token[:16] + "..."
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        # Lazy table creation (fresh-DB safety, same pattern as register).
        await conn.execute(API_KEYS_TABLE_DDL)
        # Atomic rotate: a single upsert keyed on the unique user_id index
        # replaces any previous key in one statement. (A DELETE + INSERT pair
        # would race under concurrent generates and could leave two keys.)
        row = await conn.fetchrow(
            """INSERT INTO api_keys (user_id, key_hash, prefix)
               VALUES ($1, $2, $3)
               ON CONFLICT (user_id)
               DO UPDATE SET key_hash = EXCLUDED.key_hash,
                             prefix = EXCLUDED.prefix,
                             last_used_at = NULL
               RETURNING created_at""",
            user_id,
            key_hash,
            prefix,
        )
    return ApiKeyResponse(api_key=token, prefix=prefix, created_at=row["created_at"])


@router.get("/api-key", response_model=ApiKeyResponse)
async def get_api_key(user_id: UUID = Depends(get_current_user_id)):
    """Return API-key metadata WITHOUT the secret — the plaintext is stored only
    as a hash and can never be shown again. The dashboard shows the prefix + age."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(API_KEYS_TABLE_DDL)
        row = await conn.fetchrow(
            "SELECT prefix, created_at, last_used_at FROM api_keys WHERE user_id = $1",
            user_id,
        )
        if not row:
            return ApiKeyResponse()
        return ApiKeyResponse(
            prefix=row["prefix"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
        )


@router.delete("/api-key")
async def revoke_api_key(user_id: UUID = Depends(get_current_user_id)):
    """Revoke the current user's API key. Subsequent uses return 401."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        await conn.execute(API_KEYS_TABLE_DDL)
        await conn.execute("DELETE FROM api_keys WHERE user_id = $1", user_id)
    return {"detail": "API key revoked"}


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

        # Collect the user's file URLs BEFORE deleting (content-dedup means a
        # stored object may be shared with other users' uploads).
        files = await conn.fetch(
            "SELECT id, minio_url FROM uploads WHERE user_id = $1", user_id
        )

        # Delete the user FIRST (cascade removes their uploads rows) so the
        # refcount check below sees only OTHER users' references to shared
        # objects — a shared copy survives while its last owner is removed.
        result = await conn.execute(
            "DELETE FROM users WHERE id = $1", user_id
        )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="User not found")

        # Delete each storage object only when no remaining upload references it.
        try:
            s3 = get_minio_client()
            for f in files:
                try:
                    key = "/".join(f["minio_url"].split("/")[-3:])
                    refs = await conn.fetchval(
                        "SELECT COUNT(*) FROM uploads WHERE minio_url = $1",
                        f["minio_url"],
                    )
                    if refs == 0:
                        s3.delete_object(Bucket=_settings.minio_bucket, Key=key)
                except Exception as e:
                    logger.warning("Failed to delete S3 object for file %s: %s", f["id"], e)
        except Exception:
            logger.warning("MinIO unavailable — skipping file deletion")

    logger.info("User %s (%s) deleted their own account", user_id, user["email"])
    return {"detail": "Account and all associated data deleted successfully."}


# --- Two-factor authentication (TOTP) ---------------------------------------

@router.get("/mfa", response_model=MfaStatusResponse)
async def mfa_status(user_id: UUID = Depends(get_current_user_id)):
    """Current MFA state (enabled + whether the user is an admin)."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT mfa_enabled, is_admin FROM users WHERE id = $1", user_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
    return MfaStatusResponse(enabled=bool(row["mfa_enabled"]), is_admin=bool(row["is_admin"]))


@router.post("/mfa/setup", response_model=MfaSetupResponse)
async def mfa_setup(user_id: UUID = Depends(get_current_user_id)):
    """Begin MFA enrollment: mint a TOTP secret + single-use backup codes.

    The secret and the HASHEd backup codes are persisted immediately so
    /mfa/enable can verify the caller actually holds the authenticator app
    (proving possession before the flag flips). The plaintext codes are
    returned exactly once, here."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT email, mfa_enabled FROM users WHERE id = $1", user_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        if row["mfa_enabled"]:
            raise HTTPException(
                status_code=400,
                detail="Two-factor authentication is already enabled. Disable it first to reconfigure.",
            )
        secret = generate_totp_secret()
        backup_codes = generate_backup_codes()
        await conn.execute(
            "UPDATE users SET mfa_secret = $1, mfa_backup_codes = $2 WHERE id = $3",
            secret,
            json.dumps(hash_backup_codes(backup_codes)),
            user_id,
        )
    return MfaSetupResponse(
        secret=secret,
        otpauth_uri=otpauth_uri(secret, row["email"]),
        backup_codes=backup_codes,
    )


@router.post("/mfa/enable")
async def mfa_enable(data: MfaCodeRequest, user_id: UUID = Depends(get_current_user_id)):
    """Flip MFA on, but only after the caller proves they hold the authenticator
    app by presenting a valid TOTP code for the pending secret."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT mfa_enabled, mfa_secret FROM users WHERE id = $1", user_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        if row["mfa_enabled"]:
            raise HTTPException(
                status_code=400,
                detail="Two-factor authentication is already enabled",
            )
        secret = row["mfa_secret"]
        if not secret or not verify_totp(secret, data.code):
            raise HTTPException(
                status_code=400,
                detail="Invalid code — check the time on your device and try again",
            )
        await conn.execute("UPDATE users SET mfa_enabled = TRUE WHERE id = $1", user_id)

    # Rotate sessions: every token minted before MFA was enabled was issued
    # without the second factor and must now be rejected.
    await _revoke_sessions(user_id)
    logger.info("User %s enabled two-factor authentication", user_id)
    record_event("mfa_enabled", detail=str(user_id))
    return {"detail": "Two-factor authentication enabled. Sign in again with your code."}


@router.post("/mfa/disable")
async def mfa_disable(data: MfaCodeRequest, user_id: UUID = Depends(get_current_user_id)):
    """Turn MFA off. Requires a CURRENT code (TOTP or backup) so a stolen
    session alone can't strip the second factor."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT mfa_enabled, mfa_secret, mfa_backup_codes FROM users WHERE id = $1",
            user_id,
        )
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        if not row["mfa_enabled"]:
            raise HTTPException(
                status_code=400,
                detail="Two-factor authentication is not enabled",
            )
        if not _mfa_code_valid(row, data.code):
            raise HTTPException(status_code=400, detail="Invalid code")
        await conn.execute(
            "UPDATE users SET mfa_enabled = FALSE, mfa_secret = NULL, mfa_backup_codes = NULL "
            "WHERE id = $1",
            user_id,
        )

    await _revoke_sessions(user_id)
    logger.info("User %s disabled two-factor authentication", user_id)
    record_event("mfa_disabled", detail=str(user_id))
    return {"detail": "Two-factor authentication disabled. Sign in again."}


@router.post("/mfa/confirm")
async def mfa_confirm(data: MfaConfirmRequest, request: Request):
    """Complete a pending MFA login: verify the code and mint the real session
    token. TOTP codes and backup codes both work; a used backup code is
    consumed atomically (a replayed code fails, and two concurrent users of the
    same code can't both succeed)."""
    ip = get_client_ip(request)
    await _check_mfa_rate_limit(ip)

    user_id_str = decode_mfa_token(data.mfa_token)
    if not user_id_str:
        raise HTTPException(
            status_code=401,
            detail="MFA session expired — please log in again",
        )

    user_uuid = UUID(user_id_str)
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT mfa_enabled, mfa_secret, mfa_backup_codes FROM users WHERE id = $1",
            user_uuid,
        )
        if not row or not row["mfa_enabled"]:
            await _record_mfa_failure(ip)
            record_event("mfa_failure", detail="no MFA enabled", ip=ip)
            raise HTTPException(status_code=401, detail="Invalid authentication code")

        valid = False
        secret = row["mfa_secret"]
        if secret and verify_totp(secret, data.code):
            valid = True
        else:
            # Backup code — consume it atomically: the UPDATE only matches when
            # the stored list is unchanged, so a concurrent use of the same code
            # loses the race and fails (single-use invariant).
            stored_raw = row.get("mfa_backup_codes")
            try:
                stored = json.loads(stored_raw) if stored_raw else []
            except (ValueError, TypeError):
                stored = []
            target = hashlib.sha256(data.code.encode("utf-8")).hexdigest()
            if target in stored:
                remaining = [c for c in stored if c != target]
                res = await conn.execute(
                    "UPDATE users SET mfa_backup_codes = $2 "
                    "WHERE id = $1 AND mfa_backup_codes = $3",
                    user_uuid,
                    json.dumps(remaining),
                    stored_raw,
                )
                valid = res == "UPDATE 1"

        if not valid:
            await _record_mfa_failure(ip)
            record_event("mfa_failure", detail="bad code", ip=ip)
            raise HTTPException(status_code=401, detail="Invalid authentication code")

    # Successful confirm — reset this IP's failed-attempt counter (mirrors the
    # login success path) so a user who mistyped a few codes then succeeded
    # isn't carrying a poisoned counter into a spurious 429.
    try:
        redis = await get_redis()
        if redis is not None:
            await redis.delete(f"mfa_attempts:{ip}")
        else:
            _mfa_attempts.pop(ip, None)
    except Exception:
        pass

    logger.info("MFA confirmed for user %s", user_id_str)
    token = create_access_token({"sub": user_id_str})
    return {"access_token": token, "token_type": "bearer"}
