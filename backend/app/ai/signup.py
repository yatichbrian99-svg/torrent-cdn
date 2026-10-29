"""The production trigger for the signup credit.

``Coordinator.grant_signup_credits`` implements the gates — verified email, one
grant per verified identity, velocity limits per network/device/domain, a daily
cap, a monthly platform budget and disposable-inbox blocking — and each has a
test. This module is the only seam between the CDN's account lifecycle and that
policy, because the AI surface reuses the CDN's accounts: "signup" for a
developer is the CDN's own registration and its email-verification gate.

Rules kept here rather than in the route handlers:

* verification **proof** travels with the call. ``proof="link"`` means a signed
  token from the inbox was presented. ``proof="auto"`` means the CDN marked the
  account verified because no email provider was configured — proof of nothing —
  and the coordinator refuses it in production, so a production deployment
  without an email provider cannot hand out free credit to unproven addresses.
* the email is read from the CDN's own ``users`` row by the coordinator, never
  supplied by the caller, so a caller cannot claim someone else's verified
  inbox. Only hashes reach the grant table and the audit log.
* the AI package is imported lazily, so a deployment with the feature flag off
  pays nothing for this hook (and cannot fail at import time because of it);
* nothing in here may raise into registration or verification — a broken AI
  coordinator must never cost a user their account, so every failure is logged
  and swallowed. The grant is idempotent per account anyway: a missed call is
  retried on the next verification path, a duplicate call credits nothing.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from .signup_policy import device_hash, ip_hash

logger = logging.getLogger(__name__)

#: ``link``: a signed token from the verified inbox was presented.
#: ``auto``: the CDN verified the account because no email provider exists.
VerificationProof = Literal["link", "auto"]


def _flag_on() -> bool:
    return os.environ.get("ENABLE_TCDN_AI", "").strip().lower() in {"1", "true", "yes"}


async def grant_signup_credit(
    user_id: object,
    ip: str | None,
    *,
    proof: VerificationProof = "link",
    request: object | None = None,
) -> int:
    """Issue the free credit for a now-verified account. Returns micro-credits.

    Returns 0 when the flag is off, the grant is a duplicate, a gate refuses it
    (identity/velocity/daily/monthly/disposable) or anything fails. Callers use
    the return value for logging only — never for control flow.
    """
    if not _flag_on():
        return 0
    try:
        from .errors import AIError
        from .routes import get_coordinator

        coordinator = await get_coordinator()
        granted = await coordinator.grant_signup_credits(
            str(user_id),
            ip_hash(ip),
            device_hash=device_hash(request),
            proof=proof,
        )
    except AIError as exc:
        # Expected refusals (cap reached, budget exhausted, unproven address)
        # are operational facts, not bugs: the account keeps working, just
        # without free credit. The coordinator has already audited the refusal.
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


__all__ = ["VerificationProof", "grant_signup_credit", "ip_hash"]
