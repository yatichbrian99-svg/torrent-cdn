"""The production trigger for the signup credit.

``Coordinator.grant_signup_credits`` implements four gates — verified email, one
grant per account, a per-IP cap and a monthly platform budget — and each was
covered by tests. Nothing called it. The AI surface reuses the CDN's accounts,
so "signup" for a developer is the CDN's own registration and its
email-verification gate; without this seam every account started at zero and the
free credit the docs promised never reached a real signup.

Rules kept here rather than in the route handlers:

* the AI package is imported lazily, so a deployment with the feature flag off
  pays nothing for this hook (and cannot fail at import time because of it);
* nothing in here may raise into registration or verification — a broken AI
  coordinator must never cost a user their account, so every failure is logged
  and swallowed. The grant is idempotent per account anyway: a missed call is
  retried on the next verification path, a duplicate call credits nothing.
"""

from __future__ import annotations

import hashlib
import logging
import os

logger = logging.getLogger(__name__)


def _flag_on() -> bool:
    return os.environ.get("ENABLE_TCDN_AI", "").strip().lower() in {"1", "true", "yes"}


def ip_hash(ip: str | None) -> str:
    """Stable, non-reversible network key for the per-IP grant cap.

    Domain-separated like the other hashes in the package so this value can
    never collide with a password, key or token hash. An unknown address is
    hashed as ``unknown``: it shares one bucket, which is the conservative
    direction (the cap fires sooner, never later).
    """
    raw = (ip or "unknown").strip().lower()
    return hashlib.sha256(f"tcdn-ai/signup-ip/v1:{raw}".encode()).hexdigest()


async def grant_signup_credit(user_id: object, ip: str | None) -> int:
    """Issue the free credit for a now-verified account. Returns micro-credits.

    Returns 0 when the flag is off, the grant is a duplicate, a gate refuses it
    (per-IP cap, monthly budget) or anything fails. Callers use the return value
    for logging only — never for control flow.
    """
    if not _flag_on():
        return 0
    try:
        from .errors import AIError
        from .routes import get_coordinator

        coordinator = await get_coordinator()
        granted = await coordinator.grant_signup_credits(str(user_id), ip_hash(ip))
    except AIError as exc:
        # Expected refusals (cap reached, budget exhausted) are operational
        # facts, not bugs: the account keeps working, just without free credit.
        logger.info(
            "signup credit not granted for %s: %s", user_id, getattr(exc, "code", "")
        )
        return 0
    except Exception:
        logger.warning("signup credit grant failed for %s", user_id, exc_info=True)
        return 0
    if granted:
        logger.info("signup credit granted for %s: %s micro", user_id, granted)
    return granted


__all__ = ["grant_signup_credit", "ip_hash"]
