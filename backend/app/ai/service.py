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

import asyncio
import hashlib
import logging
import os
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from . import crypto
from .attestation import AttestationService
from .clock import now_unix
from .config import AISettings
from .errors import (
    AuthenticationError,
    AuthorizationError,
    ConcurrencyLimitExceeded,
    ConflictError,
    NoEligibleWorker,
    NotFoundError,
    RateLimitedError,
    ReceiptError,
    ValidationError,
)
from .keyring import KeyRing
from .ledger import (
    PLATFORM_REVENUE,
    SIGNUP_GRANTS,
    Split,
    compute_earnings,
    split_amounts,
)
from .models import (
    AckPayload,
    AttestationEvidencePayload,
    BalanceResponse,
    Confidentiality,
    GatewayMeasurement,
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
    parse_signed_payload,
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
from .signup_policy import (
    canonical_email,
    email_domain,
    email_hash,
    is_disposable_domain,
)
from .signup_policy import device_hash as device_bucket
from .store import JobRecord, NodeRecord, Store, VerificationRecord
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
            self.attestation.configure_redis(
                on_failure=self._note_redis_failure, redis_client=redis_client
            )
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
                on_failure=self._note_redis_failure,
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
        self.verification = VerificationSampler(
            settings.verification_sample_pct,
            secret=settings.verification_sampling_secret(),
        )
        # Kept for the shared daily-cap counter (and for anything else that needs
        # cross-instance state); None in dev/test without Redis.
        self._redis = redis_client
        # Daily dispatch cap so a sample rate cannot turn into an unbounded
        # second-inference bill. Shared through Redis when present, so two
        # coordinator instances cannot each spend the whole cap (checklist 5.4);
        # the process-local fallback is for deployments without Redis.
        self._verification_count = 0
        self._verification_day = -1
        # Redis failures observed by the synchronous security paths (ticket
        # claim, nonce consume, set-if-absent). Those paths run either inside
        # the event loop or on the sync-bridge helper thread, so the audit write
        # is scheduled when a loop is running locally and otherwise drained by
        # flush_redis_failures() (the background sweep calls it every tick).
        self.redis_failures: deque[dict[str, Any]] = deque(maxlen=200)
        self._redis_audit_tasks: set[asyncio.Task[Any]] = set()
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

    #: Idempotency keys are namespaced per operation so a key can never replay a
    #: different kind of creation request (a key used for a placement must not
    #: shadow an API-key creation with the same value).
    _IDEMPOTENCY_SCOPE = "placement"

    async def place(
        self,
        auth: AuthContext,
        request: PlacementRequest,
        *,
        idempotency_key: str | None = None,
    ) -> TicketResponse:
        """Place a job, replaying the original ticket for a repeated key.

        A client that retries after a dropped response must not be charged a
        second hold, and a retry must not be able to place a *different* job
        under the same key. Both are enforced here rather than in the route, so
        every caller (SDK, gateway, HTTP) gets the same guarantee.
        """
        if idempotency_key is None:
            return await self._place(auth, request)
        if not 1 <= len(idempotency_key) <= 128:
            raise ValidationError(
                "Idempotency-Key must be 1-128 characters",
                code="invalid_idempotency_key",
            )
        request_hash = hashlib.sha256(
            crypto.canonical_json(request.model_dump(mode="json"))
        ).hexdigest()
        existing = await self.store.reserve_idempotency(
            auth.account_id, self._IDEMPOTENCY_SCOPE, idempotency_key, request_hash
        )
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise ConflictError(
                    "Idempotency-Key was already used with a different request body",
                    code="idempotency_key_reused",
                )
            if existing["response"] is None:
                # The original request is still running. Returning a half-built
                # job would be worse than asking the client to retry once.
                raise ConflictError(
                    "a request with this Idempotency-Key is still in progress",
                    code="idempotency_in_progress",
                )
            return TicketResponse.model_validate(existing["response"])
        try:
            response = await self._place(auth, request)
        except BaseException:
            # A failed attempt must not burn the key: the client's retry is a new
            # attempt at the same request, not a replay of a response.
            await self.store.clear_idempotency(
                auth.account_id, self._IDEMPOTENCY_SCOPE, idempotency_key
            )
            raise
        await self.store.complete_idempotency(
            auth.account_id,
            self._IDEMPOTENCY_SCOPE,
            idempotency_key,
            response.model_dump(mode="json"),
        )
        return response

    async def _place(
        self, auth: AuthContext, request: PlacementRequest
    ) -> TicketResponse:
        self.require_scope(auth, "inference")
        # Concurrency cap (row 2.6): a key may hold at most
        # ``max_concurrent_jobs_per_key`` un-settled jobs. Counted from the store
        # so it is the account's number on every instance, and placed before any
        # pricing or hold work so a capped client cannot spend anything to be
        # told to stop. Abandoned jobs stop counting when the TTL sweep expires
        # them, which is what keeps a crashed client from wedging its own key.
        active = await self.store.count_active_jobs(auth.account_id)
        if active >= self.settings.max_concurrent_jobs_per_key:
            raise ConcurrencyLimitExceeded(
                "too many jobs in flight for this key",
                details={
                    "limit": self.settings.max_concurrent_jobs_per_key,
                    "in_flight": active,
                    "ttl_seconds": JOB_TTL_SECONDS,
                },
            )
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
        requirements = Requirements(
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
        )
        selection = select_worker(candidates, requirements, now=now)
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
        await self._schedule_verification(
            job_id=job_id,
            account_id=auth.account_id,
            primary_node=node,
            candidates=candidates,
            requirements=requirements,
            request=request,
            quote=quote,
            now=now,
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

    # ── redundant execution (checklist 6.1) ────────────────────────────────

    async def _schedule_verification(
        self,
        *,
        job_id: str,
        account_id: str,
        primary_node: NodeRecord,
        candidates: list[WorkerCandidate],
        requirements: Requirements,
        request: PlacementRequest,
        quote: Any,
        now: int,
    ) -> None:
        """Decide the sample and issue a second ticket to a *different* operator.

        Sampling is deterministic on the job id (an attacker cannot influence it
        by retrying), the second node is excluded by id (never the same
        operator), and the second ticket is an ordinary signed ticket with its
        own job id and no hold — the verification run is priced at zero because
        the customer already paid for one execution.
        """
        decision = self.verification.decide(job_id)
        if not decision.sampled:
            return
        if not await self._claim_verification_slot(now):
            await self.store.save_verification(
                VerificationRecord(
                    job_id=job_id,
                    account_id=account_id,
                    primary_node_id=primary_node.node_id,
                    verification_job_id=f"v{uuid.uuid4().hex}",
                    verification_node_id="",
                    status="daily_cap_reached",
                    created_at=now,
                )
            )
            return

        try:
            selection = select_worker(
                candidates,
                requirements,
                now=now,
                exclude=frozenset({primary_node.node_id}),
            )
        except NoEligibleWorker:
            await self.store.save_verification(
                VerificationRecord(
                    job_id=job_id,
                    account_id=account_id,
                    primary_node_id=primary_node.node_id,
                    verification_job_id=f"v{uuid.uuid4().hex}",
                    verification_node_id="",
                    status="no_alternate_node",
                    created_at=now,
                )
            )
            await self.store.audit(
                "job.verification_skipped",
                actor=account_id,
                subject=job_id,
                detail={"reason": "no_alternate_node", "model": request.model},
            )
            return

        second = await self.store.get_node(selection.worker.node_id)
        if second is None:  # pragma: no cover - the candidate came from the store
            return
        verification_job_id = f"v{uuid.uuid4().hex}"
        ticket_document, _ticket = self.tickets.issue(
            job_id=verification_job_id,
            account_id=account_id,
            node_id=second.node_id,
            model=request.model,
            worker_public_key=second.session_public_key,
            confidentiality=Confidentiality(second.confidentiality),
            max_tokens=request.max_tokens,
            max_price_micro=quote.total_micro,
            now=now,
        )
        await self.store.save_verification(
            VerificationRecord(
                job_id=job_id,
                account_id=account_id,
                primary_node_id=primary_node.node_id,
                verification_job_id=verification_job_id,
                verification_node_id=second.node_id,
                status="scheduled",
                created_at=now,
                ticket=ticket_document,
            )
        )
        await self.store.audit(
            "job.verification_scheduled",
            actor=account_id,
            subject=job_id,
            detail={
                "bucket": decision.bucket,
                "primary_node_id": primary_node.node_id,
                "verification_node_id": second.node_id,
                "verification_job_id": verification_job_id,
            },
        )

    async def _claim_verification_slot(self, now: int) -> bool:
        """Take one slot from the shared per-UTC-day dispatch budget.

        Redis-backed when available (INCR + a TTL past midnight), so N coordinator
        instances share one cap instead of N caps. Without Redis the in-process
        counter is used, which is the documented single-instance limitation.
        """
        cap = self.settings.verification_daily_cap
        if cap <= 0:
            return False
        if self._redis is not None:
            key = f"tcdn:ai:verification:dispatches:{int(now) // 86_400}"
            try:
                claimed = await self._redis.incr(key)
                if claimed == 1:
                    await self._redis.expire(key, 2 * 86_400)
                return int(claimed) <= cap
            except Exception:
                logger.warning(
                    "verification dispatch counter unavailable in Redis; "
                    "falling back to the process-local counter"
                )
        today = int(now) // 86_400
        if today != self._verification_day:
            self._verification_day = today
            self._verification_count = 0
        self._verification_count += 1
        return self._verification_count <= cap

    async def verification_assignment(self, job_id: str) -> dict[str, Any] | None:
        """Hand the caller a ready-to-send verification run, once.

        The gateway is the only caller with the plaintext, so it owns the
        dispatch; the coordinator only supplies the signed ticket and the node
        to send it to. Asking twice returns None — a job cannot be dispatched
        to two verifiers.
        """
        record = await self.store.get_verification(job_id)
        if record is None or record.status != "scheduled" or not record.ticket:
            return None
        node = await self.store.get_node(record.verification_node_id)
        if node is None or not node.endpoint:
            await self.store.update_verification(job_id, status="no_alternate_endpoint")
            return None
        await self.store.update_verification(job_id, status="dispatched")
        await self.store.audit(
            "job.verification_dispatched",
            subject=job_id,
            detail={
                "verification_job_id": record.verification_job_id,
                "verification_node_id": record.verification_node_id,
            },
        )
        return {
            "job_id": record.job_id,
            "verification_job_id": record.verification_job_id,
            "node_id": record.verification_node_id,
            "endpoint": node.endpoint,
            "worker_public_key": node.session_public_key,
            "ticket": record.ticket,
        }

    async def submit_verification_receipt(self, envelope: SignedEnvelope) -> dict:
        """Reconcile a verification run's receipt against the primary result.

        No money moves here: the verification run has no hold and never settles.
        Its only product is the comparison, and the penalty path it feeds is the
        existing one (`record_verification`).
        """
        shadow_job_id = str(envelope.payload.get("job_id", ""))
        record = await self.store.get_verification_by_shadow(shadow_job_id)
        if record is None:
            raise NotFoundError("unknown verification job")
        node = await self.store.get_node(record.verification_node_id)
        if node is None:
            raise NotFoundError("the verification node is no longer registered")
        self._verify_self_signed(envelope, RECEIPT_DOMAIN, node.identity_public_key)

        shadow_hash = envelope.payload.get("output_sha256")
        if not isinstance(shadow_hash, str) or len(shadow_hash) != 64:
            raise ReceiptError(
                "verification receipt is missing output_sha256", code="bad_receipt"
            )
        if record.shadow_output_sha256 is not None and not crypto.constant_time_eq(
            record.shadow_output_sha256, shadow_hash
        ):
            raise ConflictError(
                "a different verification receipt already exists for this job",
                code="duplicate_receipt",
            )
        input_tokens = self._receipt_token(envelope.payload, "input_tokens")
        output_tokens = self._receipt_token(envelope.payload, "output_tokens")
        await self.store.update_verification(
            record.job_id, shadow_output_sha256=shadow_hash
        )
        paid = await self._pay_verification_node(
            record,
            node,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        # Whichever run finishes second triggers the comparison; the hash is
        # persisted so a restart between the two does not lose the sample.
        result = await self._reconcile_verification(record.job_id)
        result["payout_micro"] = paid
        return result

    @staticmethod
    def _receipt_token(payload: dict[str, Any], name: str) -> int:
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReceiptError(
                f"verification receipt is missing {name}", code="bad_receipt"
            )
        return value

    async def _pay_verification_node(
        self,
        record: VerificationRecord,
        node: NodeRecord,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> int:
        """Pay the operator that ran the shadow execution, out of platform revenue.

        The verification run is a real inference on a real machine, so paying
        nobody would make honesty a loss for the operator being checked. It is
        funded by ``platform:revenue`` — never by the customer, who paid once, and
        never out of another node's earnings — using the shadow receipt's own
        token counts at the primary job's locked rates, capped per run by
        ``verification_payout_cap_micro`` so a self-reported token count cannot
        turn into an unbounded transfer. One idempotency key per shadow job, so a
        retried receipt pays once.
        """
        job = await self.store.get_job(record.job_id)
        if job is None:
            await self.store.audit(
                "job.verification_pay_skipped",
                subject=record.job_id,
                detail={"reason": "primary_job_missing"},
            )
            return 0
        cost = self._cost(job, input_tokens, output_tokens)
        earned = split_amounts(cost, job.split).compute_micro
        amount = min(earned, self.settings.verification_payout_cap_micro)
        if amount <= 0:
            await self.store.audit(
                "job.verification_pay_skipped",
                subject=record.job_id,
                detail={"reason": "nothing_to_pay", "shadow_cost_micro": cost},
            )
            return 0
        await self.store.post_ledger(
            debit=PLATFORM_REVENUE,
            credit=compute_earnings(node.node_id),
            amount_micro=amount,
            idempotency_key=f"verification:{record.verification_job_id}:compute",
            kind="verification_compute",
            job_id=record.job_id,
        )
        await self.store.audit(
            "job.verification_paid",
            subject=record.job_id,
            detail={
                "verification_job_id": record.verification_job_id,
                "verification_node_id": node.node_id,
                "amount_micro": amount,
                "shadow_cost_micro": cost,
                "funded_from": PLATFORM_REVENUE,
                "capped": amount != earned,
            },
        )
        return amount

    async def _reconcile_verification(self, job_id: str) -> dict:
        """Compare the two outputs once both receipts are known."""
        record = await self.store.get_verification(job_id)
        if record is None:
            return {"status": "unknown"}
        if record.status == "completed":
            return {
                "status": "completed",
                "outcome": record.outcome,
                "faulty_nodes": [record.faulty_node_id]
                if record.faulty_node_id
                else [],
            }
        primary_receipt = await self.store.get_receipt(record.job_id)
        primary_hash = ""
        if primary_receipt:
            primary_hash = str(
                primary_receipt.get("payload", {}).get("output_sha256", "")
            )
        if not primary_hash or not record.shadow_output_sha256:
            # Only one side is in; state which side is missing rather than
            # guessing an outcome. The other submission completes the pair.
            await self.store.update_verification(
                job_id,
                status="awaiting_primary"
                if not primary_hash
                else "awaiting_verification",
            )
            return {
                "status": "awaiting_primary"
                if not primary_hash
                else "awaiting_verification"
            }

        outcome = await self.record_verification(
            record.job_id,
            {
                record.primary_node_id: primary_hash,
                record.verification_node_id: record.shadow_output_sha256,
            },
        )
        await self.store.update_verification(
            record.job_id,
            status="completed",
            outcome=outcome.status,
            faulty_node_id=(outcome.faulty_nodes[0] if outcome.faulty_nodes else None),
            completed_at=int(self.clock()),
        )
        await self.store.audit(
            "job.verification_completed",
            subject=record.job_id,
            detail={
                "outcome": outcome.status,
                "faulty_nodes": list(outcome.faulty_nodes),
                "primary_node_id": record.primary_node_id,
                "verification_node_id": record.verification_node_id,
            },
        )
        return {
            "status": "completed",
            "outcome": outcome.status,
            "faulty_nodes": list(outcome.faulty_nodes),
        }

    async def _note_undispatched_verification(self, job_id: str) -> None:
        """Mark a sampled job that finished without a verification run.

        A receipt arriving over the SDK path means the client did the work and
        the coordinator never held the plaintext, so it cannot re-run the job.
        Recording that honestly is the difference between "unsampled" and
        "sampled but unverifiable" — the latter is the coverage gap 6.1 names.
        """
        record = await self.store.get_verification(job_id)
        if record is None or record.status != "scheduled":
            return
        await self.store.update_verification(job_id, status="not_dispatched_sdk_mode")
        await self.store.audit(
            "job.verification_skipped",
            subject=job_id,
            detail={"reason": "not_dispatched_sdk_mode"},
        )

    async def fail_verification(self, job_id: str, reason: str) -> None:
        """Record that a scheduled verification could not be dispatched."""
        record = await self.store.get_verification(job_id)
        if record is None or record.status == "completed":
            return
        await self.store.update_verification(job_id, status="dispatch_failed")
        await self.store.audit(
            "job.verification_failed",
            subject=job_id,
            detail={"reason": reason[:300]},
        )

    # ── Redis failure signalling (the paths fail closed; say so) ───────────

    def _note_redis_failure(self, event: dict[str, Any]) -> None:
        """Alert seam for the Redis-backed security paths.

        ``note_redis_failure`` already logged at ERROR and incremented a
        counter; a log line alone is not enough when the failure is *why* every
        ticket suddenly looks like a replay. This queues the event and, when a
        loop is running here, writes the ``ai.redis_unavailable`` audit row
        immediately; otherwise the queue is drained by ``flush_redis_failures``.
        """
        self.redis_failures.append(dict(event))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Helper thread / plain sync context: the background sweep drains.
            return
        task = loop.create_task(self.flush_redis_failures())
        self._redis_audit_tasks.add(task)
        task.add_done_callback(self._redis_audit_tasks.discard)

    async def flush_redis_failures(self) -> int:
        """Write one audit row per queued Redis failure; return how many.

        Safe to call from anywhere and to call concurrently: events are removed
        from the queue before the write, so nothing is audited twice.
        """
        written = 0
        while self.redis_failures:
            event = self.redis_failures.popleft()
            detail = {
                "operation": event.get("operation", "unknown"),
                "key_fingerprint": event.get("key_fingerprint", ""),
                "error": event.get("error", "unavailable"),
                "fail_closed": True,
            }
            try:
                await self.store.audit(
                    "ai.redis_unavailable",
                    actor="coordinator",
                    subject=str(detail["operation"]),
                    detail=detail,
                )
            except Exception:
                logger.exception("ai.redis_unavailable audit write failed")
                continue
            written += 1
        return written

    async def expire_stale_jobs(self) -> list[str]:
        """Fail jobs whose worker never reported and release their holds."""
        await self.flush_redis_failures()
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
        # Typed, bounded and strict, before the signature check and before any
        # store access: a hostile payload must be a 422, never a 500 and never a
        # side effect. See docs/tcdn-ai/SECURITY.md §"signed payloads".
        payload = parse_signed_payload(WorkerRegistrationPayload, envelope)
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
        # First statement on purpose. `HeartbeatPayload(**envelope.payload)` used
        # to raise pydantic's ValidationError (not an AIError) straight out of the
        # handler: a 500 on a signature-only route, 156 of them in the ZAP run.
        payload = parse_signed_payload(HeartbeatPayload, envelope)
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
        evidence = parse_signed_payload(AttestationEvidencePayload, envelope)
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
        await self._note_undispatched_verification(job_id)
        if job.status == "settled":
            await self._reconcile_verification(job_id)
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
        receipt_doc = job.receipt or await self.store.get_receipt(job_id)
        receipt = (
            ReceiptPayload(**receipt_doc["payload"])
            if receipt_doc is not None
            else None
        )
        if job.status == "settled":
            # The normal order: the receipt already billed the job before the
            # ack arrived. The charge stands, but a contested ack is still
            # recorded — the operator reviewing the charge needs to know the
            # payer disputed the output, and the payer cannot retract the charge
            # by acking late (or not at all).
            review = await self._record_ack_mismatch(job, receipt, ack_payload)
            if review:
                return self._settlement_response(
                    job, f"already settled; ack flagged for review: {review}"
                )
            return self._settlement_response(job, "already settled")
        return await self._reconcile_and_settle(job, receipt)

    async def submit_gateway_measurement(
        self, job_id: str, measurement: GatewayMeasurement
    ) -> SettlementResponse:
        """Record the gateway's own measurement of a completion and settle it.

        Internal to the coordinator: there is **no route** for this, so the
        measurement is never client-supplied. It is written by ``Gateway.finalize``
        in gateway mode, at the moment the completion is handed to the customer,
        and it is what makes billing authoritative — the gateway counted the
        prompt it encrypted and the tokens it decrypted, so a receipt that bills
        above that count can be disproved rather than believed.
        """
        job = await self.store.get_job(job_id)
        if job is None:
            raise NotFoundError("unknown job")
        if measurement.job_id != job_id:
            raise ValidationError(
                "gateway measurement does not match the job",
                code="measurement_mismatch",
            )
        body = measurement.model_dump(mode="json")
        existing = await self.store.get_gateway_measurement(job_id)
        if existing is not None:
            if existing != body:
                raise ConflictError(
                    "a different gateway measurement already exists for this job"
                )
        else:
            await self.store.save_receipt(job_id, "gateway", job.node_id, body)
        await self.store.audit(
            "job.gateway_measured",
            subject=job_id,
            detail={
                "prompt_tokens": measurement.prompt_tokens,
                "completion_tokens": measurement.completion_tokens,
                "token_frames": measurement.token_frames,
            },
        )

        receipt_doc = job.receipt or await self.store.get_receipt(job_id)
        receipt = (
            ReceiptPayload(**receipt_doc["payload"])
            if receipt_doc is not None
            else None
        )
        if receipt is None:
            # The worker has not reported yet. The measurement is stored either
            # way, so the receipt path settles with it when it arrives — the
            # customer never has to wait for settlement.
            return self._settlement_response(
                job, "measured; awaiting the worker receipt"
            )
        if job.status == "settled":
            return self._settlement_response(job, "already settled")
        return await self._reconcile_and_settle(job, receipt)

    async def _account_node_billing(
        self, node_id: str | None, *, overclaim: bool, mismatch: bool
    ) -> None:
        """Count a settled job against the node, and hold it for review if it is over.

        The decision is deliberately mechanical and per node (the counters live
        in the node row, shared by every instance): once a node has
        ``node_overclaim_min_samples`` settled jobs and its receipts billed above
        the gateway's own count in more than ``node_overclaim_rate_pct`` of them,
        it is suspended and its pending payouts are held. Suspending is
        reversible by an operator; paying an overclaiming node is not.
        """
        if not node_id:
            return
        try:
            node = await self.store.record_node_billing(
                node_id, overclaim=overclaim, mismatch=mismatch
            )
        except Exception:
            logger.exception("node billing counters failed for %s", node_id)
            return
        if node.payouts_held:
            return
        if node.settled_jobs < self.settings.node_overclaim_min_samples:
            return
        if node.overclaim_rate_pct < self.settings.node_overclaim_rate_pct:
            return
        reason = (
            f"receipts overclaimed the gateway measurement in "
            f"{node.overclaim_rate_pct}% of {node.settled_jobs} settled jobs"
        )
        held = await self.store.hold_node_for_review(node_id, reason)
        await self.store.audit(
            "node.held_for_review",
            subject=node_id,
            detail={
                "reason": reason,
                "settled_jobs": held.settled_jobs,
                "overclaim_events": held.overclaim_events,
                "mismatch_events": held.mismatch_events,
                "threshold_pct": self.settings.node_overclaim_rate_pct,
                "min_samples": self.settings.node_overclaim_min_samples,
                "payouts_held": True,
            },
        )

    async def _record_ack_mismatch(
        self, job: JobRecord, receipt: ReceiptPayload | None, ack: AckPayload
    ) -> str | None:
        """Flag an already-billed job whose ack does not match the receipt.

        Evidence only: nothing is charged again and nothing is refunded here.
        """
        if receipt is None:
            return None
        decision = reconcile(
            receipt, ack, tolerance_pct=self.settings.receipt_token_tolerance_pct
        )
        if decision.status != "disputed":
            return None
        reason = decision.reason or "ack_mismatch"
        if job.dispute_reason == reason:
            return reason
        await self.store.flag_job_for_review(job.job_id, reason)
        await self.store.audit(
            "job.billing_review",
            subject=job.job_id,
            detail={
                "reason": reason,
                "charged_micro": job.total_micro,
                "receipt_output_tokens": receipt.output_tokens,
                "ack_tokens_received": ack.tokens_received,
            },
        )
        job.dispute_reason = reason
        return reason

    def _cost(self, job: JobRecord, input_tokens: int, output_tokens: int) -> int:
        return ceil_div(
            input_tokens * job.input_micro_per_mtok, MICRO_PER_MTOK
        ) + ceil_div(output_tokens * job.output_micro_per_mtok, MICRO_PER_MTOK)

    async def _reconcile_and_settle(
        self, job: JobRecord, receipt: ReceiptPayload | None
    ) -> SettlementResponse:
        """Bill the job from the WORKER'S SIGNED RECEIPT.

        Billing used to require a matching client ack, which made revenue
        optional for the payer: a client that never acked, or acked a different
        hash (or a different token count), left the job unsettled — and the
        expiry sweep then released the hold, so the completion was free. The
        signed receipt is the only claim anybody in this system can be held to,
        so it is what we bill. The client ack is now evidence for review, never
        a payment trigger: it can flag the job, it cannot cancel the charge.
        """
        ack_doc = await self.store.get_ack(job.job_id)
        ack = AckPayload(**ack_doc) if ack_doc is not None else None
        if receipt is None:
            # Nothing signed to bill yet. The hold stays until the job expires,
            # which is correct: no proof of work, no charge.
            return SettlementResponse(
                job_id=job.job_id,
                status=JobStatus.RUNNING,
                billable_input_tokens=0,
                billable_output_tokens=0,
                total_micro=0,
                compute_micro=0,
                seeder_micro=0,
                platform_micro=0,
                reason="no_worker_receipt",
            )
        if job.status == "settled":
            return self._settlement_response(job, "already settled")

        decision: Reconciliation = reconcile(
            receipt, ack, tolerance_pct=self.settings.receipt_token_tolerance_pct
        )
        billed_input = receipt.input_tokens
        billed_output = receipt.output_tokens
        total = self._cost(job, billed_input, billed_output)
        review_reason = decision.reason if decision.status == "disputed" else None

        # Gateway mode: the coordinator's own gateway held the plaintext, so it
        # counted the completion itself. Where that count and the receipt's
        # disagree beyond tolerance, bill the LOWER one — a worker cannot bill
        # what the coordinator can disprove from the text it just served, and a
        # worker cannot be billed less than it claimed either (that direction is
        # recorded, not charged). This is the authority the ack must never have:
        # the ack is client-supplied, the measurement is not (no HTTP route).
        measurement = await self.store.get_gateway_measurement(job.job_id)
        overclaim = False
        if self.settings.gateway_billing_authority and measurement is not None:
            gateway_input = int(measurement.get("prompt_tokens", 0) or 0)
            gateway_output = int(measurement.get("completion_tokens", 0) or 0)
            gateway_total = self._cost(job, gateway_input, gateway_output)
            tolerance = self.settings.gateway_token_tolerance_pct
            if total > gateway_total + (gateway_total * tolerance // 100):
                billed_input, billed_output, total = (
                    gateway_input,
                    gateway_output,
                    gateway_total,
                )
                overclaim = True
                review_reason = review_reason or "node_overclaim"
            elif gateway_total > total + (total * tolerance // 100):
                # The gateway counted more than the worker claims. Bill the
                # worker's (lower) number and keep the discrepancy as evidence:
                # an under-claiming engine is a correctness signal about the
                # node, not a reason to charge the customer more.
                review_reason = review_reason or "node_receipt_underclaim"

        if total > job.hold_micro:
            # A worker cannot bill past the reservation placed for this job: the
            # excess is not billed, but it is flagged rather than refused —
            # refusing it would hand the customer the completion for free. The
            # cap is recorded alongside whatever else was already wrong with this
            # bill, so a reviewer sees every reason, not just the first.
            total = job.hold_micro
            review_reason = (
                f"{review_reason}+settlement_exceeds_hold"
                if review_reason
                else "settlement_exceeds_hold"
            )

        if total <= 0:
            # A receipt that reports no tokens: close the job and free the
            # reservation without a ledger entry (there is no split of zero).
            await self.store.mark_job(job.job_id, status="settled")
            await self.store.release_hold(job.job_id)
            await self.store.audit(
                "job.settled",
                subject=job.job_id,
                detail={"total_micro": 0, "reason": "zero_token_receipt"},
            )
            await self._reconcile_verification(job.job_id)
            job.status = "settled"
            return self._settlement_response(job, "settled with no billable tokens")

        job = await self.store.settle_job(
            job,
            input_tokens=billed_input,
            output_tokens=billed_output,
            total_micro=total,
            dispute_reason=None,
        )
        if review_reason:
            await self.store.flag_job_for_review(job.job_id, review_reason)
            await self.store.audit(
                "job.billing_review",
                subject=job.job_id,
                detail={
                    "reason": review_reason,
                    "receipt_input_tokens": receipt.input_tokens,
                    "receipt_output_tokens": receipt.output_tokens,
                    "charged_input_tokens": billed_input,
                    "charged_output_tokens": billed_output,
                    "charged_micro": total,
                    "gateway_measured": measurement is not None,
                    "ack_present": ack is not None,
                },
            )
        await self.store.audit(
            "job.settled",
            subject=job.job_id,
            detail={
                "total_micro": total,
                "compute_micro": job.compute_micro,
                "seeder_micro": job.seeder_micro,
                "platform_micro": job.platform_micro,
                "billed_from": "gateway_measurement" if overclaim else "worker_receipt",
            },
        )
        await self._account_node_billing(
            job.node_id, overclaim=overclaim, mismatch=decision.status == "disputed"
        )
        await self._reconcile_verification(job.job_id)
        if review_reason:
            return self._settlement_response(
                job,
                f"billed from the worker receipt; flagged for review: {review_reason}",
            )
        return self._settlement_response(job, "billed from the worker receipt")

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

    async def grant_signup_credits(
        self,
        account_id: str,
        ip_hash: str,
        *,
        device_hash: str = "",
        proof: str = "link",
    ) -> int:
        """Issue the one-time free credit, platform-funded, with every gate on.

        Gates, in the order they run (cheapest and most decisive first):

        1. **Proof of an inbox.** ``users.verified`` must be true *and* the
           verification must have been a signed link (``proof="link"``). The
           CDN auto-verifies an account when no email provider is configured;
           that is proof of nothing, so it is refused in production.
        2. **A real identity.** The address comes from the account row, is
           canonicalised (plus-tags and Gmail dots removed), and one canonical
           address may only ever claim one grant.
        3. **Not a throwaway inbox.** Known disposable providers are refused.
        4. **Velocity.** Per-IP, per-device and per-email-domain counts over a
           rolling window, so one network/browser/domain cannot mint accounts.
        5. **Daily and monthly ceilings.** A UTC-day grant cap bounds the rate;
           the monthly budget bounds the platform's total cost.

        Every refusal is audited with hashes only (never the raw address, IP or
        device id), and so is every grant.
        """
        if self.settings.signup_credits <= 0:
            return 0

        # A caller that forgets the device key shares one conservative bucket
        # instead of skipping the gate.
        device_hash = device_hash or device_bucket(None)
        verified, address = await self.store.account_identity(account_id)
        canonical = canonical_email(address)
        identity_hash = email_hash(address)
        domain = email_domain(address)
        audit_detail = {
            "ip_hash": ip_hash,
            "device_hash": device_hash,
            "email_hash": identity_hash,
            "email_domain": domain,
            "proof": proof,
        }

        async def audited_refusal(code: str, message: str) -> None:
            # Expected refusal, not an error path: audited, then raised so the
            # caller answers with the stable code and the seam logs it once. The
            # caller never learns which inbox, IP or device tripped it.
            await self.store.audit(
                "account.signup_credit_refused",
                actor=account_id,
                subject=account_id,
                detail={**audit_detail, "reason": code},
            )
            if code in {
                "email_not_verified",
                "signup_identity_missing",
                "signup_unproven_email",
            }:
                raise AuthorizationError(message, code=code)
            raise ConflictError(message, code=code, details={"reason": code})

        if not verified:
            await audited_refusal(
                "email_not_verified",
                "email must be verified before the signup credit is granted",
            )
        if await self.store.count_grants_for_account(account_id):
            # A verification retry (resend, a second click on the link) is not a
            # farming attempt: answer idempotently and credit nothing again.
            return 0
        # Auto-verification means "the deployment has no email provider", not
        # "this person owns the inbox". It is a development convenience and must
        # never buy free credit in production; the one exception is a stack
        # explicitly marked staging (config refuses the flag anywhere else).
        auto_proof_ok = proof == "auto" and (
            not self.settings.is_production or self.settings.signup_allow_auto_verified
        )
        if proof != "link" and not auto_proof_ok:
            await audited_refusal(
                "signup_unproven_email",
                "the signup credit needs a verified email, not an auto-verified account",
            )
        if not canonical or not identity_hash:
            await audited_refusal(
                "signup_identity_missing",
                "the account has no usable email identity",
            )
        if self.settings.signup_block_disposable_email and is_disposable_domain(
            domain, extra_blocked=self.settings.signup_extra_blocked_email_domains
        ):
            await audited_refusal(
                "signup_disposable_email",
                "disposable email providers are not eligible for the signup credit",
            )

        if await self.store.count_grants_for_identity(identity_hash):
            await audited_refusal(
                "signup_identity_already_granted",
                "this email identity has already received the signup credit",
            )

        now = int(self.clock())
        window_start = now - self.settings.signup_velocity_window_seconds
        if (
            await self.store.count_grants_for_ip(ip_hash, window_start)
            >= self.settings.signup_ip_cap
        ):
            await audited_refusal(
                "signup_credit_limit",
                "signup credit limit reached for this network",
            )
        if (
            await self.store.count_grants_for_device(device_hash, window_start)
            >= self.settings.signup_device_cap
        ):
            await audited_refusal(
                "signup_device_limit",
                "signup credit limit reached for this device",
            )
        if (
            await self.store.count_grants_for_domain(domain, window_start)
            >= self.settings.signup_email_domain_cap
        ):
            await audited_refusal(
                "signup_domain_limit",
                "signup credit limit reached for this email domain",
            )

        day_start = int(
            datetime.fromtimestamp(now, tz=UTC)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        if (
            await self.store.count_signup_grants_since(day_start)
            >= self.settings.signup_daily_cap
        ):
            await audited_refusal(
                "signup_daily_cap",
                "daily signup credit budget is exhausted",
            )

        month_start = int(
            datetime.fromtimestamp(now, tz=UTC)
            .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        issued = await self.store.count_signup_grants_since(month_start)
        budgeted = self.settings.signup_monthly_cap_micro
        if (
            budgeted
            and (issued + 1) * self.settings.signup_credits * 1_000_000 > budgeted
        ):
            await audited_refusal(
                "signup_monthly_cap",
                "monthly signup credit budget is exhausted",
            )

        first = await self.store.record_signup_grant(
            account_id,
            ip_hash,
            now,
            email_hash=identity_hash,
            device_hash=device_hash,
            email_domain=domain,
        )
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
            detail={**audit_detail, "micro": amount},
        )
        return amount

    # ── credit purchases ───────────────────────────────────────────────────

    async def handle_payment_webhook(self, body: bytes, signature: str) -> dict:
        event: PaymentEvent = self.payment_provider.verify_webhook(body, signature)
        # A correctly-signed event is authentic, not necessarily well-formed:
        # the provider validates the fields it needs, but the account id is
        # opaque to it. Without this check a signed event naming a non-UUID
        # account reached the ledger's UUID cast and surfaced as a 500.
        try:
            uuid.UUID(event.account_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValidationError(
                "payment event account is not a valid identifier",
                code="invalid_payment_event",
            ) from exc
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

    async def refund_credits(
        self, *, account_id: str, amount_micro: int, reason: str, reference: str
    ) -> None:
        """Credit an account from the platform share, as a ledger entry.

        Refunds and goodwill adjustments are transactions, not edits to a
        balance: the entry has a debit account (platform revenue) and a credit
        account (the customer), so the ledger still sums to zero and a
        reconciliation export can tell a refund from a purchase.
        """
        await self._refuse_when_payouts_are_paused("refund")
        await self.store.credit(
            account_id,
            amount_micro,
            source=PLATFORM_REVENUE,
            idempotency_key=f"refund:{reference}",
            kind="refund",
        )
        logger.info(
            "refund issued reference=%s account=%s micro=%s reason=%s",
            reference,
            account_id,
            amount_micro,
            reason,
        )

    # ── operator controls (kill switch) ────────────────────────────────────

    #: Name of the switch row shared by every coordinator instance.
    PAYOUT_SWITCH = "payouts"

    async def _refuse_when_payouts_are_paused(self, action: str) -> None:
        state = await self.store.get_control(self.PAYOUT_SWITCH)
        if state and state.get("paused"):
            raise ConflictError(
                f"{action} is paused by an operator: {state.get('reason', 'no reason')}",
                code="payouts_paused",
                details={"reason": state.get("reason", "")},
            )

    async def set_payout_kill_switch(
        self, *, paused: bool, reason: str, actor: str
    ) -> dict[str, Any]:
        """Stop or resume every money-out action, recorded with who and why.

        Stored in the shared control row (not in process memory) so a switch
        flipped on one instance is honoured by all of them, and it survives a
        restart — a kill switch that a deploy can silently clear is not one.
        """
        if not reason.strip():
            raise ValidationError(
                "a kill-switch change needs a reason", code="missing_reason"
            )
        value = {
            "paused": bool(paused),
            "reason": reason.strip()[:300],
            "changed_at": int(self.clock()),
        }
        await self.store.set_control(self.PAYOUT_SWITCH, value, actor=actor)
        await self.store.audit(
            "admin.payout_kill_switch",
            actor=actor,
            subject=self.PAYOUT_SWITCH,
            detail=value,
        )
        return value

    async def payout_status(self) -> dict[str, Any]:
        state = await self.store.get_control(self.PAYOUT_SWITCH)
        return {"paused": bool(state and state.get("paused")), "state": state}

    async def record_payout(self, record: dict[str, Any], *, actor: str = "") -> None:
        """Persist a payout record, unless payouts are paused or the node is held.

        A node under billing review has its payouts **held**, not paid: the row
        is stored as ``held_for_review`` and the money stays in the ledger, so an
        operator can release or claw it back instead of finding out afterwards
        that it is already gone.
        """
        await self._refuse_when_payouts_are_paused("payout")
        node_id = str(record.get("node_id", "") or "")
        node = await self.store.get_node(node_id) if node_id else None
        if node is not None and node.payouts_held:
            reason = node.review_reason or "node suspended"
            held = {
                **record,
                "status": "held_for_review",
                "hold_reason": reason,
            }
            await self.store.save_payout(held)
            await self.store.audit(
                "payout.held_node_review",
                actor=actor,
                subject=node_id,
                detail={
                    "amount_micro": record.get("amount_micro"),
                    "component": record.get("component"),
                    "hold_reason": reason,
                    "paid": False,
                },
            )
            return
        await self.store.save_payout(record)
        await self.store.audit(
            "payout.recorded",
            actor=actor,
            subject=str(record.get("node_id", "")),
            detail={
                "amount_micro": record.get("amount_micro"),
                "component": record.get("component"),
                "period_start": record.get("period_start"),
                "period_end": record.get("period_end"),
            },
        )

    # ── redundant-execution verification ───────────────────────────────────

    def verification_decision(self, job_id: str):
        """Deterministic: an auditor can recompute which jobs were sampled."""
        return self.verification.decide(job_id)

    async def record_verification(
        self, job_id: str, outputs: dict[str, str]
    ) -> VerificationOutcome:
        """Resolve independent executions; penalise and possibly suspend the faulty node.

        This records the *resolution* of a sampled job. Scheduling the second
        execution is `_schedule_verification` (placement hands out a second
        signed ticket for a different operator) and dispatch is the gateway's,
        the only party holding the plaintext. Two executions that disagree have
        no majority, so `resolve_executions` returns ``inconclusive`` and nobody
        is penalised — exact-hash comparison cannot attribute a near-miss.
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
