"""Plan lifecycle: downgrade a paid plan at the end of the period it paid for.

The webhook used to only ever *upgrade* a plan. When Paystack reported that a
subscription was disabled, would not renew, expired, or failed to charge for a
renewal, nothing happened: the customer kept the higher limits for free,
indefinitely.

Downgrading immediately would be wrong in the other direction — a customer who
paid for the current period stays on that plan until the period ends. So an
event records *when* the plan stops being paid for (``plan_expires_at``) and the
plan it falls back to (``pending_plan``); the downgrade is applied the first time
the account is looked at after that instant, or immediately when the period has
already ended.

Both steps write a row to ``billing_audit_log`` (durable, unlike the in-memory
security feed), and the applied downgrade also notifies the customer by email
and in-app.

Everything here takes an asyncpg connection so the webhook can run the whole
thing inside one transaction and the status endpoint can reuse the same code.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

#: What a lapsed paid plan falls back to. ``free`` is the default tier for every
#: account, has no Paystack plan, and is never itself downgraded.
DOWNGRADE_TARGET = "free"


def parse_period_end(raw: Any) -> datetime | None:
    """Parse the period end Paystack sends (ISO-8601, usually with a Z).

    Returns ``None`` when the field is absent or unparseable — the caller then
    treats the cancellation as effective now, which is the fail-closed choice:
    the customer is never given a paid plan indefinitely because of a malformed
    timestamp.
    """
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        logger.warning("plan lifecycle: unparseable period end %r", raw)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def record_billing_audit(
    conn,
    event: str,
    *,
    user_id: UUID | None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append a durable billing audit row.

    A billing change that a customer can see must leave a record an operator can
    read later, and it must not depend on the log aggregator being configured.
    """
    await conn.execute(
        "INSERT INTO billing_audit_log (user_id, event, detail) VALUES ($1, $2, $3::jsonb)",
        user_id,
        event,
        json.dumps(detail or {}, default=str),
    )


async def schedule_downgrade(
    conn,
    user_id: UUID,
    *,
    period_end: datetime | None,
    reason: str,
    event: str,
    payload_reference: str = "",
) -> dict[str, Any]:
    """Mark the plan to fall back to ``free`` at ``period_end`` (or now).

    Idempotent and order-independent: a later event with an *earlier* end date
    wins, so a duplicate or out-of-order delivery cannot extend a paid plan.
    """
    effective = period_end or datetime.now(UTC)
    await conn.execute(
        "UPDATE users SET pending_plan = $2, "
        "plan_expires_at = LEAST(COALESCE(plan_expires_at, $3), $3) "
        "WHERE id = $1",
        user_id,
        DOWNGRADE_TARGET,
        effective,
    )
    await record_billing_audit(
        conn,
        "billing.plan_downgrade_scheduled",
        user_id=user_id,
        detail={
            "reason": reason,
            "event": event,
            "effective_at": effective.isoformat(),
            "target_plan": DOWNGRADE_TARGET,
            "reference": payload_reference,
        },
    )
    logger.info(
        "plan downgrade scheduled user=%s reason=%s effective=%s",
        user_id,
        reason,
        effective.isoformat(),
    )
    return {"effective_at": effective, "target_plan": DOWNGRADE_TARGET}


async def apply_due_downgrade(
    conn, user_id: UUID, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Apply the scheduled downgrade if its moment has passed.

    Returns what was applied (for the caller to email/notify), or ``None`` when
    there is nothing due. Safe to call on every read: the state check is the
    guard, so concurrent callers cannot both apply it (the second sees
    ``plan_expires_at`` cleared).
    """
    moment = now or datetime.now(UTC)
    row = await conn.fetchrow(
        "SELECT email, plan, pending_plan, plan_expires_at FROM users WHERE id = $1",
        user_id,
    )
    if not row:
        return None
    plan = row.get("plan") or DOWNGRADE_TARGET
    pending = row.get("pending_plan")
    expires = row.get("plan_expires_at")
    if not pending or expires is None:
        return None
    if plan == pending:
        # Already downgraded (a racing caller won); just clear the marker.
        await conn.execute(
            "UPDATE users SET pending_plan = NULL, plan_expires_at = NULL WHERE id = $1",
            user_id,
        )
        return None
    if expires > moment:
        return None
    await conn.execute(
        "UPDATE users SET plan = $2, pending_plan = NULL, plan_expires_at = NULL "
        "WHERE id = $1",
        user_id,
        pending,
    )
    await record_billing_audit(
        conn,
        "billing.plan_downgraded",
        user_id=user_id,
        detail={"from_plan": plan, "to_plan": pending, "at": moment.isoformat()},
    )
    await conn.execute(
        "INSERT INTO notifications (user_id, type, message) VALUES ($1, 'billing', $2)",
        user_id,
        f"Your {plan.title()} plan ended. You are now on the {pending.title()} plan.",
    )
    logger.info("plan downgraded user=%s from=%s to=%s", user_id, plan, pending)
    return {
        "user_id": str(user_id),
        "email": row.get("email") or "",
        "from_plan": plan,
        "to_plan": pending,
        "applied_at": moment,
    }
