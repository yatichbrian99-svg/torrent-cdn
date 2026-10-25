"""Result verification — deterministic sampling entry point.

The coordinator-side sampler here is the *job selection* layer only, and it is
the old exact-hash scheme: its seed is ``sha256(salt + job_id)``, which the
worker can compute. The Part 3 commitment protocol, tolerance re-scoring and
verdict shapes live in :mod:`app.ai.verification2`; the coordinator still calls
this sampler until milestone 2 replaces it.

This module is retained so existing coordinator paths and audit tooling that
already import ``VerificationSampler`` / ``VerificationOutcome`` keep working.
New code should import from :mod:`app.ai.verification2` for the transcript
commitment / challenge / tolerance verifier path.

Honest limitation, stated in the docs as well: verification gives
**probabilistic** assurance through spot-checking. Receipts prove which code ran
and what it claimed; they cannot prove an output is correct.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import crypto

#: Tolerance is exact-hash for deterministic replays. A fuzzy comparison would
#: let a wrong worker hide inside the tolerance, so it is deliberately absent.
DEFAULT_SALT = "tcdn-ai/verification/v1"


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
    def __init__(self, sample_pct: int, *, salt: str = DEFAULT_SALT) -> None:
        if not 0 <= sample_pct <= 100:
            raise ValueError("sample_pct must be between 0 and 100")
        self.sample_pct = sample_pct
        self.salt = salt

    def decide(self, job_id: str) -> VerificationDecision:
        digest = crypto.sha256_hex(f"{self.salt}:{job_id}".encode())
        bucket = int(digest[:8], 16) % 10_000
        sampled = bucket < self.sample_pct * 100
        return VerificationDecision(
            sampled=sampled,
            bucket=bucket,
            reason="deterministic_sample" if sampled else "outside_sample",
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
