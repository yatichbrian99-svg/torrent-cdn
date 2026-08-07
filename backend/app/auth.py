import hashlib
import logging
import time
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from jwt.exceptions import InvalidTokenError
import bcrypt
from .config import get_settings
from .database import get_db_pool, get_redis

logger = logging.getLogger(__name__)

settings = get_settings()
security = HTTPBearer()

# API keys are long-lived credentials in the form `tcdn_<random>` (see
# routes/auth.py — generated via secrets.token_urlsafe, so ~256 bits of
# entropy). Only the SHA-256 hash is stored in api_keys.key_hash, never the
# plaintext — a database leak exposes hashes that can't be reversed, and the
# dashboard shows a truncated prefix instead of the secret.
API_KEY_PREFIX = "tcdn_"


def hash_api_key(token: str) -> str:
    """SHA-256 of the full tcdn_ token — the only form stored in the DB."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _resolve_api_key_user(token: str) -> Optional[UUID]:
    """Resolve a tcdn_ API key to its owner, or None if unknown/revoked.

    Not constant-time-sensitive in a useful way: the DB equality lookup runs on
    an indexed column against a 256-bit hash the attacker can't influence
    beyond trying full tokens (each 401 costs a DB round-trip, and the global
    rate limiter still applies). Fail-closed on DB errors: a key that can't be
    verified is never accepted."""
    if not token.startswith(API_KEY_PREFIX):
        return None
    key_hash = hash_api_key(token)
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT user_id FROM api_keys WHERE key_hash = $1", key_hash
            )
            if row is None:
                return None
            # Best-effort last-used timestamp for the dashboard — never fail
            # the request on a write error.
            try:
                await conn.execute(
                    "UPDATE api_keys SET last_used_at = NOW() WHERE key_hash = $1",
                    key_hash,
                )
            except Exception:
                pass
            return UUID(str(row["user_id"]))
    except Exception:
        # Table missing (fresh DB before migrations ran) or DB error — fail
        # closed rather than accepting an unverifiable credential.
        return None


def hash_password(password: str) -> str:
    """Hash a password with bcrypt (cost factor 12, the current OWASP default).

    Uses the bcrypt library directly instead of passlib: passlib 1.7.4 (the
    last release, from 2020) is unmaintained and its bcrypt backend breaks with
    modern bcrypt releases (it traps errors reading the version from bcrypt's
    __about__ module). Stored hashes use the standard $2b$ format, so existing
    rows remain verifiable and the scheme is portable.
    """
    # bcrypt only uses the first 72 bytes of input; longer passwords are
    # truncated silently, which can make two different long passwords compare
    # equal. Reject the extreme tail explicitly rather than relying on the
    # caller's length validation alone (defense in depth).
    if len(password.encode("utf-8")) > 72:
        raise ValueError("Password exceeds the 72-byte bcrypt limit")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"), hashed_password.encode("utf-8")
        )
    except ValueError:
        # Malformed stored hash — treat as mismatch, never crash the login path.
        return False


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    now = datetime.utcnow()
    expire = now + timedelta(minutes=settings.jwt_expire_minutes)
    # iat anchors this token's issue time so the session-revocation check in
    # get_current_user_id can reject tokens issued BEFORE a password change or
    # privilege change (each change bumps a pw_change marker in Redis).
    to_encode.update({"iat": now, "exp": expire})
    # PyJWT 2.x returns a str token (bearer-compatible).
    return jwt.encode(
        to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm
    )


def decode_access_token(token: str) -> Optional[dict]:
    try:
        # PyJWT verifies exp + signature; the explicit algorithms list is
        # required by PyJWT and prevents algorithm-confusion attacks.
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except InvalidTokenError:
        return None
    # A token minted for the MFA second-factor step (create_mfa_token) shares
    # this signing secret, so it would otherwise pass signature/exp here and
    # authenticate as a full session token WITHOUT a TOTP/backup code. The
    # mfa_pending claim must never be accepted by the session path — reject it
    # at this single point so get_current_user_id / get_optional_user_id can't
    # be tricked. (decode_mfa_token in app/mfa.py uses jwt.decode directly and
    # is unaffected.)
    if payload.get("mfa_pending"):
        return None
    return payload


async def revoke_user_sessions(user_id) -> None:
    """Rotate every pre-existing session token for a user.

    Called on password reset, MFA enable/disable, and admin privilege changes.
    Writes BOTH revocation markers so rotation works with or without Redis:

    - DB ``security_stamp`` column on users — the AUTHORITATIVE marker. It
      survives Redis restarts/absence, which is what makes "enable MFA" (or
      "reset my password") actually kick existing sessions on deployments that
      don't run Redis (like production today).
    - Redis ``pw_change:{user_id}`` — the fast-path marker used when Redis is
      available, so the per-request check stays a cheap Redis GET.

    Best-effort by design: a storage failure here must never fail the caller's
    main operation (a password reset must still succeed).
    """
    uid = str(user_id)
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET security_stamp = NOW() WHERE id = $1", uid
            )
    except Exception as e:
        logger.warning("security_stamp bump failed for %s: %s", uid, e)
    try:
        redis = await get_redis()
        if redis is not None:
            await redis.set(f"pw_change:{uid}", str(time.time()))
    except Exception as e:
        logger.warning("pw_change marker failed for %s: %s", uid, e)


async def get_user_security_stamp(user_id) -> Optional[datetime]:
    """Return the user's session-revocation stamp (None if never changed).

    A separate helper so unit tests can mock exactly this DB read (the auth
    dependency runs on every request; see conftest's apply_mocks).
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT security_stamp FROM users WHERE id = $1", UUID(str(user_id))
        )
    return row["security_stamp"] if row else None


# Endpoints an UNVERIFIED session may still reach when email verification is
# required: completing verification itself and inspecting its own identity +
# verification state (the dashboard's "verify your email" banner flow). Every
# other endpoint requires a verified email — a freshly registered account's
# token must not unlock uploads, files, billing, API keys, or admin surfaces
# until the owner proves the inbox. (POST /auth/verify-email needs no session
# at all — it authenticates with the emailed JWT.)
_UNVERIFIED_ALLOWLIST = {
    "/auth/verification-status",
    "/auth/resend-verification",
    "/auth/me",
}


async def _enforce_email_verified(request: Request, user_id: UUID) -> None:
    """403 when the session owner's email is unverified unless the endpoint is
    in the verification allowlist.

    Mirrors the login gate (``settings.email_verification_required and not
    verified``) at the session layer, so a register-issued token for an
    unverified account can no longer reach anything but the verification flow.
    No-op when the flag is off (dev/test), so existing behaviour is unchanged.
    A missing user row (deleted account) fails closed with 401."""
    _settings = get_settings()
    if not _settings.email_verification_required:
        return
    if request.url.path in _UNVERIFIED_ALLOWLIST:
        return
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT verified FROM users WHERE id = $1", str(user_id)
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not row["verified"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Please check your inbox or request a new verification link.",
        )


async def _token_revoked_by_password_change(payload: dict) -> bool:
    """Return True when the token was issued BEFORE a password/privilege change.

    Every login/register mints a token with an ``iat`` claim. Password resets,
    MFA enable/disable, and admin privilege changes bump a revocation marker
    (see revoke_user_sessions):

    - Redis ``pw_change:{user_id}`` — a fast path. When present it is used
      directly (it is written alongside the DB stamp on every change).
    - The DB ``security_stamp`` column — the AUTHORITATIVE check. Consulted
      whenever Redis is absent, errors, OR is up but has no marker (the marker
      may have been lost to a Redis restart / volatile-lru eviction while the
      DB stamp persisted). Without it, a stolen pre-MFA token would keep
      authenticating after the owner enables MFA — on deployments without
      Redis, and in the Redis-flaky window.

    A token whose ``iat`` predates the marker is stale and rejected. Fails
    open ONLY when the DB itself errors (DB down = the whole app is down
    anyway, and every handler would 500 regardless).
    """
    iat = payload.get("iat")
    if not iat:
        return False
    sub = payload.get("sub")
    # Fast path: Redis marker (written alongside the DB stamp on every change).
    try:
        redis = await get_redis()
        if redis is not None:
            changed = await redis.get(f"pw_change:{sub}")
            if changed is not None:
                return float(changed) > float(iat)
    except Exception:
        # Redis error mid-check — fall through to the authoritative DB stamp.
        pass
    # Authoritative path: DB security_stamp (works with no Redis at all, and
    # covers the Redis-up-but-markerless window after a restart/eviction).
    try:
        stamp = await get_user_security_stamp(sub)
        if stamp is not None:
            return stamp.timestamp() > float(iat)
    except Exception as e:
        logger.warning("security_stamp check failed for %s: %s", sub, e)
    return False


async def get_current_user_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UUID:
    """Dependency that extracts and validates the user ID from a Bearer credential.

    Accepts either a JWT (validated signature/exp, with password-change
    revocation via the pw_change marker) or a tcdn_ API key (validated against
    the stored hash). API keys intentionally survive password changes — they
    are long-lived credentials and are revoked explicitly from the dashboard.

    When email verification is required, unverified sessions are restricted to
    the verification allowlist (see _enforce_email_verified) — closing the
    register-issued-token hole where a brand-new account got full access
    before proving its inbox."""
    token = credentials.credentials
    if token.startswith(API_KEY_PREFIX):
        user_id = await _resolve_api_key_user(token)
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        await _enforce_email_verified(request, user_id)
        return user_id
    payload = decode_access_token(token)
    if payload is None or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if await _token_revoked_by_password_change(payload):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session invalidated. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = UUID(payload["sub"])
    await _enforce_email_verified(request, user_id)
    return user_id


async def get_optional_user_id(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        HTTPBearer(auto_error=False)
    ),
) -> Optional[UUID]:
    """Optional auth dependency — returns None if no token provided.

    API keys work here too: a valid tcdn_ key resolves to its owner, an invalid
    one resolves to None (optional — never an error)."""
    if credentials is None:
        return None
    token = credentials.credentials
    if token.startswith(API_KEY_PREFIX):
        return await _resolve_api_key_user(token)
    payload = decode_access_token(token)
    if payload is None or "sub" not in payload:
        return None
    return UUID(payload["sub"])
