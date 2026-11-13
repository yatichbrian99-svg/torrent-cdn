# 18 — KES credit packs (HIGH)

## The bug

`POST /api/ai/v1/billing/checkout` initialises Paystack in **KES**
(`routes_console.py`, `currency: "KES"`, `amount: pack["kes_minor"]`), but
`PaystackPaymentProvider.verify_webhook` refused anything but USD:

```
app.ai.errors.ValidationError: only USD payments can be converted to credits
```

A customer who paid KES 650 for the `starter` pack was charged and received no
credits: Paystack retried the signed `charge.success` and got the same refusal
every time. `tests-before.log` is that reproduction, produced by running the new
test file against `main` before the fix: **12 failed, 2 passed**.

## The fix

The event's metadata (`purpose: "ai_credits"`, `pack_id`) selects a pack-priced
path. KES is never relabelled as USD — that would credit 650 USD-cents worth of
usage for a KES 650 charge, ~130× (KES_PER_USD) over-crediting.

1. The pack is looked up **server-side** in `console.PACKS` by `pack_id`.
2. The event's `currency` must equal the pack's currency and its `amount` must
   equal the pack's `kes_minor` **exactly**. Anything else is refused
   (`payment_currency_mismatch`, `payment_amount_mismatch`,
   `unknown_credit_pack`) and audited as
   `billing.credit_purchase_needs_review` with the expected value.
3. The transaction is re-checked with Paystack's `GET /transaction/verify/{ref}`
   **by reference** (`PaystackPaymentProvider.verify_transaction`). A status,
   currency, amount, account or pack mismatch, or any HTTP/parse error, fails
   closed: no credit, and a `billing.credit_purchase_needs_review` audit entry
   carrying the reason (`verify_amount_mismatch`, `verify_error`, …).
4. Only then is the ledger credited, with `pack.credit_micro` — never a number
   from the event or its metadata.
5. Idempotency is unchanged: `payment:{provider}:{reference}` is the ledger
   key, so a replay credits once and never re-hits the verify API.

Legacy (non-pack) charges keep the existing USD-only conversion, so
`test_non_usd_currency_refused` still passes unchanged.

## Evidence

| File | What it is |
| --- | --- |
| `tests-before.log` | The new test file run against `main` before the fix: 12 failed / 2 passed, first failure `unsupported_currency`. |
| `tests-after.log` | The same file after the fix: 14 passed. |

Coverage (`backend/tests/test_ai_credit_pack_kes.py`):

- a signed **KES** `charge.success` for `starter` credits exactly
  `5_000_000` micro (5 USD-cents-worth of credits), ledger invariant holds;
- metadata `credit_micro` of 999,000,000,000 is ignored — the pack is the price;
- the audit entry records pack id, currency `KES`, `amount_minor` 65000;
- replay credits once and calls the verify API once;
- wrong amount (64999), wrong currency (USD), unknown pack id and missing pack
  id are all refused with **no** ledger entry and **no** verify call;
- verify-API amount mismatch / non-success status / transport error / pack
  mismatch each produce `payment_needs_review`, no credit, and a
  `billing.credit_purchase_needs_review` audit row;
- a bad signature never reaches the ledger.

## Still open

- **Test-mode run against real Paystack**: not possible from this environment —
  no `sk_test_…` key or sandbox account is configured here, and the repo's
  Playwright/Paystack e2e path needs one. Command to run once a sandbox key
  exists (checkout → pay with the test card → confirm the webhook lands):

  ```bash
  cd backend
  AI_PAYMENT_PROVIDER=paystack PAYSTACK_SECRET_KEY=sk_test_… \
    .venv/bin/python scripts/paystack_pack_probe.py --pack starter
  ```

  (the probe script is **not** in this PR; the smallest honest substitute is the
  Paystack dashboard's "Test webhook" button pointed at
  `/api/ai/v1/billing/webhook` with a `charge.success` body carrying
  `metadata.purpose=ai_credits` and `pack_id=starter`.)
- Item 2's dispatcher is what routes a single Paystack webhook URL to this
  handler; until it lands, the AI webhook URL is configured separately.
