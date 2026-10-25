# TCDN-AI — build report

Status: built and tested in the existing Python/FastAPI backend. Nothing in this report
claims more than the code does; the honest gaps are in [LIMITATIONS.md](LIMITATIONS.md) and
repeated at the bottom.

## 1. What was built

| Component | Location | State |
|---|---|---|
| Crypto helpers (Ed25519, X25519, HKDF-SHA-256, AES-256-GCM, canonical JSON, domain separation, HMAC API keys, zeroisation) | `backend/app/ai/crypto.py` | Real, tested |
| Key registry: rotation with overlap, revocation list, secret-store interface (`FileSecretStore` 0600, platform-injected `EnvSecretStore` read-only, in-memory dev store) | `backend/app/ai/keyring.py` | Real, tested |
| Signed placement tickets + replay cache + clock skew bounds | `backend/app/ai/tickets.py` | Real, tested |
| Pricing table (micro-credits, `effective_from`, launch-price lock, confidential +25 %) | `backend/app/ai/pricing.py` | Real, tested |
| Double-entry ledger, holds, idempotency, exact 70/10/20 split | `backend/app/ai/ledger.py` | Real, tested (property tests) |
| Scheduler (VRAM/latency/price/reputation/load filtering, confidential-only routing) | `backend/app/ai/scheduler.py` | Real, tested |
| Receipts + client acks + reconciliation + dispute tolerance | `backend/app/ai/receipts.py` | Real, tested |
| Redundant-execution sampling, comparison, tiebreak, penalties | `backend/app/ai/verification.py` | Logic real + tested; launch orchestration is manual (see gaps) |
| Signed manifests, Merkle proofs, swarm fetch with malicious-peer handling, `DiskSink`/`GpuDirectSink` | `backend/app/ai/shards.py` | Integrity + fetch real; GPU sink raises `HardwareUnavailable` |
| Attestation interface + policy + simulated adapter + fail-closed SEV-SNP/TDX/NVIDIA adapters | `backend/app/ai/attestation.py` | Simulated works in dev; real adapters fail closed |
| Payments (`PaymentProvider`, dev fake, Paystack adapter with signed idempotent webhooks) | `backend/app/ai/payments.py` | Real integration path, untested against live Paystack |
| Seeder transfer receipts, fraud controls, payout distribution math | `backend/app/ai/payouts.py` | Real, tested; receipt ingestion and payout issuance are not exposed yet (see gaps) |
| Burn module (feature-flagged off, dry-run only) | `backend/app/ai/burn.py` | Disabled by design |
| Postgres store + additive `ai_*` DDL, in-memory store for tests | `backend/app/ai/store.py` | Postgres path not exercised against a live DB in CI |
| Coordinator service + FastAPI surface `/api/ai/v1` (+ admin) | `backend/app/ai/service.py`, `routes.py`, `ratelimit.py` | Real, tested through the ASGI app |
| Worker agent: ticket validation, AEAD job handling, streaming, receipts, buffer wipe, vLLM/Ollama/dev adapters | `backend/app/ai/worker.py`, `scripts/ai_worker.py` | Real; engines are loopback HTTP clients |
| OpenAI-compatible SDK (sync + stream, retries, re-placement, typed errors, TLS 1.3 pinning) | `backend/tcdn_ai_sdk/` | Real, tested end to end |
| Load script | `backend/scripts/ai_load_test.py` | Placement-path load only |
| Dev stack + example configs | `deploy/tcdn-ai/` | Compose for coordinator/worker/engine |
| Docs | `docs/tcdn-ai/` | PLAN, ASSUMPTIONS, ARCHITECTURE, SECURITY, THREAT_MODEL, API, RUNBOOK, DEV_SETUP, SDK, SDK-PORT-SPEC, LIMITATIONS |

End-to-end coverage in `backend/tests/test_ai_sdk_e2e.py`: SDK → coordinator (ASGI) →
worker → streaming AEAD frames → signed receipt → ack → settlement, with real signatures and
key agreement. The coordinator only ever sees ciphertext; a test asserts that.

## 2. How to run everything

```bash
# infrastructure
docker compose -f deploy/tcdn-ai/docker-compose.dev.yml up -d postgres redis

# coordinator (from backend/)
TCDN_AI_ENVIRONMENT=development \
TCDN_AI_API_KEY_PEPPER=dev-pepper \
.venv/bin/uvicorn app.main:app --port 8000

# provision an API key + register a model (scripts/ai_provision.py)
.venv/bin/python scripts/ai_provision.py --account <uuid> --model llama-3.1-8b-instruct

# worker (dev stub backend, no GPU needed)
.venv/bin/python scripts/ai_worker.py --coordinator http://127.0.0.1:8000 \
  --models-file ../deploy/tcdn-ai/models.example.json

# SDK smoke test / load test
.venv/bin/python -c "from tcdn_ai_sdk import Client; ..."
.venv/bin/python scripts/ai_load_test.py --base-url http://127.0.0.1:8000 \
  --api-key "$TCDN_AI_API_KEY" --model llama-3.1-8b-instruct --requests 100
```

Exact commands, environment variables and engine setup: [DEV_SETUP.md](DEV_SETUP.md) and
[RUNBOOK.md](RUNBOOK.md).

Tests and lint:

```bash
cd backend
.venv/bin/python -m pytest tests/test_ai_*.py -q   # 154 tests, ~3 s
.venv/bin/python -m pytest -q                     # 849 tests, the whole backend suite (~26 min)
.venv/bin/python -m ruff check app tests scripts
```

Last verified run: 154 AI tests passed (3 s), 849 backend tests passed (26 min), ruff clean,
dead-code and SQL-injection audits clean, secret scan clean.

## 3. Key inventory and rotation

| Key | Holder | Storage | Rotation |
|---|---|---|---|
| Coordinator signing (Ed25519) | Coordinator | Secret-store interface (`FileSecretStore` 0600, or platform-injected `EnvSecretStore`; KMS/HSM adapter point in prod) | `TCDN_AI_KEY_OVERLAP_SECONDS` overlap window (default 3600 s); SDK pins current + next public key from `GET /api/ai/v1/keys/coordinator` |
| Worker identity (Ed25519) | Worker | `TCDN_AI_IDENTITY_FILE` (0600) | Regenerate on re-registration/compromise; old key added to `revocations` |
| Worker session (X25519) | Worker | In memory | `TCDN_AI_WORKER_SESSION_ROTATE_SECONDS` (default 3600) and on restart; re-published signed by the identity key |
| Per-container instance (Ed25519) | Guest only | Guest memory | New per container instance; public key bound into attestation report data |
| SDK ephemeral (X25519) | SDK | Memory only | Single use per request, discarded |
| API key pepper | Coordinator | `TCDN_AI_API_KEY_PEPPER` (versioned by `_PEPPER_ID`) | Rotate by adding a new pepper id; keys re-hash on next use |
| Receipt/ledger statement signing | Coordinator | Same secret store | Scheduled; statements carry `key_id` |
| Seeder/peer identity (Ed25519) | P2P node | Node-local file | On compromise; transfer receipts carry `key_id` |

Revocation: append to the signed revocation list (`GET /api/ai/v1/keys/coordinator`); verifiers
reject unknown, expired or revoked `key_id` values. Step-by-step incident/rotation procedures:
[RUNBOOK.md](RUNBOOK.md).

## 4. OWASP status

[SECURITY.md](SECURITY.md) carries the three mapping tables (OWASP Top 10 2021, OWASP API
Security Top 10 2023, OWASP LLM Top 10). Every row names the control and an existing, passing
test; a script check confirmed each cited test function exists in `backend/tests/test_ai_*.py`.

Open rows (documented, not silently passed):

* **A09 alerting** — audit rows are written, but no alert rules ship.
* **A06 SBOM** — generated by CI (`.github/workflows/dependency-audit.yml`, CycloneDX artifact),
  not by module code.
* **A05 separate listener** — admin endpoints use the existing admin JWT on the shared listener.
* Rate-limit buckets, replay caches and attestation nonces are process-local.

## 5. Assumptions

All 25 are in [ASSUMPTIONS.md](ASSUMPTIONS.md). The ones with the largest blast radius:
module lives in `backend/app/ai` (A1), AES-256-GCM because `cryptography` has no
XChaCha20-Poly1305 (A4), integer micro-credits with platform absorbing split remainders (A8/A9),
dev keyring is in-memory unless a key file is configured (A6), Python-side P2P transport is a
`ShardPeer` protocol rather than browser WebRTC (A17), Paystack for payments because the repo
already uses it (A18).

## 6. Simulated adapters and limitations

* **Attestation** — `SimulatedVerifier` is dev-only and refused in production. `SevSnpVerifier`,
  `TdxVerifier` and `NvidiaGpuVerifier` verify against an operator-supplied trust policy (vendor
  roots, measurements, TCB floor) and fail closed with `ATTESTATION_VERIFIER_UNAVAILABLE` when no
  policy exists. No vendor report parser ships, so the confidential tier cannot be exercised
  against real H100/SEV-SNP/TDX evidence in this repo yet.
* **Direct-to-VRAM** — no CUDA binding; `GpuDirectSink` raises `HardwareUnavailable` rather than
  pretending. `DiskSink` to tmpfs is the documented equivalent when the engine needs a path.
* **P2P weight transport** — manifest/Merkle/integrity and swarm scheduling are real and tested
  against 3+ local peers including a corrupt one; the browser WebRTC data-channel transport is
  not reused from Python, so the peer adapter is a protocol boundary, not a working WebRTC client.
* **Inference engines** — vLLM/Ollama adapters are HTTP clients; CI uses the deterministic echo
  backend, not a live engine.
* **Postgres** — schema and queries are parameterised and tested through the store protocol with
  `MemoryStore`; no live-database integration job exists yet.
* **Paystack** — webhook signature verification and idempotency are unit-tested; no live charge
  was made.
* **Redundant execution** — comparison, tiebreak and penalties are implemented and tested; the
  coordinator does not automatically launch the second worker run.
* **Seeder payouts** — co-signed receipt verification, fraud controls and exact distribution are
  implemented and tested, but nothing ingests transfer receipts or issues a payout statement yet,
  so the seeder pool accrues without being paid out.
* **Burn module** — disabled; dry-run only.

## 7. Recommended next steps

1. Wire real attestation report parsing (SEV-SNP/TDX/NVIDIA) against vendor roots and add a
   hardware-gated CI job.
2. Move rate-limit buckets, ticket replay sets and attestation nonces to Redis for multi-instance
   correctness.
3. Add a Postgres integration test job (dockerised) that runs the `ai_*` DDL and the
   parameterised queries end to end.
4. Automate redundant-execution orchestration and payout holds on mismatch.
5. Ship a JS/TS SDK from `SDK-PORT-SPEC.md` so browser and Node developers get the same client.
6. Add alerting on the audit log (auth failures, hash mismatches, attestation failures, payout
   anomalies) and a TCDN-AI dashboard in the frontend.
7. Split the admin surface onto its own listener before any public launch.

## 8. Part 3 result verification — milestone 1 only

Part 3 replaces the exact-hash scheme above. Only milestone 1 (receipt + transcript
commitment) is implemented; **the coordinator still samples and settles through the
old `verification.py` path**, so this is not yet an active verification system.

Built in milestone 1:

- `app/ai/transcripts.py` — per-token capture (`TranscriptRecorder`), salted prompt
  hash, and an AES-256-GCM record store with a TTL (default 24 h) and wipe/expiry.
- `VerificationReceipt` in `app/ai/models.py` — the `verification` extension on the
  signed execution receipt: unique `receipt_id`, `transcript_root`, token count,
  declared engine/version/dtype/quantization/GPU class, sampling parameters,
  salted `prompt_hash`, attestation reference and transcript source.
- Worker capture — vLLM and Ollama request per-token logprobs; the echo dev backend
  labels its synthetic records `simulated`. A transcript that does not line up 1:1
  with the engine's token count is dropped rather than committed.
- `verifier_sign.ed25519_sign` / `verify_voucher` — real Ed25519 over a closed set
  of Part 3 domains, with `key_id` binding and tamper tests.

Not built yet: coordinator job sampling and challenge authentication, calibration
and tolerance profiles, the verifier service, the suspect flow, the penalty ladder
and ledger entries, privacy bundles/deletion, synthetic probes, and the §10
acceptance measurements. Confidential-tier instance-key co-signing is also not
wired: the receipt fields exist but are not populated from an attestation yet.
