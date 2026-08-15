"""
Two-factor authentication (TOTP, RFC 6238) — stdlib only.

Deliberately no third-party dependency (pyotp): HMAC-SHA1 TOTP is a few dozen
lines of stdlib (hmac/base64/struct/secrets), and this project's posture is
dependency-minimal (see requirements.txt). Standard 30-second window, 6 digits,
SHA-1 — interoperable with Google Authenticator, Authy, 1Password, etc.

Backup codes are single-use recovery credentials (10 × 10 chars, unambiguous
alphabet). Only their SHA-256 hashes are stored, so a database leak never
exposes usable codes — the same hashing stance as API keys.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt
from jwt.exceptions import InvalidTokenError

from .config import get_settings

# 10-char codes, alphabet without 0/O/1/I (no ambiguous characters when typed
# by hand or read aloud).
BACKUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
BACKUP_CODE_LENGTH = 10
BACKUP_CODE_COUNT = 10


def generate_totp_secret() -> str:
    """20 random bytes, base32-encoded (the standard authenticator-app format)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def totp_code(secret: str, at_epoch: Optional[float] = None) -> str:
    """RFC 6238 TOTP code (6 digits) for `secret` at `at_epoch` (default: now)."""
    key = base64.b32decode(secret, casefold=True)
    counter = struct.pack(
        ">Q",
        int(at_epoch if at_epoch is not None else time.time()) // 30,
    )
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % 1_000_000:06d}"


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    """Verify a 6-digit code with ±`window` steps (30s each) of clock drift."""
    if not code or len(code) != 6 or not code.isdigit():
        return False
    now = int(time.time())
    for step in range(-window, window + 1):
        if hmac.compare_digest(totp_code(secret, now + step * 30), code):
            return True
    return False


def generate_backup_codes(count: int = BACKUP_CODE_COUNT) -> list[str]:
    """Generate `count` single-use backup codes (secrets.choice — CSPRNG)."""
    return [
        "".join(secrets.choice(BACKUP_CODE_ALPHABET) for _ in range(BACKUP_CODE_LENGTH))
        for _ in range(count)
    ]


def hash_backup_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def hash_backup_codes(codes: list[str]) -> list[str]:
    return [hash_backup_code(c) for c in codes]


def backup_code_matches(stored_hashes: list[str], code: str) -> bool:
    """Constant-time-ish membership check of `code` against stored SHA-256 hashes."""
    if not code:
        return False
    target = hash_backup_code(code)
    return any(hmac.compare_digest(target, h) for h in stored_hashes)


def otpauth_uri(secret: str, email: str) -> str:
    """otpauth:// URI for authenticator-app QR scanning / manual entry.

    The label renders as ``TorrentCDN:account@example.com`` (colon and @ are
    URI sub-delims — kept literal so authenticator apps parse the issuer
    prefix; everything else is percent-encoded)."""
    label = urllib.parse.quote(f"TorrentCDN:{email}", safe=":@")
    return (
        f"otpauth://totp/{label}?secret={secret}"
        "&issuer=TorrentCDN&algorithm=SHA1&digits=6&period=30"
    )


def create_mfa_token(user_id: str) -> str:
    """Short-lived JWT proving the password half of a login succeeded.

    Carries an explicit ``mfa_pending`` claim so a regular session token can
    never be mistaken for (or replayed as) an MFA step. Lifetime is
    ``mfa_expire_minutes`` (default 5 min) — the user must present their code
    before it expires, otherwise they restart with the password.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "mfa_pending": True,
        "iat": now,
        "exp": now + timedelta(minutes=settings.mfa_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_mfa_token(token: str) -> Optional[str]:
    """Return the user_id if `token` is a valid, unexpired MFA-pending JWT."""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except InvalidTokenError:
        return None
    if not payload.get("mfa_pending") or "sub" not in payload:
        return None
    return payload["sub"]
