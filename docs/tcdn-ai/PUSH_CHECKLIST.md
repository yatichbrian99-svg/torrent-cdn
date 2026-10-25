# TCDN-AI — PUSH CHECKLIST (Section E)

**Generated:** 2026-10-25 · **Branch:** `fix/tcdn-ai-redis-replay-nonce`
**Rule:** a row is `VERIFIED` only where a command or test was run and its output captured.
Nothing here is inherited from an earlier report.

---

## Summary

| Level | VERIFIED | PARTIAL | BUILT-UNVERIFIED | MISSING | DEFERRED | NOT VERIFIABLE HERE | Total |
|---|---|---|---|---|---|---|---|
| **P1** | **10** | **37** | 0 | **7** | 0 | 0 | 54 |
| **P2** | 0 | 1 | 0 | 27 | 0 | 1 | 29 |
| **DEFER** | 0 | 0 | 0 | 0 | 2 | 0 | 2 |

Counts produced by parsing the tables in this file (not estimated).

**Level 1 ready: NO.**

All seven [P1] `MISSING` items block the push:

| Blocker | Why it stops Level 1 |
|---|---|
| **1.13** | `CONTRACTS.md` does not exist; no contract tests. |
| **5.5** | No SAST, no ZAP, no Trivy, fuzz budget not run — Level 1 requires **no open critical/high**, which cannot be asserted without these. |
| **3.1** | No real engine has ever run through the adapters; only stubs. |
| **6.1** | Redundant execution is not wired into placement. |
| **10.2** | No capacity test results, so no measured limits or server sizing. |
| **12.3** | No draft PR. |
| **12.4** | Post-push confirmation cannot exist before a push. |

Additional [P1] `PARTIAL`s that must reach `VERIFIED` before the same push:

- **1.8** — the Postgres+Redis job is repaired and green locally against fresh containers, but
  no GitHub run exists yet (nothing pushed), and the rest of the Level-1 CI set (SAST, container,
  SBOM) is still absent from the workflows.
- **1.3 / 1.7 / 1.6** — no fresh-clone run, no SBOM, `ruff format --check` fails on 21 files,
  and untracked artefacts (`backend/.coverage`, `clips/`, `frontend/__debug_sdk.mjs`) sit in the
  working tree.
- **1.9** — no migration framework, so "additive and reversible / upgrade from previous state"
  cannot be claimed.
- **2.1 / 2.2 / 2.3 / 5.7** — no official JavaScript client test; gateway-mode disclosure missing
  from docs/Terms; no environment-wide canary scan.
- **10.3 / 12.2** — `STATE.md` and `VERIFICATION_REPORT.md` still carry wrong counts (C-4).

---

## 1. Repository, build and safety

| ID | Item | Level | Status | Evidence | Notes |
|---|---|---|---|---|---|
| 1.1 | Feature branch, no force-push, no rewritten history | P1 | **VERIFIED** | `git branch --show-current` → `fix/tcdn-ai-redis-replay-nonce`; `git log --oneline` linear; no force-push performed | `main` untouched at `200109e` |
| 1.2 | Flag exists, defaults off; suite green off and on | P1 | **VERIFIED** | `app.openapi()`: 79 paths flag off / 95 flag on, 0 vs 16 AI paths; suite **897 passed, 57 skipped** (off) and **943 passed, 11 skipped** (on) after M0.1 | counts recorded in HANDOFF_AUDIT §2 |
| 1.3 | Fresh clone + empty cache: install, build, lint, type-check | P1 | **PARTIAL** | `ruff check` clean; suite green; **no fresh-clone run**; **no backend type-checker exists**; `ruff format --check app/ai` → 21 of 31 files would reformat | needs a clean-clone pass |
| 1.4 | Whole suite in one invocation, no hang | P1 | **VERIFIED** | 897 passed in 86 s (off), 943 in 121 s (on), exit 0 both | one invocation, no per-file split |
| 1.5 | Secrets scan clean, whole history, all branches | P1 | **VERIFIED (limited)** | `git grep` over all 334 revisions for private-key/`sk_live`/`AKIA`/`ghp_`/`xox*`: 0 hits; no `.pem/.key/.p12` tracked; `.gitignore` covers `.env*`, `.venv`, `__pycache__`, `node_modules` | **gitleaks is not installed**; this is pattern matching, not gitleaks |
| 1.6 | No artefacts, DBs, large binaries committed | P1 | **PARTIAL** | untracked in tree: `backend/.coverage`, `clips/*.mp4/gif/png`, `frontend/__debug_sdk.mjs`, `frontend/__sdk_debug.mjs` | must be ignored/removed before push |
| 1.7 | Dependency audit clean, lockfiles, SBOM, justified deps | P1 | **PARTIAL** | `pip-audit -r requirements.txt` → "No known vulnerabilities found"; `frontend/package-lock.json` committed (untracked change) | **no SBOM generated**; crypto deps not individually justified |
| 1.8 | CI green on GitHub: build, lint, tests w/ Postgres, SAST, deps, secrets, container, SBOM | P1 | **PARTIAL** | `ai-store` job repaired and run locally against **fresh** `postgres:16.4-alpine` + `redis:7.4.2-alpine`: **61 passed** (core+redis) and **7 passed** (real Postgres), exit 0; workflow YAML parses | **no GitHub run yet** (nothing pushed); SAST / container / SBOM jobs still absent from the workflows |
| 1.9 | Migrations additive + reversible; fresh DB and upgrade both tested | P1 | **PARTIAL** | fresh DB bootstrap proven: `TEST_POSTGRES_URL=… pytest tests/test_ai_postgres.py` → **7 passed**; schema is idempotent `AI_TABLES_DDL` | no migration framework, no down-path, no upgrade test |
| 1.10 | Production refuses simulated verifier, stub backend, memory store, fake payments, TLS off, missing secrets | P1 | **PARTIAL** | refusal code in `config.py:141,202`, `attestation.py`, `payments.py`; `test_ai_security.py::test_production_refuses_to_auto_generate_keys` passes | not one test per refusal listed; TLS/missing-secret refusals not proven |
| 1.11 | Simulated/dry-run labelled everywhere and impossible in prod | P1 | **PARTIAL** | 45 label mentions in `app/ai`; `SimulatedVerifier`, `EchoBackend`, fake payment provider gate on `is_production`; `GpuDirectSink` fails closed | API-output/docs labelling not audited row by row |
| 1.12 | No duplicates, no dead code, no TODO on money/security | P1 | **PARTIAL** | `ruff check app/ai` clean; duplicated/truncated blocks in `redis_cache.py` found and removed (F-04) | no repo-wide duplicate/dead-code sweep run this pass |
| 1.13 | One written contract per boundary + contract tests | P1 | **MISSING** | `docs/tcdn-ai/CONTRACTS.md` does not exist (`ls` → No such file) | blocks Level 1 |

## 2. API and SDK

| ID | Item | Level | Status | Evidence | Notes |
|---|---|---|---|---|---|
| 2.1 | `/v1/chat/completions`, `/v1/models`, streaming, OpenAI errors; official Python **and JS** clients | P1 | **PARTIAL** | openapi lists both paths; `test_ai_openai_compat.py` uses official `AsyncOpenAI` and passes in the 943-green run | **no official JavaScript client test**; streaming covered by compat tests |
| 2.2 | Gateway mode standard-tier only, disclosed; prompts never logged/stored; canary proof | P1 | **PARTIAL** | tier gating in `routes_openai.py`; canary assertions in `test_ai_service.py` | no canary test specific to the `/v1` gateway path; Terms disclosure absent |
| 2.3 | SDK mode e2e encryption; coordinator never sees prompt; confidential needs SDK mode | P1 | **PARTIAL** | `test_ai_sdk_e2e.py::test_coordinator_never_sees_the_prompt` passes in the 943-green run | "confidential requires SDK mode" not proven by a test |
| 2.4 | Full lifecycle incl. dispute and hold release | P1 | **VERIFIED** | `test_ai_service.py` (16 tests) passes: placement/hold/receipt/ack/settle/dispute; `test_disputed_job_releases_the_hold`; Postgres lane rejects overspend | — |
| 2.5 | `Idempotency-Key` on creation requests, replay-tested | P1 | **PARTIAL** | `idempotency_key` in `ledger.py`, `store.py`, `service.py` (ledger credits) | no HTTP `Idempotency-Key` header handling found for placements |
| 2.6 | Rate limits, quotas, size, max messages/tokens, concurrency | P1 | **PARTIAL** | per-key and per-IP buckets now **actually enforce** (M0.1 fixed the un-awaited `allow`); `TestRateLimit` + shared-bucket compat test pass | global concurrency cap config exists (`max_concurrent_jobs_per_key`) but enforcement unproven |
| 2.7 | `/v1/completions`, embeddings, tools, JSON mode, `seed`, `n`, `include_usage`, matrix | P2 | **MISSING** | no such routes in openapi | — |
| 2.8 | Token counting authority rules | P2 | **MISSING** | no canonical tokenizer module | — |
| 2.9 | Cursor/Cline/Continue/OpenCode guides + tests | P2 | **MISSING** | — | — |
| 2.10 | `/v1/audio/transcriptions` | P2 | **MISSING** | — | — |
| 2.11 | Versioning promise, deprecation policy, changelog | P2 | **MISSING** | — | — |

## 3. Workers and inference

| ID | Item | Level | Status | Evidence | Notes |
|---|---|---|---|---|---|
| 3.1 | Real open-weight model through Ollama/vLLM, streaming, with evidence | P1 | **MISSING** | no engine in this environment; no CI job exists (`test_ai_engine.py` is 4 tests, skipped without an engine) | blocks Level 1 |
| 3.2 | Signed registration/heartbeat, liveness gating, rotation, supervised task with backoff | P1 | **PARTIAL** | `service.py` heartbeat + nonce + rotation; `scripts/ai_worker.py:228` `create_task` | no restart/backoff supervision |
| 3.3 | Failure handling: crash mid-stream, disappear mid-job, coordinator restart | P1 | **PARTIAL** | `test_ai_failure_injection.py` has **1** test; `expire_stale_jobs` releases holds (Postgres test passes) | crash/restart matrix not covered |

## 4. Money

| ID | Item | Level | Status | Evidence | Notes |
|---|---|---|---|---|---|
| 4.1 | Integer micro-credit double-entry; sums to zero; no negatives; property tests | P1 | **VERIFIED** | `test_ai_core.py` ledger property tests; `test_every_ledger_entry_has_equal_debits_and_credits` on Postgres | floats/bools rejected at boundary |
| 4.2 | Configurable split, validated at startup, default 70/10/20, recorded per job | P1 | **VERIFIED** | `config.py` split validation; `Ledger` split tests; job records split | — |
| 4.3 | Concurrency: holds/settlements/refunds/penalties, no double-spend | P1 | **VERIFIED** | `test_ai_postgres.py::test_concurrent_holds_cannot_overspend`, `::test_concurrent_credits_with_the_same_key_post_once` — **7/7 passed** | — |
| 4.4 | Paystack test mode: card + M-Pesa, signature, idempotency, forged/duplicate/late/failed/mismatch/refund | P1 | **PARTIAL** | `test_ai_billing.py` (11 tests) pass — signature + replay + state machine | no Paystack sandbox session; **M-Pesa channel not enabled on the account** |
| 4.5 | Free credit: email-verified gate, one per account, per-IP cap, monthly cap, platform-funded | P1 | **PARTIAL** | grant + one-per-account + per-IP cap present and tested | **no `email_verified` gate exists** (grep: 0 matches in `app/ai/**`, `app/routes/**`); monthly cap not found |
| 4.6 | Refunds/admin adjustments as ledger entries, audit-logged | P1 | **PARTIAL** | adjustments are ledger entries; audit log present | no admin refund endpoint found |
| 4.7 | Burn/staking disabled, dry-run only, prod-gated, no token transaction | P1 | **VERIFIED** | `burn.py` docstring + `ENABLE_TCDN_BURN` default false; `config.py:166` requires `TCDN_AI_BURN_LEGAL_REVIEW_ACK` in production | interface only, no tx submission path |
| 4.8 | Paystack Transfers payouts + controls | P2 | **MISSING** | — | owner-deferred issuance |
| 4.9 | Nightly reconciliation + checksummed export | P2 | **MISSING** | — | — |
| 4.10 | Multi-currency display, per-country routing, FX stored, tax fields | P2 | **MISSING** | — | — |
| 4.11 | Seeder receipt ingestion + payout statement export | P2 | **MISSING** | — | — |

## 5. Security

| ID | Item | Level | Status | Evidence | Notes |
|---|---|---|---|---|---|
| 5.1 | Crypto review: vetted libs, no custom crypto, no nonce reuse (tested), domain separation, constant time | P1 | **PARTIAL** | `test_ai_core.py` crypto/tamper/domain tests; `test_ai_components.py:428 test_nonce_cannot_be_reused` | checklist not assembled as one artifact; AES-GCM rationale in `SECURITY.md` not verified row by row |
| 5.2 | Key inventory: rotation with overlap, revocation, no coordinator keys in repo, prod secret store | P1 | **PARTIAL** | `keyring.py` rotate/revoke + tests; no key files tracked | production secret-store mode unproven |
| 5.3 | Authorization matrix over every route and role; no IDOR | P1 | **PARTIAL** | `test_authorization_matrix.py` (7 tests) incl. route-inventory assertions, foreign-tenant 404, worker-key rejection — all pass | does not cover every legacy CDN route × every role |
| 5.4 | Replay + rate-limit state shared via Redis, two instances | P1 | **PARTIAL** | tickets, attestation nonces and rate limits are Redis-backed; `test_ai_redis.py` **18/18** incl. running-loop path | **no two-instance test**; receipt replay still in-process |
| 5.5 | SAST, fuzzing to budget, ZAP, Trivy; no open critical/high | P1 | **MISSING** | gitleaks/bandit/semgrep/Trivy/ZAP **not installed**; `scripts/fuzz_ai.py` exists but budget not run | blocks Level 1 |
| 5.6 | Injection, traversal, SSRF, oversize, malformed inputs | P1 | **PARTIAL** | `test_ai_security.py` (17 tests) pass; body-size guard tested | payment-callback and P2P-sourced payloads not covered |
| 5.7 | Privacy invariant proven with canaries across DB/logs/metrics/backups | P1 | **PARTIAL** | canary assertions in `test_ai_service.py` pass | no environment-wide scan (logs, metrics, temp files, backups) |
| 5.8 | Break tests on 13 named controls | P1 | **PARTIAL** | `docs/tcdn-ai/evidence/06-break-tests/{break-tests.json,log,txt}` present | **not re-run by me**; cannot confirm 10/10 in this tree |
| 5.9 | Business-logic abuse tests | P1 | **PARTIAL** | negative/zero/overflow and wash-transfer tests in `test_ai_core.py`/`test_ai_components.py`; Postgres double-spend test | refund-then-use, free-credit farming, payout kill-switch not tested (endpoint missing) |
| 5.10 | OWASP Top 10 + API Top 10 + LLM Top 10 table, rows point to tests | P1 | **PARTIAL** | `SECURITY.md` has all three sections (lines 74, 89, 104; 19 control references) | row-by-row code/test linkage not re-verified |
| 5.11 | Alert rules → webhook | P2 | **MISSING** | — | — |
| 5.12 | Headers, strict CORS, TLS 1.3 everywhere | P2 | **PARTIAL (CDN)** | existing CDN hardening; AI hops unverified | — |
| 5.13 | Admin surface separated by host/allowlist, 2FA | P2 | **MISSING** | admin routes share the listener (`/api/ai/v1/admin/*`) | — |
| 5.14 | Third-party pentest + crypto review | P2 | **NOT VERIFIABLE HERE** | owner action | self-run review is not a substitute |

## 6. Verification engine (Appendix 3)

| ID | Item | Level | Status | Evidence | Notes |
|---|---|---|---|---|---|
| 6.1 | Sampling unpredictable, after commitment, **wired into placement**, 3%, cap, different operator | P1 | **MISSING** | placement uses the previous sampler; no second-run launch | blocks Level 1 |
| 6.2 | Commitment protocol correctness | P1 | **PARTIAL** | `verification2.py` primitives + transcript/Merkle tests in `test_ai_components.py`, `test_part3_coverage.py` | coordinator does not consume it yet |
| 6.3 | Docs state verification is probabilistic with limits | P1 | **PARTIAL** | `LIMITATIONS.md` / `SECURITY.md` discuss probabilistic assurance | not phrased as required for the shipped engine (engine not wired) |
| 6.4 | Calibration tool + signed tolerance registry | P2 | **MISSING** | placeholders only | — |
| 6.5 | Verifier service, suspect flow, penalties, appeals | P2 | **MISSING** | — | — |
| 6.6 | Synthetic probes | P2 | **MISSING** | — | — |
| 6.7 | Substitution detection measured | P2 | **MISSING** | — | — |

## 7. P2P weights and model delivery

| ID | Item | Level | Status | Evidence | Notes |
|---|---|---|---|---|---|
| 7.1 | Manifests, Merkle, per-shard SHA-256, banning, resume, quotas, HTTPS fallback | P1 | **PARTIAL** | `shards.py` + `test_ai_components.py` (44 tests) pass in the 943-green run | HTTPS fallback not exercised end to end |
| 7.2 | Cold start from empty cache; corrupt shard rejected + sender penalised; origin fallback | P1 | **PARTIAL** | in-process corrupt-shard/ban tests pass | no cold-start or fallback run; no real swarm |
| 7.3 | Node sidecar WebRTC transport, measured cold start | P2 | **MISSING** | — | — |
| 7.4 | TCDN Pull CLI, Ollama/vLLM integration, metering | P2 | **MISSING** | — | — |
| 7.5 | Direct-to-VRAM `GpuDirectSink` | DEFER | **DEFERRED** | `shards.py:288` fails closed; `DiskSink` is the default | owner-approved; docs must not claim diskless |

## 8. Confidential tier

| ID | Item | Level | Status | Evidence | Notes |
|---|---|---|---|---|---|
| 8.1 | Confidential job never reaches non-attested node; worker refuses; both tested | P1 | **PARTIAL** | scheduler/worker checks in code; policy tests in `test_ai_components.py` | "tested both sides" not confirmed as a pair |
| 8.2 | Real adapters fail closed; simulated impossible in prod; session key bound | P1 | **PARTIAL** | `SevSnpVerifier`/`TdxVerifier`/`NvidiaGpuVerifier` raise without `TrustPolicy`; simulated refused in production (test passes) | session-key→attestation binding not proven by a test |
| 8.3 | Real-hardware attestation | DEFER | **DEFERRED** | owner-approved; tier marketed as not available | — |

## 9. Accounts and consoles

| ID | Item | Level | Status | Evidence | Notes |
|---|---|---|---|---|---|
| 9.1 | Human accounts, real email verification, Argon2id, 2FA, sessions | P2 | **MISSING (AI-specific)** | the CDN has accounts/2FA; the AI surface reuses it but no verified-email gate for AI | — |
| 9.2 | Developer / host / admin consoles | P2 | **MISSING** | — | — |
| 9.3 | Abuse reporting, block lists, model license registry | P2 | **MISSING** | — | — |

## 10. Operations and documentation

| ID | Item | Level | Status | Evidence | Notes |
|---|---|---|---|---|---|
| 10.1 | Prod+dev compose, non-root images, health checks; DEV_SETUP followed literally | P1 | **PARTIAL** | `docker-compose.yml` brings up Postgres (verified: container reached ready, 7 Postgres tests passed); backend `Dockerfile` non-root | full stack (proxy/redis/coordinator/worker) not brought up from `DEV_SETUP.md` |
| 10.2 | Capacity test with many keys: ramp, 30-min sustain, 10x spike, server size | P1 | **MISSING** | `backend/scripts/load_test.py` exists | **no results**; blocks Level 1 |
| 10.3 | All docs present and accurate | P1 | **PARTIAL** | most docs exist; `PUSH_CHECKLIST.md` created now; `CONTRACTS.md` **missing**; `VERIFICATION_REPORT.md` counts wrong (C-4); `STATE.md` stale | accuracy blocker |
| 10.4 | 4-hour soak, chaos, full fuzz budget | P2 | **MISSING** | — | — |
| 10.5 | Encrypted backups + tested restore, DR, monitoring, status page | P2 | **MISSING** | — | — |

## 11. Legal, compliance and global

| ID | Item | Level | Status | Evidence | Notes |
|---|---|---|---|---|---|
| 11.1 | Standard tier labelled non-confidential; confidential labelled unavailable; gateway disclosure | P1 | **PARTIAL** | `confidentiality: "none"` in models/registry; `LIMITATIONS.md` says confidential is not available | gateway-mode disclosure absent from docs/Terms; no Terms file at all |
| 11.2 | Draft Terms, DPA, AUP, privacy, refund, sub-processors, `security.txt` | P2 | **MISSING** | `security.txt` exists in `app/main.py`; no `docs/tcdn-ai/legal/` | — |
| 11.3 | `allowed_regions` invariant, measured regions, license geofencing | P2 | **MISSING** | — | — |
| 11.4 | Sanctions screening hooks, KYC-lite, per-country fraud rules | P2 | **MISSING** | — | — |
| 11.5 | Data-subject request workflow, data-flow map | P2 | **MISSING** | — | — |
| 11.6 | Worker agent separable + licence note | P2 | **MISSING** | — | — |

## 12. Push mechanics

| ID | Item | Level | Status | Evidence | Notes |
|---|---|---|---|---|---|
| 12.1 | This checklist complete, counts, "Level 1 ready" line | P1 | **VERIFIED** | this file | says **NO** with reasons |
| 12.2 | `VERIFICATION_REPORT.md` per Appendix 4 §7 | P1 | **PARTIAL** | file exists and follows the shape | **contains wrong counts** (175 AI / 192 legacy; actual 200 / 897) |
| 12.3 | Draft PR description with all required sections | P1 | **MISSING** | no PR opened | blocked by the items above |
| 12.4 | After push: CI green, no secrets, PR link | P1 | **MISSING** | not pushed | follows from 1.8 and 12.3 |

---

## Fixes landed during this confirm pass (M0.1)

| Fix | File | Proof |
|---|---|---|
| `TokenBucketLimiter` took `max_per_second`/`max_burst`; all 4 call sites pass `rate_per_minute`/`burst` → `TypeError` on every AI request | `app/ai/ratelimit.py` | flag-on suite went from **32 failed** to **0** |
| Rate limiting never ran: `async allow()` called without `await`, so the bucket was never consulted (silent bypass) | `app/ai/routes.py:156-170` | `TestRateLimit::test_api_key_bucket_enforced` and the shared-bucket compat test go 200 → 429; previously always 200 |

## Fixes landed in M0.2 — the `ai-store` CI job

Four defects, all reproduced before being fixed. Order matters: the first one killed the job
before the others could even be reached.

| # | Defect | Reproduction | Fix |
|---|---|---|---|
| 1 | Test paths were prefixed with `backend/` while the step's `cwd` was already `backend`, so pytest resolved `backend/backend/tests/…` | `pytest backend/tests/test_ai_core.py` from `backend/` → **exit 4**, `no tests ran` | paths are relative to `backend/` |
| 2 | `TEST_POSTGRES_URL` never set, and `test_ai_postgres.py` is `skipif`'d on it — the money-path lane would have skipped and the job gone green having proved nothing | `env -u TEST_POSTGRES_URL pytest tests/test_ai_postgres.py -rs` → **7 skipped, exit 0** | derived from the container's own credentials and exported |
| 3 | `DATABASE_URL`/`TEST_POSTGRES_URL` hard-coded `tcdn:tcdn@…/tcdn`, but the compose service inits `${POSTGRES_USER:-user}` / `torrentcdn` — so no connection could ever authenticate, even with 1 and 2 fixed | `asyncpg.connect('postgresql://tcdn:tcdn@127.0.0.1:5432/tcdn')` → `InvalidPasswordError` | DSN built from `${POSTGRES_USER}`/`${POSTGRES_PASSWORD}`/`torrentcdn` |
| 4 | Trailing `echo "… exit code $$?"` printed the **shell PID** plus a literal `?` (`exit code 617972?`), so the job's own log reported nonsense | `bash -c 'echo "exit code $$?"'` | line removed; `set -euo pipefail` is explicit |

**Correction to the earlier report.** This checklist previously said `$$?` *masked* failures. It did
not: GitHub runs `bash` steps with `-e`, so a failing pytest already aborted the step with its own
status (`bash -eo pipefail -c '<failing pytest>; echo reached'` → wrapper exit 4, `reached` never
printed). Without `-e` the same line does mask it (`reached echo, status was 4`, wrapper exit 0) —
which is why the fix keeps `set -euo pipefail` explicit rather than relying on the runner's shell
defaults. The misleading `$$?` output is real; the masking claim was wrong.

Also in this pass: `pg_isready`/`redis-cli ping` now run **inside** the containers
(`docker compose exec -T`), so the job no longer depends on the runner image shipping a Postgres
client, and Redis readiness is actually waited for (the job used real Redis but never waited for it).

**Rebuilt from scratch:** `actions/setup-python@v7` (3.12, pip-cached on both requirements files)
so the job no longer relies on whatever `python` the runner happens to expose.

## Next actions (in order)

1. **1.8 / 12.4**: push and get the repaired `ai-store` job green **on GitHub**; add the missing
   SAST / container / SBOM jobs to reach the full Level-1 CI set. *(The job itself is repaired and
   verified locally against fresh containers; only the remote run is outstanding.)*
2. **1.13**: write `CONTRACTS.md` for the seven boundaries + at least one contract test per boundary.
3. **1.6 / 1.7**: gitignore `backend/.coverage`, `clips/`, debug scripts; generate an SBOM.
4. **1.9**: introduce ordered migration files (or document idempotent DDL as the accepted design
   with an upgrade test).
5. **1.3**: fresh-clone verification run; format `app/ai`.
6. **3.1 / 2.1**: Ollama nightly job; official JS client test.
7. **6.1**: wire redundant execution into placement.
8. **10.2**: capacity run with many keys.
9. **12.2 / 12.3**: correct the report counts; only then draft the PR.
