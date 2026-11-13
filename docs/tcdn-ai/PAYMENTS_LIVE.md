# Turning on live credit purchases (`TCDN_AI_PAYMENTS_LIVE`)

Credit purchases are **off by default**. The webhook path is complete and
tested against signed fixtures, but no real charge has ever been credited in
production (checklist row 4.4). This file is what the owner needs to run that
live test, and how to stop it.

Nothing here is a substitute for the code: the gate is
`Coordinator._authorise_live_payment` in `backend/app/ai/service.py`, and the
tests are `backend/tests/test_ai_payments_live.py` and
`backend/tests/test_billing_webhook_dispatch.py`.

## The one webhook URL to set

Paystack allows a single webhook URL per mode. Configure exactly:

```
https://<your-domain>/billing/webhook
```

That router receives every event and dispatches after one signature check:
`metadata.purpose == "ai_credits"` goes to the AI coordinator (pack-priced
credits, reference-keyed idempotency), subscription and transfer events take
their existing paths, and anything else is acknowledged so Paystack stops
redelivering it. Do not configure `/api/ai/v1/billing/webhook` in the dashboard
as well — only one URL is delivered to.

---

## What the switch does

| Env var / control | Default | Effect |
|---|---|---|
| `TCDN_AI_PAYMENTS_LIVE` | `false` | A correctly-signed **Paystack** webhook is refused (`503 payments_not_live`) before the ledger write. The development `fake` provider is unaffected. |
| `TCDN_AI_PAYMENTS_LIVE_ALLOWLIST` | empty | Comma-separated account ids allowed to buy. Empty = nobody. Production refuses to boot with the flag on and this empty. |
| `TCDN_AI_PAYMENTS_LIVE_MAX_PURCHASE_MICRO` | `5000000000` | Per-transaction ceiling ($50). A larger charge is refused (`422 payment_above_cap`) — the customer is charged by Paystack but not credited, so this is a *stop* signal, not something to raise casually. |
| `TCDN_AI_PAYMENTS_LIVE_DAILY_CAP_MICRO` | `20000000000` | Sum of credited purchases in the current UTC day ($200). Measured from the ledger, so it is shared by every coordinator instance and survives a restart. |
| `POST /api/ai/v1/admin/payments-live` | — | Runtime kill switch, `{"paused": true, "reason": "..."}`. Stored in the `ai_controls` row, so it survives a redeploy and is honoured by every instance. Independent of the payouts switch: pausing money **out** does not stop money **in**, and vice versa. |
| `GET /api/ai/v1/admin/payments-live` | — | Current posture: flag, allowlist size, caps, switch state. |

Caps are in **micro-credits**: 1 credit = 1,000,000 micro = $0.01 at the peg.
$50 = 5,000,000,000.

## What a refusal looks like

All refusals happen after signature verification and before the ledger write, so
none of them can credit anything:

| Situation | HTTP | `error.code` |
|---|---|---|
| flag off | 503 | `payments_not_live` |
| switch paused | 409 | `payments_live_paused` |
| account not allowlisted | 403 | `payments_account_not_allowlisted` |
| above the per-purchase cap | 422 | `payment_above_cap` |
| daily cap reached | 429 | `payment_daily_cap_reached` |

A **replay** of an event already in the ledger is answered `200` with
`"duplicate": true` before any cap is consulted. Paystack retries unacknowledged
webhooks; a replay must neither credit twice nor consume the day's budget twice.

## Owner live-test procedure

Do this with **one** account and **one** small charge. The point is to exercise
the provider round trip, not to sell anything.

1. **Sandbox first, flag off.** With `TCDN_AI_PAYMENT_PROVIDER=paystack` and a
   `sk_test_…` key, `TCDN_AI_PAYMENTS_LIVE=false`, make a test charge. Expect
   `503 payments_not_live` in the webhook log and **no** balance change. This
   proves the gate is shut and that the provider is hitting the right URL.
2. **Open it for one account.** Set
   `TCDN_AI_PAYMENTS_LIVE=true`,
   `TCDN_AI_PAYMENTS_LIVE_ALLOWLIST=<that account id>`, and deploy. Check
   `GET /api/ai/v1/admin/payments-live` reports `enabled: true`,
   `allowlist_size: 1`, `paused: false`.
3. **Charge the minimum the provider allows** in test mode. Expect
   `200 {"status": "credited"}`. What the balance moves by depends on how the
   checkout was started:
   - **pack purchase** (`metadata.purpose = "ai_credits"`, the console's
     `/api/ai/v1/billing/checkout`): exactly the pack's `credit_micro`
     (`starter` = 5,000,000). The KES amount is never converted — the event's
     currency and amount must match the pack, and the reference is re-checked
     against Paystack's verify API first.
   - **any other charge**: `amount_minor × 1,000,000` (legacy USD conversion).

   Confirm the From/amount in the Paystack dashboard matches the ledger row
   (`SELECT * FROM ai_ledger_entries WHERE idempotency_key =
   'payment:paystack:<reference>'`).
4. **Replay the webhook** (Paystack dashboard → resend, or re-POST the same body
   and signature). Expect `200` with `"duplicate": true` and **no** second
   ledger row.
5. **Try an account that is not allowlisted.** Expect
   `403 payments_account_not_allowlisted`, no credit.
6. **Flip the kill switch**: `POST …/admin/payments-live {"paused": true,
   "reason": "live test complete"}`. Charge again — expect
   `409 payments_live_paused`. Flip it back.
7. **Reconcile**: export the day's transactions from Paystack and run
   `python backend/scripts/reconcile_payments.py --provider-export export.json
   --database-url "$DATABASE_URL"`. Expect `findings: 0`. A non-zero result is
   the bug this whole procedure exists to find.
8. **Only then** a single real charge at the smallest amount the account allows,
   repeating steps 3, 4 and 7.
9. **Close it**: set `TCDN_AI_PAYMENTS_LIVE=false` (or leave the switch paused).
   Refunds remain the admin route (`POST …/admin/refunds`, idempotent by
   reference); the kill switch does **not** refund anything.

## What this does not cover

* The provider's own behaviour: KES rounding, webhook ordering, how a partially
  refunded purchase is reported. Step 8 is the first real evidence of those.
* Refund round trips through the provider and back through a webhook — no
  `refund.processed` handling exists; refunds are issued from the admin route
  and recorded in our own ledger.
* M-Pesa. The channel is not enabled on the account; the provider adapter only
  accepts `charge.success` in USD.

## Rollback

1. `POST /api/ai/v1/admin/payments-live {"paused": true, "reason": "rollback"}` —
   instant, no deploy, blocks new credits.
2. Set `TCDN_AI_PAYMENTS_LIVE=false` and redeploy — the code path then refuses
   every real event regardless of the stored switch.
3. Reconciliation is unchanged and still safe to run: it only reads.

No schema change is involved, so there is no database step.
