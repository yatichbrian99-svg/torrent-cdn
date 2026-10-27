# TCDN-AI — independent audit report

**Auditor:** independent (did not write the branch).
**Subject:** branch `fix/tcdn-ai-redis-replay-nonce`, HEAD `0e27a88`, 41 commits ahead of `main` (`200109e`).
**Date:** 2026-10-27.
**Method:** fresh `git clone file:///…` (no shared object cache), new virtualenv built from
`backend/requirements{,-dev}.txt` with an empty pip cache, real Postgres 16 + Redis 7, plus
source review and purpose-built attacks. Nothing on the branch was fixed, committed or pushed
*at audit time*; only this file was created. See the fix status below for what changed after
the audit.

---

## Fix status (added 2026-10-27, after the audit — the report below is otherwise as written)

| Finding | State | Fix | Evidence |
|---|---|---|---|
| F-1 settlement bypass (free inference for a client that never acks) | **Fixed** | Billing is driven by the worker's signed receipt, not the client ack: the receipt settles the job on its own token counts, capped at the placed hold. A mismatched ack is evidence — it flags the job for review (`dispute_reason`, `job.billing_review` audit row) and cannot cancel or re-price the charge; adjusting it stays the admin refund path's job | `test_ai_service.py::TestEncryptedJob::test_placement_worker_receipt_ack_settlement` (settles with **no** ack, then a late matching ack pays nobody twice), `...::test_mismatched_ack_flags_for_review_without_cancelling_the_charge`, `TestHoldHeadroomAndDisputes::test_over_hold_receipt_is_capped_and_flagged`; break controls *a worker receipt bills the job with no client ack*, *an over-hold receipt is capped at the hold and flagged* |
| F-2 nonce replay race (299/300) | **Fixed** | The nonce is taken with one server-side operation (`GETDEL`, Lua fallback) before validation, so the loser of a race sees a missing key; the lazy blocking-twin client is initialised under a lock | `test_ai_redis.py::TestNonceConsumptionIsAtomic` (8 threads at one nonce, exactly one winner, in-process + two Redis-backed stores), `test_ai_two_instances_live.py::test_concurrent_spends_of_one_live_nonce_produce_one_winner` (real server); break controls *attestation nonce consumption is atomic* (redis, in-process) |
| F-4 `AI_TABLES_DDL` ran with the flag off | **Fixed** | The block is gated on `_ai_enabled` (the flag-off log line is gone: it used to log the skip at DEBUG while creating 17 tables) | `test_ai_migrations.py::test_the_ai_schema_is_only_migrated_when_the_flag_is_on` records the statements `run_startup_migrations` issues in both flag positions; break control *the AI schema migration is gated on ENABLE_TCDN_AI* |
| F-5 gitleaks allowlist widened to whole files | **Fixed** | `.gitleaks.toml` allowlists placeholder **values/secret matches** only (no `paths`), and `security.yml`'s comment now describes that | gitleaks 8.24.0 with this config: 0 findings on the tree and on full history; a planted `ghp_…` token in `.env.example`, `frontend/app/docs/page.tsx` and `frontend/app/page.tsx` yields **0** findings under the path-scoped revision and **3** under this one |
| F-3 SSRF pinning step never used | **Fixed** | `validate_url` still resolves once and validates every answer; the address it returns is now the connect target. A shared `app/pinned_http.PinnedHTTPTransport` (httpcore backend pinned to that IP) is used by both outbound fetches — the AI origin shard fetch and the import-by-URL download — so httpx can never resolve the hostname again at connect time. The request URL is not rewritten: Host/SNI and certificate validation still use the allowlisted name, redirects are still refused, and environment proxies are bypassed (a proxy would re-resolve). `ssrf.py` also blocks private/loopback/link-local/CGNAT/multicast/reserved/metadata ranges explicitly on every hop, plus internal hostname suffixes even when allowlisted | `test_ai_ssrf_pinning.py` — real TLS server, a resolver that answers public at validation and private on any later lookup (the rebinding case) asserts the socket went to the validated public IP and the name was resolved exactly once; a name that cannot resolve at all still fetches (only the pin can do that); a certificate for the wrong host is still rejected; 14 blocked addresses and 6 internal hostnames are parametrically refused. Break control *the origin shard fetch dials the address that was validated (F-3)* |
| F-6 admin money endpoints accept CDN API keys | **Fixed** | New `get_current_user_id_jwt_only` (refuses any `tcdn_` credential from the token itself, before any lookup — no admin oracle) and `require_admin_jwt`. Every AI admin route (`refunds`, `api-keys`, `kill-switch` read/write, `verification`) and the three CDN payout routes that move money or bank details (create/deactivate recipient, initiate transfer) now depend on it; `require_admin` stays for read-only admin views | `test_admin_jwt_only.py` — a valid admin-owning API key is 403 on all 8 money routes **and** the mocked DB is asserted not to have been queried for `is_admin`; a session JWT still reaches the same routes (control); a non-admin JWT is still 403; a structural sweep fails if any money route's dependency tree loses `require_admin_jwt`, in both flag positions. Break control *a CDN API key cannot spend admin money authority (F-6)* |
| Billing authority (extends the F-1 fix) | **Added** | In gateway mode the gateway counts the plaintext it holds — prompt tokens it encrypted, completion token frames it decrypted and delivered — and records that as an internal `GatewayMeasurement` (no HTTP route: the payer cannot price its own job). Settlement bills the **lower** of gateway count and worker receipt when they differ beyond the tolerance, flags the node (`node_overclaim` / `node_receipt_underclaim`), and leaves the receipt as authority in SDK mode or when no measurement exists. Per-node overclaim rate is tracked over settled jobs; at or above the configured share with the sample floor met, the node is suspended, its pending payouts are recorded as `held_for_review` (not paid, money stays the node's) and it is no longer scheduled | `test_ai_node_billing.py` (overclaim billed at the gateway's 100 instead of the claimed 3000; underclaim keeps its own lower count; inside-tolerance keeps the claim; no measurement still settles; suspension + payout hold + not scheduled; honest-node control), plus the gateway-drives-a-lying-worker test; ledger evidence in `evidence/12-billing-authority/overclaim-tests.txt` (three postings, 504 compute + 72 seeder + 144 platform = 720 µ, not the 18 120 µ claimed). Break controls *the gateway's own count outranks an overclaiming receipt*, *an overclaiming node is suspended for review*, *payouts of a node under review are held, not paid* |

Break-test harness after the fixes: **22/22 CAUGHT, 0 unproven**
(`evidence/06-break-tests/break-tests.txt`, regenerated). Three harness drifts were also
repaired: the admin-JWT control's expected occurrence count (3 → 5 admin routes), a
`ruff format` reflow in `gateway.py`, and the stale `require_admin` count after the
JWT-only split.

Suites re-run on this tree (2026-10-28): flag off **1045 passed, 98 skipped**; flag on
**1114 passed, 29 skipped**; `ruff check .`, `ruff format --check app/ai tcdn_ai_sdk` and
`mypy --config-file mypy-ai.ini` clean.

---

## (a) Verdict

**Ready to merge with the flag OFF only after two fixes. Not ready to enable the flag.**

Two fixes are required before this branch can merge even with `ENABLE_TCDN_AI` unset:

1. **`AI_TABLES_DDL` runs unconditionally at startup.** With the flag off, a fresh database
   gets all 17 `ai_*` tables (reproduced below). The code comment and row 1.2 say the opposite.
   Gate the block on `_ai_enabled`.
2. **The gitleaks allowlist was widened from exact placeholder strings to whole files**
   (`.env.example`, `docs/page.tsx`, `page.tsx`). A real credential pasted into any of them is
   now invisible to the CI secret gate. Revert to value-scoped allowlisting.

Turning the flag on is **not** justified: I reproduced a settlement bypass that gives any
customer free inference (F-1), an attestation-nonce replay race that succeeds 299/300 times
(F-2), and an SSRF control whose pinning step is never used (F-3). The author's own
"Level 1 ready: **NO**" is correct, and the five [P1] blockers he lists (5.5, 10.2, 12.3,
12.4, 12.5) remain open. The flag-off product surface (OpenAPI) is byte-identical to `main`,
which is the branch's strongest result.

---

## (b) Every checklist row

Status is the author's; verdict is mine: **agree**, **disagree** (claim not true / not
reproducible), **partial** (true but weaker than stated), **could not verify**.

### P1 — rows marked VERIFIED

| ID | Author | My verdict | Evidence / why |
|---|---|---|---|
| 1.1 | VERIFIED | agree | HEAD is 41 linear commits off `main`; `main` untouched; my fresh clone checks out cleanly. |
| 1.2 | VERIFIED | **disagree (detail)** | Flag off works: OpenAPI is **79 paths, identical methods to `main`**; on: **97 paths / 18 AI paths**, not "95 / 16". Suite here: off **1020 passed, 64 skipped (90 s)**, on **1080 passed, 4 skipped (257 s)**. But "does not run `AI_TABLES_DDL`" is false — F-4. |
| 1.3 | VERIFIED | agree (caveat) | Fresh venv, fresh resolve, no installed package reuse: install succeeded; `ruff check .` clean, `ruff format --check app/ai tcdn_ai_sdk` 36 files, `mypy --config-file mypy-ai.ini` clean. The initial `--no-cache-dir` installs timed out on this network; the completed run used pip's cache. Frontend `npm ci`/`next build` not run (see (e)). |
| 1.4 | VERIFIED | agree | One pytest invocation, no hang, exit 0 both ways. |
| 1.5 | VERIFIED (limited) | **partial** | Two independent scans found no secret: `detect-secrets` over the tree (all hits triaged to keywords/placeholders/test fixtures) and my own blob scanner over every ref in the clone (428 commits, 3 925 blobs): zero AWS/GitHub/Resend/Stripe/Slack/Google keys, zero private-key blocks, zero JWTs; 47 basic-auth-shaped hits were all the literal doc string `https://user:TOKEN@github.com/...`. **But** `.gitleaks.toml` now path-allowlists entire files, so the CI gate is weaker than row 1.5 describes (F-5). gitleaks itself is not installed here. |
| 1.6 | VERIFIED | agree | `git status` clean except the owner-classified untracked file; no coverage/DB/clip artefacts tracked. |
| 1.7 | VERIFIED | agree (limited) | `pip-audit -r requirements*.txt` → no known vulnerabilities; `npm audit --package-lock-only --omit=dev` → 0. SBOM generation not reproduced locally. Requirements are barely pinned (`>=`), so today's fresh install resolves fastapi 0.162/starlette 1.10/pytest 9 — a reproducibility risk the row does not mention. |
| 1.8 | VERIFIED | **could not verify** | No access to GitHub runs from here; I reproduced the *steps* that matter locally (full suite, lint/types, dependency audit). The claim "no open critical/high" is unsupported: `container-scan` is `--exit-code 0` (report-only) and no ZAP/Trivy result is in the tree. |
| 1.9 | VERIFIED (scope) | **partial** | `test_ai_postgres + test_ai_migrations + test_ai_schema_contract` = **18 passed** on real Postgres. But the schema is now **17** tables/17 DDL blocks, not 15, and the "flag-off app … no AI route mounted" half is false at the *schema* level (F-4). |
| 1.10 | VERIFIED | agree | `TestProductionGates` pass; refusal messages are asserted; `test_a_complete_production_config_is_accepted` prevents vacuity. |
| 1.11 | VERIFIED | agree | Simulation labels are asserted on the API surfaces; production refusal overlaps 1.10. |
| 1.12 | VERIFIED | **disagree** | `scripts/dead_code_audit.py` says "No dead code found", but it explicitly does not report a helper referenced only from tests. `Coordinator.grant_signup_credits` and all of `burn.py` are unreachable from any route/CLI; `Settings.mediamtx_hook_secret` is never read (F-7, F-14). |
| 1.13 | VERIFIED | agree | `test_ai_contracts.py` 23 tests pass; doc-resolution test present; TB1–TB7 documented. |
| 2.1 | VERIFIED | **partial** | Python official-client compat tests pass in the suite. The JS harness (`scripts/gateway_harness.py` + `js_client_check.mjs`) was not run here; CI evidence only. |
| 2.2 | VERIFIED | **partial** | The in-process canary test passes (prompt in answer, absent from caplog/store snapshot); the gateway never logs content. Still no end-to-end canary against real Postgres/Redis, and no `/metrics` endpoint exists to leak through. |
| 2.4 | VERIFIED | agree, with F-1 caveat | Lifecycle tests pass, including dispute→hold release. That release is itself part of the billing bypass. |
| 2.5 | VERIFIED | agree | 7 idempotency tests + the Postgres cross-connection test pass. |
| 3.1 | VERIFIED (limited) | **could not verify** | No Ollama/vLLM or GPU here; `test_ai_engine.py` skips without the harness. |
| 4.1 | VERIFIED | agree | Ledger property tests pass; double-entry balances; bool/float rejected. |
| 4.2 | VERIFIED | agree | Split validation and per-job recording tested. |
| 4.3 | VERIFIED | **partial** | Holds/settlements are concurrency-safe as tested, but the customer decides whether settlement happens at all (F-1), so "no double-spend" is true while "gets paid" is not. |
| 4.5 | VERIFIED | **disagree** | `grant_signup_credits` has **no caller** in `app/` — no route, no worker, no script. The five gates are library code exercised only by tests. The row describes a live anti-farming control that the product does not have. |
| 4.6 | VERIFIED | agree (PG divergence) | Refund tests pass (ledger + audit + retry-once). In Postgres a reused `reference` silently does nothing (`ON CONFLICT DO NOTHING`) while the in-memory ledger raises `ConflictError`; the route still reports "refunded" (F-10). |
| 4.7 | VERIFIED | agree | Burn is disabled/dry-run, production-gated, no transaction path; it is also unreachable (fine for an interface-only row). |
| 5.1 | VERIFIED (self-review) | agree (scope) | `CRYPTO_REVIEW.md` citations resolve; primitives are vetted libs; random 96-bit GCM nonces; domain separation. This is a self-review, as labelled. |
| 5.2 | VERIFIED | agree | Key inventory, rotation overlap, revocation list, 0600/injected keyring modes all have passing tests. |
| 5.3 | VERIFIED (limited) | **disagree** | The matrix test passes, but at HEAD it classifies **112 routes**, not 68, with counts (public 20 / jwt 45 / admin 23 / api-key 10 / signature 1 / viewer 7 / internal-hook 6) that do not match the row. The anonymous sweep skips every `{param}` route (`SKIP_PARAM_ROUTES = True`), and any new `/api/ai/v1/*` route with no dependency is auto-classified "signature" without a probe — so "a new endpoint that forgets its auth dependency cannot pass" is false for the AI namespace. Also `require_admin` → `get_current_user_id` accepts **CDN API keys**, so AI money endpoints are not "admin JWT only" (F-6). |
| 5.4 | VERIFIED | **partial** | The 6 fake-Redis + 6 live-Redis tests pass; I reproduced the author's regression claim exactly (reverting the blocking-twin fix: 2 failed, 4 passed; fixed: 12/12 with both files). But nonce consumption is GET-then-DELETE, not atomic: **299/300 concurrent consumes both succeeded** (F-2). |
| 5.6 | VERIFIED | agree (with F-3) | Webhook input hardening, oversize 413, peer payload/manifest traversal tests pass; SQL audit clean. The SSRF row is undermined by 7.1. |
| 5.7 | VERIFIED | **partial** | I executed the scanner against real Postgres **and** Redis: a planted canary in a `text` and a `jsonb` column and in a Redis value was found (exit 1), and the clean run passes. So the scanner itself works. The end-to-end canary is still the in-memory store, not the Postgres tables. |
| 5.8 | VERIFIED | **disagree** | At HEAD `scripts/break_tests.py` prints **12/13 CAUGHT, 1 unproven**, exit 1: `HARNESS-ERROR AI admin endpoints require admin JWT: expected 3 occurrence(s), found 5`. Changing the expected count to 5 (two kill-switch routes were added in M4) makes it 13/13. The shipped evidence still says 10/10. The controls are real; the "13/13 in this tree" claim is not. |
| 5.9 | VERIFIED | agree (narrow) | Kill switch (5 tests), refund idempotency, no-overdraw pass. It does not cover the settlement bypass (F-1). |
| 5.10 | VERIFIED | agree | `TestSecurityDocCitations` passes (52 citations resolve); spot-checked rows. |
| 6.2 | VERIFIED | agree | Commitment/transcript tests pass; domain separation tested. |
| 6.3 | VERIFIED | **disagree** | `LIMITATIONS.md` still says the sample uses "a fixed salt" (gap 4) and that the Redis-backed state "is not shipped" (gap 5) — both contradicted by the code and by the branch's own row 5.4. Two places in the same file say the opposite. |
| 7.1 | VERIFIED | **disagree** | `HttpsOriginPeer` calls `validate_url` at construction but then fetches `self._url(index)` through a normal client; `PinnedTarget.ip` is **never used anywhere in `app/`**. DNS is therefore re-resolved per fetch, and the row's "resolved once / pinned address" guarantee does not hold (F-3). Traversal/oversize/allowlist checks do hold. |
| 8.1 | VERIFIED | agree | Both coordinator and worker halves refuse confidential jobs without attestation; bypass test exists. |
| 8.2 | VERIFIED | agree | Adapters fail closed; simulated refused in production; session key bound twice. |
| 10.3 | VERIFIED | **disagree** | Multiple stale claims: 15 tables (17), 95 paths (97), 68 routes (112), fixed-salt sentence, "Redis not shipped", 365/371 AI tests (1080 here), 13/13 break tests (12/13). Docs that "fail on drift" do not cover prose numbers. |
| 11.1 | VERIFIED | agree | Tier labels on responses and in the disclosure table. |
| 12.1 | VERIFIED | agree | The file says `Level 1 ready: NO` with reasons. |
| 12.2 | VERIFIED | **disagree (numbers)** | The report's counts are not the counts at HEAD (I measured 1020/1080 with Postgres+Redis; without them the author's numbers are plausible, but they are presented as the current record). Break-test evidence in the tree is 10/10. |

### P1 — PARTIAL / MISSING / NOT VERIFIABLE

| ID | Author | My verdict |
|---|---|---|
| 2.3 | PARTIAL | agree — confidential-without-SDK refusal still has no dedicated test. |
| 2.6 | PARTIAL | agree — `max_concurrent_jobs_per_key` has no enforcement anywhere (grep: config only). |
| 3.2 | PARTIAL | agree — bare `create_task`, no supervision/backoff. |
| 3.3 | PARTIAL | agree — one failure-injection test. |
| 4.4 | PARTIAL | agree — signed fixtures only; no sandbox charge. Additionally **no checkout/initialise endpoint exists**, so a user cannot start a purchase at all (webhook-only). |
| 5.5 | MISSING | agree — bandit/pip-audit are clean *when I run them*, but no SAST/ZAP/fuzz budget/blocking Trivy is wired; "no open critical/high" is unsupported. |
| 6.1 | PARTIAL | agree — and note the sampling test is weak: two independent 3 % samples agree ~3 % of the time, so "predicts under a third" is trivially true. The HMAC construction itself is correct. |
| 7.2 | PARTIAL | agree — no real swarm/cold start; origin peer exists but is not IP-pinned (F-3). |
| 10.1 | PARTIAL | agree — full compose stack not run here either. |
| 10.2 | MISSING | agree — no capacity numbers. |
| 12.3–12.5 | MISSING | agree — no PR, post-push confirmation impossible, three workflows still carry the branch push trigger. |
| 5.14 | NOT VERIFIABLE | agree — owner action; self-review is not a substitute. |

### P2 rows (all others)

`2.7, 2.8, 2.9, 2.10, 2.11, 4.8, 4.9, 4.10, 4.11, 5.11, 5.13, 6.4, 6.5, 6.6, 6.7, 7.3,
7.4, 9.1, 9.2, 9.3, 10.4, 10.5, 11.2, 11.3, 11.4, 11.5, 11.6` are MISSING in the checklist and
I found no hidden implementation: agree. `5.12` (headers/CORS/TLS for AI hops) is PARTIAL and I
agree — AI hops were not tested. `7.5`/`8.3` are DEFERRED and the code/docs match (fail-closed
sink, no real attestation): agree.

---

## (c) Findings by severity

### HIGH

#### F-1 — Settlement is client-controlled: any customer gets free inference
`settle` only happens when the **customer** posts an ack that matches the worker's receipt.
A customer who never acks is never charged; a customer who acks a wrong hash or a wrong token
count turns the job into `disputed`, which releases the hold and posts **no** ledger entry.
The completion has already been delivered (native/SDK mode returns it from the worker).

Reproduction (PoC run under `backend/tests/`, deleted afterwards; equivalent through
`POST /api/ai/v1/jobs/{job_id}/ack` with the account's API key):

```
place → worker receipt → ack(tokens_received=0, output_sha256="0"*64)
result.status = disputed, result.total_micro = 0
balance before == after (1000000000), held == 0, job.status == disputed
the gateway's later correct ack now raises ConflictError (409)
```

The no-ack variant is simpler:

```
place → worker receipt → no ack
status = running ("awaiting_client_ack"); job.status = placed
clock += JOB_TTL; expire_stale_jobs() → job expired, hold released
balance before == after, held == 0
```

Impact: 100 % of SDK/native revenue can be avoided by the payer; honest workers are never paid
for those jobs either. The module docstring in `receipts.py` ("bill only `min(worker, client)`
tokens") is not what the code does — it bills zero.
Fix direction: settle from the signed worker receipt and use the ack only as dispute *evidence*
with an operator decision (or charge the minimum on mismatch), never make payment conditional on
a caller-supplied success signal. **(Done that way: `_reconcile_and_settle` now bills the
receipt's own counts capped at the hold, `receipts.py`'s docstring states it, and a mismatched
ack sets `dispute_reason` instead of withholding the charge — see the fix status above.)**

#### F-2 — Attestation nonce is not single-use under concurrency
`RedisNonceStore.consume` / `_consume_blocking` do `GET` then `DELETE`; two callers can both see
the value. With a real Redis and a real `redis.asyncio` client:

```
nonce accepted twice in 299/300 concurrent trials
```

(Re-measured 2026-10-27 while fixing it, with two barrier-released consumers on this Redis:
**122/300** trials double-succeed at no gap and **299/300** with the GET→DELETE window widened
to 5 ms — the window is simply the two round trips, so an attacker only has to race the
verifier, not guess anything. `GETDEL` removes the window instead of narrowing it.)

This defeats the "single-use nonce across instances" property row 5.4/contract TB3 claims
(ticket replay is atomic — `SET NX` — nonces are not). Fix: `GETDEL` (Redis 6.2+) or a Lua
compare-and-delete, and a concurrency test.
Impact today is bounded (attestation evidence replay; no real TEE verifier ships), but the
control is advertised and is broken.

### MEDIUM

#### F-3 — Origin fetch does not use the pinned IP (SSRF rebinding)
`ssrf.validate_url` resolves, validates every address, returns `PinnedTarget.ip` — and nothing
reads it. `HttpsOriginPeer.fetch_shard` fetches `base_url/model/shard` through httpx, which
resolves DNS again. Container/host DNS that changes between construction and fetch (rebinding,
compromised allowlisted origin, expiring domain) reaches internal addresses; the allowlist and
"no cross-host redirect" checks still apply. `docs`/row 7.1 claim "resolved once … caller must
connect to a pinned address". Fix: connect to `PinnedTarget.ip` with `Host`/SNI set to the
allowlisted host, per request, or drop the pinning claim.

#### F-4 — AI schema is created with the feature flag off
`app/main.py:run_startup_migrations` runs `AI_TABLES_DDL` outside `if _ai_enabled`; only the log
level depends on the flag. Reproduced on a fresh database:

```
ENABLE_TCDN_AI=false APP_ENV=development → run_startup_migrations()
_ai_enabled = False
ai_* tables before 0 → after 17
```

Contradicts the module comment ("When off … does not run AI_TABLES_DDL") and row 1.2. It also
means a flag-off production deploy mutates the production schema and can fail (logged at DEBUG)
without anyone noticing. Fix: `if _ai_enabled:` around the block.

#### F-5 — Secret-scan gate weakened by whole-file allowlists
`.gitleaks.toml` now allowlists by **path**: `.env.example`, `docs/page.tsx`, `page.tsx`.
Previously it allowlisted exact placeholder values/lines. A real key pasted into any of those
files passes gitleaks; the compensating `secret_scan.sh` is a separate tool the same author
controls and is not run in the same job. This is the one change in the diff that reduces an
existing security control. Fix: value/line-scoped allowlist only.

#### F-6 — "admin JWT only" money endpoints accept CDN API keys
`/api/ai/v1/admin/{refunds,api-keys,kill-switch,verification}` depend on `require_admin` →
`get_current_user_id`, which accepts `tcdn_` **CDN API keys** (looked up in `api_keys`). An
admin's long-lived API key can therefore mint credits, create AI keys and flip the kill switch;
the route docstring and checklist say "admin JWT". (AI keys `tcdn_ai_*` do not resolve — their
digest is not in `api_keys` — so the narrower claim in `routes.py` about *AI* keys holds.)
Fix: a JWT-only dependency for money-moving admin endpoints, or document that admin API keys
carry admin authority.

#### F-7 — Claimed anti-farming control is unreachable
`Coordinator.grant_signup_credits` (the five gates of row 4.5) has no production caller; the
`ip_hash` it needs is never computed. `test_ai_signup`-style tests pass against dead code.
Consequence: no free credits exist today (not exploitable), but the row certifies a control that
is not in the product. If wired later, remember the per-IP cap takes a caller-supplied hash —
do not take it from the request.

#### F-8 — `LIMITATIONS.md` contradicts the code in the two places buyers read
Gap 4 still says the sample uses "a fixed salt, so a worker can predict which of its jobs will
be sampled"; gap 5 says the Redis-backed replay/nonce implementation "is not shipped". Both are
false at HEAD (keyed HMAC; `RedisReplayCache`/`RedisNonceStore` shipped and live-tested). Row 6.3
and 10.3 claim the file was updated. A security document that understates and misstates the
mechanism is worse than silence.

### LOW

- **F-9** `TicketService.from_redis(keyring, None)` builds `RedisReplayCache(None)` instead of
  the in-memory cache; the first `verify(..., claim_replay=True)` fails as `ReplayError`
  ("redis replay claim failed … falling back to reject"). No current app path uses that call
  (gateway passes `claim_replay=False`; the worker has its own service), so it is a latent trap
  for the next caller and for any single-instance deployment that verifies receipts centrally.
- **F-10** Refund `reference` collision: Postgres `ON CONFLICT DO NOTHING` silently drops a
  second refund that reuses a reference for a different account, while the route returns
  `status: refunded`; the in-memory store raises `ConflictError` instead. Refunding a
  non-existent account is also accepted (credits a ghost account).
- **F-11** Heartbeat has no timestamp/nonce; a captured signed heartbeat can be replayed to keep
  a dead node eligible (liveness gate is the only check).
- **F-12** Stale numbers throughout: 15 vs 17 tables, 95 vs 97 paths, 68 vs 112 routes,
  365/371 vs 1080 AI tests, break-test evidence 10/10 vs claimed 13/13, `STATE.md` still says
  14 tables/39 statements. The drift-detection tests only cover test/doc identifiers, not prose.
- **F-13** `scripts/break_tests.py` prints a `HARNESS-ERROR` and exits 1 at HEAD because M4 added
  two `Depends(require_admin)` routes without updating `occurrences: 3`. This is the second time
  this harness has drifted; the "printing HARNESS-ERROR" mitigation still lets a stale count
  ship as a red gate.
- **F-14** `Settings.mediamtx_hook_secret` is declared with a hard-coded dev default and never
  read; `live_internal` reads the env var directly. Dead config with a weak default is a trap.
- **F-15** Requirements use open lower bounds (`fastapi>=0.104`, `cryptography>=50,<51`, …); a
  fresh install today resolves much newer majors than the author tested (fastapi 0.162,
  starlette 1.10, pytest 9.1). CI green today does not imply green tomorrow. The `ai-types` job
  installs only `requirements-dev.txt`, which currently pulls the same set, but nothing pins it.
- **F-16** The branch's auth change rejects every token minted before `975f7ef` (2026-08-04,
  24 h lifetime). No token from then can still be alive, so "no migration" holds **provided the
  deployed release already mints `iat`**. An older deployment upgrading directly would log out
  every user. Worth a release note.

### Things that are genuinely absent (not defects, but gaps the checklist does not cover)

- No endpoint to **buy** credits (only the signed webhook); no checkout/init, no price display.
- No `/metrics`, no alerting, no reconciliation job, no payout path (`4.8`), no payment refunds.
- No capacity/load result, no soak, no fuzz budget, no ZAP, no blocking container scan.
- Free-credit and referral abuse cannot be assessed because the grant path is not wired.
- Standard-tier gateway mode sends prompts to **self-service** workers: any account with a
  worker-scope key can register a node endpoint and receive other customers' prompts if
  selected. The docs disclose "the coordinator relays plaintext prompts to the worker" but not
  that operators are unvetted. That is a product-trust decision, not a code bug.
- No revocation/monitoring for leaked AI keys beyond rotating the pepper (no per-key revocation
  endpoint on the AI surface; `ai_api_keys` rows can be deleted by hand only).

---

## (d) Disagreements with the author's claims (summary)

1. **"13/13 CAUGHT, 0 unproven in this tree"** — false at HEAD: 12/13, exit 1, harness error.
2. **"flag-off app … does not run AI_TABLES_DDL"** — false; 17 tables are created with the flag
   off. Also "15 tables" is stale (17).
3. **"A new endpoint that forgets its auth dependency cannot pass"** — false for `/api/ai/v1/*`,
   `/v1/*`, `/webhooks*` (prefix fallback), and the param routes are skipped entirely.
4. **"68 routes classified (public 25, jwt 16, admin 12, admin+ai 3, api-key 8, signature 4,
   internal-hook 6, viewer 7)"** — not reproducible: 112 routes, and there is no `admin+ai` class.
5. **"admin endpoints use the existing admin JWT dependency"** — they also accept CDN API keys.
6. **"nonce is single use across instances"** — not under concurrency (299/300).
7. **"HttpsOriginPeer … resolved once … pinned address"** — the pin is never used.
8. **"free-credit gates VERIFIED"** — the function has no caller.
9. **"LIMITATIONS.md updated"** — it still says fixed-salt and "Redis not shipped".
10. **"97 vs 95 paths"**, **"365/371 AI tests"**, **"1.3 install from scratch proven"** (it was
    proven in CI per the report; my local no-cache attempt timed out and was completed with
    cache) — stale or unreproduced numbers.
11. **"refund-then-use … refunded credit still cannot overdraw"** — true, but irrelevant to F-1,
    which avoids the charge entirely.

Where the author is right and deserves credit: flag-off OpenAPI parity is exact; the JWT claim
requirements behave as described (legacy `{sub,exp}` rejected, current/14-day accepted, 100-year
and future-`iat` rejected); the Redis ticket-replay fix is real and I reproduced both its failure
(2 failed without it) and its success; the canary scanner works on real Postgres/Redis; the
ledger is genuinely double-entry; crypto uses vetted primitives with random nonces and domain
separation; the production config gates are not vacuous; the crypto review citations resolve.

---

## (e) What I could not verify / the owner must check

- **The master prompt (Sections A–E, Appendices 1–5) is not in the repository and was not in my
  context**, so requirement-by-requirement conformance to it could not be checked. I audited
  against `PLAN.md`, `PUSH_CHECKLIST.md`, `HANDOFF_AUDIT.md`, `SECURITY.md` and the code.
- GitHub Actions results: no access. I re-ran the underlying commands locally, not the workflows.
- **Frontend**: `npm ci`/`next build`/Vitest and the JS OpenAI-client harness were not run
  (network-bound and not required to judge the AI backend). The `npm prune --omit=dev` change in
  `frontend/Dockerfile` is plausible but unbuilt here.
- **Container scan**: no Trivy/ZAP available; `/tmp/gitleaks` download timed out. The author's
  container scan is report-only, so "no open critical/high" is unproven either way.
- **Real engine (row 3.1)**: no Ollama/vLLM; the engine test skips.
- **Real attestation / confidential tier**: no TEE hardware; by design unavailable.
- **Paystack sandbox (4.4)**: no provider account; only signed fixtures. M-Pesa channel still
  not enabled.
- **Full compose stack (10.1)**, capacity (10.2), soak (10.4): not run.
- **History scan scope**: the fresh clone contained 7 refs / 428 commits (the main checkout lists
  more remote branches), so the author's "39 refs / 396 commits" is not reproducible from the
  clone. No secret was found in what I could see.
- **Real-swarm/cold-start (7.2)**: no peer transport; in-process only.
- The `evidence/` directory is the author's capture; I did not treat any of it as proof.

## Audit environment (for reproduction)

```
HEAD 0e27a88 (fix/tcdn-ai-redis-replay-nonce) vs main 200109e
fresh clone: git clone file:///home/yatich/torrent-cdn /tmp/audit-clone
python 3.14.7, new venv, pip install --no-cache-dir -r requirements.txt -r requirements-dev.txt
Postgres 16-alpine on 127.0.0.1:55432 (user/ci-test-password/torrentcdn)
Redis 7-alpine on 127.0.0.1:56379 (db 15 for tests)
ENABLE_TCDN_AI=false: 1020 passed, 64 skipped, 90 s, exit 0
ENABLE_TCDN_AI=true : 1080 passed,   4 skipped, 257 s, exit 0
ruff check . → clean; ruff format --check app/ai tcdn_ai_sdk → 36 formatted
mypy --config-file mypy-ai.ini → no issues in 36 files
bandit -ll -r app/ai tcdn_ai_sdk → no findings
pip-audit → no known vulnerabilities
npm audit --omit=dev → 0 vulnerabilities
detect-secrets + own blob scan (428 commits, 3925 blobs) → no high-signal secret
```

PoC files used for F-1/F-2/F-9 were placed under `backend/tests/` in the scratch clone only and
deleted; the race script and the DDL probe are reproduced inline above.

---

## (f) Top five risks

1. **Revenue is optional for the payer (F-1).** Settlement depends on the customer's ack; no ack
   or a mismatched ack means zero charge, and the hold is released. This is the first thing a
   paying-customer-turned-abuser will find.
2. **Verification/attestation replay protection is not atomic (F-2).** Ticket replay is fixed;
   nonces are not. The property is advertised in contracts, tests and docs, and fails 299/300
   under two concurrent consumers.
3. **Origin fetch has no SSRF pinning (F-3).** The allowlist is the only barrier; DNS
   rebinding/compromised origin reaches internal networks, contrary to the written control.
4. **Flag-off deployments still change the production database (F-4)** and the CI secret gate was
   weakened by path allowlists (F-5). Both are merge-time risks to the existing product, not AI
   feature risks.
5. **The evidence layer overstates the system.** 12/13 presented as 13/13, 68 routes presented as
   the full matrix, "no dead code" over unreachable controls, and a LIMITATIONS file that
   contradicts the code. A reviewer who trusts the checklist would approve things that are not
   true; that is the systemic risk behind the individual defects.

*This report does not claim the system is secure. It claims the specific defects above are real
and reproducible, and that the flag-off product surface is unchanged except where stated.*
