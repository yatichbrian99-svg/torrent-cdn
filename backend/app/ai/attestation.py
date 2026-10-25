"""Attestation for the confidential tier.

Reality check, encoded in the types: real remote attestation needs vendor roots,
hardware, and a verifier service this environment does not have. So:

* ``SimulatedVerifier`` produces verdicts with ``status="simulated"``, is only
  constructible when ``TCDN_AI_ALLOW_SIMULATED_ATTESTATION`` is true, and its
  verdicts are rejected by ``AttestationService.accept`` in production.
* ``SevSnpVerifier`` / ``TdxVerifier`` / ``NvidiaGpuVerifier`` are the real
  adapter seams. With no operator-supplied ``TrustPolicy`` they **fail closed**
  (``attestation_verifier_unavailable``). They never return "verified" as a
  default, and they never claim a platform they cannot prove.

Nonces are single-use and short-lived, and the attested report must bind the
worker's session public key by hash — otherwise an attested machine could be
used to vouch for a key it does not hold.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .clock import now_unix
from .config import AISettings
from .errors import AttestationError
from .keyring import KeyRing
from .models import (
    AttestationEvidencePayload,
    AttestationPlatform,
    AttestationVerdict,
    Confidentiality,
)
from .nonce_store import NonceStore
from .redis_cache import RedisNonceStore

__all__ = [
    "NonceStore"
]  # backwards-compat re-export for tests that import NonceStore from attestation

VERDICT_DOMAIN = "tcdn-ai/attestation-verdict/v1"


@dataclass(frozen=True)
class TrustPolicy:
    """Operator-supplied verification policy for real verifiers.

    ``vendor_roots_pem`` and ``allowed_measurements`` must come from the
    operator's hardware vendor data (AMD KDS / Intel PCS / NVIDIA NRAS). Empty
    values mean "not configured", which is a fail-closed state.
    """

    allowed_platforms: tuple[AttestationPlatform, ...] = ()
    allowed_measurements: frozenset[str] = frozenset()
    min_tcb: str | None = None
    vendor_roots_pem: tuple[str, ...] = ()
    require_gpu_cc: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.allowed_platforms and self.vendor_roots_pem)


class AttestationVerifier(Protocol):
    name: str

    def verify(
        self, evidence: AttestationEvidencePayload, *, now: int
    ) -> AttestationVerdict: ...


class SimulatedVerifier:
    """Development-only. Every verdict says ``simulated`` in the type and reason."""

    name = "simulated"

    def __init__(self, *, allow: bool) -> None:
        if not allow:
            raise AttestationError(
                "simulated attestation is disabled in this environment",
                code="simulated_attestation_disabled",
            )

    def verify(
        self, evidence: AttestationEvidencePayload, *, now: int
    ) -> AttestationVerdict:
        return AttestationVerdict(
            node_id=evidence.node_id,
            platform=evidence.platform,
            status="simulated",
            confidentiality=Confidentiality.ATTESTED_TEE,
            nonce=evidence.nonce,
            session_key_sha256=evidence.session_key_sha256,
            measurement="simulated-measurement",
            tcb="simulated",
            verifier=self.name,
            issued_at=now,
            expires_at=now + 3600,
            reason="development-only simulated attestation; no hardware was verified",
        )


class _FailClosedVerifier:
    """Base for real adapters: verify or raise, never guess."""

    name = "unconfigured"
    platform: AttestationPlatform = AttestationPlatform.SEV_SNP

    def __init__(self, policy: TrustPolicy | None = None) -> None:
        self.policy = policy or TrustPolicy()

    def verify(
        self, evidence: AttestationEvidencePayload, *, now: int
    ) -> AttestationVerdict:
        if evidence.platform is not self.platform:
            raise AttestationError(
                "evidence platform does not match this verifier",
                code="platform_mismatch",
            )
        if not self.policy.configured:
            raise AttestationError(
                f"{self.name} is not configured with vendor roots and measurements; "
                "refusing to produce a verdict",
                code="attestation_verifier_unavailable",
            )
        # A configured policy means the operator has plugged in a verifier
        # service/binding. This build does not ship the vendor report parsers
        # (see LIMITATIONS.md), so it still refuses rather than half-verifying.
        raise AttestationError(
            f"{self.name} report parsing is not implemented in this build",
            code="attestation_verifier_unavailable",
        )


class SevSnpVerifier(_FailClosedVerifier):
    name = "sev-snp"
    platform = AttestationPlatform.SEV_SNP


class TdxVerifier(_FailClosedVerifier):
    name = "tdx"
    platform = AttestationPlatform.TDX


class NvidiaGpuVerifier(_FailClosedVerifier):
    name = "nvidia-gpu"
    platform = AttestationPlatform.NVIDIA_GPU


@dataclass
class AttestationService:
    keyring: KeyRing
    settings: AISettings
    nonces: NonceStore = field(default_factory=NonceStore)
    verifiers: dict[AttestationPlatform, AttestationVerifier] = field(
        default_factory=dict
    )
    # Callable, not object: the service calls it as ``self.clock()``.
    clock: Callable[[], float] = now_unix

    def __post_init__(self) -> None:
        if not self.verifiers and self.settings.allow_simulated_attestation:
            simulated = SimulatedVerifier(allow=True)
            self.verifiers = {
                AttestationPlatform.SEV_SNP: simulated,
                AttestationPlatform.TDX: simulated,
                AttestationPlatform.NVIDIA_GPU: simulated,
            }

    def configure_redis(self, redis_client: Any) -> None:
        """Swap the nonce store for a Redis-backed one when Redis is available.

        ``redis_client`` may be ``None`` or an object lacking the expected
        methods; in either case this is a no-op and the in-process nonce store
        stays in use.
        """
        try:
            self.nonces = RedisNonceStore(
                redis_client, ttl_seconds=self.settings.attestation_nonce_ttl_seconds
            )
        except Exception:
            pass  # redis_client absent/broken → keep the in-process NonceStore

    def issue_nonce(self, node_id: str) -> str:
        return self.nonces.issue(node_id)

    def attest(
        self, evidence_document: AttestationEvidencePayload
    ) -> tuple[AttestationVerdict, dict]:
        """Verify evidence and return a coordinator-signed verdict document."""
        now = int(self.clock())
        evidence = evidence_document
        max_age = self.settings.attestation_nonce_ttl_seconds
        if now - evidence.issued_at > max_age or evidence.issued_at > now + 5:
            raise AttestationError(
                "attestation evidence is stale or from the future",
                code="stale_evidence",
            )
        self.nonces.consume(evidence.nonce, evidence.node_id)

        verifier = self.verifiers.get(evidence.platform)
        if verifier is None:
            raise AttestationError(
                "no verifier for this platform", code="unsupported_platform"
            )
        verdict = verifier.verify(evidence, now=now)

        if verdict.session_key_sha256 != evidence.session_key_sha256:
            raise AttestationError(
                "verdict is not bound to the submitted session key",
                code="key_binding_mismatch",
            )
        self.accept(verdict)
        document = self.keyring.sign(VERDICT_DOMAIN, verdict.model_dump(mode="json"))
        return verdict, document

    def accept(self, verdict: AttestationVerdict) -> None:
        """Policy gate applied to every verdict before it can enable placement."""
        if verdict.status == "rejected":
            raise AttestationError(
                "attestation verdict is a rejection", code="verdict_rejected"
            )
        if verdict.status == "simulated" and self.settings.is_production:
            raise AttestationError(
                "simulated attestation is never accepted in production",
                code="simulated_verdict_rejected",
            )
        if verdict.expires_at <= int(self.clock()):
            raise AttestationError(
                "attestation verdict has expired", code="verdict_expired"
            )
        if verdict.confidentiality is not Confidentiality.ATTESTED_TEE:
            raise AttestationError(
                "verdict does not grant the confidential tier",
                code="verdict_tier_insufficient",
            )
