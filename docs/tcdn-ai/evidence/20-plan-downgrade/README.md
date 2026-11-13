# 20 — plan downgrades (MED, live today)

## The problem

The Paystack webhook only ever upgraded a plan (`charge.success` with a `plan`
object). Nothing handled the events that mean a subscription has stopped being
paid for, so a customer who cancelled, did not renew, let the subscription
expire, or whose renewal charge failed kept the paid tier — and its limits —
for free, indefinitely.

## The rule implemented

D **not** cut the plan off at the event: the customer keeps what they paid for
until the period ends. Instead:

1. a lifecycle event records `users.plan_expires_at` (the period end) and
   `users.pending_plan = 'free'`, and writes
   `billing_audit_log.billing.plan_downgrade_scheduled`;
2. the downgrade is applied the first time the account is read after that
   instant (`GET /billing/status` and the webhook itself run the same helper),
   writing `billing.plan_downgraded`, an in-app notification, and an email
   (`send_plan_downgrade_email`);
3. a later event with an **earlier** end date wins (`LEAST`), so a duplicate or
   out-of-order delivery cannot extend a paid plan;
4. a missing or unparseable `next_payment_date` is "effective now", never
   "keep the plan forever".

| Event | Effect |
| --- | --- |
| `subscription.disable` | schedule at period end (or now) |
| `subscription.not_renew` | schedule at period end (or now) |
| `subscription.expiry` | plan is already unpaid → downgrade now |
| `invoice.payment_failed` | schedule at period end (dunning, not an instant cut) |

The subscriber is resolved from `metadata.user_id` when present, otherwise from
the customer email Paystack sends on subscription events; an unknown subscriber
is acknowledged and ignored.

No cron is introduced. `plan_lifecycle.apply_due_downgrade` is the single
enforcement point, called from the status read and the webhook, and its state
check (`pending_plan` set, `plan_expires_at <= now`, `plan != pending`) makes it
safe under concurrency.

### Schema

`backend/app/main.py` startup DDL adds, idempotently:

- `users.plan_expires_at TIMESTAMPTZ`
- `users.pending_plan VARCHAR(50)`
- `billing_audit_log(id, user_id, event, detail JSONB, created_at)` + index
- partial index `idx_users_plan_expiry`

### Documentation

`docs/tcdn-ai/PAYMENTS_LIVE.md` now lists every Paystack event that must be
delivered, what each one does, and how the downgrade is applied. A test
(`test_documented_events_match_the_handled_set`) fails if the handled set and
the documentation drift apart.

## Evidence

| File | What it is |
| --- | --- |
| `tests-before.log` | the new tests on `main` (route/email/schema changes stashed): **5 failed, 8 errors, 8 passed** |
| `tests-after.log` | the same tests with the change: **21 passed** |

Coverage (`backend/tests/test_billing_plan_downgrade.py`):

- each of the four events schedules a downgrade, leaves the current plan alone
  while the period is still running, writes the audit row, and sends no email
  yet;
- the audit row carries the reason, the target plan and the effective date;
- subscription events without metadata resolve the account by email;
- an unknown subscriber is ignored with no write;
- an already-expired period downgrades in the same request: plan UPDATE, audit
  row, notification, and exactly one email (`from_plan` → `free`);
- a missing period end is treated as now;
- `GET /billing/status` applies a due downgrade and leaves a live one alone;
- an email failure does not fail the webhook or lose the downgrade;
- Paystack timestamp forms parse; junk parses to `None`;
- the handled event set matches `PAYMENTS_LIVE.md`.

Regression: `test_billing.py` (subscription/transfer paths) and `test_admin.py`
still pass — 65 passed across the three files. `ruff check` clean.

## Not done here

- No provider round trip: no Paystack sandbox key in this environment, so the
  event shapes are the documented Paystack ones (see PAYMENTS_LIVE.md) and the
  live-test procedure is documented, not executed.
- Refunds/chargebacks (`refund.processed`) remain unhandled; that is a separate
  finding, not part of this one.
