# TCDN-AI — threat model

Method: STRIDE per component and per trust boundary. Scope: the AI module only
(`backend/app/ai`, `backend/tcdn_ai_sdk`). The existing CDN/auth subsystem is modelled only where
the AI module crosses it.

## 1. Assets

| Asset | Why it matters | Where it lives |
|---|---|---|
| Developer prompts and completions | The only confidential data in the system | In flight: SDK→worker ciphertext. At rest: **nowhere** (invariant) |
| API keys | Billing identity | `ai_api_keys.key_hash` (HMAC + pepper) |
| Coordinator signing key | Forges tickets/verdicts/manifests → full compromise | Secret store / file 0600 (dev) |
| Worker identity keys | Forges receipts → steals payouts | Worker host |
| Per-container keys (confidential) | Binds results to an attested environment | Inside the attested guest |
| Ledger integrity | Money | `ai_ledger_entries` (double entry), `ai_holds` |
| Model weights | Public, but integrity is load-bearing | P2P shards, signed manifest |
| Seeder accounting | Payout fraud | `ai_shard_transfer_receipts` |

## 2. Trust boundaries

```
 TB1                TB2                    TB3                TB4
[S Developer SDK] -- HTTPS/TLS1.3 --> [Coordinator] <-- TLS --> [Worker agent]
        |                                   ^                       |
        |            TB5                    |                       |
        +--- TLS1.3 to worker --------------+                       |
                                                                    |
                                              TB6                   |
                                     [Host OS] --- [Attested guest]
                                              TB7
                                     [Worker] <-> [P2P seeder swarm]
```

| Boundary | What crosses | Trust stance |
|---|---|---|
| TB1 SDK→Coordinator | model id, token budget, price cap, `confidential` flag, API key. **No prompt content.** | Coordinator is trusted for metadata, not for plaintext |
| TB2 SDK→Worker | ticket, ciphertext (X25519+HKDF+AES-256-GCM inside TLS 1.3) | Worker is untrusted for confidentiality in the standard tier (`confidentiality: "none"`); trusted for execution only insofar as receipts + sampling show |
| TB3 Worker→Coordinator | signed receipts, heartbeats, attestation evidence | Coordinator authenticates worker identity key; never trusts counts alone |
| TB4 Coordinator→Worker | tickets, jobs | Worker verifies coordinator signature + expiry + single use |
| TB5 Coordinator→Seeder | manifests, download tickets | Manifest signature required; shard hash required |
| TB6 Host OS→guest | nothing, in the confidential tier | CoCo/Kata microVM; host must not read guest memory |
| TB7 P2P swarm | shards | Untrusted: every shard verified against signed manifest |

## 3. STRIDE per component

### 3.1 Coordinator

| Threat | Class | Control | Test |
|---|---|---|---|
| Forged ticket grants free inference | S | Ed25519 ticket signature over domain-separated canonical payload; key id checked against registry | `test_ai_core.py::TestTickets::test_forged_ticket_is_rejected` |
| Replayed ticket reused for a second job | R | `jti` in a seen-set until expiry; job ticket hash stored | `test_ai_core.py::TestTickets::test_replay_is_rejected` |
| Stolen API key reads another account's usage | I/E | HMAC-hashed keys + object-level ownership check on every read | `test_ai_api.py::TestAccountIsolation::test_usage_is_scoped_to_the_key_owner` |
| Double spend of credits by concurrent placements | T | Holds placed in one transaction; balance minus open holds; idempotency keys | `test_ai_core.py::TestLedger::test_concurrent_holds_cannot_overdraw` |
| Prompt content leaked into logs/metrics | I | No prompt field exists on any coordinator model; redaction filter; canary test | `test_ai_service.py::TestLogHygiene::test_prompt_and_api_key_never_logged` |
| Injected SQL via model id / node id | T | Parameterised queries only; enum/allowlist validation for models | `test_ai_components.py::TestSSRFAndValidation::test_model_name_injection_rejected` |
| SSRF via manifest origin URL | I | Origin URL allowlist, private/link-local/metadata ranges blocked, DNS pinned | `test_ai_components.py::TestSSRFAndValidation::test_loopback_and_metadata_blocked` |
| Unbounded token/request budget → bill shock / DoS | D | Bounded models (`max_tokens`, `max_messages`, body size), per-key rate limit, concurrency cap | `test_ai_api.py::TestPlacement::test_token_limit_enforced` |
| Admin action with an AI API key | E | Admin surface uses the existing JWT admin dependency, not API-key auth | `test_ai_api.py::TestAccountIsolation::test_api_key_cannot_reach_admin` |
| Revoked node keeps earning | R | Revocation list checked on ticket issue and receipt verification | `test_ai_core.py::TestTickets::test_revoked_key_is_rejected` |

### 3.2 Worker agent

| Threat | Class | Control | Test |
|---|---|---|---|
| Malicious coordinator sends a job to a non-attested worker | S | Worker independently checks its own attestation state before accepting a `confidential` job | `test_ai_service.py::TestConfidentialTier::test_worker_refuses_confidential_without_attestation` |
| Host operator reads prompts (standard tier) | I | **Not prevented** — labelled `confidentiality: "none"` in registry, tickets, SDK response | `test_ai_api.py::TestPlacement::test_placement_holds_credits_and_returns_ticket` |
| Host operator reads prompts (confidential tier) | I | Only attested CC-capable hardware; prompts decrypted in guest; host memory excluded by CoCo | simulated only — see `LIMITATIONS.md` |
| Receipt forged by another node | S | Receipt signed with node identity key, validated against the node assigned to the job | `test_ai_service.py::TestEncryptedJob::test_receipt_from_unassigned_node_rejected` |
| Output tampered in transit | T | Per-chunk AEAD with job id + sequence as AAD; a reordered or tampered chunk fails to decrypt | `test_ai_service.py::TestEncryptedJob::test_tampered_ciphertext_is_rejected` |
| Prompt written to disk/logs | I | Worker has no persistence path; log filter canary-tested; buffers zeroised after use | `test_ai_service.py::TestEncryptedJob::test_coordinator_never_receives_or_stores_prompt` |
| Job overruns worker capacity | D | Per-job timeout, max tokens, concurrency semaphore, graceful shutdown | `test_ai_service.py::TestLogHygiene::test_oversized_prompt_rejected` |

### 3.3 P2P weight streaming

| Threat | Class | Control | Test |
|---|---|---|---|
| Malicious seeder serves corrupt shard | T | Per-shard SHA-256 against signed manifest; reject + penalise + ban for the swarm | `test_ai_components.py::TestShards::test_corrupt_peer_banned_and_good_peer_used` |
| Manifest swapped for a different model | T | Manifest signed by coordinator; model referenced by manifest hash in tickets and jobs | `test_ai_components.py::TestShards::test_manifest_merkle_and_signature` |
| Path traversal through shard index/filename | T | Sink filename derived from validated integer index only | `test_ai_components.py::TestShards::test_disk_sink_rejects_path_traversal` |
| Model format that executes on load | E | `safetensors`-only allowlist; pickle/gguf-with-code rejected | `test_ai_components.py::TestShards::test_unsafe_model_format_rejected` |
| Disk exhaustion by a seeder | D | Quota check before write; LRU eviction | `test_ai_components.py::TestShards::test_disk_sink_enforces_quota` |
| Wash trading to farm seeder rewards | T | Transfer receipts co-signed by a Coordinator-ticketed downloader, per-pair caps, payout delay | `test_ai_core.py::TestPayouts::test_wash_trade_flagged_and_unpaid` |

### 3.4 Billing and credits

| Threat | Class | Control | Test |
|---|---|---|---|
| Replayed payment webhook credits twice | T | Event id idempotency + signature verification | `test_ai_billing.py::TestFakeProvider::test_replayed_webhook_is_idempotent` |
| Rounding drift makes the ledger unbalanced | T | Integer-only math; property test over random amounts | `test_ai_core.py::TestLedger::test_split_always_sums_exactly` |
| Free-credit farming | E | One grant per account, per-IP cap, platform-funded only | `test_ai_billing.py::TestSignupCredits::test_granted_once_per_account_from_platform_pool` |
| Overbilling on a disputed job | T | Bill `min(worker, client)` verified tokens; mismatched hashes → dispute, no auto-charge | `test_ai_service.py::TestEncryptedJob::test_disputed_ack_does_not_settle` |
| Burn module moves real value | E | Disabled by default; startup refuses enablement in prod without explicit legal ack; only dry-run strategy implemented | `test_ai_components.py::TestBurn::test_disabled_by_default` |

### 3.5 Confidential tier

| Threat | Class | Control | Test |
|---|---|---|---|
| Silent downgrade to a standard node | S | Placement filter requires a live attestation verdict; no fallback path exists | `test_ai_components.py::TestScheduler::test_confidential_requires_live_attestation` |
| Stale attestation reused | S | Freshness nonce + short verdict expiry + nonce consumed once | `test_ai_components.py::TestAttestation::test_nonce_cannot_be_reused` |
| Attestation key not bound to session key | S | Report data must contain SHA-256 of the worker session public key; mismatch rejects | `test_ai_components.py::TestAttestation::test_session_key_binding_enforced` |
| Simulated verdict accepted in production | S | `SimulatedVerifier` refused when `TCDN_AI_ENVIRONMENT=production`; verdict carries `simulated` and is rejected by policy in prod | `test_ai_components.py::TestAttestation::test_simulated_verdict_rejected_in_production` |

## 4. Out of scope / explicitly not claimed

- **No claim** that the standard tier protects prompts from the host operator.
- **No claim** of mathematical correctness of model output; receipts prove which code ran and what
  it claimed. Sampling gives probabilistic assurance only.
- **No claim** of diskless model loading: `GpuDirectSink` is unavailable without CUDA bindings and
  fails closed.
- **No claim** of cryptographic deletion: buffer zeroisation is best-effort in a managed runtime.
- Supply-chain compromise of PyPI packages, kernel/hypervisor compromise, and physical attacks on
  a host are outside this model and are covered (partially) by the repo's existing dependency
  audit and CI gates, not by TCDN-AI code.
