"""Strict request/response models.

Every model forbids unknown fields: a typo such as ``confidental`` must be a 422,
never a silently ignored security flag. All money is ``int`` micro-credits and
``bool`` is rejected where an amount is expected (``bool`` is an ``int``
subclass in Python, so ``True`` would otherwise pass as 1).

The coordinator's placement request deliberately carries **no messages** — the
prompt never leaves the SDK/worker pair (see THREAT_MODEL.md TB1).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class Confidentiality(str, Enum):
    NONE = "none"
    ATTESTED_TEE = "attested-tee"


class NodeType(str, Enum):
    COMPUTE = "compute"
    SEEDER = "seeder"
    BOTH = "both"


class JobStatus(str, Enum):
    PLACED = "placed"
    RUNNING = "running"
    SETTLED = "settled"
    DISPUTED = "disputed"
    FAILED = "failed"
    EXPIRED = "expired"


class AttestationPlatform(str, Enum):
    SEV_SNP = "sev-snp"
    TDX = "tdx"
    NVIDIA_GPU = "nvidia-gpu"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _validate_model_name(value: str) -> str:
    if not MODEL_NAME_RE.match(value):
        raise ValueError("model name must match the allowlist pattern")
    return value


def _validate_money(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer number of micro-credits")
    if value < 0:
        raise ValueError(f"{field} must be >= 0")
    return value


# ── SDK / coordinator placement ─────────────────────────────────────────────


class PlacementRequest(_Strict):
    """What the coordinator is allowed to know before placement."""

    model: str
    max_tokens: int = Field(default=512, ge=1, le=32768)
    stream: bool = False
    confidential: bool = False
    prompt_tokens_estimate: int = Field(default=0, ge=0, le=1_000_000)
    region: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]*$")
    latency_target_ms: int | None = Field(default=None, ge=1, le=60_000)
    vram_gb: int | None = Field(default=None, ge=1, le=2048)
    max_price_micro: int | None = None

    @field_validator("model")
    @classmethod
    def _model(cls, value: str) -> str:
        return _validate_model_name(value)

    @field_validator("max_price_micro")
    @classmethod
    def _price(cls, value: int | None) -> int | None:
        return value if value is None else _validate_money(value, "max_price_micro")


class PlacementTicket(_Strict):
    """The signed payload inside a placement ticket (what the SDK relies on)."""

    job_id: str
    account_id: str
    node_id: str
    model: str
    confidentiality: Confidentiality
    worker_public_key: str
    max_tokens: int
    max_price_micro: int
    tier: Literal["standard", "confidential"]
    jti: str
    issued_at: int
    expires_at: int

    @field_validator("job_id", "node_id", "jti", "account_id")
    @classmethod
    def _ids(cls, value: str) -> str:
        if not SAFE_ID_RE.match(value):
            raise ValueError("identifier contains invalid characters")
        return value


class TicketResponse(_Strict):
    job_id: str
    model: str
    confidentiality: Confidentiality
    confidentiality_notice: str
    worker_endpoint: str
    worker_public_key: str
    key_attestation_ref: str | None = None
    expires_at: int
    tier: Literal["standard", "confidential"]
    ticket: dict[str, Any]
    coordinator_key_id: str


# ── node registry ───────────────────────────────────────────────────────────


class WorkerRegistrationPayload(_Strict):
    identity_public_key: str = Field(min_length=32, max_length=64)
    session_public_key: str = Field(min_length=32, max_length=64)
    node_type: NodeType = NodeType.COMPUTE
    models: list[str] = Field(default_factory=list, max_length=64)
    gpu_model: str = Field(default="unknown", max_length=64)
    vram_gb: int = Field(default=0, ge=0, le=2048)
    region: str = Field(default="unknown", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")
    price_micro_per_mtok: int = Field(default=0, ge=0, le=10**12)
    confidentiality: Confidentiality = Confidentiality.NONE
    software_version: str = Field(default="0.1.0", max_length=32)
    endpoint: str = Field(default="", max_length=255)

    @field_validator("models")
    @classmethod
    def _models(cls, values: list[str]) -> list[str]:
        for value in values:
            _validate_model_name(value)
        if len(set(values)) != len(values):
            raise ValueError("models must be unique")
        return values

    @field_validator("price_micro_per_mtok")
    @classmethod
    def _price(cls, value: int) -> int:
        return _validate_money(value, "price_micro_per_mtok")


class HeartbeatPayload(_Strict):
    node_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    free_vram_gb: int = Field(default=0, ge=0, le=2048)
    loaded_models: list[str] = Field(default_factory=list, max_length=64)
    queue_depth: int = Field(default=0, ge=0, le=1024)
    relay_reachable: bool = True
    sent_at: int


class SignedEnvelope(_Strict):
    """Self-signed request body (registration, heartbeat, receipts).

    ``payload`` is verified as an opaque dict first, then validated against the
    concrete model by the route — signature verification must not depend on the
    parser accepting attacker-shaped fields.
    """

    payload: dict[str, Any]
    key_id: str = Field(min_length=1, max_length=64)
    alg: Literal["Ed25519"] = "Ed25519"
    sig: str = Field(min_length=16, max_length=256)


class NodeRegistrationResponse(_Strict):
    node_id: str
    identity_key_id: str
    session_key_id: str
    confidentiality: Confidentiality
    confidentiality_notice: str
    attestation_required: bool
    registered_at: int


# ── execution receipts ──────────────────────────────────────────────────────


class ReceiptPayload(_Strict):
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    node_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    account_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    model: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_tokens: int = Field(ge=0, le=10**9)
    output_tokens: int = Field(ge=0, le=10**9)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: int
    finished_at: int
    verification: VerificationReceipt | None = None

    @model_validator(mode="after")
    def _times(self) -> ReceiptPayload:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        return self


class VerificationReceipt(_Strict):
    """Part 3 transcript commitment carried inside an execution receipt.

    Optional: receipts from workers or engines without logprob support remain
    valid, but they carry no verification evidence. ``transcript_source``
    distinguishes real engine logprobs from the development stub's synthetic
    records so a simulated transcript is never mistaken for evidence.
    """

    receipt_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    transcript_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    token_count: int = Field(ge=1, le=10**9)
    engine: str = Field(min_length=1, max_length=32)
    engine_version: str = Field(default="unknown", max_length=64)
    dtype: str = Field(default="unknown", max_length=32)
    quantization: str = Field(default="unknown", max_length=32)
    gpu_class: str = Field(default="unknown", max_length=64)
    model_version: str | None = Field(default=None, max_length=64)
    sampling_params: dict[str, Any] = Field(default_factory=dict)
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_source: Literal["engine", "simulated"] = "engine"
    attestation_ref: str | None = Field(default=None, max_length=128)
    instance_key_id: str | None = Field(default=None, max_length=64)


class AckPayload(_Strict):
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    tokens_received: int = Field(ge=0, le=10**9)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    acked_at: int


class SettlementResponse(_Strict):
    job_id: str
    status: JobStatus
    billable_input_tokens: int
    billable_output_tokens: int
    total_micro: int
    compute_micro: int
    seeder_micro: int
    platform_micro: int
    reason: str | None = None


# ── attestation ─────────────────────────────────────────────────────────────


class AttestationEvidencePayload(_Strict):
    node_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    platform: AttestationPlatform
    nonce: str = Field(min_length=16, max_length=128)
    session_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_b64: str = Field(min_length=16, max_length=1_000_000)
    cert_chain_pem: list[str] = Field(default_factory=list, max_length=8)
    issued_at: int


class AttestationVerdict(_Strict):
    node_id: str
    platform: AttestationPlatform
    status: Literal["verified", "rejected", "simulated"]
    confidentiality: Confidentiality
    nonce: str
    session_key_sha256: str
    measurement: str | None = None
    tcb: str | None = None
    verifier: str
    issued_at: int
    expires_at: int
    reason: str | None = None


# ── catalogue / account ─────────────────────────────────────────────────────


class ModelInfo(_Strict):
    model_id: str
    name: str
    format: str
    manifest_hash: str
    size_bytes: int
    required_vram_gb: int
    license_note: str
    allowlisted: bool
    input_micro_per_mtok: int
    output_micro_per_mtok: int
    confidential_input_micro_per_mtok: int
    confidential_output_micro_per_mtok: int
    launch_pricing_until: str


class BalanceResponse(_Strict):
    account_id: str
    credits: float
    micro_credits: int
    held_micro: int
    available_micro: int


class UsageRecord(_Strict):
    job_id: str
    model: str
    status: str
    input_tokens: int
    output_tokens: int
    total_micro: int
    created_at: int


class UsageResponse(_Strict):
    account_id: str
    jobs: list[UsageRecord]
    total_micro: int


class CoordinatorKeyResponse(_Strict):
    bundle: dict[str, Any]
    revocations: dict[str, Any]


class KillSwitchRequest(_Strict):
    """Flip the money-out switch (checklist 5.9). A reason is mandatory: an
    unexplained stop on refunds and payouts is unreviewable afterwards."""

    paused: bool
    reason: str = Field(min_length=3, max_length=300)


class RefundRequest(_Strict):
    """An admin credit adjustment. Money only moves as a ledger entry (4.6)."""

    account_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    amount_micro: int
    reason: str = Field(min_length=3, max_length=500)
    #: Operator reference (ticket id). Required because it is the idempotency
    #: key: a retried refund must not credit the account twice.
    reference: str = Field(min_length=1, max_length=128)

    @field_validator("amount_micro")
    @classmethod
    def _amount(cls, value: int) -> int:
        value = _validate_money(value, "amount_micro")
        if value == 0:
            raise ValueError("amount_micro must be greater than zero")
        return value
