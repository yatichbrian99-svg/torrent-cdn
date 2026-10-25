"""Worker placement.

Hard filters first (a rejected candidate is never scored), then a weighted score
with a small random jitter so equally good workers do not become a hot spot.
The confidential filter is absolute: if no candidate has a live, verified
attestation the placement fails with ``no_attested_worker`` — there is no
downgrade path anywhere in this module.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .clock import now_unix
from .errors import NoEligibleWorker
from .models import Confidentiality, NodeType


@dataclass(frozen=True)
class WorkerCandidate:
    node_id: str
    owner_id: str
    node_type: NodeType
    models: frozenset[str]
    vram_gb: int
    region: str
    price_micro_per_mtok: int
    reputation: int = 50
    load: float = 0.0
    latency_ms_p95: int = 200
    healthy: bool = True
    suspended: bool = False
    confidentiality: Confidentiality = Confidentiality.NONE
    attestation_expires_at: int | None = None
    attestation_verdict: str | None = None

    @property
    def attested(self) -> bool:
        return (
            self.confidentiality is Confidentiality.ATTESTED_TEE
            and self.attestation_verdict == "verified"
            and self.attestation_expires_at is not None
        )


@dataclass(frozen=True)
class Requirements:
    model: str
    vram_gb: int | None = None
    region: str | None = None
    latency_target_ms: int | None = None
    max_price_micro: int | None = None
    confidential: bool = False
    #: Development only: a *simulated* attestation verdict may satisfy the
    #: confidential gate. Production sets this False (config.py enforces that
    #: simulated attestation itself is off there), so a simulated verdict can
    #: never route a real confidential job.
    allow_simulated_attestation: bool = False


@dataclass
class Selection:
    worker: WorkerCandidate
    score: float
    considered: int
    rejected: dict[str, int] = field(default_factory=dict)


def _reject_reason(node: WorkerCandidate, req: Requirements, now: int) -> str | None:
    if node.suspended:
        return "suspended"
    if not node.healthy:
        return "unhealthy"
    if node.node_type is NodeType.SEEDER:
        return "not_a_compute_node"
    if req.model not in node.models:
        return "model_not_loaded"
    if req.vram_gb is not None and node.vram_gb < req.vram_gb:
        return "insufficient_vram"
    if req.region is not None and node.region != req.region:
        return "region_mismatch"
    if (
        req.latency_target_ms is not None
        and node.latency_ms_p95 > req.latency_target_ms
    ):
        return "latency_target_missed"
    if (
        req.max_price_micro is not None
        and node.price_micro_per_mtok > req.max_price_micro
    ):
        return "price_above_cap"
    if node.load >= 0.99:
        return "at_capacity"
    if req.confidential:
        if node.confidentiality is not Confidentiality.ATTESTED_TEE:
            return "not_confidential_capable"
        acceptable = {"verified"}
        if req.allow_simulated_attestation:
            acceptable.add("simulated")
        if node.attestation_verdict not in acceptable:
            return "attestation_not_verified"
        if node.attestation_expires_at is None or node.attestation_expires_at <= now:
            return "attestation_expired"
    return None


def score(node: WorkerCandidate, req: Requirements) -> float:
    """Higher is better. Reputation dominates, then price, latency, and load."""
    price_penalty = min(
        1.0, node.price_micro_per_mtok / 50_000_000
    )  # 50 credits/1M = max
    latency_penalty = min(1.0, node.latency_ms_p95 / 2000.0)
    region_bonus = 0.15 if req.region and node.region == req.region else 0.0
    return round(
        (node.reputation / 100.0) * 0.55
        + (1.0 - price_penalty) * 0.20
        + (1.0 - latency_penalty) * 0.15
        + (1.0 - node.load) * 0.10
        + region_bonus,
        6,
    )


def select_worker(
    candidates: list[WorkerCandidate],
    req: Requirements,
    *,
    now: int | None = None,
    rng: random.Random | None = None,
) -> Selection:
    now_ts = int(now if now is not None else now_unix())
    rng = rng or random.SystemRandom()

    eligible: list[WorkerCandidate] = []
    rejected: dict[str, int] = {}
    for node in candidates:
        reason = _reject_reason(node, req, now_ts)
        if reason is None:
            eligible.append(node)
        else:
            rejected[reason] = rejected.get(reason, 0) + 1

    if not eligible:
        code = "no_attested_worker" if req.confidential else "no_eligible_worker"
        raise NoEligibleWorker(
            "no worker satisfies this request",
            code=code,
            details={"rejected": rejected},
        )

    scored = [(score(node, req) + rng.random() * 0.01, node) for node in eligible]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = scored[0]
    return Selection(
        worker=best,
        score=best_score,
        considered=len(candidates),
        rejected=rejected,
    )
