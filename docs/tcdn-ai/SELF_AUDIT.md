# Self-audit — 2026-11-11

An independent-review read of the TCDN-AI branch (`fix/tcdn-ai-redis-replay-nonce`)
plus the product branch it will rebase onto. Every status here comes from a
command that was run on this commit; where something could not be run, it says
so and is not counted as a pass.

**Commit audited:** `d14b211` (branch `fix/tcdn-ai-redis-replay-nonce`)
**Numbers in this file are from the runs recorded under `docs/tcdn-ai/evidence/`.**

---

## 1. What was actually exercised

| Area | Command | Result |
|---|---|---|
| Whole backend suite, AI flag on | `ENABLE_TCDN_AI=true pytest -q` | **1356 passed, 30 skipped, 0 failed** (234 s) |
| The 30 skips, re-run with real services | `TEST_POSTGRES_URL=… TEST_REDIS_URL=… pytest tests/test_ai_migrations.py tests/test_ai_postgres.py tests/test_ai_schema_contract.py tests/test_ai_two_instances_live.py` | **28 passed** (Postgres 16.4-alpine, Redis 7.4.2-alpine, disposable containers) |
| Authorization matrix | `pytest tests/test_authorization_matrix.py` (flag on) | **21 passed** |
| Money paths | billing, idempotency, admin refunds, node billing, payments-live, signup abuse | **115 passed** |
| Crypto / signed inputs / contracts | `test_ai_core`, `test_ai_signed_inputs`, `test_ai_contracts` | **122 passed** |
| SSRF / SQLi (static + dynamic) / binding | `test_ai_ssrf_pinning`, `test_ai_sqli_routes`, `test_ai_sqli_binding`, `test_sql_injection` | **71 passed** |
| Rate limits / abuse | `test_ai_security`, `test_ai_abuse` | **39 passed** |
| Failure injection, Redis lanes, two instances | `test_ai_failure_injection`, `test_ai_redis`, `test_live_degraded_redis`, `test_ai_two_instances` | **37 passed** |
| Secrets in logs | `test_ai_canary_scan` | **3 passed** |
| Flag **off** | `env -u ENABLE_TCDN_AI pytest tests/test_ai_migrations.py` / `test_authorization_matrix.py` | **8 passed** (no AI DDL) / **11 passed, 10 skipped** (AI routes absent) |
| Python deps | `pip-audit -r requirements.txt` | **No known vulnerabilities found** |
| Node deps (frontend) | `npm audit --package-lock-only` | **3 moderate, all dev-only vitest** (see F6) |
| Type/format | `mypy --config-file mypy-ai.ini`, `ruff format --check app/ai tcdn_ai_sdk` | **Success, 40 files** / **already formatted** |

Evidence files: `evidence/01-tests/full-suite-flag-on-2026-11-11.txt`,
`evidence/01-tests/skips-flag-on-2026-11-11.txt`,
`evidence/01-tests/real-pg-redis-lanes-2026-11-11.txt`,
`evidence/03-security/self-audit-suites-2026-11-11.txt`,
`evidence/03-security/flag-off-behaviour-2026-11-11.txt`.

### Not exercised (and therefore not claimed)

* **A live Paystack charge, decline, refund or M-Pesa.** No live key was used
  (prohibited) and no sandbox session is available. Row 4.4 stays `PARTIAL`.
* **ZAP/DAST locally.** No scan was launched from this environment. It *was*
  run by CI on this commit: the `ZAP (unauthenticated + JWT + AI key, flag on)`
  check is **green** on `47e6b93` (run `43117987233`), so the DAST evidence for
  this commit comes from CI, not from the audit workstation. The gate itself is
  covered here by `test_zap_gate.py` (10 passed).
* **Container image scan.** `trivy` is not installed in this environment, so the
  local claim is "not run". CI's `container-scan` job is the evidence path; it is
  path-filtered on `backend/**`, `Caddyfile`, `docker-compose.yml`, `render.yaml`.
* **A copy of a production-shaped database.** The migration proof used a fresh
  Postgres 16.4 database and a synthetic "previous version" schema, not a real
  data dump. What that does not cover: data volume, long-running locks, and
  index build time on production-sized tables.
* **The frontend build on a clean machine** — it was run on this workstation
  (`npm run build` passes, 20 test files / 179 tests), not in a fresh container.

---

## 2. OWASP Top 10 (2021), mapped to this code

| # | Category | Where | Evidence / status |
|---|---|---|---|
| A01 | Broken access control | `app/ai/routes.py` (`require_admin_jwt` on `/admin/*`, scoped `authenticate` on `/api/ai/v1/*`); `app/auth.py` | `test_authorization_matrix` 21 passed, incl. every `/api/ai/v1` input route refusing a JWT-only and an unscoped caller. **Open on `main` until #46 merges:** the logout/MFA revocation path (F2). |
| A02 | Cryptographic failures | `app/ai/crypto.py`, `keyring.py`, `tickets.py`, `verifier_sign.py`; host `app/auth.py` (JWT) | 122 crypto/contract tests: Ed25519 signatures, HKDF domain separation, ticket TTL + skew, key rotation with overlap, revocation. No hand-rolled primitive is used; `CRYPTO_REVIEW.md` records the reasoning. |
| A03 | Injection | Every asyncpg call in `app/ai/store.py`, `app/routes/live.py` | Static AST audit `[OK] ALL DB CALLS PARAMETERIZED`; dynamic proof **12 routes / 216 requests** with 8 payload families, asserting no 5xx, no DB error text, no state change, no timing blow-up; statement-level binding proof. Template injection: no user-controlled template is rendered. |
| A04 | Insecure design | `app/ai/config.py` startup gate; money paths | Production refuses to boot on simulated attestation, dev backend, fake payments, burn without legal ack, and (new) live payments without an allowlist. |
| A05 | Security misconfiguration | `app/main.py` (CSP, `nosniff`, HSTS), CI gates | Headers set in `main.py:380-433`. Deployment posture: branch protection was **absent** until today (F4). |
| A06 | Vulnerable components | `requirements.txt`, `frontend/package-lock.json` | `pip-audit`: clean. `npm audit`: 3 dev-only moderates (F6). Backend image scan is CI-only and was **not** run locally. |
| A07 | AuthN failures | `app/auth.py`, `app/ai/keyring.py` | Argon2 password hashing (host), API keys stored as peppered hashes with a status column; MFA paths covered in `test_auth.py`. Token lifetime bounded on `#46` (not yet on `main`). |
| A08 | Data/deserialization integrity | `app/ai/models.py` (strict pydantic), `app/ai/receipts.py` | No `pickle`, no `yaml.load`, no `eval` on untrusted input in `app/`; the only `eval` is Redis Lua (`redis_cache.py`). `shards.py` refuses `pickle/pt/bin` model formats by name. Signed envelopes validated before use. |
| A09 | Logging/monitoring failures | `app/ai/*`, host logging | 26 logger calls under `app/ai/`; none interpolates a prompt, token, API key or request body (grep in this audit). `test_ai_canary_scan` passes: canary strings do not reach logs. Audit table `ai_audit_log` records admin and money actions with actor + reason. |
| A10 | SSRF | `app/ai/ssrf.py` (`validate_url` → `PinnedTarget`), `app/routes/upload.py` (import-by-url) | DNS resolved then **pinned** to the validated address; private/link-local/metadata ranges refused. `test_ai_ssrf_pinning` is in the 71-pass injection group. |

---

## 3. Authentication and authorization per route

Re-run rather than remembered: `test_authorization_matrix.py` enumerates the
routers and asserts refusals, so a new route is covered automatically.

* `/api/ai/v1/*` — requires a scoped `tcdn_ai_` key; a session JWT is refused
  (`authenticate` wants the key, not a cookie). Worker-scope keys cannot place
  jobs; inference-scope keys cannot submit receipts.
* `/api/ai/v1/admin/*` — requires an **admin JWT** (`require_admin_jwt`), a
  different authority from an AI key. Covered for api-keys, kill-switch,
  refunds, verification, and the new `/payments-live`.
* `/api/ai/v1/billing/webhook` — no key by design; the HMAC-SHA512 signature over
  the raw body is the authority, and it now sits behind the live-payments gate.
* Host routes (`/auth`, `/files`, `/billing`, `/admin`, `/api/live`) — JWT or
  session cookie as before; the matrix asserts the anonymous/expired/wrong-role
  refusals.

---

## 4. Money paths

| Risk | Control | Evidence |
|---|---|---|
| Double-spend of a hold | `place_hold` uses `pg_advisory_xact_lock(hashtext($1))` and a balance check inside the transaction | `test_ai_postgres` races pass on real Postgres |
| Replay of a webhook | idempotency key `payment:<provider>:<reference>`, unique in `ai_ledger_entries`; replay answered before caps | `test_ai_billing`, `test_ai_payments_live` (replay test) |
| Negative / zero / overflow amounts | `_check_amount`; provider refuses `amount <= 0`; per-purchase cap | `test_ai_billing`, `test_ai_payments_live` |
| Credit from a forged event | HMAC over raw body, constant-time compare; forged/tampered bodies refused | `test_ai_billing` (forged, tampered, replayed) |
| Hold leaks | holds are closed by settlement or expiry; ledger sums to zero | `test_ai_node_billing`, `test_ai_core` invariant checks |
| Refund race | admin refund requires a `reference` that doubles as the idempotency key | `test_ai_admin_refunds` |
| Refund vs. payout switch | refunds refuse while payouts are paused (deliberate, money-out) | `test_ai_admin_refunds` |
| Signup-credit farming | per-IP / per-device / per-domain velocity windows, monthly budget, daily cap, disposable-domain block | `test_ai_signup_abuse` |
| Money in with no way to stop it | **finding F1, fixed this session** | `test_ai_payments_live` 23 passed |

---

## 5. Crypto

* Ed25519 for coordinator/node signatures; HKDF with distinct domain prefixes per
  use (ticket, receipt, verification sample) — no key reuse across domains.
* Nonces are single-use and consumed atomically (`nonce_store`), replayed tickets
  raise `ReplayError`.
* No custom primitive is implemented: `cryptography` primitives only.
* API keys: `sha256(pepper || key)`, peppered from the environment, production
  refuses to start without a pepper.
* **Not verified:** that the nonce store is durable across a Redis restart with
  every deployment's configuration; the two-instance lane proves sharing, not
  eviction policy under memory pressure.

---

## 6. Rate limits, headers, paths

* `app/ai/ratelimit.py` — per-key token bucket, shared through Redis with a
  local fallback; `test_ai_security`/`test_ai_abuse` cover the limits and the
  degraded path (`test_live_degraded_redis`).
* Headers — `app/main.py` sets CSP (or report-only per config), `nosniff`, HSTS
  when TLS is required.
* Path traversal — filenames/paths go through `security.py` validators and are
  never used to build a filesystem path directly; uploads stream to object
  storage. `test_upload_security` and `test_files_dfs` cover the refusals.

---

## 7. Flag-off behaviour

Proven, not asserted: with `ENABLE_TCDN_AI` unset,

* the `/api/ai/v1` routers are **not mounted** (`test_authorization_matrix`
  11 passed / 10 skipped — the AI cases skip precisely because the routes are
  absent);
* startup issues **no `ai_*` DDL** and the legacy app boots and serves
  (`test_ai_migrations` 8 passed on a real Postgres);
* no `TCDN_AI_*` setting changes existing behaviour: the flag gate is
  `is_ai_enabled_for_host`, and every AI code path is behind a mounted router or
  an AI-scoped dependency.

`TCDN_AI_PAYMENTS_LIVE` is off by default and does not alter the fake-provider
development path (`test_ai_payments_live::test_fake_provider_still_credits_with_the_flag_off`).

---

## 8. Migration safety

`AI_TABLES_DDL` is a hand-written idempotent block. Against real Postgres 16.4:

* fresh database → 14 tables; running the block twice is a no-op;
* a database from a previous version upgrades by re-running the block, existing
  rows intact;
* `ALTER TABLE … ADD COLUMN IF NOT EXISTS` is re-runnable;
* dropping every `ai_*` table is reversible for `users` (CASCADE leaves it
  intact) and the app returns to "AI enabled, no data";
* a missing column is **not** reconciled by `CREATE TABLE IF NOT EXISTS` — pinned
  by `test_missing_column_is_not_reconciled`, so a future column must ship as an
  explicit `ALTER`.

Limit: synthetic schema, not production data volume (see §1).

---

## 9. Failure modes

| Failure | Behaviour | Evidence |
|---|---|---|
| Redis down | limiter degrades rather than 500s; session revocation fails **open** (documented) | `test_live_degraded_redis`, `test_ai_redis` |
| Postgres down | requests fail with 5xx and a typed error; no partial ledger writes (single transaction per posting) | `test_ai_failure_injection` |
| Worker misbehaviour | receipt over-claim → lower count billed + node flagged; sampled jobs re-executed; over-claim rate suspends payouts | `test_ai_node_billing`, `test_ai_redundant_execution` |
| Provider webhook storm | replay answered early; caps bound the day | `test_ai_payments_live` |

---

## 10. Findings, ranked

### F1 — HIGH — A signed Paystack webhook credited any account, with no switch
*Was:* `handle_payment_webhook` verified the signature and credited immediately.
Any account id in a signed event was credited; the only way to stop it was to
remove the provider key.
*Now:* `TCDN_AI_PAYMENTS_LIVE` (default off), owner allowlist, per-purchase and
ledger-backed daily caps, a shared kill switch, and a reconciliation script.
*Evidence:* `test_ai_payments_live` 23 passed.
*Residual:* no live charge has been made (row 4.4). The first live transaction
is still the first real test of the provider round trip.

### F2 — HIGH — Logout and MFA revocation were silent no-ops on `main`
*Was:* the blocklist check sat inside `except Exception`, swallowing
`HTTPException`; logout wrote the key and the token kept working.
*Now:* fixed on `fix/product-security-hardening` (PR #46, ready for review,
CI green on `41b4ae3`).
*Open:* **`main` still has the bug** until #46 merges. This is the highest-value
merge in the queue.

### F3 — MEDIUM — NUL bytes in viewer payloads caused 500s
Fixed on #46 (`94022d2`, 12 tests) and independently on this branch (`80ac4ff`).
`main` still returns 500 for those payloads until #46 merges.

### F4 — MEDIUM — Repository controls were off
No branch protection, secret scanning disabled, push protection disabled.
*Now:* protection on `main` (PR required, strict up-to-date, 8 required checks,
no force-push, no deletion, admins included), secret scanning + push protection
enabled. Evidence: `evidence/03-security/branch-protection-2026-11-11.json`.
*Open:* `DAST` and `Full-stack Docker` are **not** among the required checks
(see F7).

### F5 — MEDIUM — `main`'s scheduled workflows have been red for days, for four distinct reasons
`Dependency Audit`, `Tests & Security Audits` and the E2E delivery gate failed
daily from at least 2026-11-08 to 2026-11-11 on `200109e`. The GitHub jobs API
first returned HTTP 502 for every run; it answered on 2026-11-12, and the job
logs give four separate causes — all of them fixed or addressed on this branch:

1. **Dependency Audit** — the step is `Fail on new advisories vs baseline`, with
   `critical=1 high=3 moderate=5 npm=9 new=8`. Dependabot shows 22 open alerts on
   the default branch, including two CRITICAL/HIGH `next` advisories
   (`GHSA-2464-fhr4-w9xw`, `GHSA-p3m5-59mp-jv5r`). The `next` bump to 15.5.29 on
   this branch is why `dependency-audit` is green here (35 s).
2. **`Tests & Security Audits` → `static-audit`** — the failing step is `Gitleaks
   — scan for hardcoded secrets`, `leaks found: 7`, all in test/guard fixtures
   (`REDACTED` in the log). This branch's value-scoped `.gitleaks.toml`
   (commit `18418db`, fixtures in `1a789aa`) is why `gitleaks` is green here —
   and that same config still reports a planted `ghp_…` token, which is the
   property that matters (row 1.5).
3. **`frontend-checks`** — `Build` fails with `An error occurred in next/font /
   TypeError: Cannot read properties of null (reading '1')`. Green here after the
   same `next` bump.
4. **E2E delivery gate and the four live probes** — the suite reports
   `E2E_EMAIL account is not in the MFA-free state the journey starts from
   (disable MFA first)`. This is **not** evidence that `E2E_ADMIN_TOKEN` is
   revoked; it is an account-state problem in the owner's E2E identity. Only the
   owner can change it (F8).

*Action:* merge this branch (or #46 plus the bump) and re-check; the E2E account
state needs an owner decision either way.

### F6 — LOW (accepted) — 3 dev-only advisories in vitest
`vitest`, `@vitest/mocker`, `@vitest/coverage-v8` 4.1.11. Fix requires vitest 5,
which needs Node ≥22.12; the image and CI run Node 20. Dev-only, never shipped
(the runtime image prunes dev dependencies). Accepted, recorded here.

### F7 — LOW — Two CI workflows cannot be required checks
`dast-zap.yml` and `ci-fullstack.yml` have `paths:` filters, so they produce
**no check run** for a PR that does not touch backend/docker paths. Requiring
them would leave such PRs permanently unmergeable. Either the filters are
removed (DAST then runs on every PR, including docs-only ones) or the checks
stay advisory. Owner decision.

### F8 — LOW — one Action secret is missing and the E2E account is in the wrong state
Present: `E2E_EMAIL`, `E2E_PASSWORD`, `E2E_ADMIN_TOKEN` (last updated
2026-08-07). Referenced but absent: `R2_PUBLIC_URL` (has an inline default, so
not fatal). The E2E gate's red is **not** a token problem after all — its log
says `E2E_EMAIL account is not in the MFA-free state the journey starts from
(disable MFA first)` (F5.4). Whether the token itself is still valid remains
unproven, because using it against the live backend is out of bounds.
*Owner:* disable MFA on the E2E account, or make the journey handle the second
factor; then re-run the gate.

### F9 — LOW — Reconciliation is a script, not a schedule
`backend/scripts/reconcile_payments.py` exists and is tested (7 cases) but
nothing runs it on a timer. Until it is scheduled, "money in without credits" is
found by a human running it.

### F10 — INFO — `mypy` on `app/ai` pulls in host modules with pre-existing errors
`mypy app/ai` (ad-hoc, not CI's config) reports 13 errors in `app/config.py`,
`app/database.py`, `app/routes/admin.py`, `app/routes/upload.py` — none in AI
code. CI's `mypy --config-file mypy-ai.ini` is clean (40 files). Not introduced
here; not a gate.

---

## 11. What a reviewer should not trust

* The ZAP acceptance (row 5.5) is only as good as a run that has not been
  re-executed on this commit.
* Row 4.4 (Paystack) is `PARTIAL`: code + switch + reconciliation, no live
  transaction.
* Rows 6.1 (redundant execution) and 4.4 are the only unmet P1 rows;
  `LEVEL1_EXCEPTIONS.md` says why. Level 1 is **NO**.
* Any "production-shaped" migration claim: the proof is schema-shaped, not
  data-volume-shaped.
