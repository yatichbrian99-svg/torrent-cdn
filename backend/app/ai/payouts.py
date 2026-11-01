"""Seeder accounting: co-signed transfer receipts, fraud controls, exact payouts.

A seeder is paid only for bytes proven by a receipt **co-signed by the
downloading peer**, where that downloader was itself ticketed by the coordinator.
Self-reported logs are worthless here: without the downloader's signature any
seeder could mint traffic by writing rows.

Layered controls, each with a named reason so a payout statement is auditable:

1. signature of the downloader over the claim (the seeder's signature proves
   the seeder agrees with the same claim);
2. downloader must be in the coordinator's ticketed set;
3. per-pair byte cap;
4. same-owner transfers are flagged (wash trading);
5. a payout delay window, enforced by the caller (``payout_delay_seconds``).

Distribution uses the largest-remainder method so the parts sum to exactly the
pool amount — never a float, never a lost micro-credit.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from . import crypto

TRANSFER_DOMAIN = "tcdn-ai/transfer-receipt/v1"


@dataclass(frozen=True)
class TransferClaim:
    downloader_id: str
    seeder_id: str
    shard_sha256: str
    bytes_served: int
    created_at: int

    def payload(self) -> dict:
        return {
            "downloader_id": self.downloader_id,
            "seeder_id": self.seeder_id,
            "shard_sha256": self.shard_sha256,
            "bytes": self.bytes_served,
            "created_at": self.created_at,
        }

    @property
    def claim_hash(self) -> str:
        return hashlib.sha256(crypto.canonical_json(self.payload())).hexdigest()


@dataclass(frozen=True)
class ReviewedClaim:
    claim: TransferClaim
    accepted_bytes: int
    flags: tuple[str, ...] = ()

    @property
    def payable(self) -> bool:
        return self.accepted_bytes > 0


def sign_claim(claim: TransferClaim, private_key: str) -> str:
    return crypto.sign(TRANSFER_DOMAIN, claim.payload(), private_key)


def verify_transfer_receipt(
    claim: TransferClaim,
    *,
    downloader_signature: str,
    seeder_signature: str,
    downloader_public_key: str,
    seeder_public_key: str,
) -> None:
    if not crypto.verify(
        TRANSFER_DOMAIN, claim.payload(), downloader_signature, downloader_public_key
    ):
        raise ValueError("downloader signature is invalid")
    if not crypto.verify(
        TRANSFER_DOMAIN, claim.payload(), seeder_signature, seeder_public_key
    ):
        raise ValueError("seeder signature is invalid")


def review_claims(
    claims: Iterable[TransferClaim],
    *,
    ticketed_downloaders: set[str],
    owner_of: dict[str, str],
    pair_cap_bytes: int,
) -> list[ReviewedClaim]:
    """Apply the fraud controls. Flags are kept, not silently dropped."""
    seen: set[tuple[str, str, str, int]] = set()
    reviewed: list[ReviewedClaim] = []
    per_pair: dict[tuple[str, str], int] = {}

    for claim in claims:
        flags: list[str] = []
        accepted = claim.bytes_served

        if claim.downloader_id not in ticketed_downloaders:
            reviewed.append(ReviewedClaim(claim, 0, ("downloader_not_ticketed",)))
            continue
        if owner_of.get(claim.downloader_id) and (
            owner_of.get(claim.downloader_id) == owner_of.get(claim.seeder_id)
        ):
            flags.append("same_owner")
        duplicate_key = (
            claim.downloader_id,
            claim.seeder_id,
            claim.shard_sha256,
            claim.bytes_served,
        )
        if duplicate_key in seen:
            reviewed.append(ReviewedClaim(claim, 0, ("duplicate_claim",)))
            continue
        seen.add(duplicate_key)

        pair = (claim.downloader_id, claim.seeder_id)
        already = per_pair.get(pair, 0)
        remaining = max(0, pair_cap_bytes - already)
        if accepted > remaining:
            accepted = remaining
            flags.append("pair_cap")
        per_pair[pair] = already + accepted
        if accepted <= 0:
            flags.append("no_payable_bytes")
        reviewed.append(ReviewedClaim(claim, accepted, tuple(flags)))
    return reviewed


def distribute(pool_micro: int, weights: dict[str, int]) -> dict[str, int]:
    """Largest-remainder split of ``pool_micro`` by integer byte weights.

    Guarantees: result values are non-negative ints and sum exactly to the pool
    (when the pool is non-negative and any weight is positive).
    """
    if (
        isinstance(pool_micro, bool)
        or not isinstance(pool_micro, int)
        or pool_micro < 0
    ):
        raise ValueError("pool must be a non-negative integer number of micro-credits")
    payable = {node: weight for node, weight in weights.items() if weight > 0}
    total_weight = sum(payable.values())
    if not payable or total_weight <= 0 or pool_micro == 0:
        return {node: 0 for node in weights}

    shares: dict[str, int] = {}
    remainders: list[tuple[int, str]] = []
    assigned = 0
    for node, weight in payable.items():
        numerator = pool_micro * weight
        share, remainder = divmod(numerator, total_weight)
        shares[node] = share
        assigned += share
        remainders.append((remainder, node))

    leftover = pool_micro - assigned
    # Deterministic tie-break by node id so statements are reproducible.
    for _, node in sorted(remainders, key=lambda pair: (-pair[0], pair[1]))[:leftover]:
        shares[node] += 1
    for node in weights:
        shares.setdefault(node, 0)
    return shares
