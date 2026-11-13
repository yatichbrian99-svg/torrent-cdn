# 19 — one Paystack webhook URL, two products (HIGH)

## The problem

Paystack allows **one** webhook URL per mode. The AI console had its own
(`POST /api/ai/v1/billing/webhook`) and subscriptions used
`POST /billing/webhook`, so whichever single URL is configured in the dashboard
receives only one product's events. With `/billing/webhook` configured, every
credit-pack purchase is acknowledged as "Not a subscription charge" and no
credits are granted; with the AI URL configured, plan upgrades never arrive.

## The fix

`POST /billing/webhook` is the dispatcher. After its existing single signature
check and JSON parse it looks at `data.metadata.purpose`:

| Event | Where it goes |
| --- | --- |
| `charge.success` with `metadata.purpose == "ai_credits"` | the AI coordinator's `handle_payment_webhook`, called with the **raw body and the same signature** (it re-verifies; a router bug cannot bypass authentication) |
| subscription `charge.success` (with a `plan`) | the existing plan upgrade, byte-for-byte |
| `transfer.success/failed/reversed/otp` | the existing payout status sync |
| anything else | acknowledged (200), deduped, no work |

Refusal mapping (the coordinator's own refusals are already audited):

| Coordinator outcome | HTTP | Why |
| --- | --- | --- |
| `payment_needs_review`, `payment_currency_mismatch`, `payment_amount_mismatch`, `unknown_credit_pack`, `invalid_payment_event` | **200** with the reason | deterministic: a retry cannot change the outcome, and Paystack would otherwise redeliver for ~a day |
| gates (`payments_not_live`, paused, allowlist, caps) and provider errors | **500** | transient/config: the retry can succeed, and the ledger key makes it idempotent |
| anything unexpected | **500** | never acknowledge an unknown failure |
| AI console unavailable on this deployment | **200**, logged | a deployment without the console must not 500 the URL forever |

Docs: `docs/tcdn-ai/STAGING.md` §3.2 and `docs/tcdn-ai/PAYMENTS_LIVE.md` now name
the single URL (`https://<domain>/billing/webhook`) and warn against also
configuring the AI endpoint.

## Evidence

| File | What it is |
| --- | --- |
| `tests-before.log` | `backend/tests/test_billing_webhook_dispatch.py` on `main` before the dispatcher: **6 failed, 7 passed** |
| `tests-after.log` | the same file after: **13 passed** |

Why some tests pass on `main`: a KES pack event on the old code fell through to
the subscription branch, matched no plan, and returned 200 "Not a subscription
charge" — the same status, for the wrong reason, with no credit. The tests that
actually pin the routing (coordinator receives the exact body/signature, final
refusal acknowledged with its code, transient failure 500, disabled console
acknowledged) fail before and pass after.

Regression guard: `test_billing.py` (subscription/transfer behaviour) and
`test_ai_billing.py` + `test_authorization_matrix.py` all still pass — 55 passed,
11 skipped across the four files.
