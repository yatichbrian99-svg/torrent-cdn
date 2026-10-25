"""Execution receipts and client acknowledgements.

What a receipt proves (and nothing more): *this worker key claims this job ran
on this model, with these token counts, producing an output with this hash*.
It does not prove the output is correct — that is what redundant-execution
sampling is for, and the docs say so plainly.

Billing reconciles the worker's signed claim with the client's acknowledgement:

* hashes agree and token counts are within tolerance → settle the worker's
  counts (the signed side is authoritative, not the client's);
* hashes disagree, or counts differ beyond tolerance → ``disputed``: bill only
  ``min(worker, client)`` tokens, settle nothing automatically, and hand the job
  to human review. Never bill the larger of the two.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from . import crypto
from .errors import ReceiptError
from .models import AckPayload, ReceiptPayload

RECEIPT_DOMAIN = "tcdn-ai/receipt/v1"


@dataclass(frozen=True)
class JobContext:
    job_id: str
    account_id: str
    node_id: str
    model: str
    manifest_hash: str
    max_tokens: int


@dataclass(frozen=True)
class Reconciliation:
    status: str  # "agreed" | "disputed" | "awaiting_ack"
    input_tokens: int
    output_tokens: int
    reason: str | None = None

    @property
    def billable(self) -> bool:
        return self.status == "agreed" and (self.input_tokens + self.output_tokens) > 0


def sign_receipt(payload: dict[str, Any], private_key: str) -> dict[str, Any]:
    validated = ReceiptPayload(**payload).model_dump(mode="json")
    return {
        "key_id": crypto.key_id_for(crypto.ed25519_public_from_private(private_key)),
        "alg": "Ed25519",
        "domain": RECEIPT_DOMAIN,
        "payload": validated,
        "sig": crypto.sign(RECEIPT_DOMAIN, validated, private_key),
    }


def verify_receipt(
    document: dict[str, Any],
    *,
    worker_public_key: str,
    job: JobContext,
) -> ReceiptPayload:
    if not isinstance(document, dict):
        raise ReceiptError("malformed receipt")
    if document.get("domain") != RECEIPT_DOMAIN:
        raise ReceiptError("receipt has the wrong signature domain", code="bad_domain")
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise ReceiptError("malformed receipt payload")
    if not crypto.verify(
        RECEIPT_DOMAIN, payload, str(document.get("sig", "")), worker_public_key
    ):
        raise ReceiptError(
            "receipt signature verification failed", code="bad_signature"
        )
    try:
        receipt = ReceiptPayload(**payload)
    except PydanticValidationError as exc:
        raise ReceiptError(
            "receipt fields are invalid", code="invalid_receipt"
        ) from exc

    if receipt.job_id != job.job_id:
        raise ReceiptError(
            "receipt is for a different job", code="receipt_job_mismatch"
        )
    if receipt.node_id != job.node_id:
        raise ReceiptError(
            "receipt is signed by a node that was not assigned this job",
            code="receipt_node_mismatch",
        )
    if receipt.account_id != job.account_id:
        raise ReceiptError(
            "receipt account does not match the job", code="receipt_account_mismatch"
        )
    if receipt.model != job.model:
        raise ReceiptError(
            "receipt model does not match the job", code="receipt_model_mismatch"
        )
    if receipt.manifest_hash != job.manifest_hash:
        raise ReceiptError(
            "receipt model manifest does not match the pinned manifest",
            code="receipt_manifest_mismatch",
        )
    if receipt.output_tokens > job.max_tokens:
        raise ReceiptError(
            "receipt claims more output tokens than the ticket allowed",
            code="receipt_token_overflow",
        )
    if receipt.finished_at < receipt.started_at:
        raise ReceiptError("receipt timestamps are inverted", code="receipt_bad_timing")
    return receipt


def reconcile(
    receipt: ReceiptPayload | None,
    ack: AckPayload | None,
    *,
    tolerance_pct: int = 2,
) -> Reconciliation:
    if receipt is None:
        return Reconciliation("awaiting_ack", 0, 0, reason="no_worker_receipt")
    if ack is None:
        return Reconciliation("awaiting_ack", 0, 0, reason="awaiting_client_ack")

    allowed_delta = max(1, receipt.output_tokens * tolerance_pct // 100)
    output_delta = abs(receipt.output_tokens - ack.tokens_received)
    hashes_match = crypto.constant_time_eq(receipt.output_sha256, ack.output_sha256)

    if not hashes_match:
        return Reconciliation(
            "disputed",
            input_tokens=receipt.input_tokens,
            output_tokens=min(receipt.output_tokens, ack.tokens_received),
            reason="output_hash_mismatch",
        )
    if output_delta > allowed_delta:
        return Reconciliation(
            "disputed",
            input_tokens=receipt.input_tokens,
            output_tokens=min(receipt.output_tokens, ack.tokens_received),
            reason="token_count_mismatch",
        )
    return Reconciliation(
        "agreed",
        input_tokens=receipt.input_tokens,
        output_tokens=receipt.output_tokens,
    )
