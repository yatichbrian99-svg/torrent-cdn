"""Result verification — deterministic sampling entry point.

The coordinator-side sampler here is the *job selection* layer. Its bucket is
``HMAC-SHA-256(coordinator_secret, domain || job_id)``, not ``sha256(salt ||
job_id)``: the job id alone is not enough to recompute it, and the operator
always knows the job id (it has to run the job). A fixed public salt let a node
predict which jobs would be checked and behave honestly only for those.

The Part 3 commitment protocol, tolerance re-scoring and verdict shapes live in
:mod:`app.ai.verification2`; placement still calls this sampler (checklist 6.1),
whose remaining limits — the decision is taken at placement rather than after a
commitment, and the sample only covers gateway-mode jobs — are stated in
``LIMITATIONS.md`` where a buyer reads them.

This module is retained so existing coordinator paths and audit tooling that
already import ``VerificationSampler`` / ``VerificationOutcome`` keep working.
New code should import from :mod:`app.ai.verification2` for the transcript
commitment / challenge / tolerance verifier path.

Honest limitation, stated in the docs as well: verification gives
**probabilistic** assurance through spot-checking. Receipts prove which code ran
and what it claimed; they cannot prove an output is correct.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

#: Domain separation: the same secret may also key other coordinator functions,
#: so the sample MAC is bound to its own label (and to nothing else).
SAMPLE_DOMAIN = b"tcdn-ai/verification/sample/v1"
#: Bucket resolution. ``sample_pct`` is expressed in percent, so each percent is
#: 100 buckets; 3 % is 300 of 10,000.
BUCKETS = 10_000


@dataclass(frozen=True)
class VerificationDecision:
    sampled: bool
    bucket: int
    reason: str


@dataclass(frozen=True)
class VerificationOutcome:
    status: str  # agreed | majority | inconclusive | not_sampled
    faulty_nodes: tuple[str, ...]
    detail: str


class VerificationSampler:
    """Keyed sample selection.

    ``secret`` is coordinator-side and never sent to a worker, so two
    coordinators with different secrets sample different jobs and a node cannot
    derive the decision from the job id it was handed.
    """

    def __init__(self, sample_pct: int, *, secret: bytes) -> None:
        if not 0 <= sample_pct <= 100:
            raise ValueError("sample_pct must be between 0 and 100")
        if not secret:
            raise ValueError("sampling secret must be non-empty")
        self.sample_pct = sample_pct
        self._secret = bytes(secret)

    def decide(self, job_id: str) -> VerificationDecision:
        mac = hmac.new(
            self._secret,
            SAMPLE_DOMAIN + b":" + job_id.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        bucket = int.from_bytes(mac[:8], "big") % BUCKETS
        sampled = bucket < self.sample_pct * 100
        return VerificationDecision(
            sampled=sampled,
            bucket=bucket,
            reason="keyed_sample" if sampled else "outside_sample",
        )


def resolve_executions(*, job_id: str, outputs: dict[str, str]) -> VerificationOutcome:
    """Compare output hashes from independent workers.

    ``outputs`` maps worker node id → output SHA-256.
    """
    if len(outputs) < 2:
        return VerificationOutcome("not_sampled", (), "fewer than two executions")
    hashes = list(outputs.values())
    if len(set(hashes)) == 1:
        return VerificationOutcome("agreed", (), "all executions agree")
    if len(outputs) >= 3:
        counts: dict[str, int] = {}
        for digest in hashes:
            counts[digest] = counts.get(digest, 0) + 1
        best_hash, best_count = max(counts.items(), key=lambda kv: kv[1])
        if best_count >= 2:
            faulty = tuple(
                node for node, digest in outputs.items() if digest != best_hash
            )
            return VerificationOutcome(
                "majority",
                faulty,
                f"majority hash {best_hash[:12]}… agreed by {best_count} workers",
            )
    return VerificationOutcome(
        "inconclusive", (), "no majority among independent executions"
    )
