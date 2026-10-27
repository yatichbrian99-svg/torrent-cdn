"""TCDN-AI coordinator API (``/api/ai/v1``).

Auth split, deliberately:

* developer endpoints authenticate with an **AI API key** (HMAC-hashed at rest);
* worker endpoints are self-authenticating — the body is signed by the node's
  identity key, and the coordinator looks the key up by node id;
* admin endpoints use the repo's existing **admin JWT** dependency and can never
  be reached with an AI API key.

The router does no business logic: it validates transport concerns (size, rate,
auth), then delegates to ``Coordinator``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from ..database import get_db_pool, get_redis
from ..routes.admin import require_admin
from ..security import get_client_ip
from .clock import now_unix
from .config import AISettings, get_ai_settings
from .errors import (
    AIError,
    AuthenticationError,
    LimitExceeded,
    PayloadTooLarge,
    ValidationError,
)
from .keyring import KeyRing, load_keyring
from .models import (
    AckPayload,
    BalanceResponse,
    CoordinatorKeyResponse,
    KillSwitchRequest,
    ModelInfo,
    NodeRegistrationResponse,
    PlacementRequest,
    RefundRequest,
    SettlementResponse,
    SignedEnvelope,
    TicketResponse,
    UsageResponse,
)
from .ratelimit import TokenBucketLimiter
from .service import AuthContext, Coordinator
from .store import PostgresStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai/v1", tags=["tcdn-ai"])
admin_router = APIRouter(prefix="/api/ai/v1/admin", tags=["tcdn-ai-admin"])

_coordinator: Coordinator | None = None
_coordinator_override: Coordinator | None = None
_keyring_singleton: KeyRing | None = None
_key_limiter: TokenBucketLimiter | None = None
_ip_limiter: TokenBucketLimiter | None = None


def reset_for_tests() -> None:
    """Clear process singletons (tests only)."""
    global \
        _coordinator, \
        _coordinator_override, \
        _keyring_singleton, \
        _key_limiter, \
        _ip_limiter
    _coordinator = None
    _coordinator_override = None
    _keyring_singleton = None
    _key_limiter = None
    _ip_limiter = None


def set_coordinator_for_tests(coordinator: Coordinator | None) -> None:
    global _coordinator_override
    _coordinator_override = coordinator


def _keyring(settings: AISettings) -> KeyRing:
    global _keyring_singleton
    if _keyring_singleton is None:
        _keyring_singleton = load_keyring(settings)
    return _keyring_singleton


async def get_coordinator() -> Coordinator:
    global _coordinator
    if _coordinator_override is not None:
        return _coordinator_override
    if _coordinator is None:
        settings = get_ai_settings()
        pool = await get_db_pool()
        store = PostgresStore(pool, settings)
        redis_client = await get_redis()
        _coordinator = Coordinator(
            store=store,
            keyring=_keyring(settings),
            settings=settings,
            redis_client=redis_client,
            clock=now_unix,
        )
    return _coordinator


def _limiters(settings: AISettings) -> tuple[TokenBucketLimiter, TokenBucketLimiter]:
    global _key_limiter, _ip_limiter
    if _key_limiter is None:
        _key_limiter = TokenBucketLimiter(
            rate_per_minute=settings.rate_limit_per_minute,
            burst=settings.rate_limit_burst,
        )
    if _ip_limiter is None:
        _ip_limiter = TokenBucketLimiter(
            rate_per_minute=settings.rate_limit_per_minute * 2,
            burst=settings.rate_limit_burst * 2,
        )
    return _key_limiter, _ip_limiter


def _bearer(request: Request, x_api_key: str | None) -> str:
    header = request.headers.get("authorization", "")
    raw = (
        header[7:].strip()
        if header.lower().startswith("bearer ")
        else (x_api_key or "")
    )
    if not raw:
        raise AuthenticationError("missing API key")
    return raw


async def enforce_body_size(
    request: Request,
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> None:
    """Reject oversized bodies before they are parsed.

    Signed endpoints (receipts, heartbeats, attestations, payment webhooks) do
    not authenticate with an API key, so they must apply the same limit as
    ``authenticate`` explicitly -- otherwise an anonymous caller can make the
    process buffer an arbitrarily large JSON body.
    """
    content_length = request.headers.get("content-length")
    if content_length is None:
        return
    try:
        declared = int(content_length)
    except ValueError as exc:
        raise ValidationError("invalid Content-Length header") from exc
    if declared > coordinator.settings.max_body_bytes:
        raise PayloadTooLarge("request body is too large")


async def authenticate(
    request: Request,
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> AuthContext:
    await enforce_body_size(request, coordinator)

    raw = _bearer(request, x_api_key)
    auth = await coordinator.authenticate(raw)

    ip = get_client_ip(request) or "unknown"
    key_limiter, ip_limiter = _limiters(coordinator.settings)
    # ``allow`` is async — calling it without awaiting returned a truthy coroutine
    # and silently disabled both buckets.
    key_result = await key_limiter.allow(f"key:{auth.key_prefix}")
    if not key_result.allowed:
        raise LimitExceeded(
            "rate limit exceeded for this API key",
            code="rate_limited",
            details={"retry_after": key_result.retry_after_seconds},
        )
    ip_result = await ip_limiter.allow(f"ip:{ip}")
    if not ip_result.allowed:
        raise LimitExceeded(
            "rate limit exceeded for this client address", code="rate_limited"
        )
    return auth


# ── catalogue ───────────────────────────────────────────────────────────────


@router.get("/models", response_model=list[ModelInfo])
async def list_models(
    auth: Annotated[AuthContext, Depends(authenticate)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> list[ModelInfo]:
    Coordinator.require_scope(auth, "inference")
    return await coordinator.list_models()


# ── placement ───────────────────────────────────────────────────────────────


@router.post("/placements", response_model=TicketResponse)
async def create_placement(
    request: PlacementRequest,
    auth: Annotated[AuthContext, Depends(authenticate)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
    # Optional, but a retry is the difference between one hold and two. See
    # Coordinator.place: the replay is scoped to the account and refuses a
    # reused key with a different body.
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TicketResponse:
    return await coordinator.place(auth, request, idempotency_key=idempotency_key)


# ── receipts / acks ─────────────────────────────────────────────────────────


@router.post("/jobs/{job_id}/receipt", response_model=SettlementResponse)
async def submit_receipt(
    job_id: str,
    envelope: SignedEnvelope,
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
    _limit: Annotated[None, Depends(enforce_body_size)],
) -> SettlementResponse:
    if envelope.payload.get("job_id") != job_id:
        raise ValidationError(
            "receipt job_id does not match the path", code="job_mismatch"
        )
    return await coordinator.submit_receipt(envelope)


@router.post("/jobs/{job_id}/ack", response_model=SettlementResponse)
async def submit_ack(
    job_id: str,
    ack: AckPayload,
    auth: Annotated[AuthContext, Depends(authenticate)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> SettlementResponse:
    return await coordinator.submit_ack(auth, job_id, ack)


# ── node registry ───────────────────────────────────────────────────────────


@router.post("/nodes/register", response_model=NodeRegistrationResponse)
async def register_node(
    envelope: SignedEnvelope,
    auth: Annotated[AuthContext, Depends(authenticate)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> NodeRegistrationResponse:
    return await coordinator.register_node(auth, envelope)


@router.post("/nodes/heartbeat")
async def heartbeat(
    envelope: SignedEnvelope,
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
    _limit: Annotated[None, Depends(enforce_body_size)],
) -> dict:
    return await coordinator.heartbeat(envelope)


@router.post("/nodes/{node_id}/attestation-nonce")
async def attestation_nonce(
    node_id: str,
    auth: Annotated[AuthContext, Depends(authenticate)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict:
    Coordinator.require_scope(auth, "worker")
    node = await coordinator.store.get_node(node_id)
    if node is None or (node.owner_id and node.owner_id != auth.account_id):
        # Do not confirm that another account's node exists.
        from .errors import NotFoundError

        raise NotFoundError("unknown node")
    return {
        "node_id": node_id,
        "nonce": await coordinator.issue_attestation_nonce(node_id),
    }


@router.post("/attestations")
async def submit_attestation(
    envelope: SignedEnvelope,
    auth: Annotated[AuthContext, Depends(authenticate)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
    _limit: Annotated[None, Depends(enforce_body_size)],
) -> dict:
    return await coordinator.attest(auth, envelope)


# ── account ─────────────────────────────────────────────────────────────────


@router.get("/balance", response_model=BalanceResponse)
async def balance(
    auth: Annotated[AuthContext, Depends(authenticate)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> BalanceResponse:
    return await coordinator.balance(auth)


@router.get("/usage", response_model=UsageResponse)
async def usage(
    auth: Annotated[AuthContext, Depends(authenticate)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
    limit: int = 100,
) -> UsageResponse:
    return await coordinator.usage(auth, limit=limit)


@router.post("/billing/webhook")
async def payment_webhook(
    request: Request,
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
    _limit: Annotated[None, Depends(enforce_body_size)],
) -> dict:
    """Signed provider webhook. No API key: the signature *is* the authority."""
    body = await request.body()
    signature = (
        request.headers.get("x-paystack-signature")
        or request.headers.get("x-tcdn-signature")
        or ""
    )
    return await coordinator.handle_payment_webhook(body, signature)


@router.get("/keys/coordinator", response_model=CoordinatorKeyResponse)
async def coordinator_keys(
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> CoordinatorKeyResponse:
    keys = coordinator.keys()
    return CoordinatorKeyResponse(
        bundle=keys["bundle"], revocations=keys["revocations"]
    )


# ── admin ───────────────────────────────────────────────────────────────────


@admin_router.post("/verification")
async def record_verification(
    job_id: str,
    outputs: dict[str, str],
    admin_uid: Annotated[object, Depends(require_admin)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict:
    """Record independent execution hashes for a sampled job."""
    outcome = await coordinator.record_verification(job_id, outputs)
    await coordinator.store.audit(
        "admin.verification_recorded",
        actor=str(admin_uid),
        subject=job_id,
        detail={"status": outcome.status},
    )
    return {
        "status": outcome.status,
        "faulty_nodes": list(outcome.faulty_nodes),
        "detail": outcome.detail,
    }


@admin_router.post("/api-keys")
async def create_api_key(
    account_id: str,
    admin_uid: Annotated[object, Depends(require_admin)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
    scopes: str = "inference",
) -> dict:
    allowed = {"inference", "worker"}
    requested = tuple(scope.strip() for scope in scopes.split(",") if scope.strip())
    if not requested or not set(requested).issubset(allowed):
        raise ValidationError("scopes must be a subset of inference,worker")
    raw = await coordinator.provision_api_key(account_id, scopes=requested)
    await coordinator.store.audit(
        "admin.api_key_created",
        actor=str(admin_uid),
        subject=account_id,
        detail={"scopes": list(requested)},
    )
    # Shown exactly once; never logged.
    return {"api_key": raw, "account_id": account_id, "scopes": list(requested)}


@admin_router.get("/kill-switch")
async def read_kill_switch(
    admin_uid: Annotated[object, Depends(require_admin)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict:
    """Current state of the money-out switch (checklist 5.9)."""
    return await coordinator.payout_status()


@admin_router.post("/kill-switch")
async def set_kill_switch(
    body: KillSwitchRequest,
    admin_uid: Annotated[object, Depends(require_admin)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict:
    """Pause or resume refunds and payouts, with an audited reason.

    The switch is stored in the shared ``ai_controls`` row, so every coordinator
    instance honours it and a redeploy cannot quietly clear it.
    """
    state = await coordinator.set_payout_kill_switch(
        paused=body.paused, reason=body.reason, actor=str(admin_uid)
    )
    return {"paused": state["paused"], "state": state}


@admin_router.post("/refunds")
async def refund_credits(
    body: RefundRequest,
    admin_uid: Annotated[object, Depends(require_admin)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict:
    """Credit an account as a double-entry ledger transaction (row 4.6).

    ``reference`` is required and doubles as the idempotency key, so a retried
    refund cannot pay twice. The audit row records who, how much and why.
    """
    await coordinator.refund_credits(
        account_id=body.account_id,
        amount_micro=body.amount_micro,
        reason=body.reason,
        reference=body.reference,
    )
    await coordinator.store.audit(
        "admin.refund",
        actor=str(admin_uid),
        subject=body.account_id,
        detail={
            "micro": body.amount_micro,
            "reason": body.reason,
            "reference": body.reference,
        },
    )
    return {
        "status": "refunded",
        "account_id": body.account_id,
        "amount_micro": body.amount_micro,
        "balance_micro": await coordinator.store.balance_micro(body.account_id),
    }


async def expire_jobs_loop(interval_seconds: int = 60) -> None:
    """Background safety net for jobs whose worker died mid-flight."""
    while True:
        try:
            coordinator = await get_coordinator()
            expired = await coordinator.expire_stale_jobs()
            if expired:
                logger.info("expired %s stale AI job(s); holds released", len(expired))
        except Exception as exc:  # never let the sweep kill the process
            logger.warning("stale AI job sweep failed: %s", exc)
        await asyncio.sleep(interval_seconds)


async def ai_error_handler(request: Request, exc: AIError) -> JSONResponse:
    # Codes and messages only: request bodies may contain prompts and are never
    # logged (no-retention invariant). Without this line a rejected receipt is
    # a bare 400 in the access log with no way to tell why.
    logger.info("ai error %s %s %s", request.method, request.url.path, exc.code)
    headers = {}
    retry_after = exc.details.get("retry_after")
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=exc.status_code, content=exc.as_body(), headers=headers
    )
