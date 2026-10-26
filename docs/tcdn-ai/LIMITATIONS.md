# TCDN-AI — limitations and simulated components

This file is the counterweight to the feature list. If something below is not in
your deployment, the corresponding guarantee does not exist.

## Simulated adapters (labelled, refused in production)

| Component | State | Why | What fails closed |
|---|---|---|---|
| Attestation (`SimulatedVerifier`) | **Simulated** | No confidential-computing hardware or vendor roots in this environment | Production config rejects simulated attestation; verdicts carry `status="simulated"` and are refused by `AttestationService.accept` in production |
| Attestation (`SevSnpVerifier`, `TdxVerifier`, `NvidiaGpuVerifier`) | **Fail closed** | Vendor report parsing/root chains are not shipped | Always raise `attestation_verifier_unavailable`; never return "verified" by default |
| Inference (`EchoBackend`) | **Dev stub** | No GPU/engine in CI | Refuses to construct when `TCDN_AI_ALLOW_DEV_STUB_BACKEND=false`; `/health` reports `dev-stub` |
| Payments (`FakePaymentProvider`) | **Dev only** | No live provider credentials here | Refuses construction in production; production uses Paystack |
| Burn module | **Dry run only** | Needs legal/tax review | Disabled by default; enabling in production requires explicit legal ack; no transaction code exists |

## Real gaps

1. **Direct-to-VRAM loading is not implemented.** `GpuDirectSink` raises
   `HardwareUnavailable` (no CUDA bindings/pinned-memory allocator in this repo).
   `DiskSink` writes verified shards to a directory; tmpfs is the closest
   available behaviour. The docs do **not** claim diskless loading.
2. **No Python WebRTC/WebTorrent transport.** The browser P2P layer cannot be
   reused from a Python worker. `ShardPeer` is the transport seam; the manifest,
   Merkle proof, integrity checks, peer banning and resume logic are implemented
   and tested with in-process peers. A production worker needs a transport adapter:
   the **HTTPS origin fallback is now shipped** (`HttpsOriginPeer`: https-only, an
   explicit host allowlist through `ssrf.py`, no cross-host redirects, and a
   response capped at the declared shard size — tested in
   `TestOriginFallback`), but a peer-to-peer transport (libtorrent or a JS
   sidecar) is still not shipped, so a real swarm is untested.
3. **Confidential tier is not usable with real hardware in this build.** The
   interfaces, policy enforcement and key binding exist and are tested; real
   verdict-producing verifiers do not.
4. **Redundant execution is automatic in gateway mode, impossible in SDK mode.**
   A sampled job gets a second signed ticket for a *different* operator at
   placement, and the gateway dispatches the same request to it in the
   background — the gateway holds the plaintext, so it is the only party that
   can. In SDK mode the coordinator never sees the prompt, so it cannot re-run
   the job: the sample is recorded as `not_dispatched_sdk_mode` and never
   compared. Three further limits are real: the sample is bucketed on the job id
   with a fixed salt, so a worker can predict which of its jobs will be sampled;
   the decision is taken at placement rather than after a commitment; and the
   comparison is exact-hash, so two honest but non-deterministic runs disagree
   and are recorded as `inconclusive` with no penalty instead of attributed.
5. **In-process coordination state.** Rate-limit buckets, ticket replay caches and
   attestation nonces live in process memory. With multiple coordinator instances
   the effective limits multiply and a ticket could be replayed across instances.
   The protocols match a Redis-backed implementation; that implementation is not
   shipped.
6. **Admin surface shares the backend listener.** It is protected by the existing
   admin JWT and cannot be reached with an AI key, but the prompt asked for a
   separate listener or strong separation. Network-level separation is a follow-up.
7. **Alerting is absent.** Audit rows exist but no alert rules ship with this module.
   SBOM generation is handled at the repo level (`.github/workflows/dependency-audit.yml`
   emits CycloneDX for the shipped backend environment), not by AI code.
8. **No model serving engine is bundled.** `VLLMBackend`/`OllamaBackend` are HTTP
   clients to a loopback engine. They ARE now exercised against a real engine:
   locally and in the nightly/manual `tcdn-ai-engine.yml` job, with Ollama 0.33.2
   and `qwen2.5:0.5b` on CPU (`evidence/10-real-engine/`). vLLM itself is still
   unverified (it needs a GPU); its adapter is tested against Ollama's
   OpenAI-compatible `/v1` surface, the same wire protocol.
   **Ollama jobs are not covered by the Part 3 transcript protocol:** Ollama's
   logprobs carry `token`/`bytes` and no token id, and a verifier re-scores by
   vocabulary id, so the adapter discards them and the receipt carries no
   verification block. Fixing this needs a tokenizer-backed id mapping (or a
   pinned verifier) — it is not done, and the engine does not claim otherwise.
9. **SQL is now tested against real Postgres in CI.** `tests/test_ai_postgres.py`
   (holds, credits, idempotency, expiry) plus `tests/test_ai_migrations.py` and
   `tests/test_ai_schema_contract.py` run in the `ai-store` job against a real
   Postgres container, and the whole suite still uses `MemoryStore` everywhere
   else. What remains untested is **multi-instance** behaviour: the advisory
   locks protect concurrent writers, not a second coordinator process sharing
   in-memory rate limits and replay caches (gap 5).
10. **Only the Python SDK exists.** A TypeScript SDK is specified
    (`SDK-PORT-SPEC.md`) but not implemented, per the single-language rule.
11. **Worker heartbeat/rotation scheduling is not a daemon.** The launcher registers
    and serves; periodic heartbeats are trivial to add with a task, but a crashed
    heartbeat loop is not restarted by a supervisor in this build.
12. **Free-credit abuse controls: address verification yes, device signals no.**
    A grant now requires `users.verified` (the account system's email-verification
    flag, read through the store), one grant per account, a per-IP cap and a
    monthly platform budget. What is still missing is device fingerprinting and
    phone verification: a farm of distinct verified addresses on distinct IPs can
    still claim the credit until the monthly budget stops it.
13. **Seeder payouts stop short of issuance.** Co-signed transfer-receipt
    verification, the fraud controls (ticketed downloader, pair caps, duplicate and
    same-owner flags) and the exact largest-remainder distribution are implemented
    and tested, and the seeder share accrues to `pool:seeders` at settlement. There
    is no endpoint that ingests transfer receipts or exports a payout statement yet,
    so no seeder payout is paid out by this build. The `ai_transfer_receipts` /
    `ai_payouts` tables exist for that follow-up.

14. **Gateway mode (OpenAI-compatible `/v1`) exposes prompts to the coordinator in
    transit.** `POST /v1/chat/completions` exists so an unmodified official OpenAI
    client works, which means the coordinator relays plaintext prompts to the worker
    over the same encrypted session the SDK would open. Prompts and completions are
    never logged, stored, or persisted (canary-tested), and the tier is always
    standard (`confidentiality: "none"`), but the platform does see them on the
    way through. End-to-end encryption requires SDK mode. Gateway mode is not a
    substitute for the confidential tier and rejects a `confidential` parameter.
15. **Gateway mode implements chat completions only.** `tools` / `tool_choice`,
    `response_format` (JSON mode), `logprobs`, `seed`, sampling penalties,
    `n > 1` and embeddings return an explicit `400 unsupported_parameter`; they
    are not silently dropped. These are Section C item 11 work.
16. **Gateway streaming errors after the first token are in-band.** Headers are
    already sent when a worker dies mid-stream, so the failure is delivered as an
    SSE `error` event. Such a job is not acked and therefore never settles; it
    expires and the hold is released.

## Result verification is probabilistic — read this before trusting a result

Verification of inferenced output is **probabilistic, not a proof**. What the
design does:

- a deterministic sample of jobs (default `TCDN_AI_VERIFICATION_SAMPLE_PCT=5`,
  bucket-based on the job id) is chosen for redundant execution;
- the sampled job is re-executed on a *different* operator, and the transcript
  commitments are compared (majority wins, disagreeing node loses reputation
  and can be suspended);
- the coordinator records the decision, the faulty node ids and the transcript
  root in the audit log.

What it does **not** do:

- it cannot detect a wrong answer that *every* sampled executor agrees on, or a
  model that is systematically biased rather than tampered with;
- it says nothing about the 95 % of jobs that are never sampled — those are
  trusted as reported;
- the **second execution is dispatched automatically in gateway mode** and
  cannot be dispatched in SDK mode, where the coordinator never holds the
  plaintext (gap 4). In gateway mode a sampled job is re-run on another operator
  and the two receipts are compared as soon as both arrive; in SDK mode the
  sample is recorded as `not_dispatched_sdk_mode` and nothing is compared, so
  the guarantee does not exist for SDK jobs at all;
- the sample is predictable to the node: it is bucketed on `sha256(salt + job_id)`
  with a salt that ships in the code, and it is decided at placement, before any
  commitment. A worker that wants to avoid the check can compute which of its
  jobs will be sampled;
- the comparison is exact-hash with two executions, so a disagreement produces
  `inconclusive` and penalises nobody. It detects a *deterministic* divergence
  (a substituted model or binary), not a subtly wrong answer, and the tolerance
  registry that would score near-misses is still a placeholder.

Nothing in this repository should be read as claiming "verified inference" for
unsampled jobs, or as a substitute for an independent evaluation of a model's
output quality.

## Tier and visibility disclosure

| Tier | State | What the operator can see | Where it is available |
|---|---|---|---|
| **Standard** | Available | In **SDK mode** the prompt and completion are encrypted end-to-end (X25519 + HKDF + AES-256-GCM) and the coordinator never sees either. In **gateway mode** (`/v1/chat/completions`) the coordinator relays the plaintext prompt and completion in transit, never logging or storing them. | Native API (`/api/ai/v1`) and the OpenAI-compatible surface |
| **Confidential** | **Not available in this build** | Nothing — a confidential job may only run on a node with a live, verified TEE attestation, and no real attestation verifier is shipped (gap 3) | Interfaces only; the SDK exposes `confidential=True` and the coordinator will refuse placement with no eligible attested node (`no_eligible_worker`) |

So: **standard is explicitly non-confidential.** Anything that must not be
visible to the platform needs end-to-end encryption, and this build cannot serve
that tier on real hardware. Gateway mode is standard-tier only and rejects a
`confidential` parameter rather than silently downgrading. Track these claims
against `API.md` §"OpenAI-compatible surface" and the `tcdn` object on every
gateway response (`mode`, `prompt_visibility`, `confidentiality`).

## Things that are real, not simulated

- Ed25519 tickets, receipts, manifests and verdict documents; X25519 per-request
  key agreement; HKDF-SHA-256; AES-256-GCM; TLS 1.3-only client contexts.
- Integer micro-credit ledger with holds, idempotency and exact 70/10/20 splits.
- Signed payment webhooks with idempotent replay handling.
- Shard integrity: per-shard SHA-256, Merkle proofs, corrupt-peer banning, resume,
  quota and path-traversal defences.
- Confidential placement filter with no downgrade path, plus worker-side refusal.
- Import-time production config gates that refuse to boot on insecure settings.
