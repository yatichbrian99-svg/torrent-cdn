"""Payout routes — Paystack Transfers API (Kenya, M-PESA). Admin-only.

Receiving money (plans) is handled in billing.py; this module is the other half
of the Paystack integration: moving money OUT of the Paystack balance to an
M-PESA wallet. Because payouts move real money, every endpoint is gated behind
``require_admin``, and the three endpoints that move money or bank details
(create/deactivate a recipient, initiate a transfer) are gated behind
``require_admin_jwt`` — a long-lived ``tcdn_`` API key must not be able to send
money, the same rule audit F-6 applied to the AI admin routes. Every transfer
also references a recipient that was first created (and stored) through this
module — an admin can never pay an arbitrary recipient code they typed by hand.

Flows (mirroring Seike's Paystack Transfers email 1:1):

  * POST /recipients            → Paystack ``transferrecipient`` (mobile_money,
                                  bank_code MPESA); the returned recipient_code
                                  is stored locally.
  * POST /                      → Paystack ``transfer`` (source=balance) with
                                  the recipient_code; the returned transfer_code
                                  is stored locally with its initial status
                                  (pending / otp / ...).
  * GET /{transfer_code}        → Paystack ``transfer/{code}``; local status is
                                  refreshed from the authoritative response.
  * Webhook (billing.py)        → transfer.success / transfer.failed /
                                  transfer.reversed update the local status and
                                  notify the creating admin.

Amounts are in the currency's smallest unit (KES cents) for the Paystack API —
same convention as the payment side (KES 4,000 plan = amount 400000). The UI
works in whole KES; this module converts (and stores cents).
"""
import logging
from uuid import UUID

import httpx
from asyncpg.exceptions import UniqueViolationError
from fastapi import APIRouter, Depends, HTTPException

from ..config import get_settings
from ..database import get_db_pool
from ..models import (
    PayoutBalanceResponse,
    PayoutInitiateRequest,
    PayoutRecipientCreate,
    PayoutRecipientResponse,
    PayoutResponse,
)
from ..security import contains_control_chars
from ..security_events import record as record_event
from .admin import require_admin, require_admin_jwt

# Same secret key + API base as the payment side (billing.py) — one source of
# truth; billing does not import this module, so there is no import cycle.
from .billing import _PAYSTACK_HEADERS, PAYSTACK_API_BASE

settings = get_settings()
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing/payouts", tags=["payouts"])

# Paystack's documented KES mobile-money floor is 1000 cents (KES 10.00).
# Anything below that is rejected before it ever reaches the API. (Verify
# against the live account — if the floor is ever raised, bump this to match.)
MIN_TRANSFER_CENTS = 1000


async def _reactivate_recipient(pool, recipient_code: str) -> None:
    """Best-effort revert of the local soft-deactivate (used when the Paystack
    call fails — see deactivate_recipient). Never raises: deactivation failure
    already surfaces as a 502; a failed revert is logged and left for retry."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE payout_recipients SET active = TRUE WHERE recipient_code = $1",
                recipient_code,
            )
    except Exception:
        logger.exception(
            "Could not revert local deactivation for recipient %s", recipient_code,
        )


def _payout_from_row(row) -> PayoutResponse:
    """Build the API response shape from a `payouts` table row."""
    return PayoutResponse(
        transfer_code=row["transfer_code"],
        recipient_code=row["recipient_code"],
        recipient_name=row.get("recipient_name") or "",
        amount_cents=row["amount_cents"],
        amount_kes=round(row["amount_cents"] / 100, 2),
        currency=row["currency"],
        reason=row.get("reason"),
        status=row["status"],
        reference=row.get("reference"),
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
    )


@router.get("/balance", response_model=PayoutBalanceResponse)
async def payout_balance(_: UUID = Depends(require_admin)):
    """Available Paystack balance for the configured currency (KES)."""
    try:
        async with httpx.AsyncClient(
            base_url=PAYSTACK_API_BASE, headers=_PAYSTACK_HEADERS, timeout=15
        ) as client:
            resp = await client.get("/balance")
        if resp.status_code != 200:
            logger.error("Paystack balance failed (HTTP %s): %s", resp.status_code, resp.text[:500])
            raise HTTPException(status_code=502, detail="Could not fetch balance. Please try again.")
        body = resp.json()
        if not body.get("status"):
            raise HTTPException(status_code=502, detail="Could not fetch balance. Please try again.")
        for entry in body.get("data") or []:
            if entry.get("currency") == settings.paystack_currency:
                cents = int(entry.get("balance") or 0)
                return PayoutBalanceResponse(
                    currency=entry["currency"],
                    balance_cents=cents,
                    balance_kes=round(cents / 100, 2),
                )
        raise HTTPException(status_code=502, detail=f"No {settings.paystack_currency} balance found")
    except httpx.HTTPError as e:
        logger.error("Paystack balance request failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not fetch balance. Please try again.")


@router.get("/recipients", response_model=list[PayoutRecipientResponse])
async def list_recipients(_: UUID = Depends(require_admin)):
    """All stored transfer recipients (active and deactivated)."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM payout_recipients ORDER BY created_at DESC LIMIT 100"
        )
    return [
        PayoutRecipientResponse(
            recipient_code=r["recipient_code"],
            name=r["name"],
            account_number=r["account_number"],
            bank_code=r["bank_code"],
            bank_name=r.get("bank_name"),
            type=r["type"],
            currency=r["currency"],
            active=bool(r.get("active", True)),
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.post("/recipients", response_model=PayoutRecipientResponse)
async def create_recipient(
    data: PayoutRecipientCreate,
    admin_id: UUID = Depends(require_admin_jwt),
):
    """Create an M-PESA transfer recipient on Paystack and store it locally.

    `account_number` is the recipient's M-PESA phone number. Paystack returns a
    recipient_code (RCP_...) which is the handle every future transfer uses.
    """
    # Validate BEFORE calling Paystack: `name`/`account_number` are written to
    # payout_recipients verbatim and a control character (NUL) is unstorable in
    # Postgres — reject it as a 400 rather than creating a charge-less Paystack
    # recipient and then 500ing on the INSERT.
    for _label, _value in (("name", data.name), ("account number", data.account_number),
                           ("description", data.description)):
        if _value and contains_control_chars(_value):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid recipient {_label}: control characters are not allowed",
            )
    try:
        async with httpx.AsyncClient(
            base_url=PAYSTACK_API_BASE, headers=_PAYSTACK_HEADERS, timeout=15
        ) as client:
            resp = await client.post(
                "/transferrecipient",
                json={
                    "type": data.type,
                    "name": data.name,
                    "account_number": data.account_number,
                    "bank_code": data.bank_code,
                    "currency": data.currency,
                    "description": data.description,
                },
            )
        if resp.status_code != 200:
            logger.error(
                "Paystack create recipient failed (HTTP %s): %s",
                resp.status_code, resp.text[:500],
            )
            raise HTTPException(status_code=502, detail="Could not create recipient. Please try again.")
        body = resp.json()
        if not body.get("status"):
            raise HTTPException(status_code=502, detail="Could not create recipient. Please try again.")
        ps_data = body.get("data") or {}
        recipient_code = ps_data.get("recipient_code")
        if not recipient_code:
            raise HTTPException(status_code=502, detail="Paystack returned no recipient code.")
    except httpx.HTTPError as e:
        logger.error("Paystack create recipient request failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not create recipient. Please try again.")

    details = ps_data.get("details") or {}
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO payout_recipients
                (recipient_code, name, account_number, bank_code, bank_name,
                 type, currency, created_by)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            recipient_code,
            data.name,
            data.account_number,
            data.bank_code,
            details.get("bank_name"),
            data.type,
            data.currency,
            admin_id,
        )
    logger.info("Admin %s created payout recipient %s (%s)", admin_id, recipient_code, data.name)
    record_event("payout_recipient_create", detail=f"{admin_id} -> {recipient_code} ({data.name})")
    return PayoutRecipientResponse(
        recipient_code=row["recipient_code"],
        name=row["name"],
        account_number=row["account_number"],
        bank_code=row["bank_code"],
        bank_name=row.get("bank_name"),
        type=row["type"],
        currency=row["currency"],
        active=bool(row.get("active", True)),
        created_at=row["created_at"],
    )


@router.delete("/recipients/{recipient_code}")
async def deactivate_recipient(
    recipient_code: str,
    admin_id: UUID = Depends(require_admin_jwt),
):
    """Deactivate a transfer recipient (local soft-deactivate + Paystack).

    Soft-deactivate locally FIRST so the `active` gate blocks any new transfer
    to this recipient the instant the admin confirms. Paystack's endpoint
    permanently deactivates on their side. The local existence check runs
    before anything else so a 404 can never leave Paystack already deactivated
    for a recipient we don't know about.

    If the Paystack call then fails, the local row is REVERTED to active=TRUE
    (best effort) before the 502 — Paystack is still active, so money can
    still move and the admin can simply retry the deactivation later. Without
    the revert the recipient would be stuck inactive with no re-activate
    endpoint, and recreating it would hit the recipient_code UNIQUE constraint
    (Paystack dedupes recipients by account details and returns the same
    recipient_code).
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM payout_recipients WHERE recipient_code = $1",
            recipient_code,
        )
        if not exists:
            raise HTTPException(status_code=404, detail="Recipient not found")
        # Gate first: block new transfers to this recipient immediately.
        await conn.execute(
            "UPDATE payout_recipients SET active = FALSE WHERE recipient_code = $1",
            recipient_code,
        )

    try:
        async with httpx.AsyncClient(
            base_url=PAYSTACK_API_BASE, headers=_PAYSTACK_HEADERS, timeout=15
        ) as client:
            resp = await client.delete(f"/transferrecipient/{recipient_code}")
        if resp.status_code != 200:
            logger.error(
                "Paystack deactivate recipient failed (HTTP %s): %s",
                resp.status_code, resp.text[:500],
            )
            await _reactivate_recipient(pool, recipient_code)
            raise HTTPException(status_code=502, detail="Could not deactivate recipient.")
        body = resp.json()
        if not body.get("status"):
            await _reactivate_recipient(pool, recipient_code)
            raise HTTPException(status_code=502, detail="Could not deactivate recipient.")
    except httpx.HTTPError as e:
        logger.error("Paystack deactivate recipient request failed: %s", e)
        await _reactivate_recipient(pool, recipient_code)
        raise HTTPException(status_code=502, detail="Could not deactivate recipient.")

    logger.info("Admin %s deactivated payout recipient %s", admin_id, recipient_code)
    record_event("payout_recipient_deactivate", detail=f"{admin_id} -> {recipient_code}")
    return {"detail": "Recipient deactivated"}


@router.get("", response_model=list[PayoutResponse])
async def list_payouts(_: UUID = Depends(require_admin)):
    """Recent payouts, newest first."""
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM payouts ORDER BY created_at DESC LIMIT 100"
        )
    return [_payout_from_row(r) for r in rows]


@router.post("", response_model=PayoutResponse)
async def initiate_payout(
    data: PayoutInitiateRequest,
    admin_id: UUID = Depends(require_admin_jwt),
):
    """Initiate a transfer from the Paystack balance to a stored recipient.

    `amount` is in whole KES; it is converted to cents (same smallest-unit
    convention as the payment side) before calling Paystack. Only recipients
    created through this module (present in the local table, still active) can
    be paid — an admin can never hand-type a recipient_code.

    Idempotent initiation: an optional `idempotency_key` is forwarded to
    Paystack as the transfer `reference`. Paystack dedupes on reference, so a
    retry (double-click, network timeout) can never create a second transfer of
    the same money. A locally-stored payout with that reference short-circuits
    before any outbound call, and the DB partial-unique index on reference
    catches the concurrent same-key race (returning the existing row).

    First transfers from a new balance may come back with status "otp"
    (Paystack requires a phone OTP to approve the first payout). The status is
    stored/surfaced as-is and the OTP is completed in the Paystack dashboard
    (Settings -> Preferences) — implementing POST /transfer/finalize_transfer
    is a known follow-up.
    """
    cents = data.amount * 100
    if cents < MIN_TRANSFER_CENTS:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum payout is KES {MIN_TRANSFER_CENTS // 100}",
        )

    pool = await get_db_pool()

    # Idempotency short-circuit: same key -> same transfer, returned as-is.
    if data.idempotency_key:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT * FROM payouts WHERE reference = $1",
                data.idempotency_key,
            )
        if existing:
            logger.info(
                "Admin %s re-sent idempotent payout key %s — returning existing %s",
                admin_id, data.idempotency_key, existing["transfer_code"],
            )
            return _payout_from_row(existing)

    async with pool.acquire() as conn:
        recipient = await conn.fetchrow(
            "SELECT recipient_code, name, active FROM payout_recipients WHERE recipient_code = $1",
            data.recipient_code,
        )
        if not recipient or not recipient["active"]:
            raise HTTPException(
                status_code=404,
                detail="Recipient not found — create it first (or it was deactivated)",
            )

    transfer_payload = {
        "source": "balance",
        "amount": cents,
        "recipient": data.recipient_code,
        "reason": data.reason,
        "currency": settings.paystack_currency,
    }
    if data.idempotency_key:
        # Paystack dedupes on reference — a retried initiate with the same key
        # returns the original transfer instead of sending money twice.
        transfer_payload["reference"] = data.idempotency_key

    try:
        async with httpx.AsyncClient(
            base_url=PAYSTACK_API_BASE, headers=_PAYSTACK_HEADERS, timeout=15
        ) as client:
            resp = await client.post(
                "/transfer",
                json=transfer_payload,
            )
        if resp.status_code != 200:
            logger.error(
                "Paystack initiate transfer failed (HTTP %s): %s",
                resp.status_code, resp.text[:500],
            )
            raise HTTPException(status_code=502, detail="Transfer failed. Please try again.")
        body = resp.json()
        if not body.get("status"):
            raise HTTPException(status_code=502, detail="Transfer failed. Please try again.")
        ps_data = body.get("data") or {}
        transfer_code = ps_data.get("transfer_code")
        if not transfer_code:
            raise HTTPException(status_code=502, detail="Paystack returned no transfer code.")
    except httpx.HTTPError as e:
        logger.error("Paystack initiate transfer request failed: %s", e)
        raise HTTPException(status_code=502, detail="Transfer failed. Please try again.")

    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO payouts
                    (transfer_code, recipient_code, recipient_name, amount_cents,
                     currency, reason, status, reference, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING *
                """,
                transfer_code,
                data.recipient_code,
                recipient["name"],
                cents,
                settings.paystack_currency,
                data.reason,
                ps_data.get("status") or "pending",
                ps_data.get("reference"),
                admin_id,
            )
        except UniqueViolationError:
            # Concurrent same-key initiate: the other request's row already
            # committed (Paystack deduped on reference). Return it — but if
            # the re-select finds nothing (e.g. the violation was on a
            # different constraint), surface a clear 409 instead of crashing.
            row = await conn.fetchrow(
                "SELECT * FROM payouts WHERE reference = $1",
                data.idempotency_key,
            )
            if row is None:
                logger.error(
                    "Unique violation on payout insert with no matching row "
                    "(recipient=%s, reference=%s)",
                    data.recipient_code, data.idempotency_key,
                )
                raise HTTPException(
                    status_code=409,
                    detail="Transfer already recorded but could not be retrieved — please refresh.",
                )
    logger.info(
        "Admin %s initiated payout %s (KES %s) to %s",
        admin_id, transfer_code, data.amount, data.recipient_code,
    )
    record_event(
        "payout_initiate",
        detail=f"{admin_id} -> {data.recipient_code} = KES {data.amount}",
    )
    return _payout_from_row(row)


@router.get("/{transfer_code}", response_model=PayoutResponse)
async def fetch_payout(
    transfer_code: str,
    _: UUID = Depends(require_admin),
):
    """Fetch a transfer from Paystack and refresh the local status.

    The local row is checked FIRST so an unknown transfer_code 404s without
    making an outbound call. The response from Paystack is authoritative — a
    transfer stuck in `pending` locally but `success` upstream gets corrected
    here.
    """
    pool = await get_db_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM payouts WHERE transfer_code = $1",
            transfer_code,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Transfer not found")

    try:
        async with httpx.AsyncClient(
            base_url=PAYSTACK_API_BASE, headers=_PAYSTACK_HEADERS, timeout=15
        ) as client:
            resp = await client.get(f"/transfer/{transfer_code}")
        if resp.status_code != 200:
            logger.error(
                "Paystack fetch transfer failed (HTTP %s): %s",
                resp.status_code, resp.text[:500],
            )
            raise HTTPException(status_code=502, detail="Could not fetch transfer.")
        body = resp.json()
        if not body.get("status"):
            raise HTTPException(status_code=502, detail="Could not fetch transfer.")
        ps_data = body.get("data") or {}
    except httpx.HTTPError as e:
        logger.error("Paystack fetch transfer request failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not fetch transfer.")

    async with pool.acquire() as conn:
        new_status = ps_data.get("status")
        if new_status and new_status != row["status"]:
            await conn.execute(
                "UPDATE payouts SET status = $1, updated_at = NOW() WHERE transfer_code = $2",
                new_status,
                transfer_code,
            )
            row = await conn.fetchrow(
                "SELECT * FROM payouts WHERE transfer_code = $1",
                transfer_code,
            )
    return _payout_from_row(row)
