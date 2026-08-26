import asyncio
import hashlib
import html
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from jwt.exceptions import InvalidTokenError

from .config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def _get_resend() -> Any | None:
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


def is_email_service_configured() -> bool:
    """True when a usable Resend API key is present, regardless of whether a
    specific send succeeds.

    Auto-verify fallbacks must key off THIS, not off a single send result: a
    configured-but-flaky provider must never silently mark accounts verified
    (that would reopen the unverified-access hole for the price of an email
    outage). Only a genuinely absent provider — where verification is
    impossible — may auto-verify."""
    return _get_resend() is not None


def create_verification_token(user_id: UUID) -> str:
    """Create a JWT token for email verification that expires in 24 hours.
    Uses a separate secret from auth tokens to prevent cross-use attacks.
    """
    secret = settings.jwt_verify_secret or settings.jwt_secret
    expire = datetime.now(UTC) + timedelta(hours=24)
    # PyJWT 2.x returns a str token.
    return jwt.encode(
        {"sub": str(user_id), "purpose": "email_verify", "exp": expire},
        secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_verification_token(token: str) -> str | None:
    """Decode a verification token and return the user ID if valid."""
    secret = settings.jwt_verify_secret or settings.jwt_secret
    try:
        payload = jwt.decode(
            token, secret, algorithms=[settings.jwt_algorithm]
        )
        if payload.get("purpose") != "email_verify":
            return None
        return payload.get("sub")
    except InvalidTokenError:
        return None


def build_verification_url(token: str) -> str:
    """Build the full verification URL the user clicks in their email."""
    base = settings.app_url.rstrip("/")
    return f"{base}/verify-email?token={token}"


def create_password_reset_token(user_id: UUID) -> str:
    """Create a JWT for password reset that expires in 1 hour.
    Uses a separate secret from auth tokens to prevent cross-use attacks.
    """
    secret = settings.jwt_reset_secret or settings.jwt_secret
    expire = datetime.now(UTC) + timedelta(hours=1)
    return jwt.encode(
        {"sub": str(user_id), "purpose": "reset_password", "exp": expire},
        secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_password_reset_token(token: str) -> str | None:
    """Decode a password reset token and return the user ID if valid."""
    secret = settings.jwt_reset_secret or settings.jwt_secret
    try:
        payload = jwt.decode(
            token, secret, algorithms=[settings.jwt_algorithm]
        )
        if payload.get("purpose") != "reset_password":
            return None
        return payload.get("sub")
    except InvalidTokenError:
        return None


def build_reset_url(token: str) -> str:
    """Build the full password reset URL the user clicks in their email."""
    base = settings.app_url.rstrip("/")
    return f"{base}/auth/reset-password?token={token}"


def build_reset_email_html(email: str, reset_url: str) -> str:
    """Build a styled HTML email for password reset."""
    # The email address is attacker-influenced (it's the address on the account)
    # and lands verbatim in the HTML body — escape it so a crafted address
    # can't inject markup into the email a user receives.
    safe_email = html.escape(email, quote=True)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background-color:#000;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#000">
<tr><td align="center" style="padding:40px 20px">
<table role="presentation" width="480" cellpadding="0" cellspacing="0" style="background-color:#111;border:1px solid #222;border-radius:0">
<tr><td style="padding:40px 40px 20px">
<h1 style="margin:0 0 8px;font-size:18px;font-weight:700;color:#fff;letter-spacing:-0.02em">Reset your password</h1>
<p style="margin:0 0 24px;font-size:14px;line-height:1.6;color:#888">Click the button below to reset your password for <strong style="color:#fff">{safe_email}</strong> on TorrentCDN.</p>
<table role="presentation" cellpadding="0" cellspacing="0">
<tr><td align="center" style="background-color:#fff;border-radius:0">
<a href="{reset_url}" target="_blank" style="display:inline-block;padding:12px 32px;font-size:14px;font-weight:600;color:#000;text-decoration:none">Reset password &rarr;</a>
</td></tr></table>
<p style="margin:24px 0 0;font-size:12px;line-height:1.5;color:#555">This link expires in 1 hour. If you didn't request this, you can safely ignore this email.</p>
</td></tr>
<tr><td style="padding:0 40px 20px;border-top:1px solid #222">
<p style="margin:20px 0 0;font-size:11px;color:#444">TorrentCDN &mdash; P2P video delivery</p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def build_mfa_enabled_email_html(email: str, dashboard_url: str) -> str:
    """Build a styled HTML email notifying the account owner that 2FA was just
    enabled on their account.

    The email address is attacker-influenced (it's the address on the account)
    and lands verbatim in the HTML body — escape it so a crafted address
    can't inject markup into the email a user receives.
    """
    safe_email = html.escape(email, quote=True)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background-color:#000;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#000">
<tr><td align="center" style="padding:40px 20px">
<table role="presentation" width="480" cellpadding="0" cellspacing="0" style="background-color:#111;border:1px solid #222;border-radius:0">
<tr><td style="padding:40px 40px 20px">
<h1 style="margin:0 0 8px;font-size:18px;font-weight:700;color:#fff;letter-spacing:-0.02em">Two-factor authentication enabled</h1>
<p style="margin:0 0 24px;font-size:14px;line-height:1.6;color:#888">Two-factor authentication (authenticator app) was just enabled on your TorrentCDN account for <strong style="color:#fff">{safe_email}</strong>.</p>
<p style="margin:0 0 16px;font-size:14px;line-height:1.6;color:#888">If this was you — you're all set, no further action is needed.</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="background-color:#2a1008;border:1px solid #7a2e12">
<tr><td style="padding:14px 16px;font-size:13px;line-height:1.5;color:#fca5a5">
<strong style="color:#fff">Didn't do this?</strong> Someone else may have access to your account. Reset your password immediately, review your sessions, and contact support.
</td></tr></table>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:24px">
<tr><td align="center" style="background-color:#fff;border-radius:0">
<a href="{dashboard_url}" target="_blank" style="display:inline-block;padding:12px 32px;font-size:14px;font-weight:600;color:#000;text-decoration:none">Review your account &rarr;</a>
</td></tr></table>
<p style="margin:24px 0 0;font-size:12px;line-height:1.5;color:#555">This is an automated security notification. If you don't recognize this change, act now.</p>
</td></tr>
<tr><td style="padding:0 40px 20px;border-top:1px solid #222">
<p style="margin:20px 0 0;font-size:11px;color:#444">TorrentCDN &mdash; P2P video delivery</p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def build_verification_email_html(email: str, verify_url: str) -> str:
    """Build a styled HTML email for email verification."""
    safe_email = html.escape(email, quote=True)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background-color:#000;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#000">
<tr><td align="center" style="padding:40px 20px">
<table role="presentation" width="480" cellpadding="0" cellspacing="0" style="background-color:#111;border:1px solid #222;border-radius:0">
<tr><td style="padding:40px 40px 20px">
<h1 style="margin:0 0 8px;font-size:18px;font-weight:700;color:#fff;letter-spacing:-0.02em">Verify your email</h1>
<p style="margin:0 0 24px;font-size:14px;line-height:1.6;color:#888">Click the button below to verify your email address for <strong style="color:#fff">{safe_email}</strong> on TorrentCDN.</p>
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


async def send_password_reset_email(email: str, user_id: UUID) -> bool:
    """Generate a password reset token, store in Redis, and send the email via Resend."""
    from .database import get_redis

    # Always create and store the token, even if email sending fails
    token = create_password_reset_token(user_id)

    redis = await get_redis()
    if redis is not None:
        token_key = f"reset_token:{token}"
        await redis.set(token_key, str(user_id), ex=3600)
        logger.info("Password reset token stored in Redis for user %s", user_id)
    else:
        # H4 FIX: Without Redis, store the token hash in the DB so the one-time
        # check can fall back to the database instead of JWT-expiry-only validation.
        try:
            from .database import get_db_pool
            pool = await get_db_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET reset_token_used = $1 WHERE id = $2",
                    hashlib.sha256(token.encode()).hexdigest(),
                    str(user_id),
                )
            logger.info("Redis unavailable — reset token for user %s stored in DB fallback", user_id)
        except Exception as e:
            logger.warning("DB fallback for reset token failed for %s: %s", user_id, e)

    # Try to send the email
    resend = _get_resend()
    if resend is None:
        if settings.app_env != "production":
            # Dev convenience only — never log reset tokens in production logs
            logger.info(
                "Resend not configured — password reset for %s. Token is valid for 1 hour. "
                "Reset URL: %s",
                email, build_reset_url(token),
            )
        else:
            # Warning (not info) in production: a missing/broken email provider
            # silently strands users who can never receive reset/verification
            # links — this must be visible in logs, not lost among info noise.
            logger.warning("Resend not configured — password reset for %s. Token valid for 1 hour.", email)
        return False

    reset_url = build_reset_url(token)
    html = build_reset_email_html(email, reset_url)

    from_email = os.getenv(
        "RESEND_FROM",
        "TorrentCDN <noreply@torrentcdn.store>",
    )
    try:
        params = {
            "from": from_email,
            "to": [email],
            "subject": "Reset your TorrentCDN password",
            "html": html,
        }
        await asyncio.to_thread(resend.Emails.send, params)
        logger.info("Password reset email sent to %s", email)
        return True
    except Exception as e:
        logger.error("Failed to send password reset email to %s: %s", email, e)
        return False


async def send_mfa_enabled_email(email: str) -> bool:
    """Notify the account owner that 2FA was just enabled on their account.

    Security notification: a forced enrollment — or one performed by an
    attacker who holds the password — must never go unnoticed, so this fires
    on EVERY enable (dashboard and mid-login enrollment alike). Failures are
    non-fatal: the enable has already succeeded.
    """
    resend = _get_resend()
    if resend is None:
        if settings.app_env != "production":
            # Dev convenience — visible without a provider configured.
            logger.info("Resend not configured — MFA-enabled notification for %s skipped", email)
        else:
            # Warning (not info) in production: a missing email provider
            # silently blinds users to a security-relevant change on their own
            # account — this must be visible in logs, not lost among noise.
            logger.warning("Resend not configured — MFA-enabled notification for %s NOT sent", email)
        return False

    dashboard_url = settings.app_url.rstrip("/") + "/dashboard"
    html = build_mfa_enabled_email_html(email, dashboard_url)

    from_email = os.getenv(
        "RESEND_FROM",
        "TorrentCDN <noreply@torrentcdn.store>",
    )
    try:
        params = {
            "from": from_email,
            "to": [email],
            "subject": "Two-factor authentication enabled on your TorrentCDN account",
            "html": html,
        }
        await asyncio.to_thread(resend.Emails.send, params)
        logger.info("MFA-enabled notification email sent to %s", email)
        return True
    except Exception as e:
        logger.error("Failed to send MFA-enabled notification to %s: %s", email, e)
        return False


async def send_verification_email(email: str, user_id: UUID) -> bool:
    """Generate a verification token and send the email via Resend. Returns True if sent."""
    token = create_verification_token(user_id)
    verify_url = build_verification_url(token)

    # Log the link ONLY in non-production (dev convenience).
    # Never log verification tokens in production logs.
    if settings.app_env != "production":
        logger.info(
            "Verification link for %s (user %s): %s",
            email, user_id, verify_url,
        )

    resend = _get_resend()
    if resend is None:
        logger.info(
            "Resend not configured — verification link for %s (user %s). "
            "User will NOT be auto-verified.",
            email, user_id,
        )
        if settings.app_env != "production":
            print(f"\n\n VERIFICATION LINK: {verify_url}\n\n")
        return False

    html = build_verification_email_html(email, verify_url)

    # Default to Resend's pre-verified test sender until the user configures their domain
    from_email = os.getenv(
        "RESEND_FROM",
        "TorrentCDN <noreply@torrentcdn.store>",
    )
    try:
        params = {
            "from": from_email,
            "to": [email],
            "subject": "Verify your email address",
            "html": html,
        }
        await asyncio.to_thread(resend.Emails.send, params)
        logger.info("Verification email sent to %s", email)
        return True
    except Exception as e:
        logger.error("Failed to send verification email to %s: %s", email, e)
        if settings.app_env != "production":
            print(f"\n\n VERIFICATION LINK (email delivery failed): {verify_url}\n\n")
        return False
