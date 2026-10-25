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
   and tested with in-process peers. A production worker needs a transport adapter
   (libtorrent, a JS sidecar, or an HTTPS origin) — that adapter is not shipped.
3. **Confidential tier is not usable with real hardware in this build.** The
   interfaces, policy enforcement and key binding exist and are tested; real
   verdict-producing verifiers do not.
4. **Redundant execution is only partially automated.** Sampling decisions,
   comparison, majority tiebreak, reputation penalty and suspension are implemented
   and tested (`record_verification`, admin endpoint). Automatically launching the
   second execution on another worker is not wired into the placement flow.
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
9. **SQL coverage is unit-tested through the store protocol, not against a live
   Postgres.** The Postgres implementation uses parameterised SQL, advisory locks
   and unique idempotency keys; the automated suite runs `MemoryStore`. A live-DB
   integration job is the next step (the repo's existing CI has no database
   service).
10. **Only the Python SDK exists.** A TypeScript SDK is specified
    (`SDK-PORT-SPEC.md`) but not implemented, per the single-language rule.
11. **Worker heartbeat/rotation scheduling is not a daemon.** The launcher registers
    and serves; periodic heartbeats are trivial to add with a task, but a crashed
    heartbeat loop is not restarted by a supervisor in this build.
12. **Free-credit abuse controls are partial.** One grant per account and a per-IP
    cap exist; email/phone verification and device fingerprinting are delegated to
    the existing account system, which this module does not call.
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

## Things that are real, not simulated

- Ed25519 tickets, receipts, manifests and verdict documents; X25519 per-request
  key agreement; HKDF-SHA-256; AES-256-GCM; TLS 1.3-only client contexts.
- Integer micro-credit ledger with holds, idempotency and exact 70/10/20 splits.
- Signed payment webhooks with idempotent replay handling.
- Shard integrity: per-shard SHA-256, Merkle proofs, corrupt-peer banning, resume,
  quota and path-traversal defences.
- Confidential placement filter with no downgrade path, plus worker-side refusal.
- Import-time production config gates that refuse to boot on insecure settings.
