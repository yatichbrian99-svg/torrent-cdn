import logging
import os
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from jose import JWTError, jwt
from .config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _get_resend() -> Optional[any]:
    """Lazy-import and configure Resend client. Returns None if no API key."""
    api_key = os.getenv("RESEND_API_KEY", settings.resend_api_key or "")
    if not api_key or api_key == "re_xxx" or api_key.startswith("sk_test"):
        logger.warning("RESEND_API_KEY not configured — email verification disabled")
        return None
    try:
        import resend
        resend.api_key = api_key
        return resend
    except ImportError:
        logger.warning("resend package not installed — email verification disabled")
        return None


def create_verification_token(user_id: UUID) -> str:
    """Create a JWT token for email verification that expires in 24 hours."""
    expire = datetime.utcnow() + timedelta(hours=24)
    return jwt.encode(
        {"sub": str(user_id), "purpose": "email_verify", "exp": expire},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_verification_token(token: str) -> Optional[str]:
    """Decode a verification token and return the user ID if valid."""
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        if payload.get("purpose") != "email_verify":
            return None
        return payload.get("sub")
    except JWTError:
        return None


def build_verification_url(token: str) -> str:
    """Build the full verification URL the user clicks in their email."""
    base = settings.app_url.rstrip("/")
    return f"{base}/verify-email?token={token}"


def build_verification_email_html(email: str, verify_url: str) -> str:
    """Build a styled HTML email for email verification."""
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background-color:#000;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#000">
<tr><td align="center" style="padding:40px 20px">
<table role="presentation" width="480" cellpadding="0" cellspacing="0" style="background-color:#111;border:1px solid #222;border-radius:0">
<tr><td style="padding:40px 40px 20px">
<h1 style="margin:0 0 8px;font-size:18px;font-weight:700;color:#fff;letter-spacing:-0.02em">Verify your email</h1>
<p style="margin:0 0 24px;font-size:14px;line-height:1.6;color:#888">Click the button below to verify your email address for <strong style="color:#fff">{email}</strong> on TorrentCDN.</p>
<table role="presentation" cellpadding="0" cellspacing="0">
<tr><td align="center" style="background-color:#fff;border-radius:0">
<a href="{verify_url}" target="_blank" style="display:inline-block;padding:12px 32px;font-size:14px;font-weight:600;color:#000;text-decoration:none">Verify email &rarr;</a>
</td></tr></table>
<p style="margin:24px 0 0;font-size:12px;line-height:1.5;color:#555">This link expires in 24 hours. If you didn't create this account, you can safely ignore this email.</p>
</td></tr>
<tr><td style="padding:0 40px 20px;border-top:1px solid #222">
<p style="margin:20px 0 0;font-size:11px;color:#444">TorrentCDN &mdash; P2P video delivery</p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


async def send_verification_email(email: str, user_id: UUID) -> bool:
    """Generate a verification token and send the email via Resend. Returns True if sent."""
    resend = _get_resend()
    if resend is None:
        # No Resend configured — auto-verify the user so they aren't stuck
        logger.info("Resend not configured — auto-verifying user %s", user_id)
        from .database import get_db_pool
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET verified = TRUE WHERE id = $1", user_id
            )
        return False

    token = create_verification_token(user_id)
    verify_url = build_verification_url(token)
    html = build_verification_email_html(email, verify_url)

    # Default to Resend's pre-verified test sender until the user configures their domain
    from_email = os.getenv(
        "RESEND_FROM",
        "TorrentCDN <onboarding@resend.dev>",
    )
    try:
        params = {
            "from": from_email,
            "to": [email],
            "subject": "Verify your email address",
            "html": html,
        }
        resend.Emails.send(params)
        logger.info("Verification email sent to %s", email)
        return True
    except Exception as e:
        logger.error("Failed to send verification email to %s: %s", email, e)
        # Auto-verify on failure so the user isn't stuck
        from .database import get_db_pool
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET verified = TRUE WHERE id = $1", user_id
            )
        return False
