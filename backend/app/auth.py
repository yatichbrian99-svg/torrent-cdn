import hashlib
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from jwt.exceptions import InvalidTokenError
import bcrypt
from .config import get_settings
from .database import get_db_pool, get_redis

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
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except InvalidTokenError:
        return None


async def _token_revoked_by_password_change(payload: dict) -> bool:
    """Return True when the token was issued BEFORE a password/privilege change.

    Every login/register mints a token with an ``iat`` claim. When a password
    is reset (or an admin's privileges change), the route records the change
    time in Redis under ``pw_change:{user_id}``. A token whose ``iat`` predates
    that marker is stale and must be rejected — this is what makes "reset my
    password" actually kick every existing session off.

    Fail-open by design (matches the app-wide convention for the optional
    Redis dependency): if Redis is unavailable or the token has no ``iat``
    (legacy pre-this-feature token), the check passes rather than locking out
    users during a Redis outage.
    """
    iat = payload.get("iat")
    if not iat:
        return False
    try:
        redis = await get_redis()
        if redis is None:
            return False
        changed = await redis.get(f"pw_change:{payload['sub']}")
        if changed:
            return float(changed) > float(iat)
    except Exception:
        # Redis error — fail open (availability over strictness for the marker).
        return False
    return False


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UUID:
    """Dependency that extracts and validates the user ID from a Bearer credential.

    Accepts either a JWT (validated signature/exp, with password-change
    revocation via the pw_change marker) or a tcdn_ API key (validated against
    the stored hash). API keys intentionally survive password changes — they
    are long-lived credentials and are revoked explicitly from the dashboard."""
    token = credentials.credentials
    if token.startswith(API_KEY_PREFIX):
        user_id = await _resolve_api_key_user(token)
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
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
    return UUID(payload["sub"])


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
