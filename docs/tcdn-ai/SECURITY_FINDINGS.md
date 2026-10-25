# TCDN-AI — Security & Correctness Findings Log

Every finding from the Part 4 verification gate. Severity is my assessment, not a
scanner's rating. "Fixed" means code changed **and** a test or live run proves it.

| ID | Severity | Finding | Location | Status | Evidence |
|----|----------|---------|----------|--------|----------|
| F-01 | Critical | Fresh database: startup migration aborted at `ALTER TABLE uploads` (table only created lazily by the first signup), so **no TCDN-AI tables were ever created** on a clean install. | `app/main.py`, `app/database.py` | Fixed — core tables created before the ALTER batch; AI schema in its own try block | `tests/test_ai_postgres.py::test_startup_migrations_bootstrap_a_fresh_database`; fresh `tcdn_e2e` DB showed 14 `ai_*` tables |
| F-02 | Critical | `PostgresStore` never mapped `ai_jobs.created_at`, so every job read back had `created_at=0`. Every worker receipt was rejected `400 job_expired`: **no job on Postgres could ever settle**, and holds leaked. | `app/ai/store.py::_row_to_job` | Fixed — timestamptz converted to epoch | `test_job_created_at_survives_the_postgres_round_trip`; live journey settles 2 jobs (`journey.json`) |
| F-03 | High | No path expired abandoned jobs or released their hold: a crashed worker locked the customer's credits forever. | `app/ai/store.py`, `service.py`, `main.py` | Fixed — `expire_stale_jobs` (FOR UPDATE SKIP LOCKED) + hold release + production sweep loop; the strict-xfail encoding this gap is now a normal assertion | `test_ai_failure_injection.py::test_abandoned_placement_does_not_hold_credits_forever`; `test_expire_stale_jobs_releases_the_hold` |
| F-04 | High | The documented plaintext-loopback dev worker crashed at startup: uvicorn's TLS context factory was installed even with no certificate. | `scripts/ai_worker.py` | Fixed — TLS kwargs only when a cert is configured | Live worker boot in `journey.txt` (previous run crashed with `RuntimeError: default_ssl_context_factory() requires ssl_certfile`) |
| F-05 | High | Workers never sent heartbeats and registration did not set liveness, so the scheduler kept routing jobs to dead nodes (observed: `ticket is for a different node`, `no worker satisfies this request` after a worker restart). | `scripts/ai_worker.py`, `app/ai/service.py`, `app/ai/store.py` | Fixed — 30 s signed heartbeat loop, `last_heartbeat` set at registration, 120 s liveness gate in placement | Live journey re-run against a DB containing stale nodes succeeded (`journey.txt`) |
| F-06 | Medium | Rejected receipts were a bare `400` in the access log with no code or reason, which is why F-02 took a live replay to find. | `app/ai/routes.py` | Fixed — error handler logs method, path, and error code (never bodies/prompts) | Coordinator log lines `ai error POST /api/ai/v1/jobs/.../<code>` |
| F-07 | High (spec deviation) | No OpenAI-compatible endpoint. The official `openai` client cannot be pointed at the API: `/api/ai/v1/chat/completions` returns 404. Part 3/4 require the official client. | `app/ai/routes.py` | **Open — not implemented.** The project SDK (`tcdn_ai_sdk`) is used instead and is labelled as such. | `journey.json` → `openai_client_compatibility: {status: 404, verdict: "NOT IMPLEMENTED"}` |
| F-08 | Medium | The backend suite does not complete reliably in one `pytest tests` invocation: it stalls at a non-deterministic point (before ~1 % or ~23 %). Every file passes when run alone. Pre-existing: the committed evidence file contains only the command, no output. | test harness / `tests/conftest.py` | **Open.** Documented workaround: run per file. Needs a bisect with `-p no:cacheprovider --timeout`. | `01-tests/full-suite-ENABLE_TCDN_AI-true.txt`; per-file results in this report |
| F-09 | Low (test quality) | Two break-test entries were decorative: the "receipt signature" test tampered the payload and passed on a *different* check, and the attestation mutation pointed at a config-only test. | `tests/test_ai_components.py`, `scripts/break_tests.py` | Fixed — dedicated `test_flipped_signature_rejected` (asserts `bad_signature`), attestation mutation retargeted at the runtime policy test | `06-break-tests/break-tests.txt` → 10/10 CAUGHT |
| F-10 | Low | Coverage on the AI code measured 84 % lines against the 85 % gate; largest gaps are `worker.py` (dev engine paths), the SDK client, and `store.py`. | `01-tests/ai-coverage.json` | **Open** — below threshold by 1 point. | `01-tests/ai-suite-coverage-with-postgres.txt` |

## Not findings (checked, no issue)

* Duplicate payment callback: second delivery is idempotent, balance unchanged (live journey).
* Canary prompt: absent from AI tables and coordinator log after two live jobs (live journey invariant query).
* Rate limiting under burst: 300 concurrent placements → 32×200, 268×429; no 5xx (load evidence).
* Lint on `app/`, `tests/`, `scripts/`: clean. `app/ai` clean.
* Break tests: 10/10 security controls proven to fail when the control is removed.

## Accepted risks / disclosure

`docs/tcdn-ai/VERIFICATION_REPORT.md` lists everything not verified: load at scale,
soak, chaos, full-time fuzzing, DAST (ZAP), Trivy (image download unavailable here),
real GPU/attestation hardware, live Paystack, and an independent penetration test.
None of that is a substitute for a professional pentest and crypto review before
real money or customer data is handled.
