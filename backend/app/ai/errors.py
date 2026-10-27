"""Stable error codes for the TCDN-AI surface.

Every rejection has a machine-readable ``code`` that never changes once shipped,
because SDKs branch on it. Messages stay human-readable and must never echo
secrets, prompts, or key material.

The HTTP mapping lives here (not in the route handlers) so the same failure
raised deep inside the ledger or scheduler reaches the client identically.
"""

from __future__ import annotations


class AIError(Exception):
    """Base class for every TCDN-AI rejection (fail-closed by construction)."""

    status_code = 400
    code = "ai_error"

    def __init__(
        self,
        message: str = "Request rejected",
        *,
        code: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or type(self).code
        self.details = details or {}

    def as_body(self) -> dict:
        body: dict = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = self.details
        return body


class ValidationError(AIError):
    status_code = 422
    code = "invalid_request"


class AuthenticationError(AIError):
    status_code = 401
    code = "unauthenticated"


class AuthorizationError(AIError):
    status_code = 403
    code = "forbidden"


class NotFoundError(AIError):
    status_code = 404
    code = "not_found"


class ConflictError(AIError):
    status_code = 409
    code = "conflict"


class InsufficientCredits(AIError):
    status_code = 402
    code = "insufficient_credits"


class LimitExceeded(AIError):
    """Rate limit, concurrency cap, or request-size limit (429)."""

    status_code = 429
    code = "limit_exceeded"


class PayloadTooLarge(LimitExceeded):
    """The body exceeded ``max_body_bytes``.

    413, not the 429 a plain ``LimitExceeded`` produced: 429 means "retry
    later", so an OpenAI-compatible client retried an identically oversized
    body until it gave up. OpenAI returns 413 here, and so do we.
    """

    status_code = 413
    code = "body_too_large"


class UnavailableError(AIError):
    status_code = 503
    code = "unavailable"


class AttestationError(AIError):
    """Attestation failed or could not be completed — always a rejection."""

    status_code = 403
    code = "attestation_failed"


class CryptoError(AIError):
    """Signature/AEAD/key failure. Never leaks which key or why."""

    status_code = 400
    code = "crypto_failure"


class UnknownKeyError(CryptoError):
    code = "unknown_key_id"


class RevokedKeyError(CryptoError):
    code = "revoked_key_id"


class TicketError(CryptoError):
    code = "invalid_ticket"


class ReplayError(TicketError):
    code = "replayed_ticket"


class ExpiredError(TicketError):
    code = "expired_ticket"


class NoEligibleWorker(UnavailableError):
    code = "no_eligible_worker"


class ReceiptError(AIError):
    code = "invalid_receipt"


class RateLimitedError(LimitExceeded):
    """Rate-limited on placement or attestation nonce issuance."""

    code = "rate_limited"


class ConcurrencyLimitExceeded(LimitExceeded):
    """The account already has ``max_concurrent_jobs_per_key`` jobs in flight.

    Distinct from ``rate_limited`` on purpose: a client that is over its
    concurrency cap is not being throttled for sending too fast, it is holding
    too many un-settled jobs, and the correct reaction is to finish or wait for
    them (the TTL sweep releases abandoned ones) rather than to back off and
    retry — retrying only adds load. The detail names the limit and the current
    in-flight count so the client can act on it.
    """

    code = "too_many_concurrent_jobs"


class BurnDisabled(AIError):
    status_code = 403
    code = "burn_disabled"
