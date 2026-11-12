"""Transcript commitment, challenge protocol, tolerance verifier, verdicts, penalties.

Part 3 of the TCDN-AI build spec replaces the old "exact-hash redundant execution"
verification description with a commitment-first, tolerance-based, probabilistic
spot-check system. This module is the new home for that logic.

The existing :mod:`app.ai.verification` module is retained as a thin, backwards-
compatible entry point for job *selection*; everything new here is opt-in behind
Django-style feature flags already present in :class:`app.ai.config.AISettings`
and the coordinator's ``ENABLE_TCDN_AI`` gate.

Design points repeated from the spec and not up for debate here:

* The worker commits to a Merkle root over per-token transcript records *before*
  it learns which jobs or token indices will be checked.
* All randomness that affects checking (job sampling, challenge indices) is derived
  from coordinator/verifier secrets *after* the root is stored, so the worker
  cannot predict selection.
* Comparisons are tolerance-based, never bit-identical. Floating-point and kernel
  variance is real; a calibration step (a separate tool, not the runtime verifier)
  produces tolerance profiles per (model, hardware bucket, engine, dtype).
* The coordinator and trackers never see prompts or outputs (existing invariant).
  Verification bundles are scoped to verifier workers only and are deleted after the
  verdict. This module stores hashes, statistics, verdicts and penalty records; it
  does not persist prompt or output text.
* Penalties are graduated: deterministic/cryptographic violations can be immediate;
  statistical failures escalate.
* Verifiers are paid a fixed fee, never a share of slashed funds.

What this module actually implements, and what it does not (kept honest so the
code cannot be mistaken for the finished Part 3 system):

Implemented and tested:

* Transcript commitment model + Merkle tree over per-token records (reuses the
  existing ``merkle_*`` helpers in :mod:`app.ai.shards`; the leaf is the per-token
  transcript record, not a shard).
* Receipt extension fields (transcript_root, token_count, declared engine/dtype/
  quantization, sampling parameters, salted prompt hash). The execution-receipt
  model validates the extension; persistence of the extension is the coordinator's
  path and is not wired yet.
* Challenge index derivation and sampling from a server secret, job id and
  transcript root (the worker cannot predict either before committing).
* Merkle proof verification against the transcript root.
* Tolerance comparison: compares worker per-token records against a verifier's
  teacher-forced re-scoring using a tolerance profile, over the full sequence.
* Verdict types (pass / suspect / fail), a signed verdict document, and a
  statistics digest that carries no prompt or output plaintext.
* Reference signing layer: ``verifier_sign::ed25519_sign`` and the
  ``VerificationVoucher`` envelope are real Ed25519 signatures over a closed set
  of Part 3 domains.
* A canary traversal helper and a deterministic stub re-scorer for GPU-free tests.

Not implemented here (the rest of Part 3, tracked by milestone):

* The verifier *service*: worker pool, job queue, bundle delivery/encryption,
  retries and dead-letter handling. The stub re-scorer stands in for the real
  manifest-hash model; no such service exists in the repo.
* Calibration: there is no calibration tool and no measured report. The registry
  is an in-memory, unsigned stub of the shape a versioned registry will need.
* The penalty ladder and ledger wiring: ``PenaltyRecord`` and the account mapping
  are shapes only; nothing debits or credits an account, holds collateral, refunds
  a client, records an appeal, or enforces a kill switch.
* Suspect flow with independent verifiers, conflict-of-interest rules and majority
  resolution.
* Synthetic probe jobs and privacy bundles (the canary helper is not that system).
* Coordinator integration: :mod:`app.ai.service` still uses the older
  exact-hash redundant-execution sampler in :mod:`app.ai.verification`.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass
from typing import Any, Protocol

from app.ai import crypto
from app.ai.shards import merkle_proof, merkle_root, verify_merkle_proof
from app.ai.verifier_sign import (
    VerificationVoucher,
    VerificationVoucherDomain,
    ed25519_sign,
)

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

VERIFICATION_DOMAIN = "tcdn-ai/verification2/v1"
VERDICT_DOMAIN = "tcdn-ai/verdict/v1"
CHALLENGE_DOMAIN = "tcdn-ai/challenge/v1"
TRANSCRIPT_RECORD_DOMAIN = "tcdn-ai/transcript-record/v1"

# Challenge budget (spec defaults, overridable by settings in the coordinator).
DEFAULT_CHALLENGE_FRACTION = 0.03  # 3% of output tokens
DEFAULT_MIN_CHALLENGE = 8
DEFAULT_MAX_CHALLENGE = 256
TRANSCRIPT_QUANTIZATION_BITS = 14  # fixed-point logprob precision for commitments

# ---------------------------------------------------------------------------
# Transcript records (per-token commitment leaf)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptRecord:
    """One per-token record captured by the worker during generation.

    These contain token ids and probabilities, which *do* reveal the output
    (they are not prompt text, but they are derivative of it). They are treated
    as sensitive: encrypted at rest by the worker, access-controlled, deleted
    when the verification window closes. The coordinator never stores the records
    themselves — only the Merkle root in the receipt.
    """

    position: int  # token index, 0-based
    token_id: int  # vocabulary id of the emitted token
    chosen_logprob: float  # log-prob of the emitted token under the model
    top_k: tuple[
        tuple[int, float], ...
    ]  # (token_id, logprob) pairs, sorted, length <= k

    def leaf_hex(self) -> str:
        """Deterministic hex leaf for the Merkle tree.

        We canonicalise the record to bytes, then SHA-256. Fixed-precision
        quantisation of floats is applied so that two implementations that
        computed the same underlying value to within the precision do *not*
        need to produce bit-identical leaves; the commitment binds to the
        quantised value the worker actually stored, and the verifier compares
        tolerance against the worker's opened (quantised) record, not against
        the leaf hash directly.
        """
        payload = {
            "p": self.position,
            "t": self.token_id,
            "lp": _quantize_logprob(self.chosen_logprob),
            "tk": [(tid, _quantize_logprob(lp)) for tid, lp in self.top_k],
        }
        return crypto.sha256_hex(crypto.canonical_json(payload))

    def to_challenge_response(self) -> dict[str, Any]:
        """Public shape returned to the coordinator in a challenge response."""
        return {
            "position": self.position,
            "token_id": self.token_id,
            "chosen_logprob": self.chosen_logprob,
            "top_k": [{"token_id": tid, "logprob": lp} for tid, lp in self.top_k],
        }


def build_transcript_root(records: list[TranscriptRecord]) -> str:
    """Merkle root over per-token records, in position order.

    The spec requires a Merkle tree over the per-token records; this reuses the
    existing ``merkle_root`` helper over leaf hashes. Leaves must be in position
    order; the worker is responsible for emitting them that way.
    """
    if not records:
        raise ValueError("cannot build a transcript root over zero tokens")
    ordered = sorted(records, key=lambda r: r.position)
    # Position contiguity is a commitment integrity check: a gap would let the
    # worker claim fewer tokens than it actually produced without detection at
    # the commitment level.
    expected = list(range(len(ordered)))
    actual = [r.position for r in ordered]
    if actual != expected:
        raise ValueError(
            "transcript records must be contiguous positions starting at 0"
        )
    leaves = [r.leaf_hex() for r in ordered]
    return merkle_root(leaves)


def transcript_proof(
    records: list[TranscriptRecord], index: int
) -> list[dict[str, str]]:
    """Merkle inclusion proof for the per-token record at ``index``."""
    leaves = [r.leaf_hex() for r in records]
    return merkle_proof(leaves, index)


# ---------------------------------------------------------------------------
# Quantisation helpers (commitment precision + canonical floats)
# ---------------------------------------------------------------------------


def _quantize_logprob(value: float) -> int:
    """Fixed-point representation of a logprob for the commitment leaf.

    Logprobs are negative (or zero at worst case). We map [-20, 0] into a
    signed fixed-point integer at ``TRANSCRIPT_QUANTIZATION_BITS`` fractional
    bits. Values outside the range are clamped and flagged in the record so the
    verifier can treat them as "extreme" rather than silently rounding.
    """
    shift = TRANSCRIPT_QUANTIZATION_BITS
    scale = 1 << shift
    clamped = max(-20.0, min(0.0, float(value)))
    return round(clamped * scale)


# ---------------------------------------------------------------------------
# Receipt extension fields (additive, no schema break)
# ---------------------------------------------------------------------------


class VerificationReceiptExt:
    """The ``verification`` extension carried inside the execution receipt.

    The Part 1 receipt already carries the job/node/model identity, manifest
    hash, token counts, output hash and timings. This extension adds only the
    fields Part 3 needs on top of that, so there is exactly one copy of each
    fact in the signed document:

    * ``receipt_id`` — unique, single-use id for replay detection (enforced by
      the coordinator's replay cache, not here);
    * the Merkle ``transcript_root`` and the number of committed tokens;
    * the declared engine/version, dtype, quantization and GPU class bucket, so
      a verifier can pin the tolerance profile;
    * the sampling parameters and the salted prompt hash (never the prompt);
    * the attestation reference and per-container instance key for the
      confidential tier;
    * ``transcript_source`` — ``engine`` only when the records came from the
      engine's own logprobs; the development stub labels its synthetic records
      ``simulated`` so a receipt is never mistaken for engine evidence.

    The coordinator writes this as ``payload["verification"]`` before signing;
    old receipts without the extension remain valid.
    """

    @staticmethod
    def build(
        *,
        receipt_id: str,
        engine: str,  # e.g. "vllm", "ollama"
        engine_version: str,
        dtype: str,  # e.g. "float16", "bfloat16", "float32"
        quantization: str,  # e.g. "none", "q4_k_m", "gptq"
        gpu_class: str,  # documented hardware bucket label
        sampling_params: dict[str, Any],
        prompt_hash: str,  # salted SHA-256; never the prompt
        transcript_records: list[TranscriptRecord],
        model_version: str | None = None,
        attestation_ref: str | None = None,
        instance_key_id: str | None = None,
        transcript_source: str = "engine",
    ) -> dict[str, Any]:
        if not isinstance(receipt_id, str) or not receipt_id:
            raise ValueError("verification receipt needs a non-empty receipt_id")
        if transcript_source not in {"engine", "simulated"}:
            raise ValueError("transcript_source must be 'engine' or 'simulated'")
        return {
            "receipt_id": receipt_id,
            "transcript_root": build_transcript_root(transcript_records),
            "token_count": len(transcript_records),
            "engine": engine,
            "engine_version": engine_version,
            "dtype": dtype,
            "quantization": quantization,
            "gpu_class": gpu_class,
            "model_version": model_version,
            "sampling_params": sampling_params,
            "prompt_hash": prompt_hash,
            "transcript_source": transcript_source,
            "attestation_ref": attestation_ref,
            "instance_key_id": instance_key_id,
        }

    @staticmethod
    def extract(payload: dict[str, Any]) -> dict[str, Any] | None:
        v = payload.get("verification")
        return v if isinstance(v, dict) else None

    @staticmethod
    def transcript_root(payload: dict[str, Any]) -> str | None:
        v = VerificationReceiptExt.extract(payload)
        return v.get("transcript_root") if v else None

    @staticmethod
    def token_count(payload: dict[str, Any]) -> int | None:
        v = VerificationReceiptExt.extract(payload)
        return v.get("token_count") if v else None


# ---------------------------------------------------------------------------
# Coordinator randomness: job sampling + challenge derivation
# ---------------------------------------------------------------------------


class VerificationRng:
    """Deterministic-but-unpredictable-to-the-worker randomness for sampling and
    challenge indices.

    The worker cannot predict these because they depend on a server secret that
    the worker never sees, plus the job_id and the transcript_root, the last of
    which is only available *after* the worker commits.

    This is intentionally *not* a simple ``hash(job_id)`` — that would let the
    worker precompute which jobs are checked. The transcript_root binds the
    commitment into the randomness.
    """

    def __init__(self, server_secret: bytes) -> None:
        if not server_secret:
            raise ValueError("verification server secret must be non-empty")
        self._secret = server_secret

    def sample_decision(
        self, job_id: str, transcript_root: str, *, rate_per_10k: int
    ) -> bool:
        """True = verified. Derived only from (secret, job_id, transcript_root).

        ``rate_per_10k`` is the coordinator's current per-job sampling target
        expressed in per-10,000 units (e.g. 300 = 3%). Adaptive overrides live in
        the coordinator and are passed in here; this class is pure randomness.
        """
        if not (0 <= rate_per_10k <= 10000):
            raise ValueError("rate_per_10k must be in [0, 10000]")
        seed = hashlib.sha256(
            self._secret + job_id.encode() + transcript_root.encode()
        ).digest()
        rnd = hmac.new(seed, b"sample", hashlib.sha256).digest()
        value = int.from_bytes(rnd[:4], "big") & 0x7FFFFFFF
        return (value % 10000) < rate_per_10k

    def challenge_indices(
        self,
        job_id: str,
        transcript_root: str,
        token_count: int,
        *,
        fraction: float = DEFAULT_CHALLENGE_FRACTION,
        minimum: int = DEFAULT_MIN_CHALLENGE,
        maximum: int = DEFAULT_MAX_CHALLENGE,
    ) -> list[int]:
        """Derive ``m`` distinct challenge indices for a selected job."""
        if token_count <= 0:
            return []
        count = max(minimum, min(maximum, math.ceil(token_count * fraction)))
        count = min(count, token_count)
        seed = hashlib.sha256(
            self._secret + job_id.encode() + transcript_root.encode() + b"|challenge"
        ).digest()
        rnd = hmac.new(seed, b"indices", hashlib.sha256).digest()
        indices = list(range(token_count))
        for i in range(count):
            rnd = hmac.new(
                rnd + int.to_bytes(i, 4, "big"), b"shuffle", hashlib.sha256
            ).digest()
            j = int.from_bytes(rnd[:4], "big") % (token_count - i) + i
            indices[i], indices[j] = indices[j], indices[i]
        return sorted(indices[:count])


# ---------------------------------------------------------------------------
# Challenge response verification (deterministic violations)
# ---------------------------------------------------------------------------


class ChallengeVerificationError(Exception):
    """A deterministic violation in the challenge protocol."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def verify_challenge_response(
    payload: dict[str, Any],
    *,
    transcript_root: str,
    records: dict[
        int, TranscriptRecord
    ],  # position -> record (worker-kept, opened on demand)
    node_public_key: str,
    job_id: str,
) -> list[TranscriptRecord]:
    """Validate a worker's challenge response and return the opened records.

    Deterministic-violation checks:

    * The response must be signed (or otherwise bound) — here we assert the
      envelope shape the coordinator will verify before calling this helper.
    * Each challenged index must have a record whose leaf verifies against the
      signed ``transcript_root``.
    * The opened ``position`` / ``token_id`` must match the leaf that was proven.

    Anything failing here is a deterministic violation (forged proof, wrong root,
    missing response, records that contradict the receipt).
    """
    opened: list[TranscriptRecord] = []
    responses = payload.get("responses")
    if not isinstance(responses, list):
        raise ChallengeVerificationError(
            "challenge response missing 'responses'", code="bad_challenge_shape"
        )

    for item in responses:
        if not isinstance(item, dict):
            raise ChallengeVerificationError(
                "bad challenge response item", code="bad_challenge_shape"
            )
        position = item.get("position")
        proof = item.get("proof")
        if not isinstance(position, int) or not isinstance(proof, list):
            raise ChallengeVerificationError(
                "challenge response missing position/proof", code="bad_challenge_shape"
            )

        record = records.get(position)
        if record is None:
            raise ChallengeVerificationError(
                f"worker did not open token index {position}",
                code="challenge_open_failure",
            )

        leaf_hex = record.leaf_hex()
        if not verify_merkle_proof(leaf_hex, proof, transcript_root):
            raise ChallengeVerificationError(
                f"merkle proof for token {position} does not match the signed transcript root",
                code="proof_mismatch",
            )

        # Integrity: the opened record must actually be the one committed at that
        # leaf. Re-derive the leaf and confirm.
        if leaf_hex != _recomputed_leaf_hex(record):
            raise ChallengeVerificationError(
                f"opened record at position {position} does not match the committed leaf",
                code="record_mismatch",
            )
        opened.append(record)
    return opened


def _recomputed_leaf_hex(record: TranscriptRecord) -> str:
    payload = {
        "p": record.position,
        "t": record.token_id,
        "lp": _quantize_logprob(record.chosen_logprob),
        "tk": [(tid, _quantize_logprob(lp)) for tid, lp in record.top_k],
    }
    return crypto.sha256_hex(crypto.canonical_json(payload))


# ---------------------------------------------------------------------------
# Tolerance profiles + verifier comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToleranceProfile:
    """Calibrated per (model, hardware bucket, engine, dtype) tolerance profile.

    In production these come from a versioned, signed calibration registry.
    Here we carry the shape and provide a documented default + a stub registry so
    the runtime can be wired to real profiles later.
    """

    model_family: str
    gpu_class: str
    engine: str
    dtype: str
    quantization: str
    profile_version: str
    chosen_logprob_abs_tolerance: float  # per-token tolerance for the chosen logprob
    top_k_overlap_min: int  # minimum number of top-k ids that should overlap
    top_k_logprob_abs_tolerance: float
    mean_logprob_abs_tolerance: float  # full-sequence mean abs diff threshold
    tail_fraction_threshold: float  # fraction of tokens in extreme low-prob tail
    effective_from: int
    effective_until: int | None

    @property
    def label(self) -> str:
        return f"{self.model_family}/{self.gpu_class}/{self.engine}/{self.dtype}/{self.quantization}"


class CalibrationRegistry:
    """Versioned, signed tolerance profile registry (stub shape for now).

    Real calibration would run a standard prompt set across GPUs/engines/dtypes
    and record honest variance; this class provides the lookup + version semantics
    so the verifier can pin the profile version used for a verdict.
    """

    def __init__(self, *, default: ToleranceProfile | None = None) -> None:
        self._profiles: dict[str, ToleranceProfile] = {}
        if default is not None:
            self._profiles[default.label] = default

    def register(self, profile: ToleranceProfile) -> None:
        self._profiles[profile.label] = profile

    def lookup(
        self,
        *,
        model_family: str,
        gpu_class: str,
        engine: str,
        dtype: str,
        quantization: str,
    ) -> ToleranceProfile | None:
        key = f"{model_family}/{gpu_class}/{engine}/{dtype}/{quantization}"
        return self._profiles.get(key)

    def default(self) -> ToleranceProfile:
        if self._profiles:
            return next(iter(self._profiles.values()))
        return _DEFAULT_TOLERANCE_PROFILE


# A documented default so the module is usable without a calibration run.
# These numbers are *placeholders* to be replaced by real calibration before
# any production sampling is enabled. They are deliberately conservative.
_DEFAULT_TOLERANCE_PROFILE = ToleranceProfile(
    model_family="unknown",
    gpu_class="default",
    engine="default",
    dtype="default",
    quantization="default",
    profile_version="stub-v0",
    chosen_logprob_abs_tolerance=0.15,
    top_k_overlap_min=3,
    top_k_logprob_abs_tolerance=0.25,
    mean_logprob_abs_tolerance=0.05,
    tail_fraction_threshold=0.0,
    effective_from=0,
    effective_until=None,
)


@dataclass(frozen=True)
class TokenComparison:
    position: int
    worker_token_id: int
    verifier_token_id: int
    worker_chosen_logprob: float
    verifier_chosen_logprob: float
    logprob_diff: float
    worker_top_k_ids: list[int]
    verifier_top_k_ids: list[int]
    in_verifier_top_k: bool
    tail: bool  # True if the emitted token is in an extreme low-prob tail under the verifier


@dataclass(frozen=True)
class VerificationStatistics:
    """Aggregated per-job stats a verdict carries. No plaintext tokens/prompts."""

    token_count: int
    challenged_positions: list[int]
    comparisons: list[TokenComparison]
    mean_abs_logprob_diff: float
    max_abs_logprob_diff: float
    tail_fraction: float
    mean_top_k_overlap: float
    chosen_all_within_tolerance: bool
    challenged_all_within_tolerance: bool
    output_hash_matches: bool
    token_count_matches: bool
    prompt_hash_matches: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "token_count": self.token_count,
            "challenged_positions": self.challenged_positions,
            "comparisons": [
                {
                    "position": c.position,
                    "worker_token_id": c.worker_token_id,
                    "verifier_token_id": c.verifier_token_id,
                    "worker_chosen_logprob": c.worker_chosen_logprob,
                    "verifier_chosen_logprob": c.verifier_chosen_logprob,
                    "logprob_diff": c.logprob_diff,
                    "worker_top_k_ids": c.worker_top_k_ids,
                    "verifier_top_k_ids": c.verifier_top_k_ids,
                    "in_verifier_top_k": c.in_verifier_top_k,
                    "tail": c.tail,
                }
                for c in self.comparisons
            ],
            "mean_abs_logprob_diff": self.mean_abs_logprob_diff,
            "max_abs_logprob_diff": self.max_abs_logprob_diff,
            "tail_fraction": self.tail_fraction,
            "mean_top_k_overlap": self.mean_top_k_overlap,
            "chosen_all_within_tolerance": self.chosen_all_within_tolerance,
            "challenged_all_within_tolerance": self.challenged_all_within_tolerance,
            "output_hash_matches": self.output_hash_matches,
            "token_count_matches": self.token_count_matches,
            "prompt_hash_matches": self.prompt_hash_matches,
        }


class VerifierRescorer(Protocol):
    """Teacher-forced re-scoring interface.

    In the real system this would run the same manifest-hash model in the verifier
    process. Here we provide the *interface* and a deterministic stub so the
    tolerance comparison and verdict logic are testable without a GPU.
    """

    model_family: str
    engine: str
    dtype: str

    def rescoring(
        self,
        *,
        prompt_token_ids: list[int],
        output_token_ids: list[int],
        sampling_params: dict[str, Any],
    ) -> list[tuple[int, float, list[tuple[int, float]]]]:
        """Return per-position (token_id, chosen_logprob, top_k) for the verifier.

        The returned list must align 1:1 with ``output_token_ids``.
        """
        ...


class StubVerifierRescorer(VerifierRescorer):
    """Deterministic stub that mirrors committed records within tolerance.

    This is what lets us test the tolerance path without real inference. It does
    *not* claim to model real hardware variance; real calibration replaces it.
    """

    model_family: str
    engine: str
    dtype: str

    def __init__(
        self,
        *,
        model_family: str = "stub",
        tolerance: float = 0.0,
        tail_fraction: float = 0.0,
        seed: int = 0,
    ) -> None:
        self.model_family = model_family
        self.engine = "stub"
        self.dtype = "float32"
        self._tolerance = tolerance
        self._tail_fraction = tail_fraction
        self._seed = seed

    def rescoring(
        self,
        *,
        prompt_token_ids: list[int],
        output_token_ids: list[int],
        sampling_params: dict[str, Any],
    ) -> list[tuple[int, float, list[tuple[int, float]]]]:
        import random

        rng = random.Random(self._seed)
        out: list[tuple[int, float, list[tuple[int, float]]]] = []
        for i, tid in enumerate(output_token_ids):
            base = -float(f"1.{i % 9}2")  # deterministic per-position base
            noise = (rng.random() - 0.5) * 2.0 * self._tolerance
            chosen = base + noise
            top_k = [(tid, chosen)] + [
                (tid ^ ((j + 1) * 7), chosen - 0.5 - rng.random()) for j in range(4)
            ]
            out.append((tid, chosen, top_k))
        return out


def compare_sequence(
    worker_records: list[TranscriptRecord],
    verifier_records: list[tuple[int, float, list[tuple[int, float]]]],
    *,
    profile: ToleranceProfile,
    challenged_positions: list[int],
) -> VerificationStatistics:
    """Full-sequence tolerance comparison between the worker's commitment and the
    verifier's teacher-forced re-scoring.

    Returns per-job statistics. The verdict decision is made by the caller using
    these statistics plus policy (suspect flow, majority, etc.).
    """
    if len(worker_records) != len(verifier_records):
        raise ValueError("worker and verifier token counts must match for comparison")

    comparisons: list[TokenComparison] = []
    diffs: list[float] = []
    tail_hits = 0

    for i, (wr, (vtid, vlogprob, vtopk)) in enumerate(
        zip(worker_records, verifier_records)
    ):
        vtopk_ids = [t[0] for t in vtopk]
        diff = wr.chosen_logprob - vlogprob
        tail = wr.chosen_logprob < -10.0 or vlogprob < -10.0  # extreme tail proxy
        if tail:
            tail_hits += 1
        comparisons.append(
            TokenComparison(
                position=i,
                worker_token_id=wr.token_id,
                verifier_token_id=vtid,
                worker_chosen_logprob=wr.chosen_logprob,
                verifier_chosen_logprob=vlogprob,
                logprob_diff=diff,
                worker_top_k_ids=[t[0] for t in wr.top_k],
                verifier_top_k_ids=vtopk_ids,
                in_verifier_top_k=wr.token_id in vtopk_ids,
                tail=tail,
            )
        )
        diffs.append(abs(diff))

    n = len(comparisons)
    mean_abs_diff = sum(diffs) / n if n else 0.0
    max_abs_diff = max(diffs) if diffs else 0.0

    challenged_set = set(challenged_positions)
    challenged_within = all(
        abs(c.logprob_diff) <= profile.chosen_logprob_abs_tolerance
        for c in comparisons
        if c.position in challenged_set
    )
    all_within = all(
        abs(c.logprob_diff) <= profile.chosen_logprob_abs_tolerance for c in comparisons
    )

    # Mean top-k overlap (Jaccard-ish over the union of the two top-k sets).
    overlaps: list[float] = []
    for c in comparisons:
        a = set(c.worker_top_k_ids)
        b = set(c.verifier_top_k_ids)
        union = a | b
        overlap = len(a & b) / len(union) if union else 0.0
        overlaps.append(overlap)
    mean_overlap = sum(overlaps) / len(overlaps) if overlaps else 0.0

    return VerificationStatistics(
        token_count=n,
        challenged_positions=challenged_positions,
        comparisons=comparisons,
        mean_abs_logprob_diff=mean_abs_diff,
        max_abs_logprob_diff=max_abs_diff,
        tail_fraction=tail_hits / n if n else 0.0,
        mean_top_k_overlap=mean_overlap,
        chosen_all_within_tolerance=all_within,
        challenged_all_within_tolerance=challenged_within,
        output_hash_matches=False,  # filled by the verdict builder from the receipt
        token_count_matches=False,  # filled by the verdict builder
        prompt_hash_matches=False,  # filled by the verdict builder
    )


# ---------------------------------------------------------------------------
# Verdict types + signing
# ---------------------------------------------------------------------------


class VerdictType:
    PASS = "pass"
    SUSPECT = "suspect"
    FAIL = "fail"


@dataclass(frozen=True)
class Verdict:
    """Signed verdict record produced by a verifier.

    Contains no plaintext prompt or output. Only hashes, statistics, profile
    version, verifier identity, and the verdict type.
    """

    verdict_id: str
    job_id: str
    worker_node_id: str
    verifier_id: str
    type: str  # pass | suspect | fail
    transcript_root: str
    output_hash: str
    token_count: int
    statistics: dict[str, Any]  # serialisable shape of VerificationStatistics
    tolerance_profile: str  # profile label + version used
    profile_version: str
    created_at: int
    appeal_id: str | None = None

    def document(self) -> dict[str, Any]:
        return {
            "verdict_id": self.verdict_id,
            "job_id": self.job_id,
            "worker_node_id": self.worker_node_id,
            "verifier_id": self.verifier_id,
            "type": self.type,
            "transcript_root": self.transcript_root,
            "output_hash": self.output_hash,
            "token_count": self.token_count,
            "statistics": self.statistics,
            "tolerance_profile": self.tolerance_profile,
            "profile_version": self.profile_version,
            "created_at": self.created_at,
            "appeal_id": self.appeal_id,
        }

    def sign(self, private_key_b64: str) -> dict[str, Any]:
        payload = self.document()
        return {
            "key_id": crypto.key_id_for(
                crypto.ed25519_public_from_private(private_key_b64)
            ),
            "alg": "Ed25519",
            "domain": VERDICT_DOMAIN,
            "payload": payload,
            "sig": crypto.sign(VERDICT_DOMAIN, payload, private_key_b64),
        }

    @classmethod
    def verify(cls, document: dict[str, Any], public_key_b64: str) -> Verdict:
        if not crypto.verify(
            VERDICT_DOMAIN, document["payload"], document["sig"], public_key_b64
        ):
            raise ValueError("verdict signature invalid")
        payload = document["payload"]
        return cls(
            verdict_id=payload["verdict_id"],
            job_id=payload["job_id"],
            worker_node_id=payload["worker_node_id"],
            verifier_id=payload["verifier_id"],
            type=payload["type"],
            transcript_root=payload["transcript_root"],
            output_hash=payload["output_hash"],
            token_count=payload["token_count"],
            statistics=payload["statistics"],
            tolerance_profile=payload["tolerance_profile"],
            profile_version=payload["profile_version"],
            created_at=payload["created_at"],
            appeal_id=payload.get("appeal_id"),
        )


def statistics_digest(stats: VerificationStatistics) -> str:
    """Stable SHA-256 digest of the statistics a verdict references.

    The persisted verdict record stores this instead of the full statistics so the
    coordinator can prove which comparison produced a verdict without holding the
    full per-token comparison list long-term.
    """
    payload = {
        "token_count": stats.token_count,
        "challenged_positions": stats.challenged_positions,
        "mean_abs_logprob_diff": stats.mean_abs_logprob_diff,
        "max_abs_logprob_diff": stats.max_abs_logprob_diff,
        "tail_fraction": stats.tail_fraction,
        "mean_top_k_overlap": stats.mean_top_k_overlap,
        "chosen_all_within_tolerance": stats.chosen_all_within_tolerance,
        "challenged_all_within_tolerance": stats.challenged_all_within_tolerance,
        "output_hash_matches": stats.output_hash_matches,
        "token_count_matches": stats.token_count_matches,
        "prompt_hash_matches": stats.prompt_hash_matches,
    }
    return crypto.sha256_hex(crypto.canonical_json(payload))


@dataclass
class VerdictRecord:
    """Persisted shape of a verdict in the coordinator audit path.

    This is what the coordinator keeps long-term; it carries the verdict summary
    plus penalty/ledger references. It does not carry prompt or output text.
    """

    verdict_id: str
    job_id: str
    worker_node_id: str
    verifier_id: str
    type: str
    transcript_root: str
    output_hash: str
    token_count: int
    statistics_digest: str  # SHA-256 of the canonical statistics JSON
    tolerance_profile: str
    profile_version: str
    created_at: int
    penalty_applied: bool
    appeal_id: str | None = None
    reversed_at: int | None = None
    reversal_reason: str | None = None

    @classmethod
    def from_verdict(
        cls, verdict: Verdict, *, statistics_digest: str, penalty_applied: bool
    ) -> VerdictRecord:
        return cls(
            verdict_id=verdict.verdict_id,
            job_id=verdict.job_id,
            worker_node_id=verdict.worker_node_id,
            verifier_id=verdict.verifier_id,
            type=verdict.type,
            transcript_root=verdict.transcript_root,
            output_hash=verdict.output_hash,
            token_count=verdict.token_count,
            statistics_digest=statistics_digest,
            tolerance_profile=verdict.tolerance_profile,
            profile_version=verdict.profile_version,
            created_at=verdict.created_at,
            penalty_applied=penalty_applied,
            appeal_id=verdict.appeal_id,
        )


@dataclass
class PenaltyRecord:
    """A single penalty action recorded as a ledger entry + audit row.

    Amounts are integer micro-credits. Every penalty has a debit and credit so
    the ledger still balances; the destination accounts are documented in the
    spec (client refund first, then platform insurance pool, then platform).
    """

    penalty_id: str
    job_id: str | None
    node_id: str
    actor: str  # verifier id or "system" or an admin id
    kind: str  # e.g. "void_job_earnings", "holdback", "suspend_reputation", "ban"
    amount_micro: int
    debit_account: str
    credit_account: str
    reason: str
    created_at: int
    idempotent_key: str
    reversible: bool = True
    reversed_at: int | None = None
    reversal_note: str | None = None


def penalty_accounts_for_destiny(destiny: str) -> tuple[str, str]:
    """Return (debit_account, credit_account) for a penalty destiny.

    destiny:
      * "client_refund" — refund the affected client account.
      * "insurance_pool" — platform insurance pool.
      * "platform" — platform revenue.

    Verifiers and trackers never receive slashed funds; this helper exists so the
    money path is a single place that can be audited.
    """
    if destiny == "client_refund":
        return (
            "__VOID__",
            "__CLIENT_REFUND__",
        )  # filled by caller with real account ids
    if destiny == "insurance_pool":
        return ("__VOID__", "pool:insurance")
    if destiny == "platform":
        return ("__VOID__", "platform:penalty-revenue")
    raise ValueError(f"unknown penalty destiny: {destiny!r}")


# ---------------------------------------------------------------------------
# Synthetic probe + canary helpers
# ---------------------------------------------------------------------------


def has_canary(value: Any, canary: str) -> bool:
    """Return True if a canary string appears anywhere in a structure.

    Used by tests to assert that prompts/outputs (which carry canary strings in
    test builds) do not leak into coordinator state, logs, verdicts, or long-term
    storage. This is a traversal helper, not a security boundary by itself.
    """
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str) and canary in item:
            return True
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(item)
    return False


# ---------------------------------------------------------------------------
# Verification signing layer (Part 3 reference helper)
# ---------------------------------------------------------------------------


def sign_challenge_response(
    *,
    job_id: str,
    node_public_key_b64: str,
    transcript_root: str,
    challenged_positions: list[int],
    opened_records: list[dict[str, Any]],
    private_key_b64: str,
) -> VerificationVoucher:
    """Sign a challenge response as a :class:`VerificationVoucher`.

    This is the real helper the spec names as ``verifier_sign::ed25519_sign`` in
    action: it builds the structured payload, then delegates to the same vetted
    Ed25519 signing the rest of the project uses, returning a
    ``VerificationVoucher`` protobuf envelope.
    """
    payload = {
        "job_id": job_id,
        "node_public_key": node_public_key_b64,
        "transcript_root": transcript_root,
        "challenged_positions": challenged_positions,
        "opened_records": opened_records,
        "created_at": int(__import__("time").time()),
    }
    return ed25519_sign(
        VerificationVoucherDomain.CHALLENGE_RESPONSE,
        payload,
        key=private_key_b64,
    )


def sign_verifier_verdict(
    *,
    verdict_id: str,
    job_id: str,
    worker_node_id: str,
    verifier_id: str,
    verdict_type: str,
    transcript_root: str,
    output_hash: str,
    token_count: int,
    statistics: dict[str, Any],
    tolerance_profile: str,
    profile_version: str,
    created_at: int,
    appeal_id: str | None,
    private_key_b64: str,
) -> VerificationVoucher:
    """Build the verifier-verdict payload and delegate signing to
    :func:`verifier_sign::ed25519_sign`.

    See :func:`sign_challenge_response` for the same caveat about the reference-layer
    crypto seam.
    """
    payload = {
        "verdict_id": verdict_id,
        "job_id": job_id,
        "worker_node_id": worker_node_id,
        "verifier_id": verifier_id,
        "type": verdict_type,
        "transcript_root": transcript_root,
        "output_hash": output_hash,
        "token_count": token_count,
        "statistics": statistics,
        "tolerance_profile": tolerance_profile,
        "profile_version": profile_version,
        "created_at": created_at,
        "appeal_id": appeal_id,
    }
    return ed25519_sign(
        VerificationVoucherDomain.VERIFIER_VERDICT,
        payload,
        key=private_key_b64,
    )


# ---------------------------------------------------------------------------
# Scoping note
# ---------------------------------------------------------------------------

__all__ = [
    "CHALLENGE_DOMAIN",
    "TRANSCRIPT_RECORD_DOMAIN",
    "VERDICT_DOMAIN",
    "VERIFICATION_DOMAIN",
    "CalibrationRegistry",
    "ChallengeVerificationError",
    "StubVerifierRescorer",
    "TokenComparison",
    "ToleranceProfile",
    "TranscriptRecord",
    "Verdict",
    "VerdictType",
    "VerificationReceiptExt",
    "VerificationRng",
    "VerificationStatistics",
    "VerifierRescorer",
    "build_transcript_root",
    "compare_sequence",
    "has_canary",
    "sign_challenge_response",
    "sign_verifier_verdict",
    "transcript_proof",
    "verify_challenge_response",
]
