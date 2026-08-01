"""Email sending via the Resend API (free tier: 100 emails/day).

Uses httpx directly instead of the Resend SDK so the backend has no extra
dependency. All failures are non-fatal — email delivery never blocks a request.
"""
import logging

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


async def send_welcome_email(email: str, name: str) -> bool:
    """Send the 'Welcome to TorrentCDN' onboarding email.

    Returns True if the email was accepted by Resend, False otherwise
    (missing API key, network error, or API rejection).
    """
    settings = get_settings()
    api_key = settings.resend_api_key
    if not api_key:
        logger.info("RESEND_API_KEY not configured — skipping welcome email for %s", email)
        return False

    html = """
    <div style='font-family:monospace;background:#000;color:#fff;padding:40px;'>
      <h1 style='font-size:20px;'>Welcome to TorrentCDN</h1>
      <p style='color:#999;'>Your account is ready. Here's how to get started:</p>
      <ol style='color:#999;line-height:2;'>
        <li>Upload your first video at torrentcdn.store/dashboard/upload</li>
        <li>Copy your embed code</li>
        <li>Paste it on your website</li>
        <li>Watch your bandwidth costs drop</li>
      </ol>
      <p style='color:#999;'>Free tier includes 100GB/month. No credit card needed.</p>
      <a href='https://torrentcdn.store/dashboard'
         style='display:inline-block;background:#fff;color:#000;padding:10px 20px;text-decoration:none;margin-top:20px;font-weight:bold;'>
        Go to Dashboard →
      </a>
    </div>
    """

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                RESEND_ENDPOINT,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "from": "TorrentCDN <hello@torrentcdn.store>",
                    "to": email,
                    "subject": "Welcome to TorrentCDN",
                    "html": html,
                },
            )
        if resp.status_code >= 400:
            logger.warning(
                "Welcome email rejected by Resend (%s): %s", resp.status_code, resp.text[:200]
            )
            return False
        logger.info("Welcome email sent to %s", email)
        return True
    except Exception as e:
        logger.warning("Failed to send welcome email to %s: %s", email, e)
        return False
