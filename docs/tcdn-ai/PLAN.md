# TCDN-AI — build plan

Status: implementation plan written before code, per the build prompt §0.2.
Date: 2026-10-22.

## 1. Detected stack (verified by inspection, not assumed)

| Concern | What the repo actually uses | Evidence |
|---|---|---|
| Backend language/runtime | Python 3.14 (`py311` ruff target), async | `backend/.venv`, `backend/ruff.toml` |
| Backend framework | FastAPI 0.141 + Starlette, JSON APIs under `/api/...` | `backend/app/main.py:1-40` |
| DB | PostgreSQL via `asyncpg` with hand-written idempotent DDL at startup (`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ... IF NOT EXISTS`). **No Alembic.** | `backend/app/database.py`, `backend/app/main.py:run_startup_migrations` |
| Cache / ephemeral state | Redis (`redis.asyncio`), optional, fail-open | `backend/app/database.py:get_redis` |
| Object storage | MinIO/S3 via `boto3` | `backend/app/routes/upload.py` |
| Frontend | Next.js + TypeScript (untouched by this work) | `frontend/` |
| Test framework | pytest 9 + `starlette.testclient`, all external deps mocked in `backend/tests/conftest.py` | `backend/tests/conftest.py` |
| Linter | ruff 0.16 with the default ruleset; `line-length = 88`; existing justified ignores only | `backend/ruff.toml` |
| Conventions | snake_case modules, `from .config import get_settings`, module-level `logger`, `_private` helpers, docstrings explaining *why*, pydantic v2 models, typed dataclasses for pure logic | sampled across `backend/app/*` |
| P2P present today | WebTorrent in the browser (`frontend/components/VideoPlayer.tsx`, `frontend/public/sdk.js`), torrent building in `backend/app/torrent_builder.py`, WebRTC family in `frontend/node_modules` (client-side only) | inspection |
| CI | GitHub Actions: lint, full-stack, e2e, security (gitleaks + audits), dependency audit | `.github/workflows/*` |

**Consequence:** TCDN-AI is built in Python inside `backend/`, as a new `app/ai` package plus a
separately importable `backend/tcdn_ai_sdk` client package. No new language or runtime is
introduced.

## 2. Language decision for the SDK

The prompt asks for the SDK "in the language(s) the repo already uses" and, for a
single-language repo, a second-language port *spec* rather than a new runtime.

- **Built:** Python SDK (`backend/tcdn_ai_sdk`) — matches the backend.
- **Not built:** TypeScript SDK. `docs/tcdn-ai/SDK-PORT-SPEC.md` specifies the wire
  protocol precisely enough to port without reading the Python. A TypeScript SDK would be a
  natural follow-up because the frontend is TypeScript, but it is a scope decision, not a
  stack change, and it is recorded in `ASSUMPTIONS.md`.

## 3. Files added

```
backend/app/ai/
  __init__.py          package marker + public exports
  config.py            AISettings (env prefix TCDN_AI_), fail-closed startup validation
  errors.py            stable error codes -> HTTP status mapping
  crypto.py            Ed25519 / X25519 / HKDF-SHA256 / AES-256-GCM wrappers, canonical JSON,
                       domain separation, HMAC API-key hashing, zeroisation helper
  keyring.py           key registry, rotation with overlap, revocation list, secret-store interface
  models.py            pydantic v2 request/response models (strict, bounded)
  tickets.py           placement ticket issue/verify + replay cache
  pricing.py           micro-credit price table, effective_from, confidential multiplier, cost math
  ledger.py            integer micro-credits, exact splits, double-entry ledger, holds, idempotency
  scheduler.py         worker eligibility + scoring + confidential-only routing
  receipts.py          worker receipt/client ack verification + reconciliation + dispute rule
  verification.py      redundant-execution sampling, comparison, tiebreak
  shards.py            signed model manifest, Merkle tree, swarm fetch, sinks (disk / GPU)
  attestation.py       AttestationVerifier interface, policy, simulated + real fail-closed adapters
  burn.py              feature-flagged (disabled) burn module with BurnStrategy interface
  store.py             ai_* DDL + Store protocol + PostgresStore + MemoryStore (test/dev only)
  service.py           Coordinator service: placement, receipt reconciliation, settlement, registry
  worker.py            worker agent: backends (vLLM/Ollama/echo-dev), ticket validation, receipts
  routes.py            /api/ai/v1 coordinator API + /api/ai/v1/admin surface
  ratelimit.py         per-key/per-IP token bucket (in-process; documented limitation)

backend/tcdn_ai_sdk/
  __init__.py          Client, errors
  errors.py            typed exceptions
  _crypto.py           SDK-side wrappers over `cryptography` (pinned keys, ephemeral X25519, AEAD, TLS ctx)
  client.py            OpenAI-shaped chat.completions (stream + non-stream), models.list()

backend/tests/test_ai_*.py          unit + integration + security + money + failure tests
backend/scripts/ai_load_test.py     basic documented load script

deploy/tcdn-ai/
  docker-compose.dev.yml            coordinator + worker + engine (manual engine image)
  worker.env.example                worker configuration template
  confidential/coco-kata-notes.md   CoCo/Kata prerequisites (documented, not claimed as working)

docs/tcdn-ai/
  PLAN.md ASSUMPTIONS.md ARCHITECTURE.md SECURITY.md THREAT_MODEL.md API.md RUNBOOK.md
  DEV_SETUP.md SDK.md SDK-PORT-SPEC.md LIMITATIONS.md
```

## 4. Existing files touched (and why)

| File | Change | Why |
|---|---|---|
| `backend/app/main.py` | import + run `AI_TABLES_DDL` in startup migrations; include `app.ai.routes.router` | The repo's migration mechanism is startup DDL; the AI API must mount on the existing app |
| `backend/app/database.py` | add `AI_TABLES_DDL` (re-exported from `app/ai/store.py` to keep DDL next to its models) | Follows the existing DDL-lives-with-db pattern |
| `backend/requirements.txt` | add `cryptography>=42,<47` | vetted primitives; nothing hand-rolled |
| `backend/ruff.toml` | per-file ignores for `app/ai` and `tcdn_ai_sdk` **only if** a rule is genuinely inapplicable (each with a comment) | keep lint green without weakening the rest of the repo |
| `.env.example` | document the `TCDN_AI_*` variables | repo convention is a documented env template |

No frontend files change: TCDN-AI is API/SDK-first this pass. A dashboard is listed as a
next step in `LIMITATIONS.md`, not faked.

## 5. Build order (matches prompt §11)

1. Crypto + keyring + tickets, with known-answer/tamper/replay tests.
2. Data models + `ai_*` DDL.
3. Coordinator core: auth, registry, scheduler, placement tickets.
4. Worker agent: backend adapters, encrypted job handling, receipts.
5. SDK, then end-to-end non-streaming and streaming tests.
6. Ledger, credits, metering, reconciliation.
7. P2P weight streaming: manifest/Merkle, swarm fetch, malicious-peer test.
8. Seeder transfer receipts + payout split with fraud controls.
9. Result-verification sampling.
10. Confidential tier interfaces + simulated adapter + fail-closed real adapters + deploy notes.
11. Feature-flagged burn module (disabled).
12. Hardening pass, OWASP table, docs, full test/lint/secret scan.

## 6. Risks and how they are handled

| Risk | Handling |
|---|---|
| Prompt asks for real GPU attestation and direct-to-VRAM; this machine has neither | Full interfaces, **simulated** adapters labelled in every API response/doc, real adapters fail closed. Recorded in `LIMITATIONS.md`. |
| No real vLLM/Ollama daemon in CI | `InferenceBackend` adapters are HTTP clients to localhost; the test backend is a labelled deterministic echo, refused in production config. |
| Credit math with floats | All money is `int` micro-credits; property tests assert exact splits and a balanced ledger. |
| Startup DDL on a live DB | `CREATE TABLE IF NOT EXISTS` + additive only, matching the existing migration style; no destructive statement. |
| In-process rate limiting is per-instance | Documented as a limitation; Redis-backed counter is the documented production upgrade. |
| Prompt wants a second-language SDK | Port spec shipped instead of a new runtime; recorded as an assumption. |
| Seeder rewarding invites sybil/wash farming | Transfer receipts must be co-signed by a Coordinator-ticketed downloader, plus per-pair caps, reputation weighting and a payout delay; a wash-trading test asserts the cap fires. |

## 7. Config intake (this session)

This session added a config-intake pass over the `TCDN_AI_` fields in `.env.example`.
Scope is tight: only the prefix present in `.env.example` is mapped; unrelated env
sections are untouched.

Added documents:
- `docs/tcdn-ai/CONFIG_INTAKE_SUMMARY.md` — per-field mapping of env key → internal
  name, type, required status, normalization notes, and explicit confirmation requests
  for the ambiguous/unwired fields (`TCDN_AI_ENABLED` vs `ENABLE_TCDN_AI`, the
  `TCDN_AI_ENVIRONMENT` production-default trap, and the launch-pricing effective-from
  date).
- `docs/tcdn-ai/OPEN_QUESTIONS.md` — three follow-up questions for fields that were
  named as intake topics but are **not present** in the repo's `.env.example`:
  HPA fields, the DPS `redis_delimiter` env, and a storage `type`/`value` debug log.

Wired this session (intake-confirmation items resolved in code):
- One feature flag: the AI feature is now gated by `ENABLE_TCDN_AI`, which defaults
  to `false` in `.env.example`. `TCDN_AI_ENABLED` (`AISettings.enabled`) is retained
  only as a deprecated no-op so existing `.env` files do not break.
- Fail-closed production path: when `APP_ENV=production` and `ENABLE_TCDN_AI` is on,
  the host app now requires `TCDN_AI_ENVIRONMENT=production` **and** at least one
  coordinator secret source before it mounts `/api/ai/v1`, runs `AI_TABLES_DDL`, or
  starts the AI sweep. Otherwise the AI routes stay off rather than silently falling
  back to development defaults.
- `main.py` no longer reads `ENABLE_TCDN_AI` in two places with different defaults:
  router mounting, `AI_TABLES_DDL`, and the expiry sweep all use the same
  `_ai_enabled` boolean.

Still open (not present in `.env.example`, tracked in `OPEN_QUESTIONS.md`):
- HPA fields: no such fields exist in `.env.example` or the TCDN-AI docs; need the
  intended platform and exact fields before they can be mapped.
- DPS `redis_delimiter`: no such variable exists in `.env.example`; need the source of
  the requirement and the exact var name.
- Storage `type`/`value` debug log: no such variable exists in `.env.example`; need the
  intended var name(s), scope/verbosity, and whether it is a new feature or an existing
  setting to document.

Also still open from the intake record:
- Launch pricing effective-from date: `.env.example` documents
  `TCDN_AI_LAUNCH_PRICING_UNTIL=2027-04-01` but not the start date. Tracked until
  the start date is documented somewhere authoritative.
