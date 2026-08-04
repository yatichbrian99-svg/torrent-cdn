from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
from jwt.exceptions import InvalidTokenError
import bcrypt
from .config import get_settings

settings = get_settings()
security = HTTPBearer()


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
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    to_encode.update({"exp": expire})
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


async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UUID:
    """Dependency that extracts and validates the user ID from JWT."""
    payload = decode_access_token(credentials.credentials)
    if payload is None or "sub" not in payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return UUID(payload["sub"])


async def get_optional_user_id(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        HTTPBearer(auto_error=False)
    ),
) -> Optional[UUID]:
    """Optional auth dependency — returns None if no token provided."""
    if credentials is None:
        return None
    payload = decode_access_token(credentials.credentials)
    if payload is None or "sub" not in payload:
        return None
    return UUID(payload["sub"])
