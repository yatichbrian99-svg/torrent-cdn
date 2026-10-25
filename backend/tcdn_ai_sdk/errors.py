"""Typed SDK exceptions.

The coordinator's stable error codes map onto these classes so callers can catch
a specific failure rather than string-matching messages.
"""

from __future__ import annotations


class TCDNAIError(Exception):
    def __init__(self, message: str, *, code: str = "error", status_code: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class AuthenticationError(TCDNAIError):
    pass


class InvalidRequestError(TCDNAIError):
    pass


class InsufficientCreditsError(TCDNAIError):
    pass


class RateLimitError(TCDNAIError):
    pass


class TicketVerificationError(TCDNAIError):
    pass


class AttestationRequiredError(TCDNAIError):
    """A confidential job was requested without a verifiable attestation."""


class NoEligibleWorkerError(TCDNAIError):
    pass


class WorkerUnavailableError(TCDNAIError):
    pass


class PartialResultError(TCDNAIError):
    """The worker failed after output was consumed; the job is partially billed."""

    def __init__(self, message: str, *, partial_text: str = "", job_id: str = "") -> None:
        super().__init__(message, code="partial_result")
        self.partial_text = partial_text
        self.job_id = job_id
