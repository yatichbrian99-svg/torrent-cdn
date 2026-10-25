"""Coordinator service — the only place that combines auth, money and placement.

Routes are thin wrappers over these methods so the same guarantees hold whether a
call arrives over HTTP, a test, or the provisioning script.

Ordering rules that matter for money and security:

* A hold is placed **before** the job exists, so a failed job insert releases it
  rather than leaving an untracked reservation.
* A receipt is verified against the worker key registered for the node that was
  *assigned* the job — a valid receipt from a different node is rejected.
* Settlement never bills more than the worker's signed count, and never more
  than the placed hold.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from . import crypto
from .attestation import AttestationService
from .clock import now_unix
from .config import AISettings
from .errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    RateLimitedError,
    ReceiptError,
    ValidationError,
)
from .keyring import KeyRing
from .ledger import SIGNUP_GRANTS, Split
from .models import (
    AckPayload,
    AttestationEvidencePayload,
    BalanceResponse,
    Confidentiality,
    HeartbeatPayload,
    JobStatus,
    ModelInfo,
    NodeRegistrationResponse,
    NodeType,
    PlacementRequest,
    ReceiptPayload,
    SettlementResponse,
    SignedEnvelope,
    TicketResponse,
    UsageRecord,
    UsageResponse,
    WorkerRegistrationPayload,
)
from .payments import PaymentEvent, PaymentProvider, get_payment_provider
from .pricing import MICRO_PER_MTOK, ModelPrice, PriceTable, ceil_div, micro_to_credits
from .receipts import (
    RECEIPT_DOMAIN,
    JobContext,
    Reconciliation,
    reconcile,
    verify_receipt,
)
from .scheduler import Requirements, WorkerCandidate, select_worker
from .store import JobRecord, NodeRecord, Store
from .verification import VerificationOutcome, VerificationSampler, resolve_executions

logger = logging.getLogger(__name__)

from .ratelimit import RateLimiter

NODE_REGISTER_DOMAIN = "tcdn-ai/node-register/v1"
HEARTBEAT_DOMAIN = "tcdn-ai/heartbeat/v1"
# Worker heartbeat interval is 30s; three missed beats means the node is gone.
HEARTBEAT_TIMEOUT_SECONDS = 120
ATTESTATION_EVIDENCE_DOMAIN = "tcdn-ai/attestation-evidence/v1"

JOB_TTL_SECONDS = 900

CONFIDENTIALITY_NOTICES = {
    Confidentiality.NONE.value: (
        "Standard tier (confidentiality: none). The host operator can in principle "
        "read prompts and outputs on their machine. Do not send confidential data."
    ),
    Confidentiality.ATTESTED_TEE.value: (
        "Confidential tier (confidentiality: attested-tee). Prompts are decrypted only "
        "inside an attested confidential VM/GPU environment; the coordinator never sees them."
    ),
}


@dataclass(frozen=True)
class AuthContext:
    account_id: str
    scopes: tuple[str, ...]
    key_prefix: str


class Coordinator:
    def __init__(
        self,
        *,
        store: Store,
        keyring: KeyRing,
        settings: AISettings,
        attestation: AttestationService | None = None,
        payment_provider: PaymentProvider | None = None,
        redis_client: Any = None,
        clock=now_unix,
    ) -> None:
        self.store = store
        self.keyring = keyring
        self.settings = settings
        self.clock = clock
        self.attestation = attestation or AttestationService(
            keyring=keyring, settings=settings, clock=clock
        )
        if redis_client is not None:
            self.attestation.configure_redis(redis_client)
        # Imported here to avoid a module cycle: tickets only needs the keyring.
        from .ratelimit import NoOpRateLimiter, RedisRateLimiter
        from .tickets import TicketService

        try:
            self.tickets = TicketService.from_redis(
                keyring,
                redis_client,
                ttl_seconds=settings.ticket_ttl_seconds,
                skew_seconds=settings.ticket_skew_seconds,
                clock=clock,
            )
        except Exception:
            self.tickets = TicketService(
                keyring,
                ttl_seconds=settings.ticket_ttl_seconds,
                skew_seconds=settings.ticket_skew_seconds,
                clock=clock,
            )
        self._rate_limiter: RateLimiter = (
            RedisRateLimiter(
                redis_client,
                max_per_window=settings.rate_limit_per_minute,
                window_seconds=60,
            )
            if redis_client is not None
            else NoOpRateLimiter()
        )
        self.verification = VerificationSampler(settings.verification_sample_pct)
        self.payment_provider = payment_provider or get_payment_provider(
            settings,
            secret_key=os.environ.get("TCDN_AI_PAYSTACK_SECRET_KEY", ""),
        )

    # ── authentication ─────────────────────────────────────────────────────

    async def authenticate(self, raw_key: str) -> AuthContext:
        if not raw_key or not raw_key.startswith(crypto.API_KEY_PREFIX):
            raise AuthenticationError("missing or malformed API key")
        digest = crypto.hash_api_key(raw_key, self.settings.pepper_bytes())
        record = await self.store.get_api_key(digest)
        if record is None or record.status != "active":
            # Same error for unknown and revoked: do not confirm key existence.
            raise AuthenticationError("invalid API key")
        await self.store.touch_api_key(digest, int(self.clock()))
        return AuthContext(
            account_id=record.account_id,
            scopes=record.scopes,
            key_prefix=record.prefix,
        )

    @staticmethod
    def require_scope(auth: AuthContext, scope: str) -> None:
        if scope not in auth.scopes:
            raise AuthorizationError(f"API key is missing the '{scope}' scope")

    async def provision_api_key(
        self, account_id: str, *, scopes: tuple[str, ...] = ("inference",)
    ) -> str:
        raw, prefix, digest = crypto.new_api_key(self.settings.pepper_bytes())
        from .store import ApiKeyRecord

        await self.store.create_api_key(
            ApiKeyRecord(
                account_id=account_id,
                key_hash=digest,
                prefix=prefix,
                scopes=scopes,
                pepper_id=self.settings.api_key_pepper_id,
                created_at=int(self.clock()),
            )
        )
        return raw

    # ── catalogue ──────────────────────────────────────────────────────────

    def _confidential_rates(self, input_rate: int, output_rate: int) -> tuple[int, int]:
        factor = 100 + self.settings.confidential_uplift_pct
        return input_rate * factor // 100, output_rate * factor // 100

    async def list_models(self) -> list[ModelInfo]:
        records = await self.store.list_models()
        out: list[ModelInfo] = []
        for record in records:
            confidential_in, confidential_out = self._confidential_rates(
                record.input_micro_per_mtok, record.output_micro_per_mtok
            )
            out.append(
                ModelInfo(
                    model_id=record.model_id,
                    name=record.name,
                    format=record.format,
                    manifest_hash=record.manifest_hash,
                    size_bytes=record.size_bytes,
                    required_vram_gb=record.required_vram_gb,
                    license_note=record.license_note,
                    allowlisted=record.allowlisted,
                    input_micro_per_mtok=record.input_micro_per_mtok,
                    output_micro_per_mtok=record.output_micro_per_mtok,
                    confidential_input_micro_per_mtok=confidential_in,
                    confidential_output_micro_per_mtok=confidential_out,
                    launch_pricing_until=self.settings.launch_pricing_until,
                )
            )
        return out

    def _price_table(self, record: Any) -> PriceTable:
        history = getattr(record, "history", None) or []
        rows = [
            ModelPrice(
                model_id=record.model_id,
                input_micro_per_mtok=input_rate,
                output_micro_per_mtok=output_rate,
                effective_from=effective_from,
            )
            for effective_from, input_rate, output_rate in history
        ]
        if not rows:
            rows = [
                ModelPrice(
                    model_id=record.model_id,
                    input_micro_per_mtok=record.input_micro_per_mtok,
                    output_micro_per_mtok=record.output_micro_per_mtok,
                    effective_from=record.effective_from,
                )
            ]
        return PriceTable(rows)

    # ── placement ──────────────────────────────────────────────────────────

    async def place(
        self, auth: AuthContext, request: PlacementRequest
    ) -> TicketResponse:
        self.require_scope(auth, "inference")
        rate_result = await self._rate_limiter.allow(
            f"placement:{auth.account_id}:{request.model}", cost=1
        )
        if not rate_result.allowed:
            raise RateLimitedError(
                "rate limit exceeded for this model",
                code="rate_limited",
                details={"retry_after_seconds": rate_result.retry_after_seconds},
            )
        if request.max_tokens > self.settings.max_tokens_per_request:
            raise ValidationError(
                "max_tokens exceeds the configured limit",
                code="token_limit_exceeded",
                details={"limit": self.settings.max_tokens_per_request},
            )

        model = await self.store.get_model(request.model)
        if model is None:
            raise NotFoundError("unknown model", details={"model": request.model})
        if not model.allowlisted:
            raise AuthorizationError("model is not allowlisted for inference")

        now = int(self.clock())
        lock_until = await self.store.account_price_lock(auth.account_id)
        quote = self._price_table(model).quote(
            request.model,
            input_tokens=request.prompt_tokens_estimate,
            output_tokens=request.max_tokens,
            confidential=request.confidential,
            at=now,
            uplift_pct=self.settings.confidential_uplift_pct,
            price_lock_until=lock_until,
        )
        if (
            request.max_price_micro is not None
            and quote.total_micro > request.max_price_micro
        ):
            raise ValidationError(
                "quoted price exceeds max_price_micro",
                code="price_cap_exceeded",
                details={"quoted_micro": quote.total_micro},
            )

        # Liveness gate: a node that stopped heartbeating is gone. Without this
        # the scheduler kept handing tickets to dead workers (every job failed
        # at the first streamed byte until the node was manually removed).
        nodes = [
            node
            for node in await self.store.list_nodes()
            if node.last_heartbeat is not None
            and now - int(node.last_heartbeat) <= HEARTBEAT_TIMEOUT_SECONDS
        ]
        candidates = [self._candidate(node) for node in nodes]
        selection = select_worker(
            candidates,
            Requirements(
                model=request.model,
                vram_gb=request.vram_gb or model.required_vram_gb,
                region=request.region,
                latency_target_ms=request.latency_target_ms,
                max_price_micro=request.max_price_micro,
                confidential=request.confidential,
                allow_simulated_attestation=(
                    self.settings.allow_simulated_attestation
                    and not self.settings.is_production
                ),
            ),
            now=now,
        )
        worker = selection.worker
        node = next(n for n in nodes if n.node_id == worker.node_id)

        # The hold reserves the WORST CASE, so it must cover the tokens a real
        # engine reports (chat-template overhead included) — the client estimate
        # cannot know that offset. The quoted price stays exact: the headroom
        # only widens the reservation, and unused reservation is released at
        # settlement.
        hold_extra_tokens = self.settings.hold_input_margin_tokens + (
            request.prompt_tokens_estimate * self.settings.hold_input_margin_pct // 100
        )
        hold_micro = quote.total_micro + ceil_div(
            hold_extra_tokens * quote.input_rate, MICRO_PER_MTOK
        )

        job_id = uuid.uuid4().hex
        ticket_document, ticket = self.tickets.issue(
            job_id=job_id,
            account_id=auth.account_id,
            node_id=node.node_id,
            model=request.model,
            worker_public_key=node.session_public_key,
            confidentiality=Confidentiality(node.confidentiality),
            max_tokens=request.max_tokens,
            max_price_micro=quote.total_micro,
            now=now,
        )

        await self.store.place_hold(job_id, auth.account_id, hold_micro)
        job = JobRecord(
            job_id=job_id,
            account_id=auth.account_id,
            node_id=node.node_id,
            model=request.model,
            manifest_hash=model.manifest_hash,
            status="placed",
            tier=ticket.tier,
            confidentiality=node.confidentiality,
            max_tokens=request.max_tokens,
            max_price_micro=quote.total_micro,
            prompt_tokens_estimate=request.prompt_tokens_estimate,
            hold_micro=hold_micro,
            ticket_hash=hashlib.sha256(
                crypto.canonical_json(ticket_document)
            ).hexdigest(),
            price_effective_from=quote.price_effective_from,
            split=self._split(),
            input_micro_per_mtok=quote.input_rate,
            output_micro_per_mtok=quote.output_rate,
            created_at=now,
        )
        try:
            await self.store.create_job(job)
        except Exception:
            release = getattr(self.store, "release_hold", None)
            if release is not None:
                await release(job_id)
            raise

        await self.store.audit(
            "job.placed",
            actor=auth.account_id,
            subject=job_id,
            detail={
                "node_id": node.node_id,
                "model": request.model,
                "tier": ticket.tier,
                "hold_micro": hold_micro,
                "quoted_micro": quote.total_micro,
            },
        )
        logger.info(
            "placed job %s model=%s node=%s tier=%s hold_micro=%s",
            job_id,
            request.model,
            node.node_id,
            ticket.tier,
            hold_micro,
        )
        return TicketResponse(
            job_id=job_id,
            model=request.model,
            confidentiality=Confidentiality(node.confidentiality),
            confidentiality_notice=CONFIDENTIALITY_NOTICES[node.confidentiality],
            worker_endpoint=node.endpoint,
            worker_public_key=node.session_public_key,
            key_attestation_ref=node.attestation_ref,
            expires_at=ticket.expires_at,
            tier=ticket.tier,
            ticket=ticket_document,
            coordinator_key_id=self.keyring.active.key_id,
        )

    async def expire_stale_jobs(self) -> list[str]:
        """Fail jobs whose worker never reported and release their holds."""
        expired = await self.store.expire_stale_jobs(JOB_TTL_SECONDS)
        if expired:
            await self.store.audit(
                "job.expired",
                actor="coordinator",
                subject=",".join(expired[:20]),
                detail={"count": len(expired), "ttl_seconds": JOB_TTL_SECONDS},
            )
        return expired

    def _split(self) -> Split:
        return Split(
            compute_pct=self.settings.compute_share_pct,
            seeder_pct=self.settings.seeder_share_pct,
            platform_pct=self.settings.platform_share_pct,
        )

    @staticmethod
    def _candidate(node: NodeRecord) -> WorkerCandidate:
        return WorkerCandidate(
            node_id=node.node_id,
            owner_id=node.owner_id or "",
            node_type=NodeType(node.node_type),
            models=frozenset(node.models),
            vram_gb=node.vram_gb,
            region=node.region,
            price_micro_per_mtok=node.price_micro_per_mtok,
            reputation=node.reputation,
            load=node.load,
            latency_ms_p95=node.latency_ms_p95,
            healthy=node.healthy,
            suspended=node.suspended,
            confidentiality=Confidentiality(node.confidentiality),
            attestation_expires_at=node.attestation_expires_at,
            attestation_verdict=node.attestation_verdict,
        )

    # ── node registry ──────────────────────────────────────────────────────

    async def register_node(
        self, auth: AuthContext, envelope: SignedEnvelope
    ) -> NodeRegistrationResponse:
        self.require_scope(auth, "worker")
        payload = WorkerRegistrationPayload(**envelope.payload)
        self._verify_self_signed(
            envelope, NODE_REGISTER_DOMAIN, payload.identity_public_key
        )
        node_id = (
            "node_"
            + hashlib.sha256(payload.identity_public_key.encode("utf-8")).hexdigest()[
                :24
            ]
        )
        node = NodeRecord(
            node_id=node_id,
            owner_id=auth.account_id,
            identity_public_key=payload.identity_public_key,
            identity_key_id=crypto.key_id_for(payload.identity_public_key),
            session_public_key=payload.session_public_key,
            node_type=payload.node_type.value,
            models=payload.models,
            gpu_model=payload.gpu_model,
            vram_gb=payload.vram_gb,
            region=payload.region,
            price_micro_per_mtok=payload.price_micro_per_mtok,
            confidentiality=payload.confidentiality.value,
            software_version=payload.software_version,
            endpoint=payload.endpoint,
        )
        await self.store.register_node(node)
        await self.store.audit(
            "node.registered",
            actor=auth.account_id,
            subject=node_id,
            detail={"confidentiality": node.confidentiality, "models": payload.models},
        )
        return NodeRegistrationResponse(
            node_id=node_id,
            identity_key_id=node.identity_key_id,
            session_key_id=crypto.key_id_for(payload.session_public_key),
            confidentiality=payload.confidentiality,
            confidentiality_notice=CONFIDENTIALITY_NOTICES[
                payload.confidentiality.value
            ],
            attestation_required=payload.confidentiality
            is Confidentiality.ATTESTED_TEE,
            registered_at=int(self.clock()),
        )

    async def heartbeat(self, envelope: SignedEnvelope) -> dict:
        payload = HeartbeatPayload(**envelope.payload)
        node = await self.store.get_node(payload.node_id)
        if node is None:
            raise NotFoundError("unknown node")
        self._verify_self_signed(envelope, HEARTBEAT_DOMAIN, node.identity_public_key)
        load = min(
            1.0, payload.queue_depth / max(1, self.settings.max_worker_concurrency)
        )
        await self.store.update_node_heartbeat(
            node.node_id,
            load=load,
            healthy=payload.relay_reachable,
            at=int(self.clock()),
            endpoint="",
        )
        return {"node_id": node.node_id, "acknowledged_at": int(self.clock())}

    async def attest(self, auth: AuthContext, envelope: SignedEnvelope) -> dict:
        self.require_scope(auth, "worker")
        evidence = AttestationEvidencePayload(**envelope.payload)
        node = await self.store.get_node(evidence.node_id)
        if node is None:
            raise NotFoundError("unknown node")
        if node.owner_id and node.owner_id != auth.account_id:
            raise AuthorizationError("node belongs to a different account")
        self._verify_self_signed(
            envelope, ATTESTATION_EVIDENCE_DOMAIN, node.identity_public_key
        )
        expected_hash = crypto.sha256_hex(crypto.b64d(node.session_public_key))
        if not crypto.constant_time_eq(expected_hash, evidence.session_key_sha256):
            raise AuthorizationError(
                "attestation is not bound to this node's current session key",
                code="key_binding_mismatch",
            )
        verdict, document = self.attestation.attest(evidence)
        await self.store.save_attestation(
            node.node_id,
            platform=verdict.platform.value,
            status=verdict.status,
            verifier=verdict.verifier,
            session_key_sha256=verdict.session_key_sha256,
            evidence_hash=crypto.sha256_hex(evidence.report_b64.encode("utf-8")),
            verdict_hash=hashlib.sha256(crypto.canonical_json(document)).hexdigest(),
            issued_at=verdict.issued_at,
            expires_at=verdict.expires_at,
        )
        await self.store.audit(
            "node.attested",
            actor=auth.account_id,
            subject=node.node_id,
            detail={"status": verdict.status, "verifier": verdict.verifier},
        )
        return document

    def _verify_self_signed(
        self, envelope: SignedEnvelope, domain: str, public_key: str
    ) -> None:
        expected_key_id = crypto.key_id_for(public_key)
        if not crypto.constant_time_eq(envelope.key_id, expected_key_id):
            raise AuthorizationError("signing key id does not match the node key")
        if not crypto.verify(domain, envelope.payload, envelope.sig, public_key):
            raise AuthorizationError(
                "request signature verification failed", code="bad_signature"
            )

    # ── receipts and settlement ────────────────────────────────────────────

    async def submit_receipt(self, envelope: SignedEnvelope) -> SettlementResponse:
        payload = envelope.payload
        node_id = payload.get("node_id")
        if not isinstance(node_id, str):
            raise ReceiptError("receipt is missing node_id")
        node = await self.store.get_node(node_id)
        if node is None:
            raise ReceiptError("receipt is from an unknown node", code="unknown_node")
        if not crypto.constant_time_eq(envelope.key_id, node.identity_key_id):
            raise ReceiptError("receipt key id does not match the node identity key")
        # The domain is fixed by the wire format, never taken from the request.
        if not crypto.verify(
            RECEIPT_DOMAIN, payload, envelope.sig, node.identity_public_key
        ):
            raise ReceiptError(
                "receipt signature verification failed", code="bad_signature"
            )
        document = {
            "key_id": envelope.key_id,
            "alg": envelope.alg,
            "domain": RECEIPT_DOMAIN,
            "payload": payload,
            "sig": envelope.sig,
        }

        job_id = payload.get("job_id")
        if not isinstance(job_id, str):
            raise ReceiptError("receipt is missing job_id")
        job = await self.store.get_job(job_id)
        if job is None:
            raise NotFoundError("unknown job")
        if job.status == "settled":
            return self._settlement_response(job, "already settled")
        if job.node_id != node.node_id:
            raise ReceiptError(
                "receipt is from a node that was not assigned this job",
                code="receipt_node_mismatch",
            )
        if int(self.clock()) > job.created_at + JOB_TTL_SECONDS:
            # Terminal state must also free the reservation, or the customer's
            # credits stay locked with no job left that could ever settle.
            await self.store.mark_job(job_id, status="expired")
            await self.store.release_hold(job_id)
            raise ReceiptError("job has expired", code="job_expired")

        receipt = verify_receipt(
            document,
            worker_public_key=node.identity_public_key,
            job=JobContext(
                job_id=job.job_id,
                account_id=job.account_id,
                node_id=job.node_id,
                model=job.model,
                manifest_hash=job.manifest_hash,
                max_tokens=job.max_tokens,
            ),
        )
        existing = await self.store.get_receipt(job_id)
        if existing is not None:
            # Idempotent retry of the same signed receipt.
            if crypto.sha256_hex(crypto.canonical_json(existing)) != crypto.sha256_hex(
                crypto.canonical_json(document)
            ):
                raise ConflictError("a different receipt already exists for this job")
        else:
            await self.store.save_receipt(job_id, "worker", node.node_id, document)
        job.receipt = document
        return await self._reconcile_and_settle(job, receipt)

    async def submit_ack(
        self, auth: AuthContext, job_id: str, ack_payload: AckPayload
    ) -> SettlementResponse:
        job = await self.store.get_job(job_id)
        if job is None:
            raise NotFoundError("unknown job")
        if job.account_id != auth.account_id:
            # Object-level authorisation: another account's job is a 404, not a 403,
            # so job ids cannot be probed.
            raise NotFoundError("unknown job")
        if ack_payload.job_id != job_id:
            raise ValidationError("ack does not match the job", code="ack_mismatch")
        existing = await self.store.get_ack(job_id)
        if existing is None:
            await self.store.save_receipt(
                job_id, "client", job.node_id, ack_payload.model_dump(mode="json")
            )
        elif existing != ack_payload.model_dump(mode="json"):
            raise ConflictError("a different ack already exists for this job")
        if job.status == "settled":
            return self._settlement_response(job, "already settled")
        receipt_doc = job.receipt or await self.store.get_receipt(job_id)
        receipt = (
            ReceiptPayload(**receipt_doc["payload"])
            if receipt_doc is not None
            else None
        )
        return await self._reconcile_and_settle(job, receipt)

    def _cost(self, job: JobRecord, input_tokens: int, output_tokens: int) -> int:
        return ceil_div(
            input_tokens * job.input_micro_per_mtok, MICRO_PER_MTOK
        ) + ceil_div(output_tokens * job.output_micro_per_mtok, MICRO_PER_MTOK)

    async def _reconcile_and_settle(
        self, job: JobRecord, receipt: ReceiptPayload | None
    ) -> SettlementResponse:
        ack_doc = await self.store.get_ack(job.job_id)
        ack = AckPayload(**ack_doc) if ack_doc is not None else None
        decision: Reconciliation = reconcile(
            receipt, ack, tolerance_pct=self.settings.receipt_token_tolerance_pct
        )
        if decision.status == "awaiting_ack":
            return SettlementResponse(
                job_id=job.job_id,
                status=JobStatus.RUNNING,
                billable_input_tokens=0,
                billable_output_tokens=0,
                total_micro=0,
                compute_micro=0,
                seeder_micro=0,
                platform_micro=0,
                reason=decision.reason,
            )

        total = self._cost(job, decision.input_tokens, decision.output_tokens)
        if total > job.hold_micro:
            # A worker cannot bill past the reservation that was placed for the
            # job; treat it as a dispute rather than settling an overage.
            decision = Reconciliation(
                "disputed",
                decision.input_tokens,
                decision.output_tokens,
                reason="settlement_exceeds_hold",
            )
        if decision.status == "disputed":
            await self.store.settle_job(
                job,
                input_tokens=decision.input_tokens,
                output_tokens=decision.output_tokens,
                total_micro=total,
                dispute_reason=decision.reason,
            )
            # A dispute is unresolved, so nobody is paid: the customer is not
            # charged and the host is not credited until review. The RESERVATION
            # must still be released — it used to stay open forever (H-1),
            # freezing the customer's credits for a job that never settled.
            await self.store.release_hold(job.job_id)
            await self.store.audit(
                "job.disputed",
                subject=job.job_id,
                detail={
                    "reason": decision.reason,
                    "total_micro": total,
                    "hold_released_micro": job.hold_micro,
                },
            )
            return SettlementResponse(
                job_id=job.job_id,
                status=JobStatus.DISPUTED,
                billable_input_tokens=decision.input_tokens,
                billable_output_tokens=decision.output_tokens,
                total_micro=0,
                compute_micro=0,
                seeder_micro=0,
                platform_micro=0,
                reason=decision.reason,
            )

        await self.store.settle_job(
            job,
            input_tokens=decision.input_tokens,
            output_tokens=decision.output_tokens,
            total_micro=total,
            dispute_reason=None,
        )
        await self.store.audit(
            "job.settled",
            subject=job.job_id,
            detail={
                "total_micro": total,
                "compute_micro": job.compute_micro,
                "seeder_micro": job.seeder_micro,
                "platform_micro": job.platform_micro,
            },
        )
        return self._settlement_response(job, None)

    @staticmethod
    def _settlement_response(job: JobRecord, reason: str | None) -> SettlementResponse:
        return SettlementResponse(
            job_id=job.job_id,
            status=JobStatus(job.status),
            billable_input_tokens=job.input_tokens,
            billable_output_tokens=job.output_tokens,
            total_micro=job.total_micro,
            compute_micro=job.compute_micro,
            seeder_micro=job.seeder_micro,
            platform_micro=job.platform_micro,
            reason=reason,
        )

    # ── account ────────────────────────────────────────────────────────────

    async def balance(self, auth: AuthContext) -> BalanceResponse:
        micro = await self.store.balance_micro(auth.account_id)
        held = await self.store.held_micro(auth.account_id)
        return BalanceResponse(
            account_id=auth.account_id,
            credits=float(micro_to_credits(micro)),
            micro_credits=micro,
            held_micro=held,
            available_micro=micro - held,
        )

    async def usage(self, auth: AuthContext, limit: int = 100) -> UsageResponse:
        jobs = await self.store.list_jobs(
            auth.account_id, limit=max(1, min(limit, 500))
        )
        records = [
            UsageRecord(
                job_id=job.job_id,
                model=job.model,
                status=job.status,
                input_tokens=job.input_tokens,
                output_tokens=job.output_tokens,
                total_micro=job.total_micro,
                created_at=job.created_at,
            )
            for job in jobs
        ]
        return UsageResponse(
            account_id=auth.account_id,
            jobs=records,
            total_micro=sum(record.total_micro for record in records),
        )

    async def grant_signup_credits(self, account_id: str, ip_hash: str) -> int:
        if self.settings.signup_credits <= 0:
            return 0
        grants = await self.store.count_grants_for_ip(ip_hash)
        if grants >= self.settings.signup_ip_cap:
            raise ConflictError(
                "signup credit limit reached for this network",
                code="signup_credit_limit",
            )
        first = await self.store.record_signup_grant(account_id, ip_hash)
        if not first:
            return 0
        amount = self.settings.signup_credits * 1_000_000
        await self.store.credit(
            account_id,
            amount,
            source=SIGNUP_GRANTS,
            idempotency_key=f"signup:{account_id}",
            kind="signup_credit",
        )
        await self.store.audit(
            "account.signup_credit",
            actor=account_id,
            subject=account_id,
            detail={"micro": amount},
        )
        return amount

    # ── credit purchases ───────────────────────────────────────────────────

    async def handle_payment_webhook(self, body: bytes, signature: str) -> dict:
        event: PaymentEvent = self.payment_provider.verify_webhook(body, signature)
        await self.store.credit(
            event.account_id,
            event.amount_micro,
            source=f"gateway:{event.provider}:clearing",
            idempotency_key=f"payment:{event.provider}:{event.reference}",
            kind="credit_purchase",
        )
        await self.store.audit(
            "billing.credit_purchase",
            actor=event.account_id,
            subject=event.reference,
            detail={"micro": event.amount_micro, "provider": event.provider},
        )
        return {
            "status": "credited",
            "reference": event.reference,
            "amount_micro": event.amount_micro,
            "balance_micro": await self.store.balance_micro(event.account_id),
        }

    # ── redundant-execution verification ───────────────────────────────────

    def verification_decision(self, job_id: str):
        """Deterministic: an auditor can recompute which jobs were sampled."""
        return self.verification.decide(job_id)

    async def record_verification(
        self, job_id: str, outputs: dict[str, str]
    ) -> VerificationOutcome:
        """Resolve independent executions; penalise and possibly suspend the faulty node.

        This records the *resolution* of a sampled job. Scheduling the second
        execution itself is an orchestration concern documented as a limitation
        because it requires a real second worker; the comparison, majority
        tiebreak and penalty path are complete and tested.
        """
        outcome = resolve_executions(job_id=job_id, outputs=outputs)
        for node_id in outcome.faulty_nodes:
            await self.store.penalize_node(node_id, reputation_delta=-20, suspend=False)
            await self.store.audit(
                "node.verification_failed",
                subject=node_id,
                detail={"job_id": job_id, "detail": outcome.detail},
            )
        if outcome.faulty_nodes and len(outcome.faulty_nodes) >= 2:
            # Two independent mismatches against one worker is enough evidence to
            # stop routing to it pending review.
            for node_id in outcome.faulty_nodes:
                await self.store.penalize_node(
                    node_id, reputation_delta=0, suspend=True
                )
        return outcome

    # ── coordinator keys ───────────────────────────────────────────────────

    def keys(self) -> dict[str, Any]:
        return {
            "bundle": self.keyring.public_bundle(),
            "revocations": self.keyring.revocation_list(),
        }

    async def issue_attestation_nonce(self, node_id: str) -> str:
        rate_result = await self._rate_limiter.allow(f"attest-nonce:{node_id}", cost=1)
        if not rate_result.allowed:
            raise RateLimitedError(
                "attestation nonce issuance is rate limited",
                code="rate_limited",
                details={"retry_after_seconds": rate_result.retry_after_seconds},
            )
        return self.attestation.issue_nonce(node_id)
