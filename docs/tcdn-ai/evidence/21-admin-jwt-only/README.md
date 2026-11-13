# 21 — no API key may carry admin authority (MED, product)

## The problem

`app/routes/admin.py` defined two admin dependencies. `require_admin` accepted
whatever `get_current_user_id` accepted — a session JWT **or** a `tcdn_` API
key — and all **10** `/admin/*` routes used it. On the payout surface, the three
money/bank-detail writes already used `require_admin_jwt` (audit F-6), but the
four reads (`/balance`, `/recipients`, `GET /billing/payouts`, `GET
/billing/payouts/{code}`) still accepted an API key.

An admin's API key is the credential built to be pasted into a deployment env,
a CI variable or a script — exactly the one that leaks. With this gap it carried
its owner's admin authority over the whole panel, including the read endpoints
that are the reconnaissance for a transfer.

## The fix

- every route in `app/routes/admin.py` (10) and `app/routes/payouts.py` (7) now
  depends on `require_admin_jwt` → `get_current_user_id_jwt_only`, which refuses
  a `tcdn_` credential **before** any database lookup, so a key cannot even be
  used as an admin oracle;
- `require_admin` is **deleted**, not deprecated: an unused API-key-capable
  admin dependency is an invitation for the next route to use it. A test asserts
  it is gone.

## Evidence

| File | What it is |
| --- | --- |
| `tests-before.log` | the new tests on `main`: **9 failed, 6 passed, 9 skipped** |
| `tests-after.log` | the same file with the change: **15 passed, 9 skipped** |

New coverage in `backend/tests/test_admin_jwt_only.py`:

- **request-level**: a syntactically valid `tcdn_` key whose owner the mocked DB
  reports as admin is refused with `403 "requires a session token"` on every
  admin read (`/admin/stats`, `/admin/users`, `/admin/analytics`,
  `/admin/health`) and every payout read (`/balance`, `/recipients`,
  `GET /billing/payouts`) — and the refusal happens before any `is_admin`
  query;
- **structural**: a route-dependency walk over the whole app asserts that
  **every** route under `/admin`, `/admin/*`, `/billing/payouts` and
  `/billing/payouts/*` has `require_admin_jwt` in its dependency tree, so a
  route added later cannot quietly regress; the walk fails if it finds fewer
  routes than the known read list (so it cannot pass by finding nothing);
- **guardrail**: `app.routes.admin` must not export `require_admin` any more.

Existing controls still hold: an admin **session** JWT reaches the routes, a
non-admin session is 403, and the money writes stay API-key-proof.

Regression: `test_admin.py`, `test_payouts.py`, `test_billing.py` and this file
— 95 passed, 9 skipped. `ruff check` clean.

## Notes

- The AI admin namespace (`/api/ai/v1/admin/*`) already used `require_admin_jwt`
  from the F-6 fix; its sweep is untouched.
- This narrows credentials, not permissions: an admin JWT behaves exactly as
  before.
