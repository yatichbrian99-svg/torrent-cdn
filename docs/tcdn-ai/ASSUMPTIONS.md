# TCDN-AI — assumptions log

Every decision taken where the build prompt was silent, or where the repo forced a choice.
Format: decision → reason → what would change it.

## A1 — Module location

`backend/app/ai/` (plus importable `backend/tcdn_ai_sdk/`) instead of the prompt's top-level
`tcdn-ai/` tree. The repo is a two-service monorepo (`backend/`, `frontend/`); a parallel
top-level Python tree would need a second test runner, linter config and deploy pipeline.
**Change if:** the AI module is ever split into its own deployable repo.

## A2 — Persistence and migrations

Postgres via `asyncpg`, with additive `CREATE TABLE IF NOT EXISTS` DDL run at startup, exactly
like `backend/app/database.py`. No Alembic (the repo has none). **Change if:** the repo adopts
Alembic; the DDL is isolated in `app/ai/store.py` so it can be converted to one migration.

## A3 — Store abstraction

`Store` is a protocol with two implementations: `PostgresStore` (production) and `MemoryStore`
(tests and local dev only, documented as such in the class docstring and refused when
`TCDN_AI_ENVIRONMENT=production`). The production path never falls back to memory.

## A4 — Crypto library

`cryptography` (Python Cryptographic Authority). Algorithms exactly as specified: Ed25519
signatures, X25519 key agreement, HKDF-SHA-256, AES-256-GCM AEAD, SHA-256 hashing.
**XChaCha20-Poly1305 is not available in `cryptography`**, and adding libsodium would mean a
second native dependency for no security gain here. AES-256-GCM with a fresh 96-bit nonce from
`os.urandom` per message was chosen; nonce reuse probability is negligible at any realistic
message count. Documented in `SECURITY.md` §Crypto.

## A5 — Canonical JSON and domain separation

Every signature covers `domain_ascii || 0x00 || canonical_json(payload)`, where canonical JSON is
`json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)` UTF-8. Distinct domains per
purpose: `tcdn-ai/ticket/v1`, `tcdn-ai/receipt/v1`, `tcdn-ai/ack/v1`,
`tcdn-ai/node-register/v1`, `tcdn-ai/heartbeat/v1`, `tcdn-ai/attestation-verdict/v1`,
`tcdn-ai/manifest/v1`, `tcdn-ai/transfer-receipt/v1`, `tcdn-ai/keys/v1`,
`tcdn-ai/revocations/v1`, `tcdn-ai/burn-audit/v1`.

## A6 — Key material in development

`FileSecretStore` writes the coordinator keyring to the path in
`TCDN_AI_COORDINATOR_KEY_FILE` with mode `0600`, refuses to load a file with broader
permissions, and **refuses to auto-generate** a keyring when `TCDN_AI_ENVIRONMENT=production`.
Development, with no key source configured, auto-generates one in memory (logged loudly, lost on
restart); set `TCDN_AI_COORDINATOR_KEY_FILE` to persist it to a `0600` file, or
`TCDN_AI_COORDINATOR_KEYRING_JSON` for a platform-injected read-only keyring. Production must use
one of those; the `SecretStore` protocol is the KMS/HSM adapter point documented in `RUNBOOK.md`.

## A7 — API keys

Format `tcdn_ai_<43 chars base64url>`; 256 bits of `os.urandom`. Only
`HMAC-SHA-256(pepper, raw_key)` is stored, never the raw key, never a plain SHA-256 (a plain hash
of a low-entropy secret is offline-guessable; a pepper removes that). Pepper is versioned
(`TCDN_AI_API_KEY_PEPPER`, `TCDN_AI_API_KEY_PEPPER_ID`); keys carry the pepper id so rotation can
re-hash on next use. Comparison uses `hmac.compare_digest`.

## A8 — Micro-credit scale

1 credit = 1,000,000 micro-credits; 100 credits = US$1 → 1 US$ = 100,000,000 micro-credits
(matching the prompt). All amounts are Python `int`; `bool` is explicitly rejected as an amount
because `bool` is an `int` subclass. Floats are rejected at the model boundary.

## A9 — Rounding rules (both documented in code)

1. **Per-request cost:** `ceil(tokens × rate_micro_per_mtok / 1_000_000)`, per component
   (input, then output). Ceiling favours the platform by less than one micro-credit per
   component (≈1e-8 USD) and guarantees a non-zero charge for non-zero usage.
2. **Split:** `compute = total × pct // 100`, `seeder = total × seeder_pct // 100`,
   `platform = total − compute − seeder`. The platform absorbs the remainder, and the three parts
   always sum to exactly `total`. Property-tested over random totals.
3. **Confidential multiplier:** rate is scaled by `(100 + 25) // 100` (integer floor on the rate)
   before the ceiling above.

## A10 — Split configuration

Stored in `TCDN_AI_COMPUTE_SHARE_PCT` / `_SEEDER_SHARE_PCT` / `_PLATFORM_SHARE_PCT`, validated at
startup to sum to exactly 100 and each ≥ 0. Each settled job records the percentages used, so a
later config change cannot rewrite history.

## A11 — Free signup credit

500 credits ($5), `TCDN_AI_SIGNUP_CREDITS`, funded by a ledger transfer **out of the platform
revenue account**, never from node earnings. Abuse limits: one grant per account, plus per-IP
grant cap (`TCDN_AI_SIGNUP_IP_CAP`, default 3) enforced against `ai_signup_grants`. Email/phone
verification is delegated to the existing account system; the AI module does not re-implement it.

## A12 — SDK transport and TLS

SDK → coordinator uses `httpx` with an SSL context pinned to `minimum_version=TLSv1_3`,
`check_hostname=True`, `verify_mode=CERT_REQUIRED`, compression disabled via header. SDK → worker
uses the same context. For tests only, `Client(worker_transport=...)` injects an in-process
transport that still runs the real ticket-verification, X25519, HKDF and AEAD path; the default
transport is the TLS-enforcing one and a test asserts its configuration.

## A13 — Worker session keys

One X25519 session keypair per worker, rotated hourly by default
(`TCDN_AI_WORKER_SESSION_ROTATE_SECONDS`), published as a document signed by the worker identity
key and embedded in placement tickets. The SDK never trusts an unbound session key: the ticket's
`worker_public_key` is covered by the coordinator signature.

## A14 — Inference backends

`vLLM` and `Ollama` adapters are HTTP clients to a **localhost** engine (no subprocess, no shell,
allowlisted model names, bounded timeouts). The deterministic `EchoBackend` exists for tests and
local dev, is labelled `"dev-stub"` in every health/response field, and **refuses to run when
`TCDN_AI_ENVIRONMENT=production`**, which is asserted by a test.

## A15 — Attestation reality

Real attestation verification needs vendor roots and hardware that this environment does not
have. `SimulatedVerifier` produces verdicts marked `"simulated"` and is refused in production.
`SevSnpVerifier` / `TdxVerifier` / `NvidiaGpuVerifier` accept evidence only through an
operator-supplied `TrustPolicy` (measurements, TCB floor, vendor roots); with no trust policy
configured they **fail closed** with `ATTESTATION_VERIFIER_UNAVAILABLE` — never "verified by
default". See `LIMITATIONS.md`.

## A16 — Direct-to-VRAM

`WeightSink` has `DiskSink` (works, used by default) and `GpuDirectSink`, which requires CUDA
bindings plus a pinned-memory allocator that this repo does not ship. `GpuDirectSink` raises
`HardwareUnavailable` rather than silently writing to disk. When an engine insists on a
filesystem path, `DiskSink` onto tmpfs is the documented closest equivalent; the docs do **not**
claim diskless operation.

## A17 — P2P weight transport

The browser WebTorrent/WebRTC layer cannot be reused from a Python worker without a JS runtime,
so the swarm fetch is implemented against the existing tracker/torrent concepts with a
`ShardPeer` protocol (bitfield + shard request). The manifest/Merkle/integrity layer is
transport-agnostic and is the reusable part; the peer transport adapter is a documented gap
(`LIMITATIONS.md`), not a claim of working WebRTC from Python.

## A18 — Payment provider

`PaymentProvider` protocol with `FakePaymentProvider` (dev, labelled, refused in production) and
`PaystackPaymentProvider` — the repo already uses Paystack for KES billing, so no new vendor is
introduced. Card data is never stored; webhook signatures are verified with HMAC-SHA-512 and
handled idempotently by event id.

## A19 — Burn module

`ENABLE_TCDN_BURN=false` by default. Enabling it in production raises at startup unless
`TCDN_AI_BURN_LEGAL_REVIEW_ACK` is set; even then the only implemented strategy is
`DryRunBurnStrategy`, which computes and audits amounts and never submits a transaction.

## A20 — Rate limiting

In-process token bucket per API key *and* per client IP, plus a global concurrency cap. Redis is
already a dependency; the buckets are kept in-process for this pass and the limitation (per
instance, not cluster-wide) is stated in `LIMITATIONS.md` and `SECURITY.md`.

## A21 — Admin surface

`/api/ai/v1/admin/*` on the same listener but behind the repo's existing admin JWT check
(`app.auth`), and never reachable with an AI API key (separate dependency, separate scope test).
The prompt prefers a separate listener; that is recorded as a hardening follow-up because the
deployment currently exposes a single backend port through Caddy.

## A22 — Logging and redaction

There is no log-scrubbing filter, and this file previously claimed one; that claim
was false and is corrected here. The guarantee is structural instead: the AI code
logs only metadata (job ids, model names, codes, counts), never request bodies,
prompt text, completions, API keys or key material. `app/ai/routes.py` logs the
method, path and error code of a rejection — never the body. Tests assert this
directly by grepping captured logs (and stored rows) for canary strings in
`tests/test_ai_sdk_e2e.py` and `tests/test_ai_openai_compat.py`.

This distinction matters more in gateway mode (A26): the coordinator does hold
plaintext prompts in memory for the duration of the request, so the no-logging
property is enforced by code review and canary tests, not by a scrubbing layer.

## A23 — Retry and re-placement semantics

The SDK retries placement with exponential backoff and jitter (`max_retries`), and re-places a job
on worker failure only if **no** output chunk was consumed. After the first consumed chunk it
raises `PartialResultError` and the coordinator bills only verified tokens. This is the prompt's
§7 rule, implemented literally.

## A24 — E2E without real network

End-to-end tests drive the coordinator through `httpx.ASGITransport` and the worker through an
in-process transport, both using real signed tickets, real X25519/HKDF/AEAD, and real receipts.
This proves the protocol without needing TLS certificates or a GPU in CI; TLS is separately
asserted at the transport-configuration level.

## A25 — Second-language SDK

Not built. A precise port spec (`SDK-PORT-SPEC.md`) is shipped instead of adding a Node/TypeScript
package, per the prompt's own rule for single-language repos.

## A26 — OpenAI-compatible gateway mode

`POST /v1/chat/completions` and `GET /v1/models` are mounted at the application root
(not under `/api/ai/v1`) so the official OpenAI SDKs work with
`base_url="…/v1"`. The coordinator performs the same X25519 + HKDF-SHA-256 +
AES-256-GCM session the SDK performs, relays decrypted tokens back as SSE, and acks
the job on the client's behalf.

Consequences, deliberately accepted and documented rather than hidden:

- **The coordinator sees prompt and completion content in transit** in this mode.
  It never logs, stores, or persists either (canary tests). Every response carries
  `tcdn.mode="gateway"` and `tcdn.prompt_visibility="gateway"`.
- **Standard tier only.** A `confidential` parameter gets
  `400 unsupported_parameter` pointing at the SDK; the gateway never downgrades.
- Unsupported OpenAI parameters are rejected with `400 unsupported_parameter`, never
  ignored — a caller that sends `tools` must not silently receive a tool-less answer.
- In streaming mode the first frame is read before the 200 is committed, so
  placement/connection/ticket failures surface as real HTTP errors. Failures after
  the first token are an SSE `error` event and the job is left un-acked (it expires
  and the hold is released rather than billing an unverified completion).
- The gateway re-verifies the ticket it just issued (`claim_replay=False`) and
  compares the worker key against the placement response: a bug or tampering in the
  placement path cannot be turned into a request to an unverified worker.
