# TCDN-AI — security

Scope: `backend/app/ai/`, `backend/tcdn_ai_sdk/`, the `ai_*` schema, and the worker
deployment. The CDN/auth subsystem is referenced only at its integration points.

Companion documents: `THREAT_MODEL.md` (STRIDE), `LIMITATIONS.md` (what is simulated
or absent), `RUNBOOK.md` (operations).

## 1. What is claimed, and what is not

| Claim | Evidence |
|---|---|
| Prompts/outputs never reach the coordinator | The placement API has no message field (`PlacementRequest`); tested by `test_ai_service.py::TestEncryptedJob::test_coordinator_never_receives_or_stores_prompt` and `test_ai_sdk_e2e.py::TestSDKEndToEnd::test_coordinator_never_sees_the_prompt` |
| Prompts/outputs are never persisted or logged by the worker | Worker has no persistence path; log canary tests (`TestLogHygiene`) |
| Standard tier is **not** confidential | `confidentiality: "none"` in registry, ticket, placement response, SDK `tcdn` namespace; warning text in every placement |
| Confidential tier requires attestation, never downgrades | Scheduler filter + worker-side refusal (`test_confidential_never_downgrades`, `test_worker_refuses_confidential_without_attestation`) |

**Not claimed:** protection from a compromised host OS (standard tier), real hardware
attestation in this build (adapters fail closed; simulated verdicts are labelled and
rejected in production), direct-to-VRAM loading (the GPU sink fails closed), or
mathematical correctness of model output (sampling is probabilistic).

## 2. Cryptography

| Purpose | Algorithm | Implementation | Notes |
|---|---|---|---|
| Signatures | Ed25519 | `cryptography` 42–46 | Domain-separated payloads |
| Key agreement | X25519, ephemeral per request | `cryptography` | All-zero shared secrets rejected |
| KDF | HKDF-SHA-256 | `cryptography` | Distinct `info` per direction/purpose |
| AEAD | AES-256-GCM | `cryptography` | Fresh 96-bit OS-random nonce per message |
| Hashing | SHA-256 | `hashlib` | Shard integrity, manifests, output hashes |
| API keys | HMAC-SHA-256 + server pepper | `hmac`/`hashlib` | Raw key shown once, never stored |
| Password-class compares | `hmac.compare_digest` | stdlib | Used for every secret/MAC comparison |

**Why AES-256-GCM rather than XChaCha20-Poly1305:** `cryptography` does not ship
XChaCha20, and adding libsodium would add a second native dependency for no security
gain at our message counts. Nonces are random per message and each job key is
single-use, so the 2^32-message collision bound is never approached.

**Domain separation.** Every signature covers `domain || 0x00 || canonical_json`.
Domains: `tcdn-ai/ticket/v1`, `tcdn-ai/receipt/v1`, `tcdn-ai/ack/v1`,
`tcdn-ai/node-register/v1`, `tcdn-ai/heartbeat/v1`, `tcdn-ai/session-key/v1`,
`tcdn-ai/attestation-evidence/v1`, `tcdn-ai/attestation-verdict/v1`,
`tcdn-ai/manifest/v1`, `tcdn-ai/transfer-receipt/v1`, `tcdn-ai/keys/v1`,
`tcdn-ai/revocations/v1`, `tcdn-ai/burn-audit/v1`.

**App-layer encryption sits inside TLS 1.3.** The SDK constructs its SSL context with
`minimum_version=TLSv1_3`, `check_hostname=True`, `VERIFY_REQUIRED`, compression
disabled (`test_ai_sdk_e2e.py::test_sdk_tls_context_is_tls13_only`). Plain HTTP is
accepted only for loopback development addresses
(`test_client_rejects_plaintext_remote_base_url`).

**Zeroisation** is best-effort (`crypto.zeroise`) and is described as such: CPython
may retain copies of immutable strings, so the docs never claim cryptographic
erasure.

## 3. Key inventory and rotation

| Key | Holder | Storage | Rotation | Test |
|---|---|---|---|---|
| Coordinator signing (Ed25519) | Coordinator | `FileSecretStore` (0600) or injected KMS JSON; production refuses auto-generation | `KeyRing.rotate()`; retired keys verify for `TCDN_AI_KEY_OVERLAP_SECONDS` (3600) but cannot sign | `TestTickets::test_rotation_overlap_then_expiry`, `TestSecretHandling::test_keyring_saved_as_0600` |
| Coordinator revocation list | Coordinator | Signed document served at `/api/ai/v1/keys/coordinator` | Regenerated on every request from current state | `TestTickets::test_revoked_key_is_rejected` |
| Worker identity (Ed25519) | Worker | 0600 identity file | On re-registration/compromise | `TestReceipts::test_signed_receipt_verifies` |
| Worker session (X25519) | Worker | Same 0600 file; rotated hourly | `WorkerIdentity.rotate_session()`, published signed by identity key | ticket binds `worker_public_key`; `TestEncryptedJob` |
| Per-request ephemeral X25519 | SDK | Process memory, discarded | Single use | `TestCrypto::test_hkdf_is_deterministic_and_direction_separated` |
| API key pepper | Coordinator secret store | `TCDN_AI_API_KEY_PEPPER` (+ version id) | Versioned; keys carry `pepper_id` for re-hash on next use | `TestCrypto::test_api_key_hashing_uses_pepper` |
| Seeder/peer identity | P2P node | Node operator | On compromise | `TestPayouts::test_requires_cosigned_ticketed_downloader` |
| Ledger/receipt signing | Coordinator | Same keyring as coordinator signing (separate domain) | Same schedule | `TestTickets`, ledger tests |
| Payment webhook secret | Payment provider | `TCDN_AI_PAYSTACK_SECRET_KEY` | Provider dashboard | `TestPaystackProvider` |

Revoking the last active signing key is refused (`test_cannot_revoke_last_signing_key`),
so an operator cannot accidentally lock the coordinator out of issuing tickets.

## 4. OWASP Top 10 (2021)

| ID | Where it applies | Control implemented | Test |
|---|---|---|---|
| **A01** Broken access control | Every AI endpoint; job/usage/ack reads | Deny-by-default auth dependency; object-level ownership checks (another account's job is a 404, not a 403); worker endpoints require a signature from the assigned node's identity key; admin uses the existing admin JWT, never an AI key | `TestAccountIsolation::test_usage_is_scoped_to_the_key_owner`, `test_ack_unknown_job_is_404_not_403`, `test_api_key_cannot_reach_admin`, `TestEncryptedJob::test_receipt_from_unassigned_node_rejected` |
| **A02** Cryptographic failures | Transport, tickets, receipts, keys, API keys | Ed25519/X25519/HKDF/AES-256-GCM; TLS 1.3 only; HMAC-SHA-256+pepper for API keys; 0600 key files; no secrets in logs | `TestCrypto::test_aead_roundtrip_and_tamper`, `test_sdk_tls_context_is_tls13_only`, `test_keyring_file_must_be_0600`, `TestLogHygiene` |
| **A03** Injection | SQL, model ids, shard paths, subprocess | Parameterised SQL only (`store.py`); strict pydantic models with `extra="forbid"`; model-name regex allowlist; no shell/subprocess anywhere in the AI module; shard filenames derived from a validated integer | `TestSSRFAndValidation::test_model_name_injection_rejected`, `TestShards::test_disk_sink_rejects_path_traversal`, `TestStaticSafety::test_no_interpolated_sql`, `TestWebhookInputHardening`, `TestPeerPayloadHardening::test_origin_url_cannot_carry_path_traversal` |
| **A04** Insecure design | Placement, money, tiers | Written threat model; holds before job creation; exact split arithmetic; confidential tier has no downgrade path and is refused on both sides; deterministic verification sampling | `TestScheduler::test_confidential_never_downgrades`, `TestLedger::test_concurrent_holds_cannot_overdraw`, `test_split_always_sums_exactly`, `test_confidential_job_runs_only_on_attested_node`, `test_confidential_refused_without_attestation`, `test_worker_refuses_confidential_without_attestation` |
| **A05** Security misconfiguration | Settings, worker startup, containers | Production startup validation rejects simulated attestation, dev backend, fake payments, missing pepper/keyring, TLS off, burn without legal ack; secure defaults elsewhere | `TestProductionGates::test_insecure_production_configs_are_rejected`, `TestAttestation::test_simulated_verdict_rejected_in_production` |
| **A06** Vulnerable/outdated components | Dependencies, images | `cryptography` pinned to a maintained range; repo CI already runs gitleaks + pip-audit/npm-audit against a committed baseline; `dependency-audit.yml` now also emits a CycloneDX SBOM of the shipped backend environment as a build artifact | Repo-level workflows (`.github/workflows/security.yml`, `.github/workflows/dependency-audit.yml`), not an AI test |
| **A07** Identification/authentication failures | API keys, worker identity, node registration | 256-bit keys, HMAC-hashed at rest, constant-time compare, per-key + per-IP rate limits, key revocation, scopes (`inference`/`worker`), signed node registration (proof of possession) | `TestAuthAndCatalogue::test_models_requires_api_key`, `TestRateLimit::test_api_key_bucket_enforced`, `test_revoked_key_is_rejected` |
| **A08** Software/data integrity failures | Model weights, receipts, tickets, manifests | Signed manifests + per-shard SHA-256 + Merkle proofs; safetensors-only allowlist (pickle/pt refused); signed receipts verified against the assigned node; no unsafe deserialisation anywhere in the AI module | `TestShards::test_manifest_merkle_and_signature`, `test_unsafe_model_format_rejected`, `TestReceipts::test_tampered_receipt_rejected` |
| **A09** Logging/monitoring failures | Audit log, application logs | Append-only `ai_audit_log`; no prompt/output/API-key logging (canary-tested); stable error codes; alert hooks are a documented **gap** | `TestLogHygiene::test_prompt_and_api_key_never_logged`, audit assertions in service tests |
| **A10** SSRF | Manifest origins / outbound fetches | `ssrf.py`: https-only, explicit host allowlist (empty = deny all), DNS resolved once and pinned, private/loopback/link-local/CGNAT/metadata blocked, IPv4-mapped IPv6 unwrapped; the shard origin fetcher reuses it, never follows a redirect to another host, and caps the response at the declared shard size | `TestSSRFAndValidation::test_empty_allowlist_blocks_everything`, `test_loopback_and_metadata_blocked`, `test_unlisted_host_blocked`, `TestOriginFallback::test_origin_must_be_https`, `TestOriginFallback::test_origin_host_must_be_allowlisted`, `TestOriginFallback::test_redirect_to_another_host_is_refused`, `TestOriginFallback::test_oversized_origin_response_is_refused` |

## 5. OWASP API Security Top 10 (2023)

| ID | Control | Test |
|---|---|---|
| **API1** BOLA | Ownership check on job/usage/ack; other accounts get 404; receipts must come from the assigned node | `test_usage_is_scoped_to_the_key_owner`, `test_ack_unknown_job_is_404_not_403`, `test_receipt_from_unassigned_node_rejected` |
| **API2** Broken authentication | HMAC-hashed API keys, scopes, revocation, signed worker requests | `test_models_requires_api_key`, `test_revoked_key_is_rejected` |
| **API3** Broken object property level authorisation | Strict models (`extra="forbid"`); placement carries no prompt field; response `tcdn` namespace is explicit | `test_prompt_content_is_not_part_of_placement`, `test_tokens_are_bounded` |
| **API4** Unrestricted resource consumption | Request-size cap, max messages/tokens, per-key concurrency setting, rate limits, credit holds | `test_body_size_limit` (413, not a retryable 429), `test_token_limit_enforced`, `TestRateLimit` |
| **API5** Broken function level authorisation | `inference` vs `worker` scopes; admin on a separate prefix with JWT auth | `test_api_key_cannot_reach_admin` |
| **API6** Unrestricted access to sensitive business flows | Holds + idempotency for money; `Idempotency-Key` replay on placements; signed/idempotent payment webhooks; signup credit gated on a signed email-verification link (auto-verified refused in production), one grant per canonical identity, rolling IP/device/email-domain caps, a daily grant ceiling and a monthly budget, disposable inboxes blocked, every grant and refusal audited; customer credits are inference-only (the ledger refuses any non-settlement debit); admin refunds are idempotent ledger entries | `TestLedger::test_settle_is_idempotent`, `TestFakeProvider::test_replayed_webhook_is_idempotent`, `TestSignupCredits::test_per_ip_cap`, `TestSignupCredits::test_unverified_email_gets_no_free_credit`, `TestSignupCredits::test_monthly_budget_caps_total_free_credit`, `TestFarmingAttempts::test_many_addresses_from_one_network_stop_at_the_ip_cap`, `TestFarmingAttempts::test_plus_addressing_cannot_claim_a_second_grant`, `TestFarmingAttempts::test_disposable_inbox_is_refused`, `TestFarmingAttempts::test_one_device_cannot_farm_across_networks`, `TestFarmingAttempts::test_daily_cap_bounds_the_rate_and_resets_the_next_day`, `TestFarmingAttempts::test_production_refuses_an_auto_verified_address`, `TestSignupCreditIsNotWithdrawable::test_the_ledger_refuses_a_cash_out_debit`, `TestPlacementReplay::test_same_key_replays_the_same_job_and_one_hold`, `TestPlacementReplay::test_concurrent_retry_cannot_create_a_second_job`, `TestAdminRefund::test_a_retried_refund_does_not_pay_twice` |
| **API7** SSRF | As A10 above | `TestSSRFAndValidation` |
| **API8** Security misconfiguration | As A05 above | `TestProductionGates` |
| **API9** Improper inventory management | Versioned native `/api/ai/v1` plus the OpenAI-compatible root `/v1` surface (both flag-gated, both documented); models/prices inventory in `ai_models`; API documented in `API.md` | `test_models_lists_prices`, `test_models_is_an_openai_list`, `test_flag_off_hides_openai_routes` |
| **API10** Unsafe consumption of third-party APIs | Payment webhook HMAC verified over the raw body; only `charge.success`/USD accepted; unknown events rejected | `TestPaystackProvider` |

## 6. OWASP Top 10 for LLM Applications (2025)

| ID | Where it applies | Control | Test |
|---|---|---|---|
| **LLM01** Prompt injection | Prompts are untrusted input to the model; the platform never acts on model output | No tool execution, no shell, no code execution from completions; output is data returned to the caller; coordinator makes no control decisions from output | `test_no_dynamic_execution` (AST scan for eval/exec/pickle) |
| **LLM02** Sensitive information disclosure | Prompts/outputs | Never logged/persisted by coordinator or worker; tenant isolation per job; buffers zeroised best-effort. **Gateway mode is the exception that is disclosed:** the coordinator relays the prompt in transit (`tcdn.prompt_visibility="gateway"`), still never logging or storing it, and the tier is always standard | `TestLogHygiene`, `test_coordinator_never_receives_or_stores_prompt`, `test_prompt_is_never_logged_or_persisted` |
| **LLM03** Supply chain | Model weights, manifests | Coordinator-signed manifest with per-shard hashes and Merkle root; model pinned by manifest hash on the job/receipt | `TestShards::test_manifest_merkle_and_signature`, `test_manifest_mismatch_rejected` |
| **LLM04** Data and model poisoning | Model artefacts | Manifest signature required; corrupt shards rejected and peer banned; unsafe formats refused | `test_corrupt_peer_banned_and_good_peer_used`, `test_all_corrupt_raises_and_writes_nothing` |
| **LLM05** Improper output handling | SDK consumers | SDK returns text/dataclasses only — never HTML-rendered or executed by the SDK; consumers must treat it as untrusted (documented in `SDK.md`) | `test_no_dynamic_execution` |
| **LLM06** Excessive agency | Worker | Worker has no tools, no filesystem actions from model output, no network calls driven by completions | Code review + `LIMITATIONS.md` |
| **LLM07** System prompt leakage | Worker | System messages are encrypted end-to-end and never logged; no debug dump path | `TestLogHygiene` |
| **LLM08** Vector/embedding weaknesses | N/A | No RAG/vector store in this build | — |
| **LLM09** Misinformation | Product | The platform makes no correctness claim; sampling is probabilistic (`LIMITATIONS.md`) | `TestVerification` |
| **LLM10** Unbounded consumption | Placement/worker | `max_tokens` cap at API and ticket, per-message/aggregate char cap, timeouts, concurrency setting, credit hold caps worst-case spend | `test_token_limit_enforced`, `test_oversized_prompt_rejected`, `TestLedger::test_hold_cannot_exceed_available` |

## 7. Additional controls

- **Rate limiting:** per API key and per client IP token buckets (in-process). Global
  CDN limiter still applies. Multi-instance deployments multiply the effective rate —
  documented gap.
- **Replay protection:** ticket `jti` claimed in a seen-set until expiry; worker-side
  cache; attestation nonces single-use and short-lived; money idempotency keys unique
  in the DB.
- **Fraud controls:** seeder payout only for co-signed transfer receipts from
  Coordinator-ticketed downloaders; same-owner and duplicate claims flagged; per-pair
  byte cap; payout delay.
- **Tenant isolation:** one job = one session key = one prompt; no shared KV cache or
  batch context between tenants.
- **Data retention:** coordinator stores job metadata (model, token counts, hashes,
  cost). Prompt and output content are absent from every table and from `ai_audit_log`.

## 8. Gaps (tracked in `LIMITATIONS.md`)

1. Real attestation verifiers (SEV-SNP/TDX/NVIDIA) fail closed; no vendor report
   parsers are shipped, so the confidential tier is only exercisable with the
   simulated adapter in development.
2. Direct-to-VRAM sink is unavailable (no CUDA bindings); tmpfs `DiskSink` is the
   documented equivalent.
3. Rate limiting, replay caches and nonces are process-local.
4. Alerting hooks are not implemented (audit rows exist, no alert rules). SBOM
   generation lives in `.github/workflows/dependency-audit.yml`, not in this
   module's Python code.
5. Admin endpoints share the backend listener (separate auth, not separate port).
6. Redundant-execution orchestration (actually launching the second worker) is not
   automated; comparison/tiebreak/penalty logic is implemented and tested.
