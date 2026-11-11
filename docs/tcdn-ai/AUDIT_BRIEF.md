# Audit brief — TCDN-AI dark release

**For a reviewer with no prior context.** Everything needed to judge the claims
is in this repository: the code, the tests, the commands, the captured output,
and an explicit list of what is *not* proven.

| | |
|---|---|
| Repository | `github.com/yatichbrian99-svg/torrent-cdn` (public) |
| Branch under review | `fix/tcdn-ai-redis-replay-nonce` |
| Commit | code audited at `d14b211`; the audit-package commit on top of it adds only this brief, `SELF_AUDIT.md` and the evidence captures. The archive is built from that commit and its filename carries the exact short hash |
| Product branch | `fix/product-security-hardening` (PR #46), the fixes intended to land on `main` first |
| Default branch | `main` (`200109e`), which does **not** yet contain either branch |

---

## 1. What the system is

Two things live in this repository.

1. **The CDN product** — FastAPI backend (`backend/``) + Next.js frontend
   (`frontend/`): accounts, email verification + MFA, file upload/import,
   torrent-based delivery, live streaming control, Paystack billing for the CDN
   product itself.
2. **TCDN-AI (dark release)** — a compute marketplace behind
   `ENABLE_TCDN_AI=false` by default: an OpenAI-compatible gateway, a
   coordinator/worker protocol for distributed inference, a Python SDK with
   end-to-end encryption, credit billing, node payouts and a verification
   sampler. **It is off in production and this branch does not turn it on.**

This audit covers both, but the AI surface is where the new risk is.

---

## 2. Scope

**In scope**
* `backend/app/**` — every route, auth path, money path and the AI coordinator.
* `backend/app/ai/**` — cryptography, tickets, receipts, ledger, payouts, caps.
* `frontend/**` — auth/session handling, upload UI, embed/live playback.
* CI and release gates: `.github/workflows/**`, `.github/zap/**`.
* Deployment posture: branch protection, secret scanning, Actions secrets
  (names only), image build.

**Out of scope / deliberately not done**
* No live payment (no Paystack live key, no M-Pesa), no production calls.
* No load test against production; capacity numbers are from a local run.
* No vendor-side attestation verification — the confidential tier's verifier is
  simulated in development and refused in production.

---

## 3. Threat model in one page

Full version: `THREAT_MODEL.md`. The parts that matter for review:

**Assets** — customer prompts (confidentiality), credits and payouts (money),
API keys and JWTs (access), node earnings and the ledger (integrity), model
shards (supply chain).

**Actors** — anonymous caller; authenticated customer; compromised account; a
malicious *node* (self-reported token counts, fabricated receipts); a malicious
*client* (replaying webhooks, farming signup credits); a compromised coordinator
instance; the platform operator (trusted but audited).

**Trust boundaries** (one contract per boundary, `CONTRACTS.md` + `test_ai_contracts.py`):

| # | Boundary | What crosses | Who validates |
|---|---|---|---|
| TB1 | SDK → coordinator | signed envelope | coordinator |
| TB2 | SDK → worker | encrypted frames | worker |
| TB3 | worker → coordinator | receipt envelope | coordinator |
| TB4 | coordinator → worker | signed ticket | worker |
| TB5 | coordinator → seeder | manifest | coordinator |
| TB6 | host → model guest | attestation verdict | coordinator |
| TB7 | worker → swarm | shards | worker |

**Key invariants a reviewer should attack first**
1. The coordinator must never hold a confidential prompt (canary tests exist).
2. A credit must only be created by a signed event, once (ledger idempotency).
3. A node must not be paid on its own unverified arithmetic (gateway billing
   authority + sampling).
4. With `ENABLE_TCDN_AI` unset, no AI route and no AI DDL exists.
5. No secret or prompt reaches a log.

---

## 4. How to run everything

Prerequisites: Python 3.11+ (CI uses 3.11/3.12; a local 3.14 venv also passes),
Node 20+, Docker.

### 4.1 Backend, fast (no services)

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

pytest -q                      # flag off
ENABLE_TCDN_AI=true pytest -q  # flag on — this is the release gate
```

### 4.2 Backend with the real-service lanes

The suite skips 30 tests without these; they are the ones that prove concurrency
and migrations.

```bash
docker run -d --name audit-pg -e POSTGRES_USER=audit -e POSTGRES_PASSWORD=auditpw \
  -e POSTGRES_DB=tcdn -p 127.0.0.1:55432:5432 postgres:16.4-alpine
docker run -d --name audit-redis -p 127.0.0.1:56379:6379 redis:7.4.2-alpine

cd backend
ENABLE_TCDN_AI=true \
  TEST_POSTGRES_URL=postgresql://audit:auditpw@127.0.0.1:55432/tcdn \
  TEST_REDIS_URL=redis://127.0.0.1:56379/0 \
  pytest tests/test_ai_migrations.py tests/test_ai_postgres.py \
         tests/test_ai_schema_contract.py tests/test_ai_two_instances_live.py -q
```

### 4.3 Lint, types, static security

```bash
cd backend
ruff check app tests scripts
ruff format --check app/ai tcdn_ai_sdk
mypy --config-file mypy-ai.ini
python scripts/sql_injection_audit.py        # AST: every asyncpg call parameterized
python scripts/dead_code_audit.py
python -m pytest tests/test_zap_gate.py -q   # the 40018 acceptance gate
```

### 4.4 The 40018 (SQL injection) acceptance

Four conditions, enforced per alert instance in `.github/zap/summarise.py`:
empty scanner `evidence`, no 5xx on the path, no database-error text on the path,
and the path actually exercised — all measured in the same run. It fails closed
(no proxy log ⇒ exit 2). The dynamic counterpart:

```bash
cd backend
ENABLE_TCDN_AI=true python -m pytest tests/test_ai_sqli_routes.py -q -s
# prints: input routes covered: 12 / dynamic injection requests issued: 216 over 12 routes
```

Written up in `LEVEL1_EXCEPTIONS.md` §5.5 and `SQLI_40018.md`.

### 4.5 DAST

```bash
# CI does this on PRs that touch backend/docker paths; locally:
gh workflow run dast-zap.yml      # or read .github/workflows/dast-zap.yml for the stack it builds
```
The job fails on any HIGH alert; 40018 instances are accepted only under §4.4.

### 4.6 Frontend

```bash
cd frontend
npm ci
npx vitest run        # 20 files / 179 tests
npm run build         # includes the webtorrent patch guard
npm audit --package-lock-only
```

### 4.7 Dependencies and images

```bash
cd backend && pip-audit -r requirements.txt
cd frontend && npm audit --package-lock-only
# image scan is CI-only (trivy); container-scan job in .github/workflows/ci-fullstack.yml
```

### 4.8 Payments (money in)

Off by default. To exercise the gate without a provider:

```bash
cd backend
python -m pytest tests/test_ai_payments_live.py -q     # 23 tests
python scripts/reconcile_payments.py --provider-export export.json \
       --database-url "$DATABASE_URL"                  # findings must be 0
```

Owner procedure for a live test: `PAYMENTS_LIVE.md` (sandbox first, one
allowlisted account, minimum charge, replay check, kill switch, reconcile).

---

## 5. Where the evidence is

| Question | File |
|---|---|
| Does the suite pass, flag on/off? | `evidence/01-tests/full-suite-flag-on-2026-11-11.txt`, `skips-*`, `real-pg-redis-lanes-*` |
| Which rows are done, and from what command? | `PUSH_CHECKLIST.md` (54/56 P1 VERIFIED, recount at the top) |
| What is not done, and why? | `LEVEL1_EXCEPTIONS.md` (6.1, 4.4, 5.5) |
| Security suites by area | `evidence/03-security/self-audit-suites-2026-11-11.txt` |
| Flag-off behaviour | `evidence/03-security/flag-off-behaviour-2026-11-11.txt` |
| 40018 acceptance | `evidence/03-security/sqli-40018-2026-11-11.txt` |
| Dependency scans / secrets | `evidence/03-security/{pip-audit,npm-audit,gitleaks-*}.txt` |
| Repository controls | `evidence/03-security/branch-protection-2026-11-11.json` |
| Findings and ranking | `SELF_AUDIT.md` |
| Crypto reasoning | `CRYPTO_REVIEW.md`, `KEY_INVENTORY.md` |
| Known limits | `LIMITATIONS.md`, `WORKER_NETWORKING.md` |

---

## 6. Known exceptions (do not re-litigate as surprises)

1. **6.1 — redundant execution is gateway-mode only.** ~3 % of gateway jobs are
   re-executed on a second node and compared; SDK-mode (confidential) jobs are
   never re-executed because the coordinator cannot read them. Stated to
   customers exactly that way. Level 1 requires all 56 P1 rows; this is why it is
   **NO**.
2. **4.4 — Paystack is code-complete but never charged a real card.** Signed
   fixtures cover signature, replay, refund and mismatch. `TCDN_AI_PAYMENTS_LIVE`
   (default off, allowlist, caps, kill switch, reconciliation) now bounds the
   blast radius, but the first live transaction is still the first real test.
   M-Pesa is not enabled.
3. **5.5 — ZAP 40018** is accepted at rule level under the four measured
   conditions in §4.4, not by disabling the rule or listing URLs.

---

## 7. What is NOT verified (treat as unproven)

* Any live provider round trip (charge, decline, refund, webhook ordering,
  KES rounding, M-Pesa).
* A DAST run against this exact commit. The gate is tested; the scan is not
  re-run here.
* Container image scan locally (`trivy` unavailable in the audit environment).
* Migration behaviour on a production-sized dataset — the proof is schema-shaped
  (fresh DB + synthetic previous version), not data-volume-shaped.
* That `E2E_ADMIN_TOKEN` is still valid. It was last rotated 2026-08-07 and the
  E2E gate is red; using it against the live backend was out of bounds.
* The confidential tier's attestation against a real vendor verifier (simulated
  adapter in development; production refuses it).
* `main` itself: PR #46's fixes are not merged, so `main` still has the logout
  no-op and the live-viewer 500s.

---

## 8. How to falsify the main claims

| Claim | Falsified by |
|---|---|
| SQL is never built from input | AST audit passing on a planted f-string query (break controls in `scripts/break_tests.py`); a 40018 instance with non-empty `evidence`; a 5xx or DB error text on a scanned path |
| No prompt/key reaches a log | a canary string appearing in captured logs (`test_ai_canary_scan`) |
| Money is credited exactly once | two ledger rows for one reference; reconciliation finding `duplicate_credit`; a replay producing a second credit |
| Awaiting the live gate is sufficient | a real Paystack event crediting with the flag off, or an account outside the allowlist |
| Flag off changes nothing | an `/api/ai/v1` path present in `app.openapi()` with the flag off, or `ai_*` DDL in the startup statement log |
| Migrations are additive and re-runnable | a second run of `AI_TABLES_DDL` altering a row count; a dropped `users` row after CASCADE |

If a claim above fails, the row it supports in `PUSH_CHECKLIST.md` must move
back to `PARTIAL`; that is the rule this file is written under.
