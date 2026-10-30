# TCDN-AI — STATE (single source of truth)

Last updated: **2026-10-30** (failure-injection matrix, signup-abuse gates,
cold start, compose walkthrough, capacity run, and the security scanners — the
blocking image gate is now green)
Branches: **`fix/tcdn-ai-redis-replay-nonce`** (TCDN-AI, pushed, no PR) and
**`fix/product-security-hardening`** (the CDN-side fixes, branched off `main` at
`200109e`, pushed, no PR). `main` still carries no TCDN-AI code and **not** the F-10 fix.
Last committed revision: **`3b3c588`** on the feature branch (`7d3c8d2` on the product
branch). The two branches carry byte-identical product fixes.

**Measured 2026-10-30, the security scanners** (CI run **41013374030**, all three
jobs green on `3b3c588`): the blocking Trivy gate reports **0** fixed CRITICAL/HIGH
findings — debian 12.15 **0**, python-pkg **0** — with `backend/.trivyignore`
**empty**, so nothing is tolerated. The five HIGHs it had been failing on were
not installed packages at all: pip ships vendored copies of msgpack, urllib3 and
setuptools (`pip/_vendor/vendor.txt`) and the scan reads those declarations as
installed, so no dependency bump could clear them. The image installs nothing at
run time, so pip is now removed after the dependency install; `constraints.txt`
keeps a floor under urllib3 (reachable only through botocore's `>=1.25.4`) and
setuptools. Fuzzing ran the full budget: **6 targets × 60 s, 22.8 M cases, 0
findings** (`evidence/03-security/fuzz-ai-60s.json`). ZAP is the one scanner that
has not run — there is no live stack to attack — and that is why row 5.5 stays
`PARTIAL`.

**Measured 2026-10-27** (this pass, no Postgres or Redis attached locally — those lanes
run in CI): full suite flag **on** **1056 passed, 21 skipped**, 97 s, exit 0; flag **off**
**996 passed, 81 skipped**, 81 s, exit 0. CI on the pushed commits (run numbers in
`PUSH_CHECKLIST.md` §"CI evidence — the M4 push"): `ai-store` 61 + 18 + **365** passed,
`tests` 997 passed / 81 skipped, frontend 179 passed, JS client **19/19**, real engine
**success**, `container-scan` and `static-audit` **success**.

**Measured 2026-10-28, after the staging kit** (no Postgres/Redis attached, so those
lanes skip): full suite flag **on** **1148 passed, 29 skipped**, 152 s, exit 0; flag
**off** **1072 passed, 105 skipped**, 80 s, exit 0. Route matrix **113 routes**
(public 21 / jwt 45 / admin 23 / api-key 10 / signature 1 / viewer 7 / internal-hook
6). Staging kit verified end to end on a **local production-mode deployment**
(Postgres 16 container, Redis container, `APP_ENV=production`,
`TCDN_AI_ENVIRONMENT=production`, real Ollama `qwen2.5:0.5b` worker): bootstrap
registered the account, the signup grant landed (`credits 500.00`), and
`scripts/staging_smoke.py` printed **PASS — 8/8** twice, exit 0, with the ledger
invariant and `ledger balance == /balance` both asserted against the database.
That run found and fixed the launcher heartbeat bug (checklist row 3.2).
New files: `render.staging.yaml`, `docs/tcdn-ai/STAGING.md`,
`docs/tcdn-ai/WORKER_NETWORKING.md` (design for checklist 3.4, P2),
`backend/scripts/{staging_bootstrap,staging_smoke,ai_keyring_init}.py`,
`app/ai/{status,signup}.py`.

**Measured 2026-10-28** (independent-audit fixes F-1…F-6 and Level 1 rows 2.3, 2.6,
3.2; no Postgres/Redis attached, so those lanes skip):
- Full suite, flag **off**: **1057 passed, 99 skipped**, 102 s, exit 0. Flag **on**:
  **1127 passed, 29 skipped**, 70 s, exit 0. One invocation each, no per-file split.
- Break tests **24/24 CAUGHT, 0 unproven** (`evidence/06-break-tests/`).
- Route matrix **112 routes** at that point (public 20 / jwt 45 / admin 23 / api-key 10 /
  signature 1 / viewer 7 / internal-hook 6; the `/api/ai/v1/status` route added by the
  staging kit took it to 113 — see the block above); flag-off OpenAPI **79 paths**,
  flag on **97** (**18** AI paths); schema **17** `ai_*` tables / 8 `ALTER` statements.
- `ruff check .`, `ruff format --check app/ai tcdn_ai_sdk`, `mypy --config-file
  mypy-ai.ini`, the dead-code audit, the SQL-injection audit and the static
  security-header audit all clean.
- Evidence added this pass: `evidence/12-billing-authority/overclaim-tests.txt`
  (ledger postings for a worker that claims 3 000 output tokens and is billed for the
  100 the gateway counted), `evidence/03-security/gitleaks-value-scoped-2026-10-27.txt`.

**Measured 2026-10-26** (evidence in `PUSH_CHECKLIST.md`, `evidence/06-break-tests/`,
`evidence/09-openai-gateway/`):
- Full suite, flag **on** (real Postgres attached): **1030 passed, 4 skipped**, 108 s, exit 0.
- Full suite, flag **off** (real Postgres attached): **971 passed, 63 skipped**, 70 s, exit 0.
- AI subset (flag on): **327 tests**; `ai-store` lane (Postgres + migrations + schema
  contract): **18 passed** on a fresh database.
- **M3 pass, no Postgres attached** (the Postgres lanes skip): flag on
  **1020 passed, 21 skipped** in 81 s, flag off **961 passed, 80 skipped** in 99 s;
  AI subset **303 passed, 21 skipped**; the new `test_ai_redundant_execution.py`
  **7 passed**. `ruff check` + `ruff format --check` clean on every touched file.
- Break tests: **24/24 CAUGHT, 0 unproven** (was 12/13 with the 13th silently
  unproven — the harness's mutation target had drifted and it never printed the
  error; eleven controls were added with the audit and Level 1 fixes: billing
  authority (three), the SSRF pin, the JWT-only admin money routes, the schema
  gate, atomic nonces in both stores, job ownership on ack, the per-key
  concurrency cap, and the supervisor's backoff).
- Official **JavaScript** OpenAI client: **19/19 checks** against a live uvicorn
  gateway (`evidence/09-openai-gateway/js-client.json`), now a committed, CI-gated check.
- Boundary contracts: `CONTRACTS.md` TB1–TB7 + **23** contract tests.
- Open blockers for Level 1: see `PUSH_CHECKLIST.md` — **5** unfinished [P1] rows
  (2 `MISSING`: 12.3, 12.4; 3 `PARTIAL`: 4.4, 5.5, 6.1). Rows 3.3, 4.5, 7.2, 10.1,
  10.2 and 12.5 closed on 2026-10-29/30; 2.3, 2.6 and 3.2 closed on 2026-10-28.
- Release shape: **PR #46 (draft)** carries the CDN fixes **alone** — no `app/ai`, no
  `tcdn_ai_sdk`, no `render.staging.yaml` — for a dark merge. Order, auto-deploy
  surfaces, migration impact, the production smoke list and the one-step rollback are
  in [`MERGE_PLAN.md`](MERGE_PLAN.md). ZAP has **not** run: the staging blueprint was
  never applied (its URL answers 404) and the scanner image could not be pulled here;
  see `evidence/03-security/dast-2026-10-30.txt` for what ran instead and row 5.5 for
  why it stays `PARTIAL`.

Read this first, then `HANDOFF_AUDIT.md`. The authoritative detail is the audit;
this file is the short, current status.

## 1. Milestone status

| Milestone | Status | Evidence / blocker |
|---|---|---|
| M1 Crypto, keyring, tickets | **Done and verified** | 221 AI tests; break tests 10/10 |
| M2 Models + `ai_*` DDL | **Done and verified** | Fresh DB creates all 14 tables and re-runs cleanly; `tests/test_ai_migrations.py` (row 1.9) proves upgrade from a previous version keeps rows; `tests/test_ai_schema_contract.py` plans every SQL statement in `app/ai` with `PREPARE`, so a query naming a column the DDL never got fails the build instead of production (39 statements, 0 failures); no dead tables; every table has a PK |
| M3 Coordinator: auth, registry, scheduler, placement | **Done and verified** | Live journey `JOURNEY OK` |
| M4 Worker: ticket validation, AEAD, receipts | **Done and verified** | Live journey; e2e test; F-04/F-05 fixed |
| M5 Python SDK + e2e streaming | **Done and verified** | 221 tests; live journey |
| M6 Ledger, credits, holds, settlement | **Done and verified** | Live invariants; in gateway mode the gateway's **own measurement** of the prompt and delivered tokens outranks an overclaiming receipt (the lower count is billed) and per-node overclaim rates suspend a node and hold its payouts (`test_ai_node_billing.py`, 14 tests); billing is driven by the worker's **signed receipt** (the settlement bypass that made completions free for a client that never acked is fixed: `TestEncryptedJob::test_placement_worker_receipt_ack_settlement`); the hold is a hard billing ceiling, over-hold receipts are capped and flagged, and an over-held or mismatched ack flags review without undoing the charge; Postgres lane 7/7 |
| M7 P2P weight streaming | **Partial** | Manifest/Merkle/fetch tested; no real WebRTC/libtorrent transport; no direct-to-VRAM |
| M8 Seeder receipts + payout split | **Partial** | Math/fraud controls unit-tested; no ingestion or payout issuance |
| M9 Result verification (Part 3) | **Partial — wired, keyed and paid in gateway mode** | Placement issues a second signed ticket to a *different* operator and records it in `ai_verifications`; the gateway dispatches it in the background and compares the two output hashes as soon as both receipts land (`test_ai_redundant_execution.py`). Since M4 the sample is `HMAC-SHA-256(coordinator_secret, domain \|\| job_id)` at a **3 %** default (`test_sample_is_not_derivable_from_the_job_id`), and the shadow operator is **paid** the compute share of its own tokens from `platform:revenue`, capped and idempotent per shadow job (`TestShadowPayout`). Still missing: a decision taken **after** a commitment, any SDK-mode path (no plaintext at the coordinator), and real tolerance profiles — 6.1 stays `PARTIAL` with those two clauses named |
| M10 Confidential tier | **Simulated / interfaces only** | Policy + simulated adapter tested; real SEV-SNP/TDX/NVIDIA verifiers fail closed |
| M11 Burn module | **Disabled** | Dry-run only, off by default, legal ack required in production |
| M12 Hardening, docs, full gate | **Partly done** | CI is green on GitHub (all five workflows at `0360b10`, incl. the AI Postgres/Redis job, `ai-types`, and the nightly real-engine job). `container-scan` is now **blocking** and green with 0 fixed CRITICAL/HIGH (run `41013374030`), together with SAST, gitleaks and the full fuzz budget; **ZAP has not run**, and soak/chaos/pentest still missing; coverage 84 % |
| Test-harness fix: full-suite hang | **Fixed and verified** | Root cause: every `TestClient` startup opened a real asyncpg pool from `main.py`'s own `get_db_pool` binding (unpatched). Now hermetic; whole suite in one invocation, 4/4 green (`evidence/09-openai-gateway/full-suite*.txt`) |
| M14 Real engine in CI + real email flow (Section C item 2) | **Done and verified** | Ollama 0.33.2 + `qwen2.5:0.5b` on CPU: `tests/test_ai_engine.py` 4/4; journey 12/12 with real inference and real verification link — `evidence/10-real-engine/`; nightly job `tcdn-ai-engine.yml`. Found and fixed the hold-underestimation bug and H-1 |
| M13 OpenAI gateway (Section C item 1) | **Done and verified** | `/v1/models`, `/v1/chat/completions` (JSON + SSE); 14 tests incl. official OpenAI Python client; official JS client driven against a live uvicorn server: `evidence/09-openai-gateway/` |

## 2. What is verified (ran in this session)

- Live end-to-end: signup → verified → 2FA enforced → API keys → credits
  (duplicate callback idempotent) → model registration → worker registration →
  non-streaming + streaming inference → balance/usage → ledger/hold/canary
  invariants. Raw: `evidence/08-handoff-audit/journey.txt`.
- **Whole backend suite in ONE pytest invocation, flag on and flag off**, twice
each, no hang: `925 passed, 11 skipped` (flag on) and `879 passed, 57 skipped`
(flag off) in 70-100 s. Raw: `evidence/09-openai-gateway/full-suite-on.txt`,
`full-suite-off.txt`.
- The 13 break-test controls (receipt/ticket/crypto/ledger/attestation/admin/
  Merkle/payments/gateway/hold-headroom/dispute-hold-release/ownership) —
  `evidence/06-break-tests/`.
- OpenAI gateway: official OpenAI **Python** client (non-streaming and streaming
  with `include_usage`, tools rejected with OpenAI's error shape) and official
  **JavaScript** client against a live uvicorn server — `evidence/09-openai-gateway/`.
  Canary tests prove the prompt is absent from logs and stored rows.
- **Real inference + real email verification**: Ollama 0.33.2, `qwen2.5:0.5b`,
  CPU. Adapter tests 4/4, journey 12/12 in 8 s, settlement with `held_micro=0`,
  ledger net equals balance, no canary retention — `evidence/10-real-engine/`.
  The account was unverified until the token from the recorded email was used.
- **Redundant execution** (checklist 6.1): a sampled job runs twice on two
  registered operators, the second run's receipt is verified against its own
  node key, the pair is compared once both arrive, the customer is billed once,
  and the refusal paths (`no_alternate_node`, `daily_cap_reached`,
  `not_dispatched_sdk_mode`, `dispatch_failed`) are recorded rather than silent —
  `tests/test_ai_redundant_execution.py`. The sample is keyed with a
  coordinator-side secret (3 % default, shared daily cap), and the shadow run is
  paid from `platform:revenue` with one ledger entry per shadow job.
- **M4 artifacts**: `CRYPTO_REVIEW.md` (16 primitives, citation-checked),
  `KEY_INVENTORY.md` (9 keys, rotation/revocation), the dependency-derived
  authorization matrix (68 routes, `unclassified` fails), two coordinators over
  one Redis (`test_ai_two_instances.py`, 6 tests) **and over a real Redis**
  (`test_ai_two_instances_live.py`, 6 tests, database 15), the environment-wide
  canary scanner (`scripts/canary_scan.py`), and the payout kill switch
  (`ai_controls`, `409 payouts_paused`, audited).
- **Blocker-class defect found by the live lane and fixed**: the Redis-backed
  replay/nonce stores drove an `redis.asyncio` client through a sync bridge that
  ran on a fresh event loop, so every command raised `got Future attached to a
  different loop` and the fail-closed branch refused **every ticket as a replay**
  whenever Redis was configured. The stores now derive a blocking client from the
  pool's own connection settings. Without the fix the live file fails 2 of 6;
  with it, 6/6.
- Break tests `24/24` CAUGHT, 0 unproven.
- `ruff` clean; SQL-injection audit clean; gitleaks history 0 findings;
  `secret_scan.sh --full` clean; `pip-audit` no known vulnerabilities.
- Frontend `tsc --noEmit` clean, `179` Vitest tests pass.
- **M2 data layer, against real Postgres** (fresh throwaway schema per test):
  `test_ai_postgres.py` + `test_ai_migrations.py` + `test_ai_schema_contract.py`
  = **17 passed** on one fresh database (the exact `ai-store` CI lane). The
  schema contract's negative control proves the check can fail: `PREPARE`
  rejects `SELECT does_not_exist FROM ai_jobs`, so a green run is meaningful.
- **Production probes fail closed on the scheduled run.** All four
  `*_audit_prod.sh` probes skip a rejected `E2E_ADMIN_TOKEN` on branch/PR runs
  (warning annotation, exit 0) but on the scheduled run on `main`
  (`PROBE_STRICT_CREDENTIALS`) they emit an `::error` and exit non-zero, so the
  daily job goes red instead of silently passing. Behaviour pinned by
  `tests/test_probe_strict_credentials.py` (12 tests: 4 probes x skip/strict/
  caller-propagation).
- **Dependency-audit exceptions verified against what actually ships.**
  `undici` and `vitest` are `dev=true` in `package-lock.json` (undici only via
  `jsdom`), absent from `npm ci --omit=dev`, and imported by nothing outside
  tests. `tailwindcss` was **wrongly** a runtime dependency: it is a PostCSS
  compiler, so it moved to `devDependencies` and `frontend/Dockerfile` now runs
  `npm prune --omit=dev` after the build. Verified **inside the built image**:
  `undici`, `vitest`, `tailwindcss` and `jsdom` are all absent, 302 packages
  remain, `next` and its CLI are intact, and the image serves `GET /` -> 200 in
  2 s as the non-root `node` user. The browser bundle (`.next/static`, 55
  chunks) greps clean for all three, and none is traced into `.next/standalone`
  (the only `undici` strings are URLs in Next's own comments).
- **Frontend production build succeeds** (the earlier Google-Fonts blocker was
  environmental): `npm run build` exit 0, webtorrent patch guard OK.

## 3. What is NOT verified

- The OpenAI-compatible gateway has **not** been run against a real inference
  engine (only the echo worker) and has no load/soak test yet.
- Coverage gate (84 % < 85 %).
- Real vLLM/Ollama inference, real GPU, real attestation hardware, live
  Paystack, M-Pesa.
- 30-min sustained / 4-h soak / 10× spike, chaos (kill, partition, disk, clock,
  provider outage), full-budget fuzzing, ZAP, Trivy, SBOM re-run, independent
  pentest/crypto review.
- ~~Any CI run of an AI test on GitHub~~ **Now verified**: the Postgres/Redis
  job runs `test_ai_core.py`, `test_ai_redis.py` and the real-Postgres
  `test_ai_postgres.py` on every push to the trial branch and passes in ~50 s
  (run `40184119060`), and the nightly engine job runs real Ollama inference
  (`40184119132`).
- The canary scanner's **Postgres and Redis modes** have not been executed
  anywhere: CI has no service container for it and this environment has neither
  service. The store snapshot it scans is the same object graph those modes
  persist from, so the assertion is equivalent, not identical.
- **F-10 is fixed on `fix/product-security-hardening` only** — `main` still has
  the un-required `decode_access_token`. Nothing is deployed until that branch
  merges.

## 4. What is simulated (and labelled)

| Component | Label | Production behaviour |
|---|---|---|
| `SimulatedVerifier` (attestation) | `status="simulated"`, reason string | Refused by `AttestationService.accept` — **but** see the config trap below |
| `SevSnpVerifier` / `TdxVerifier` / `NvidiaGpuVerifier` | fail closed | Raise `attestation_verifier_unavailable` |
| `EchoBackend` inference | `/health` says `dev-stub` | Refuses when `ALLOW_DEV_STUB_BACKEND=false` |
| `FakePaymentProvider` | event provider `"fake"` | Refuses construction in production |
| `GpuDirectSink` | raises `HardwareUnavailable` | Never pretends to load weights |

## 5. Decisions in force (details in `ASSUMPTIONS.md`)

- Python/FastAPI inside `backend/`; no new runtime. SDK is Python; TS is a port
  spec only.
- Postgres via asyncpg, hand-written idempotent DDL at startup, no Alembic.
- `cryptography` primitives only: Ed25519, X25519, HKDF-SHA-256, AES-256-GCM.
- Canonical JSON = sorted keys, `,`/`:`, UTF-8; every signature covers
  `domain || 0x00 || canonical_json(payload)`.
- Money is `int` micro-credits (1 credit = 1e6; $1 = 1e8). Floats rejected.
- Split is `compute//100`, `seeder//100`, platform absorbs remainder.
- IDs: `node_<hex>`, `job_<uuid>`; timestamps are UTC epoch seconds.
- Errors: JSON `{"error":{"code","message","details"}}`; codes in
  `app/ai/errors.py`.
- In-process state (rate limit, replay cache, nonces) is a documented
  limitation, not a design goal.

## 6. Known problems (current, ordered)

1. **Nothing is on `main`**: the AI code is committed on
   `fix/tcdn-ai-redis-replay-nonce` (`7b4df67`); a fresh clone of `main` has no
   AI code. Owner action O4 (branch protection) governs how it gets there.
2. ~~Feature flag is ineffective~~ **Fixed**: `ENABLE_TCDN_AI` defaults **off**,
gates the `/api/ai/v1` routes, and now also gates the startup `ai_*` schema
migration — a flag-off deployment no longer creates 17 AI tables
(`test_the_ai_schema_is_only_migrated_when_the_flag_is_on` records the
statements startup issues in both positions; break-tested).
3. **Production env trap**: `TCDN_AI_ENVIRONMENT` defaults to `development`
   under `APP_ENV=production`, allowing simulated attestation / fake payments.
4. ~~Disputed jobs freeze the hold forever~~ **Fixed** (H-1), and the dispute
   path is now a **review** path: a receipt that exceeds the hold is billed *at*
   the hold and flagged, a mismatched client ack flags the job without cancelling
   the charge, and the reservation is always settled or released — nothing is
   left open. Break-tested (`an over-hold receipt is capped at the hold and
   flagged`, `a worker receipt bills the job with no client ack`). The related
   hold-underestimation bug found with a real engine is also fixed (reservation
   headroom for chat-template tokens).
5. **OpenAI gateway covers chat completions only** — no tools, JSON mode,
   embeddings, or `n > 1` (Section C item 11).
6. **Verification is wired for gateway jobs only, and its sample is predictable.**
   A sampled gateway job is re-run on a different operator and compared; an SDK
   job cannot be (the coordinator has no plaintext) and is recorded as
   `not_dispatched_sdk_mode`. The bucket seed is `sha256(fixed salt + job_id)`, so
   a node can compute which of its jobs will be sampled, and the decision is
   taken at placement rather than after a commitment. Exact-hash comparison with
   two executions reports a disagreement as `inconclusive` and penalises nobody.
   Tolerance profiles are still placeholders.
7. **CI now covers the AI code remotely** — the Postgres/Redis job
   (`40184119060`) and the nightly engine job (`40184119132`) are green at
   `0360b10`; the caveat is that the *scheduled* run is red until O1 is done.
   Coverage below gate (84 % < 85 %). Ollama-served jobs carry no verification transcript
   (its logprobs have no token ids) — see `LIMITATIONS.md` §8.
8. Missing: host onboarding, payout issuance, consoles, legal drafts,
   production deploy manifests, alerting, Redis-backed ephemeral state.
9. Docs overclaim: `RedactingFilter` does not exist; suspension is unreachable
   in the normal sample; dead-code audit is failing. (The journey's PASS-on-404
   probe is gone: it now asserts the real gateway with the official client.)
10. Junk in the tree: `backend/.coverage` (gitignored build artefact), frontend
    debug scripts.

## 7. Next three actions

1. Section C item 3: Redis-backed rate limits / replay cache / attestation
   nonces, a Postgres service in CI so `PostgresStore` is tested for real, and
   the full suite green in one invocation (already achieved locally).
2. Commit the work on a feature branch, separated from pre-existing CDN changes
   (audit item B1.1) — do not push to `main`.
3. Section C item 4 follow-up: make the verification sample unpredictable and
   commitment-derived, and give SDK jobs a path (a commitment-only re-scoring run
   is the only one available without the plaintext).

## 8. Exact commands

All backend commands run from `backend/` unless stated.

```bash
# --- install ---
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-dev.txt   # ruff + official openai client (tests)

# --- infrastructure (repo root) ---
docker compose -f deploy/tcdn-ai/docker-compose.dev.yml up -d postgres redis

# --- coordinator ---
ENABLE_TCDN_AI=true TCDN_AI_ENVIRONMENT=development \
TCDN_AI_API_KEY_PEPPER=dev-pepper-change-me TCDN_AI_PAYMENT_PROVIDER=fake \
DATABASE_URL=postgresql://tcdn:tcdn@127.0.0.1:5432/tcdn \
REDIS_URL=redis://127.0.0.1:6379/0 \
JWT_SECRET=this-is-a-32-char-secret-for-testing-only!! \
.venv/bin/python -m uvicorn app.main:app --port 8000

# --- full end-to-end (starts its own echo worker) ---
.venv/bin/python scripts/live_ai_journey.py \
  --base-url http://127.0.0.1:8000 \
  --database-url postgresql://tcdn:tcdn@127.0.0.1:5432/tcdn \
  --out ../docs/tcdn-ai/evidence/04-e2e/journey.json

# --- tests: whole suite, one invocation, both flag states ---
ENABLE_TCDN_AI=true .venv/bin/python -m pytest -q     # 925 passed, 11 skipped
ENABLE_TCDN_AI=false .venv/bin/python -m pytest -q    # 879 passed, 57 skipped
# AI-only subset (flag on):
ENABLE_TCDN_AI=true TEST_POSTGRES_URL=postgresql://tcdn:tcdn@127.0.0.1:5432/tcdn \
  .venv/bin/python -m pytest tests/test_ai_*.py tests/test_part3_coverage.py \
  tests/test_authorization_matrix.py -q

# --- lint / audits ---
.venv/bin/python -m ruff check app tests scripts
ENABLE_TCDN_AI=true .venv/bin/python scripts/break_tests.py   # 24/24 controls

# --- real engine (Ollama) — see DEV_SETUP.md §6 for the full bring-up ---
TCDN_AI_ENGINE_URL=http://127.0.0.1:11434 TCDN_AI_ENGINE_MODEL=qwen2.5:0.5b \
  .venv/bin/python -m pytest tests/test_ai_engine.py -q   # 4 passed with an engine
.venv/bin/python scripts/live_ai_journey.py --backend ollama \
  --engine-url http://127.0.0.1:11434 --model qwen2.5:0.5b \
  --email-stub-url http://127.0.0.1:8765 --email-out /tmp/tcdn-ai-emails.jsonl
# CI: .github/workflows/tcdn-ai-engine.yml (nightly + manual, not on push)

# --- OpenAI-compatible gateway (gateway mode) ---
ENABLE_TCDN_AI=true .venv/bin/python -m pytest tests/test_ai_openai_compat.py -q
# live check with the official JS client: see evidence/09-openai-gateway/README.md
.venv/bin/python scripts/sql_injection_audit.py
.venv/bin/python scripts/dead_code_audit.py          # currently FAILS (7)
.venv/bin/python scripts/secret_scan.sh --full       # via bash
bash scripts/secret_scan.sh --full
gitleaks git --no-banner --redact
.venv/bin/python -m pip_audit -r requirements.txt

# --- frontend ---
cd ../frontend && npm ci && npx tsc --noEmit && npm test && npm run build
```

Evidence from the handoff audit: `docs/tcdn-ai/evidence/08-handoff-audit/`
(`ruff.txt`, `ai-suite.txt`, `legacy-per-file.txt`, `full-suite-attempt.txt`,
`journey.txt`, `journey.json`).

## 9. Owner actions pending (not code — cannot be done from this repo)

The first one is currently the *cause* of a red scheduled run; the rest are
decisions or settings that no commit can make.

| # | Action | Why it blocks something | Where it lives |
|---|---|---|---|
| **O1** | **Refresh `E2E_ADMIN_TOKEN`** — create a non-revoked admin key in production and replace the repository secret | The four production probes (`*_audit_prod.sh`) verify a throwaway account through `POST /admin/users/{id}/verify`. The current key is rejected (`Invalid or revoked API key`). On `workflow_dispatch` the probes **skip** and say "not exercised"; on the **scheduled run on `main` they now FAIL** by design (`PROBE_STRICT_CREDENTIALS`, pinned by `tests/test_probe_strict_credentials.py`: 12 tests driving the real extracted `verify_account`/caller pair with a stubbed 401 — skip→exit 0, strict→`::error`+exit 2). So the daily job stays red until this is refreshed — that red is the truth, not a flake | GitHub → Settings → Secrets → Actions |
| **O2** | **Classify `frontend/torrentcdn-integration.html`** — commit it if it is a real artefact of the product, delete it if it was scratch work | It is deliberately **untracked and un-ignored** (checklist 1.6): it does not look like a build output, so it was left for the owner instead of guessed at. Until then it is neither shipped nor forgotten | working tree |
| **O3** | **Choose the API hostname and create its DNS** — one canonical host for the API (today `www.torrentcdn.store/v1` via the Vercel proxy → Render), plus the storage host `cdn.torrentcdn.store` (custom domain + `MINIO_PUBLIC_URL`) | `backend/app/ai/routes_openai.py` advertises `https://www.torrentcdn.store/v1` because that is the only origin that resolves; a dedicated `api.` host needs DNS (two records, `R2_MIGRATION_RUNBOOK.md` §DNS) before any doc can promise it. The domain guard in CI only allows origins that actually resolve, so this is a decision, not a code fix | DNS / Cloudflare |
| **O4** | **Enable branch protection on `main`** — require the five workflows (Tests & Security, full-stack Docker, backend-lint, engine, Dependency Audit), no force-push, no direct merges | Every gate in M1 only binds if a merge must pass it; `main` is currently unprotected, so the green runs at `0360b10` can be bypassed | GitHub → Settings → Branches |

## 10. Handoff checklist for the next session

1. Read `HANDOFF_AUDIT.md` (critical alerts and §5 matrix) and this file.
2. Confirm the working tree still contains the AI files; if not, stop — they
   were lost.
3. Re-run the AI suite and the live journey before trusting any status here.
4. Do not describe the system as OpenAI-compatible, production-ready, or
   confidential-tier capable until the audit's Phase B items are verified.
