"""Account-scoped API for the TCDN-AI developer console.

The gateway routes in ``routes.py`` authenticate with API keys — correct for a
server, useless to the dashboard that creates those keys. These routes
authenticate the way the rest of the CDN does (``get_current_user_id``: session
JWT or ``tcdn_`` key, email-verification gate, logout blocklist) and are mounted
**only when the AI flag is on**, so a flag-off deployment gains no path here.

What this surface deliberately does not do:

* no prompts or outputs — they are not stored, and no endpoint here reads them;
* no raw keys after creation: the create response is the only place a key is
  ever visible, and it is never logged;
* no live payments: ``/billing/checkout`` refuses when ``TCDN_AI_PAYMENTS_LIVE``
  is on, so the console cannot start a real charge even if someone ships it to a
  deployment with live keys;
* no admin powers: everything is scoped to the caller's own account.

The test-only simulation endpoint (``/billing/checkout/{ref}/simulate``) exists
so the end-to-end journey can complete a purchase without a provider. It is
triple-gated: development/test environment, ``TCDN_AI_FAKE_CHECKOUT=1``, and a
reference this server minted in simulated mode. It cannot credit an account in
production.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
import uuid
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import get_current_user_id
from ..config import get_settings as get_host_settings
from ..database import get_db_pool
from . import console
from .config import get_ai_settings
from .pricing import MICRO_PER_CREDIT
from .routes import get_coordinator
from .service import Coordinator
from .status import build_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai/v1/console", tags=["tcdn-ai-console"])

PAYSTACK_API_BASE = "https://api.paystack.co"

#: Ledger idempotency prefixes the payment webhook writes. Status polling reads
#: the same keys, so "paid" means the ledger has the entry — not that a browser
#: returned from a checkout page.
_PAID_PREFIXES = ("payment:paystack:", "payment:fake:")


def _account(user_id: Any) -> str:
    return str(user_id)


def _refuse_when_live() -> None:
    """The console is test-mode only by construction."""
    if get_ai_settings().payments_live:
        raise HTTPException(
            status_code=403,
            detail=(
                "Live credit purchases are disabled on this deployment. "
                "Credit packs are available in test mode only."
            ),
        )


def _paystack_configured() -> bool:
    key = get_host_settings().paystack_secret_key.strip()
    return bool(key) and key not in ("sk_test_xxx", "sk_live_xxx")


def _ref_is_simulated(reference: str) -> bool:
    return reference.startswith("tcdnai-sim-")


def _simulation_allowed() -> bool:
    if get_ai_settings().is_production:
        return False
    return os.environ.get("TCDN_AI_FAKE_CHECKOUT", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


async def _paid_micro(coordinator: Coordinator, reference: str) -> int | None:
    """Credits posted for this reference, or None while unpaid."""
    for prefix in _PAID_PREFIXES:
        amount = await coordinator.store.ledger_amount(f"{prefix}{reference}")
        if amount is not None:
            return amount
    return None


class KeyCreateRequest(BaseModel):
    scopes: list[str] = Field(default_factory=list)


class CheckoutRequest(BaseModel):
    pack_id: str = Field(min_length=1, max_length=32)


# ── catalogue / status ─────────────────────────────────────────────────────


@router.get("/status")
async def console_status(
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict[str, Any]:
    """Public, detail-free status with the one thing the ops page needs extra:
    per-region worker counts and heartbeat ages.

    Counts and ages only — no node ids, no endpoints. Same posture as
    ``/api/ai/v1/status``, which stays unchanged for compatibility.
    """
    base = await build_status(coordinator)
    regions: dict[str, dict[str, Any]] = {}
    try:
        now = int(time.time())
        for node in await coordinator.store.list_nodes():
            region = node.region or "unknown"
            entry = regions.setdefault(
                region,
                {
                    "region": region,
                    "registered": 0,
                    "healthy": 0,
                    "newest_heartbeat_age_seconds": None,
                },
            )
            entry["registered"] += 1
            if node.last_heartbeat is None:
                continue
            age = max(0, now - int(node.last_heartbeat))
            if (
                age <= int(base["workers"].get("heartbeat_timeout_seconds") or 90)
                and not node.suspended
            ):
                entry["healthy"] += 1
            newest = entry["newest_heartbeat_age_seconds"]
            entry["newest_heartbeat_age_seconds"] = (
                age if newest is None else min(newest, age)
            )
    except Exception as exc:  # a status page must not 500
        logger.warning("console status region rollup failed: %s", type(exc).__name__)
        regions = {}
    return {**base, "regions": sorted(regions.values(), key=lambda r: r["region"])}


@router.get("/models")
async def console_models(
    user_id: Annotated[Any, Depends(get_current_user_id)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict[str, Any]:
    """Catalogue with tier labels. Standard is selectable; confidential is not."""
    del user_id  # authentication is the point of this dependency
    models = await coordinator.list_models()
    return {
        "models": [console.model_view(info) for info in models],
        "tiers": [
            {"id": "standard", "label": "Standard", "available": True},
            {
                "id": "confidential",
                "label": "Confidential",
                "available": False,
                "reason": "coming soon",
            },
        ],
    }


# ── account ────────────────────────────────────────────────────────────────


@router.get("/summary")
async def console_summary(
    user_id: Annotated[Any, Depends(get_current_user_id)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict[str, Any]:
    account = _account(user_id)
    micro = await coordinator.store.balance_micro(account)
    held = await coordinator.store.held_micro(account)
    jobs = await coordinator.store.list_jobs(account, limit=20)
    keys = await coordinator.store.list_api_keys(account)
    active_keys = [key for key in keys if key.status == "active"]
    spend = sum(job.total_micro for job in jobs)
    models = await coordinator.list_models()
    status = await build_status(coordinator)
    return {
        "account_id": account,
        "balance": {
            "micro": micro,
            "credits_usd": float(micro) / MICRO_PER_CREDIT,
            "held_micro": held,
            "available_micro": micro - held,
            "kes_per_usd": console.KES_PER_USD,
            "approx_kes": (micro / MICRO_PER_CREDIT) * console.KES_PER_USD,
        },
        "usage": {
            "jobs": len(jobs),
            "spend_micro": spend,
            "last_job_at": jobs[0].created_at if jobs else None,
        },
        "keys": {"active": len(active_keys), "total": len(keys)},
        "models": {"count": len([m for m in models if m.allowlisted])},
        "checklist": {
            "account_ready": True,
            "has_key": bool(active_keys),
            "has_job": bool(jobs),
            "has_credit": micro > 0,
        },
        "status": {
            "feature_on": bool(status["feature_flag"]["on"]),
            "environment": status["environment"],
            "commit": status["commit"],
            "database": status["database"]["status"],
            "redis": status["redis"]["status"],
            "workers": status["workers"],
            "server_time": status["server_time"],
        },
    }


# ── API keys ───────────────────────────────────────────────────────────────


@router.get("/keys")
async def list_keys(
    user_id: Annotated[Any, Depends(get_current_user_id)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict[str, Any]:
    keys = await coordinator.store.list_api_keys(_account(user_id))
    return {"keys": [console.key_view(key) for key in keys]}


@router.post("/keys")
async def create_key(
    body: KeyCreateRequest,
    user_id: Annotated[Any, Depends(get_current_user_id)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict[str, Any]:
    """Mint one key. The raw value is returned here and nowhere else, ever."""
    try:
        scopes = console.valid_scopes(body.scopes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    account = _account(user_id)
    raw = await coordinator.provision_api_key(account, scopes=scopes)
    # The prefix is the non-secret part (crypto.new_api_key stores raw[:20]):
    # safe to display and to log.
    prefix = raw[:20]
    await coordinator.store.audit(
        "console.api_key_created",
        actor=account,
        subject=account,
        detail={"scopes": list(scopes)},
    )
    logger.info("api key created account=%s scopes=%s", account, ",".join(scopes))
    return {
        "api_key": raw,
        "prefix": prefix,
        "scopes": list(scopes),
        "notice": "Copy this key now. It is not stored and cannot be shown again.",
    }


@router.post("/keys/{key_id}/revoke")
async def revoke_key(
    key_id: str,
    user_id: Annotated[Any, Depends(get_current_user_id)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict[str, Any]:
    account = _account(user_id)
    try:
        uuid.UUID(key_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Key not found") from exc
    revoked = await coordinator.store.revoke_api_key(account, key_id)
    if not revoked:
        # Same answer for "not yours" and "does not exist": no account oracle.
        raise HTTPException(status_code=404, detail="Key not found or already revoked")
    await coordinator.store.audit(
        "console.api_key_revoked", actor=account, subject=key_id, detail={}
    )
    return {"revoked": True, "id": key_id}


# ── usage and jobs ─────────────────────────────────────────────────────────


@router.get("/usage")
async def console_usage(
    user_id: Annotated[Any, Depends(get_current_user_id)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
    limit: int = Query(default=50, ge=1, le=200),
    model: str | None = Query(default=None, max_length=64),
    status: str | None = Query(default=None, max_length=24),
) -> dict[str, Any]:
    account = _account(user_id)
    # Fetch a bounded page and filter server-side: the client never receives
    # rows it did not ask for, and a crafted filter cannot widen the result set.
    jobs = await coordinator.store.list_jobs(
        account, limit=limit * 4 if (model or status) else limit
    )
    rows = [
        job
        for job in jobs
        if (not model or job.model == model) and (not status or job.status == status)
    ]
    rows = rows[:limit]
    nodes = {node.node_id: node for node in await coordinator.store.list_nodes()}
    return {
        "jobs": [console.job_view(job, nodes.get(job.node_id)) for job in rows],
        "total_micro": sum(job.total_micro for job in rows),
        "filters": {"model": model, "status": status, "limit": limit},
        "note": "Prompts and outputs are never stored, so they cannot appear here.",
    }


@router.get("/jobs/{job_id}")
async def console_job(
    job_id: str,
    user_id: Annotated[Any, Depends(get_current_user_id)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict[str, Any]:
    if not job_id.replace("-", "").isalnum() or len(job_id) > 64:
        raise HTTPException(status_code=404, detail="Job not found")
    account = _account(user_id)
    job = await coordinator.store.get_job(job_id)
    if job is None or job.account_id != account:
        # No existence oracle across accounts.
        raise HTTPException(status_code=404, detail="Job not found")
    node = await coordinator.store.get_node(job.node_id)
    verification = await coordinator.store.get_verification(job_id)
    return {
        "job": console.job_view(job, node),
        "verification": console.verification_view(verification),
        "settlement": {
            "compute_micro": job.compute_micro,
            "seeder_micro": job.seeder_micro,
            "platform_micro": job.platform_micro,
        },
    }


# ── billing ────────────────────────────────────────────────────────────────


@router.get("/billing/packs")
async def billing_packs(
    user_id: Annotated[Any, Depends(get_current_user_id)],
) -> dict[str, Any]:
    del user_id
    settings = get_ai_settings()
    return {
        "packs": console.packs(),
        "currency": {"code": "KES", "kes_per_usd": console.KES_PER_USD},
        "mode": "live" if settings.payments_live else "test",
        "provider": "Paystack",
        "note": (
            "Test mode: no real charge is made. Adds USD credits at the "
            "displayed KES price."
        ),
    }


@router.post("/billing/checkout")
async def start_checkout(
    body: CheckoutRequest,
    user_id: Annotated[Any, Depends(get_current_user_id)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict[str, Any]:
    _refuse_when_live()
    pack = console.pack(body.pack_id)
    if pack is None:
        raise HTTPException(status_code=422, detail="Unknown credit pack")
    account = _account(user_id)
    host = get_host_settings()
    callback = f"{host.app_url.rstrip('/')}/ai/billing"

    # No provider configured (local/e2e): hand back a simulated checkout so the
    # state machine can still be exercised. The reference records the pack, and
    # /simulate can only credit a reference minted here.
    if not _paystack_configured():
        if not _simulation_allowed():
            raise HTTPException(
                status_code=503,
                detail="Payments are not configured on this deployment.",
            )
        reference = f"tcdnai-sim-{pack['id']}-{secrets.token_hex(8)}"
        return {
            "mode": "simulated",
            "reference": reference,
            "authorization_url": f"{callback}?pending={reference}&simulated=1",
            "pack_id": pack["id"],
            "credit_micro": pack["credit_micro"],
            "kes": pack["kes"],
        }

    reference = f"tcdnai-{secrets.token_hex(12)}"
    metadata = {
        "purpose": "ai_credits",
        "account_id": account,
        "pack_id": pack["id"],
        "credit_micro": pack["credit_micro"],
    }
    try:
        async with httpx.AsyncClient(
            base_url=PAYSTACK_API_BASE,
            headers={"Authorization": f"Bearer {host.paystack_secret_key}"},
            timeout=15,
        ) as client:
            response = await client.post(
                "/transaction/initialize",
                json={
                    "email": await _account_email(account),
                    "amount": pack["kes_minor"],
                    "currency": "KES",
                    "reference": reference,
                    "callback_url": f"{callback}?pending={reference}",
                    "metadata": metadata,
                },
            )
    except httpx.HTTPError as exc:
        logger.error("paystack initialize failed for account=%s: %s", account, exc)
        raise HTTPException(
            status_code=502, detail="Could not start checkout. Please try again."
        ) from exc
    if response.status_code != 200:
        logger.error(
            "paystack initialize rejected account=%s http=%s body=%s",
            account,
            response.status_code,
            response.text[:300],
        )
        raise HTTPException(
            status_code=502, detail="Could not start checkout. Please try again."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502, detail="Could not start checkout. Please try again."
        ) from exc
    if not payload.get("status"):
        raise HTTPException(
            status_code=502, detail="Could not start checkout. Please try again."
        )
    return {
        "mode": "test",
        "reference": reference,
        "authorization_url": payload["data"]["authorization_url"],
        "pack_id": pack["id"],
        "credit_micro": pack["credit_micro"],
        "kes": pack["kes"],
    }


@router.get("/billing/checkout/{reference}")
async def checkout_status(
    reference: str,
    user_id: Annotated[Any, Depends(get_current_user_id)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict[str, Any]:
    if len(reference) > 96 or not reference.replace("-", "").isalnum():
        raise HTTPException(status_code=404, detail="Checkout not found")
    account = _account(user_id)
    credited = await _paid_micro(coordinator, reference)
    if credited is None:
        return {"reference": reference, "status": "pending", "credit_micro": 0}
    return {
        "reference": reference,
        "status": "paid",
        "credit_micro": credited,
        "balance_micro": await coordinator.store.balance_micro(account),
    }


@router.post("/billing/checkout/{reference}/simulate")
async def simulate_checkout(
    reference: str,
    user_id: Annotated[Any, Depends(get_current_user_id)],
    coordinator: Annotated[Coordinator, Depends(get_coordinator)],
) -> dict[str, Any]:
    """Test seam: complete a simulated checkout. Never available in production."""
    _refuse_when_live()
    if not _simulation_allowed() or not _ref_is_simulated(reference):
        raise HTTPException(status_code=404, detail="Checkout not found")
    parts = reference.split("-")
    pack = console.pack(parts[2]) if len(parts) >= 4 else None
    if pack is None:
        raise HTTPException(status_code=422, detail="Unknown credit pack")
    account = _account(user_id)
    await coordinator.store.credit(
        account,
        pack["credit_micro"],
        source="gateway:fake:clearing",
        idempotency_key=f"payment:fake:{reference}",
        kind="credit_purchase",
    )
    await coordinator.store.audit(
        "billing.credit_purchase",
        actor=account,
        subject=reference,
        detail={"micro": pack["credit_micro"], "provider": "fake"},
    )
    return {
        "reference": reference,
        "status": "paid",
        "credit_micro": pack["credit_micro"],
        "balance_micro": await coordinator.store.balance_micro(account),
    }


async def _account_email(account: str) -> str:
    """The CDN's own address for this account — never supplied by the caller."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT email FROM users WHERE id = $1", uuid.UUID(account)
        )
    if not row or not row["email"]:
        raise HTTPException(status_code=400, detail="Account has no email address")
    return str(row["email"])


__all__ = ["router"]
