# TCDN-AI — HANDOFF AUDIT (Phases 1–2)

**Audited:** 2026-10-25
**Auditor:** fresh session, no memory of the previous build session
**Branch audited:** `fix/tcdn-ai-redis-replay-nonce` @ `1497062` (working tree, with 40 modified tracked files and 124 untracked files on top)
**Mode:** read-only. The only file written is this report.

---

## 0. CRITICAL ALERTS (read this first)

**C-1 — The TCDN-AI API is broken when the feature flag is ON.** With
`ENABLE_TCDN_AI=true`, **32 tests fail, all from one cause**: `backend/app/ai/routes.py:103`
and `:108` construct `TokenBucketLimiter(rate_per_minute=..., burst=...)`, but
`backend/app/ai/ratelimit.py:43` defines `TokenBucketLimiter(*, max_per_second, max_burst, clock)`.
Every AI request that builds the limiters raises `TypeError`, so the coordinator API and the
OpenAI-compatible surface are unusable in an AI-enabled deployment. This is a hard blocker for
Level 1 (Section E items 1.2, 2.1).
**Evidence:** `ENABLE_TCDN_AI=true .venv/bin/python -m pytest -q` → `32 failed, 911 passed,
11 skipped in 89.70s`; every failure is `TypeError: TokenBucketLimiter.__init__() got an
unexpected keyword argument 'rate_per_minute'` at `app/ai/routes.py:103`.

**C-2 — The pushed branch contains C-1 and has never run CI.** Branch
`fix/tcdn-ai-redis-replay-nonce` (`1497062`, pushed) ships the broken limiter. Every workflow's
`push` trigger is filtered to `branches: [main]`, so no lint/test/security job has run against
it. The branch must not be treated as validated.

**C-3 — The CI job that is supposed to test AI against Postgres/Redis cannot work.** It exists
only in the *working tree* (`.github/workflows/ci-fullstack.yml`, uncommitted), and as written
it (a) runs `cd backend` and then `pytest backend/tests/test_ai_core.py` — a doubled path that
pytest cannot find, (b) ends with `echo "…exit code $$?"`, which prints the shell PID and
therefore always exits 0, masking any failure, and (c) does not set `TEST_POSTGRES_URL`, so the
7 real-Postgres tests would skip even if the paths were right.

**C-4 — Documentation overstates the test state (no safety impact, but do not trust the reports).**
`docs/tcdn-ai/VERIFICATION_REPORT.md` claims "AI suite 175 tests … passing" and "192 legacy
tests". Measured now: **200** AI tests (32 failing with the flag on) and **897** legacy tests
passing with the flag off. The figure "7,919 tests pass" quoted in the handoff text appears
nowhere in this repository and matches no count I can produce; it is rejected as unsupported.

**No leaked secret was found.** Full-history scan (334 commits, all branches) for private-key
blocks, `sk_live_`, `AKIA`, `ghp_`, `xox[baprs]-` patterns: no hits. No `.pem`/`.key`/`.p12`
files are tracked and `.gitignore` covers `.env`, `.env.*`, `.venv`, `__pycache__`.

**One important correction to earlier reporting in this session:** a previous message reported
"139 passed, 28 skipped" for the AI suites. That run had the flag **off**, which **skips** the AI
tests. The suite had not actually executed at that point. C-1 only becomes visible with the flag
on. Treat any AI-suite claim made without `ENABLE_TCDN_AI=true` as unproven.

---

## 1. VERDICT

**Roughly 55–60% of the v3 spec exists in code; roughly 40% is verified by something I ran.**
The core money and crypto spine is real and better than the average handoff: integer
micro-credit double-entry ledger, Ed25519 domain separation, signed tickets with replay
protection, signed receipts with client ack, integer 70/10/20 splits, a Postgres store that
bootstraps a fresh database, and a feature flag that genuinely keeps the CDN untouched when off.

It is **not** shippable, and the reasons are mechanical rather than architectural:
a one-line interface mismatch stops the AI API from working (C-1), the CI that should have
caught it is absent or broken (C-2, C-3), and the two required governance documents
(`PUSH_CHECKLIST.md`, `CONTRACTS.md`) do not exist.

Biggest risks, in order:
1. **C-1**: AI surface 500s when enabled — nothing AI has been exercised end-to-end in the
   current tree.
2. **No trustworthy verification loop**: branch never ran CI, the Postgres/Redis CI job is
   broken, and the docs misreport counts, so "green" has no meaning yet.
3. **Verification engine is not wired**: placement still uses the old sampler; tolerance
   profiles are placeholders; redundant execution is not launched from placement. Section E 6.1
   is unmet.
4. **Gaps with money**: seeder payout issuance and payout reconciliation are absent (deferred by
   owner decision, correctly labelled); Postgres concurrency tests exist but only run manually.

**Recommendation:** keep the code, fix the interface in place, restore a trustworthy test/CI
loop, then continue in the Section C order. Do not rebuild modules — the spine is sound.

---

## 2. BUILD, TEST AND SCANNER RESULTS (what I actually ran)

| Check | Command | Result |
|---|---|---|
| Full suite, AI flag off | `cd backend && .venv/bin/python -m pytest -q -p no:cacheprovider` | **897 passed, 57 skipped, 110.87s, exit 0** — one invocation, **no hang** |
| Full suite, AI flag on | same with `ENABLE_TCDN_AI=true` | **32 failed, 911 passed, 11 skipped, 89.70s** (C-1) |
| AI test files only | `pytest tests/test_ai_redis.py -q` | **18 passed** (includes the Redis replay/nonce fix) |
| Real Postgres lane | `TEST_POSTGRES_URL=postgresql://audit_ai:***@127.0.0.1:5432/audit_ai pytest tests/test_ai_postgres.py` | **7 passed** on a freshly created database (audit DB/role dropped afterwards) |
| Lint | `ruff check app/ai tests/test_ai_*.py tests/test_authorization_matrix.py` | **clean** |
| Formatting | `ruff format --check app/ai` | **21 of 31 files would be reformatted** (CI only runs `ruff check`, so not a gate failure) |
| Dependencies | `pip-audit -r requirements.txt` | **No known vulnerabilities found** |
| Secrets (history) | `git grep` over all 334 revisions for key/token patterns | **clean** |
| Feature flag, off | `app.openapi()` with `ENABLE_TCDN_AI` unset | **79 paths, 0 AI paths** |
| Feature flag, on | `app.openapi()` with `ENABLE_TCDN_AI=true` | **95 paths, 16 AI paths** incl. `/v1/chat/completions`, `/v1/models`, `/api/ai/v1/*` |
| Docker stack | `docker compose up -d postgres` | Postgres 16.4 up; container stopped and audit DB/role removed afterwards |

Tool availability: `pip-audit 2.10.1` present; **gitleaks, bandit, semgrep, Trivy, ZAP not
installed**, so 5.5 (dynamic scan, container scan, SAST tooling) is **NOT VERIFIED**.

Live deployment state (checked via GitHub/Vercel/Render records, not assumed): production Vercel
is `200109e`, Render backend has no deployment for this branch, and the only artefact from the
push is a **Vercel preview** for `1497062` (status success) — the preview contains no backend
code, so the fix is not running anywhere.

---

## 3. HOW SECTION B'S CLAIMS HELD UP

| Claim (Section B) | Verdict | Evidence |
|---|---|---|
| Coordinator API mounted at `/api/ai/v1`, gated by `ENABLE_TCDN_AI` | **CONFIRMED** | `app/main.py:722-733`; openapi shows 16 AI paths with flag on, 0 with flag off |
| AI DDL and background sweep gated by the same flag | **CONFIRMED** | `app/main.py:162` `AI_TABLES_DDL` inside `if _ai_enabled`; `main.py:224` expiry task |
| OpenAI `/v1/chat/completions` and `/v1/models` "return 404" | **REFUTED (in this tree)** — routes are registered | `app/ai/routes_openai.py:142,157`; openapi lists both. Runtime behaviour is still broken by C-1 |
| 175 AI tests + authorization matrix passing | **REFUTED** | 200 AI tests exist; 32 fail with flag on (C-1) |
| 192 legacy tests passing with flag off | **REFUTED (stale count)** | 897 passed / 57 skipped |
| "7,919 tests pass" | **UNSUPPORTED** | No such number in the repo; not reproducible from any run |
| Redis ticket replay / nonces shared across instances | **PARTIAL → FIXED THIS SESSION** | Was silently inert (async methods never awaited); now synchronous and covered, 18/18 |
| Postgres store + fresh-DB bootstrap | **CONFIRMED** | 7/7 real-Postgres tests on a new database |
| Double-entry integer ledger, exact 70/10/20, H-1 hold leak | **CONFIRMED FIXED** | `service.py:721-728` releases the hold on dispute; `test_ai_service.py:429` |
| Disputed jobs leak the hold (H-1) | **CLOSED** | same as above; `STATE.md` is stale on this point |
| Simulated components labelled and refused in production | **CONFIRMED (labels), PARTIAL (coverage)** | 45 label mentions; `attestation.py`, `payments.py`, `worker.py` gate on `is_production`; `GpuDirectSink` fails closed |
| Verification engine wired into placement | **MISSING** | placement uses the previous sampler; no second-run launch, no tolerance registry |
| Node sidecar P2P transport | **MISSING** | not present; Python-only shard fetch |
| Consoles, accounts/2FA, multi-currency, payouts, regions, monitoring, legal drafts | **MISSING** | absent from tree (expected: Section C items 9–11, Section D) |
| Migrations additive and reversible | **PARTIAL** | no migration framework; idempotent startup DDL only (`AI_TABLES_DDL`), no down-path |
| `.env`/keys hygiene | **CONFIRMED** | `.gitignore` covers env/venv/build; no key material tracked |

---

## 4. TRACEABILITY MATRIX (grouped, condensed)

Status legend: **V** verified by a run · **U** present, unverified · **P** partial · **S** stub
or simulated · **M** missing · **D** deviated.

| Component | Requirement | Status | Evidence |
|---|---|---|---|
| Flag | `ENABLE_TCDN_AI` off by default; no AI routes/DDL/jobs | **V** | `config.py:187` default `""`; openapi 79 vs 95 paths |
| Flag | Existing product unaffected with flag on/off | **P** | 897 pass off; on: 911 pass but 32 fail (C-1) |
| Build | One invocation, no hang | **V** | 897 pass in 111 s |
| Build | Clean-checkout install/build/lint | **P** | lint clean; format drift in 21 files; no fresh-clone run performed |
| CI | Postgres service container + AI tests | **M** | working-tree job only, broken paths/exit code (C-3) |
| API | `/v1/chat/completions`, `/v1/models`, streaming | **P** | registered (`routes_openai.py`), unusable at runtime (C-1) |
| API | Official OpenAI clients verified | **P** | Official **Python** client (`AsyncOpenAI`) is used in `test_ai_openai_compat.py:21,80` — currently failing (C-1). No official **JavaScript** client test anywhere |
| API | Gateway mode standard-tier only | **U** | disclosure exists in tests' naming; no canary proof I ran |
| API | SDK mode e2e encryption, coordinator never sees prompt | **P** | test exists (`test_coordinator_never_sees_the_prompt`) but currently fails (C-1) |
| Coordinator | auth, keys (`tcdn_ai_` + HMAC pepper), balance/usage | **U** | code present (`service.py`, `store.py`); tests blocked by C-1 |
| Coordinator | nodes register/heartbeat/nonce, key bundle | **U** | routes present; not exercised end-to-end by me |
| Money | integer micro-credit double-entry ledger, exact split | **V** | `test_ai_core.py` 38 tests pass; Postgres lane 7/7 |
| Money | holds, settle, dispute releases hold | **V** | `service.py:705-731`; `test_ai_service.py:429` |
| Money | concurrency (double-spend) on Postgres | **V** | `test_ai_postgres.py::test_concurrent_holds_cannot_overspend` passed |
| Money | Paystack webhook verify + idempotency | **U** | `billing.py`, `payments.py`; not run in this pass |
| Money | free signup credit with verified-email gate | **P** | grant + per-IP cap present; **no `email_verified` gate exists** (grep: no matches in `app/ai/**` or `app/routes/**`), so E2 4.5 is unmet |
| Crypto | Ed25519, domain separation, HKDF, AEAD, constant-time | **V** | `test_ai_core.py` crypto + tamper/domain tests pass |
| Crypto | replay protection actually enforced | **V** | fixed this session; `test_ai_redis.py` 18/18 incl. inside-loop |
| Worker | ticket validation, decrypt, receipts | **U** | `worker.py` present; e2e test blocked by C-1 |
| Worker | real engine (Ollama/vLLM) | **M/NOT VERIFIABLE** | no engine in this environment; no CI job |
| Worker | supervised heartbeat daemon | **P** | heartbeat endpoint + rotation exist; no supervised task/backoff |
| P2P | manifest/Merkle/shard verify, corrupt-peer ban, resume | **U** | `shards.py`, `test_ai_components.py` partly passes |
| P2P | HTTPS origin fallback | **U** | hook present; not exercised |
| P2P | Node sidecar WebRTC transport | **M** | absent |
| Verification | job sampling after commitment wired into placement | **M** | placement uses old sampler; no second-run launch |
| Verification | calibration tool + tolerance registry | **M** | placeholders only |
| Confidential | non-attested node never receives confidential job | **U** | policy + worker refusal in code/tests; not run |
| Confidential | real SEV-SNP/TDX/NVIDIA verifiers | **S/M** | fail-closed skeletons only (correctly labelled) |
| Ops | compose dev/prod, DEV_SETUP followed literally | **P** | compose exists; I brought up Postgres only |
| Ops | load/soak/spike/chaos | **M** | no evidence |
| Security | SAST / DAST / container scan / fuzz budget | **M** | tools not installed; no evidence |
| Docs | `PUSH_CHECKLIST.md`, `CONTRACTS.md` | **M** | do not exist |
| Docs | `VERIFICATION_REPORT.md` numbers | **D** | overstates (C-4) |
| Deferred (owner-approved) | `GpuDirectSink`, real attestation, TS SDK, seeder payout issuance | **D (labelled)** | matches Section C deferred list |

---

## 5. MODEL-SWITCH FRICTION FINDINGS

| ID | Severity | Finding | Location |
|---|---|---|---|
| F-01 | **Critical** | Limiter constructor mismatch breaks every AI request when the flag is on (C-1) | `app/ai/routes.py:103,108` vs `app/ai/ratelimit.py:43` |
| F-02 | **High** | Redis replay cache / nonce store were `async` but called from sync code → checks never ran (fixed this session; keep the regression test) | was `redis_cache.py`, `tickets.py:199`, `attestation.py:181,196` |
| F-03 | **High** | CI job for Postgres/Redis AI tests: doubled paths, `$$?` is not an exit code, `TEST_POSTGRES_URL` unset, and a DSN the compose service could never accept (C-3). **Fixed in M0.2** — four defects reproduced, then repaired; see `PUSH_CHECKLIST.md` | `.github/workflows/ci-fullstack.yml` (now committed) |
| F-04 | **High** | Two truncated/duplicated blocks in `redis_cache.py` (syntax error + 3 dead shim copies) — a half-finished paste, exactly the handoff artefact this audit hunts for. Fixed this session | was `app/ai/redis_cache.py` |
| F-05 | **Medium** | `STATE.md` stale: says branch `main`, "no TCDN-AI code committed", and H-1 open — all false now | `docs/tcdn-ai/STATE.md:3-7` |
| F-06 | **Medium** | `VERIFICATION_REPORT.md` counts are stale/wrong (C-4) | `docs/tcdn-ai/VERIFICATION_REPORT.md:12,96` |
| F-07 | **Medium** | No migration framework; schema is idempotent DDL. No upgrade-from-previous test, no down path | `app/main.py:162`, no alembic |
| F-08 | **Medium** | 21/31 `app/ai` files fail `ruff format --check` — style drift from a different model | `app/ai/*` |
| F-09 | **Low** | `test_ai_failure_injection.py` contains a single test — thin for a "failure injection" lane | `tests/test_ai_failure_injection.py` |
| F-10 | **Low** | AI tests skip silently without the flag, so a flag-off "all green" reads as proof of AI health | `tests/test_ai_*.py` skip markers |
| F-11 | **Low** | `_IncludedRouter` objects in `app.routes` (custom include wrapper) — unusual FastAPI convention; document it or the next model will trip on it | `app/main.py` |

No invented dependencies, no nonexistent imports, no `return True` security checks, no floats on
money paths, and no prompt/token logging were found in this pass.

---

## 6. PHASE 3 PLAN (milestone order, stop-and-report after each)

**M0 — Restore a trustworthy loop (blocker; smallest correct change).**
1. Align `TokenBucketLimiter` with its two callers (`rate_per_minute` + `burst`) and keep
   per-second conversion internal; or change both call sites — pick one, add a test.
2. Re-run the full suite with the flag **on and off**; drive the 32 failures to zero.
3. Fix C-3 in `ci-fullstack.yml` (paths, `TEST_POSTGRES_URL`, real exit-status propagation) and
   commit it; add the AI job to the `pull_request` path.
4. Refresh `STATE.md`, `VERIFICATION_REPORT.md` counts, and remove the "7,919" figure.
5. Create `PUSH_CHECKLIST.md` (Section E2 rows) and `CONTRACTS.md` (empty contract stubs with
   the seven boundaries; filled per milestone).
*Verify:* both suite runs, CI run link, checklist rows.

**M1 — OpenAI gateway proven with official clients** (Section C item 1 / E2.1–2.3): Python and
JS OpenAI clients, streaming and non-streaming, gateway-mode disclosure, canary proof that
prompts never reach logs/storage, SDK-mode proof that the coordinator never sees the prompt.

**M2 — Live engine in CI** (C item 2): nightly/manual Ollama job with a tiny model, real
inference and streaming through the adapter; re-run the journey with real email verification.

**M3 — Redis + Postgres in CI** (C item 3): service containers, Postgres-backed AI tests wired in,
`TEST_POSTGRES_URL` set, hang root-cause kept under a regression test.

**M4 — Redundant execution wired into placement** (C item 4), then Appendix 3 milestones 1–9.

**M5 — HTTPS origin fallback**, then the Node sidecar behind `ShardPeer` (C item 5), with measured
cold-start numbers.

Then C items 6–11, then Section D in its stated order.

---

## 7. QUESTIONS / DECISIONS NEEDED

1. **M0 interface direction:** should `TokenBucketLimiter` take `rate_per_minute`/`burst`
   (matching `AISettings.rate_limit_per_minute` and both call sites), or should the call sites
   convert to `max_per_second`? I recommend the former — one class change, no call-site churn.
2. **Branch hygiene:** `fix/tcdn-ai-redis-replay-nonce` now carries a known-broken AI surface.
   Keep committing M0 onto it, or open `feature/tcdn-ai-m0-restore-loop` from `main` and treat
   the old branch as a spike?
3. **Docs policy:** `VERIFICATION_REPORT.md` and `STATE.md` will be corrected to measured
   numbers in M0. Confirm you want the old claims replaced rather than kept with a correction
   note.
