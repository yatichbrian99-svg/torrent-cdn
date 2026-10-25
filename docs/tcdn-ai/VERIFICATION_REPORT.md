# TCDN-AI — Verification Report (Part 4 gate)

Status: **gate not passed.** The end-to-end journey and the security controls are
verified on a live local stack; stress/soak/chaos, full-budget fuzzing, DAST and
container scanning were not completed. Do **not** push to GitHub on the strength
of this report.

## 1. Summary

| Verdict | Items |
|---|---|
| **Verified (ran, passed)** | Fresh-DB bootstrap; live end-to-end journey (signup → email verified → 2FA enforced → API keys → fake-provider purchase incl. duplicate → model registration → worker registration + heartbeat → non-streaming and streaming inference → balance/usage → ledger/hold/canary invariants); AI suite **200** tests (re-measured 2026-10-25; the earlier “175” was stale) and 7 real-Postgres tests passed on a fresh database; **897** legacy tests with `ENABLE_TCDN_AI=false` (re-measured 2026-10-25; the earlier “192” was stale). Flag-**on** suite: **943 passed, 11 skipped** after the M0.1 fixes; it was 32 failed before them; break tests 10/10; fuzz 6 targets × 60 s (0 findings); lint clean; ruff on `app/ai` clean; rate limiting holds under a 300-request burst; five live bugs found and fixed (F-01…F-06) |
| **Failed** | Nothing failed after fixes; the first four journey runs failed and exposed F-01, F-02, F-04, F-05 (see findings log) |
| **Not verified** | Load at scale (only 300 placement requests, no ramp/spike); 30-min sustained, 4-h soak; chaos (kill/partition/disk/memory/clock); fuzzing at the 30-min/target default; DAST/ZAP; Trivy image + IaC scan; SBOM/vuln re-run after dependency edits; full `pytest tests` in one invocation (hangs, F-08); official OpenAI client (not implemented, F-07); real GPU/inference engine, real attestation hardware, Paystack production, M-Pesa; independent pentest and crypto review |
| **Out of scope** | Production Render/Supabase deployment, real customer data, legal review, pricing correctness against a real market |

## 2. Environment

* 8 vCPU, 15.0 GiB RAM, Linux 7.0.12 x86_64, Python 3.14.5 venv.
* Postgres 16.4 (`tcdn-ai-dev-postgres-1`), Redis 7.4.2 (`tcdn-ai-dev-redis-1`), uvicorn (single process), echo worker on `127.0.0.1:9100`.
* Coordinator: `DATABASE_URL=postgresql://tcdn:tcdn@127.0.0.1:5432/tcdn_e2e`, `TCDN_AI_ENVIRONMENT=development`, `TCDN_AI_PAYMENT_PROVIDER=fake`, `ENABLE_TCDN_AI=true`.
* Reproduce:
  ```bash
  docker compose -f deploy/tcdn-ai/docker-compose.dev.yml up -d postgres redis
  docker exec tcdn-ai-dev-postgres-1 psql -U tcdn -d postgres -c 'CREATE DATABASE tcdn_e2e;'
  cd backend
  ENABLE_TCDN_AI=true TCDN_AI_ENVIRONMENT=development TCDN_AI_API_KEY_PEPPER=dev-pepper-change-me \
    TCDN_AI_PAYMENT_PROVIDER=fake DATABASE_URL=postgresql://tcdn:tcdn@127.0.0.1:5432/tcdn_e2e \
    JWT_SECRET="this-is-a-32-char-secret-for-testing-only!!" \
    .venv/bin/python -m uvicorn app.main:app --port 8000
  DATABASE_URL=... .venv/bin/python scripts/live_ai_journey.py --out ../docs/tcdn-ai/evidence/04-e2e/journey.json
  ```
  Raw outputs: `docs/tcdn-ai/evidence/` (`01-tests`, `02-lint`, `03-security`, `04-e2e`, `05-load`, `06-break-tests`, `07-chaos`).

## 3. End-to-end results

One command: `backend/scripts/live_ai_journey.py` (real HTTP, real Ed25519 tickets, X25519+HKDF+AES-GCM transport, Postgres, Redis).

| Step | Result | Notes |
|---|---|---|
| health | PASS | `healthy` |
| signup + email verification | PASS | no email provider configured → auto-verified (documented dev path; production gate still enforced when a provider exists) |
| 2FA enrolled and enforced | PASS | fresh login returns `mfa_required`; `/auth/mfa/confirm` mints the session |
| API keys (admin HTTP) | PASS | inference + worker scopes |
| model registration | PASS | documented `ai_provision.py` |
| purchase + duplicate callback | PASS | 5,000,000 micro; second delivery ignored |
| worker registration + heartbeat | PASS | node visible and schedulable |
| official OpenAI client | **NOT IMPLEMENTED** | `/api/ai/v1/chat/completions` → 404 (F-07) |
| non-streaming inference (project SDK) | PASS | 10 tokens, `finish=stop` |
| streaming inference (project SDK) | PASS | 3 chunks |
| balance/usage | PASS | 4,999,922 micro, 0 held, usage lists 2 jobs |
| invariants | PASS | 7 ledger postings, net = reported balance, splits sum to totals, 0 open holds, canary absent from all AI tables and the coordinator log |

Evidence: `04-e2e/journey.txt`, `04-e2e/journey.json`.

## 4. Invariants

* Ledger: account net equals reported balance exactly; every posting positive; per-job `compute+seeder+platform == total`.
* No holds left open after settlement; abandoned jobs (no receipt) are expired and released (unit + Postgres tests).
* Canary string: not present in `ai_jobs`, `ai_receipts`, `ai_audit_log`, `ai_ledger_entries`, or the coordinator log.
* Duplicate payment event: idempotent, single credit.
* **Not verified:** invariant checks after chaos/soak; cross-account free-credit farming at scale; refund flows live.

## 5. Stress and chaos

| Target (proposed) | Measured | Verdict |
|---|---|---|
| Placement p50 < 50 ms @ 20 RPS | 8.9 ms @ ~2 concurrent, 75.6 ms in a 300-request burst | Met at low concurrency; burst measured through the rate limiter |
| Placement p95 < 200 ms | 21.0 ms (steady), 501.7 ms (burst, mostly 429s) | Met steady-state |
| Error rate under burst | 32×200 / 268×429, **0×5xx** | Rate limiting holds |
| Sustained 30 min @ 70–80 % saturation | Not run | **NOT VERIFIED** |
| Spike 10× + recovery | Not run (only the unplanned 300-request burst) | **NOT VERIFIED** |
| 4 h soak (memory/FD/pool/disk/log growth) | Not run | **NOT VERIFIED** |
| Chaos: kill coordinator/DB mid-stream, partition, disk full, clock skew, provider outage | Only in-process failure injection | **NOT VERIFIED** |

Launch sizing cannot be recommended from this data; the only number measured is
single-host placement latency with a stub worker. Inference sizing needs a real
engine and GPU.

## 6. Security

* **SAST:** `ruff` clean across `app/`, `tests/`, `scripts/`; `app/ai` clean. Bandit output in `03-security/bandit.txt`.
* **Secrets:** gitleaks worktree + full history (all branches) → `03-security/gitleaks-*.txt/.json` (history scan reports no leaks).
* **Dependencies:** `pip-audit`, `npm audit` captured; SBOM at `03-security/sbom-backend.cdx.json`. Re-run needed after this session's edits.
* **Containers:** not scanned — Trivy download failed in this environment. Dockerfiles were read manually: non-root users (backend/frontend), patch-pinned bases, no secrets baked in; `deploy/mediamtx` runs upstream distroless image as its shipped user. **NOT VERIFIED** by a scanner.
* **DAST/ZAP:** not run. The authorization matrix test (`tests/test_authorization_matrix.py`, 10 tests) covers route × role × ownership against the in-process app, not a live staging host.
* **Fuzzing:** `scripts/fuzz_ai.py`, 6 targets (`ticket.verify`, `receipt.verify`, `shards.verify_merkle_proof`, `crypto.aead_decrypt`, `PlacementRequest`, `payments.verify_webhook`), 60 s each, seed 1337 → **0 findings in 360 s**. Default budget is 30 min/target: **NOT VERIFIED at full budget.**
* **Break tests:** 10/10 critical controls proven falsifiable (receipt signature, domain separation, AEAD AAD binding, ticket replay, hold overdraw, simulated attestation in production, admin JWT separation, Merkle proof, webhook signature, job ownership). `06-break-tests/break-tests.txt`.
* **Crypto review (manual):** Ed25519/X25519/HKDF/AES-GCM from `cryptography`; domain-separated signing; per-message nonces; `hmac.compare_digest`/constant-time comparisons; simulated attestation refused in production (break-tested). No custom primitives. **Not** independently reviewed.
* **Privacy:** canary scan passed for the exercised paths; no scan of backups, disk images, or crash dumps.

## 7. Traceability (Parts 1–4)

| Requirement | Status | Evidence |
|---|---|---|
| Feature flag `ENABLE_TCDN_AI`, no regressions | Implemented, verified | 897 legacy tests with flag off (re-measured 2026-10-25); AI tables only created when the app boots (idempotent DDL only — there is no down-migration) |
| OpenAI-compatible API for official client | **Not implemented** | journey probe → 404 (F-07) |
| Signup, 2FA, API keys, credits, inference, usage | Implemented, verified | live journey |
| Host installer / qualification / probation / payouts | Partially implemented; **not verified live** (payout unit tests only: `tests/test_payouts.py`, 36 passed) | — |
| Cold-start P2P from peers, corrupted shard rejection, origin fallback | Implemented; tests only (`tests/test_ai_components.py::TestShards`, Merkle break test) | **Not verified on a real swarm** |
| Confidential tier routing + attestation refusal | Implemented; simulated adapter tested; production refusal break-tested | **Real hardware NOT VERIFIED** |
| Verification engine (honest/substituted/forged) | Implemented; `tests/test_part3_coverage.py` (46 passed) | Simulated transcripts labelled |
| Payments: success, duplicate, forged, refunds | Success/duplicate/forged verified live + tests; **refund and late callbacks not verified live** | `test_ai_billing.py` |
| Failure handling (crash, restart, DB restore) | Abandoned-job path verified; **mid-stream crash/restart NOT VERIFIED** | `07-chaos/README.txt` |
| Ledger invariants, no orphaned holds | Implemented, verified | journey + Postgres tests |
| No prompt/output/key retention | Verified for exercised paths (canary) | journey invariants |
| Authorization matrix | Implemented, verified in-process (10 tests) | `tests/test_authorization_matrix.py` |
| Break tests for critical controls | Verified 10/10 | `06-break-tests/` |
| Stress, soak, chaos | **Not verified** | section 5 |
| Full git-history secret scan | Verified clean | `03-security/gitleaks-history.txt` |
| CI green with all gates | **Not verified** — CI workflows exist but were not run against this branch; push blocked | `.github/workflows/` |
| Docs complete | Plan/architecture/security/threat model/API/runbook/dev setup/assumptions exist; this report and findings log added today | — |

## 8. Known limitations and risks

1. The official OpenAI client does not work (F-07); any launch messaging that says "drop-in OpenAI replacement" would be false.
2. The backend suite cannot be trusted to finish in one invocation (F-08) — CI must run per file or the hang must be fixed first.
3. No chaos or soak evidence: crash/restart money correctness is proven only for the abandoned-job path.
4. Fuzzing was 1/30 of the required budget per target; DAST and container scanning were not run.
5. Coverage on new AI code is 84 % vs the 85 % gate (F-10).
6. Everything ran on one host with an echo stub: no real GPU, no real engine, no real attestation, no Paystack production, no cellular/M-Pesa.
7. An automated and self-run review is **not** a substitute for a professional penetration test and an independent cryptography review. Arrange both before handling real money or real customer data.

## 9. Recommended next steps

1. Fix F-08 (suite hang) and raise coverage to the gate; then run the whole suite in CI per file as a stopgap.
2. Run the missing stress plan (ramp, 30-min sustained, spike, 4-h soak) with k6 against the Compose stack; add toxiproxy chaos and a coordinator/database kill under load.
3. Run ZAP against a staging host and Trivy against every image; triage and fix before any push.
4. Decide F-07: either add an OpenAI-compatible shim (placement + encrypted worker protocol behind `/v1/chat/completions`) or withdraw the compatibility claim.
5. Fuzz at 30 min per target and keep corpora; then commission the external pentest and crypto review.
6. Only after 1–5: complete the pre-push checklist, push a feature branch, and open a PR for review.
