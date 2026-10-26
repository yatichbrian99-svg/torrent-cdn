# TCDN-AI — written contracts, one per trust boundary

Every boundary has: what crosses it, in what exact shape, who validates what,
what happens when it is violated, and the test that pins it. The test names are
resolved by `tests/test_ai_contracts.py` — one test class per boundary, and the
class docstring names the row it covers.

Boundary identifiers (TB1–TB7) come from `THREAT_MODEL.md` §2. The contracts are
deliberately narrower than the code: they describe the wire, not the
implementation, so a port (TypeScript SDK, Go worker) can be written against
this file alone.

| # | Boundary | Direction | Contract version | Pinned by |
|---|---|---|---|---|
| TB1 | SDK → Coordinator | outbound HTTP | `tcdn-ai/placement/v1` | `TestTB1SDKToCoordinator` |
| TB2 | SDK → Worker | HTTPS + AEAD frames | `tcdn-ai/session/v1` | `TestTB2SDKToWorker` |
| TB3 | Worker → Coordinator | signed envelopes | `tcdn-ai/receipt/v1`, `/heartbeat/v1`, `/attestation/v1` | `TestTB3WorkerToCoordinator` |
| TB4 | Coordinator → Worker | signed ticket | `tcdn-ai/ticket/v1` | `TestTB4CoordinatorToWorker` |
| TB5 | Coordinator → Seeder/registry | signed manifest | `tcdn-ai/manifest/v1` | `TestTB5CoordinatorToSeeder` |
| TB6 | Host OS → attested guest | nothing | — (hardware) | `TestTB6HostToGuest` |
| TB7 | Worker ↔ P2P swarm | shard bytes | `tcdn-ai/manifest/v1` + SHA-256 | `TestTB7WorkerToSwarm` |

---

## TB1 — SDK → Coordinator (placement)

**What crosses:** model id, `max_tokens`, `stream`, `confidential`,
`prompt_tokens_estimate`, optional `region`, `latency_target_ms`, `vram_gb`,
`max_price_micro`. **The prompt never crosses this boundary**, which is what
makes SDK mode end-to-end encrypted: the coordinator prices a job it cannot read.

**Shape:** `POST /api/ai/v1/placements` with `Authorization: Bearer tcdn_ai_…`,
optional `Idempotency-Key` (1–128 chars). The request model is strict
(`extra="forbid"`), so an unknown field — including a client that tries to send
`prompt` or `messages` — is a `422`, never ignored.

**Response:** `TicketResponse` = `job_id, model, confidentiality,
confidentiality_notice, worker_endpoint, worker_public_key,
key_attestation_ref, expires_at, tier, ticket, coordinator_key_id`.

**Validation:** API key hash + scope `inference`; body size
(`TCDN_AI_MAX_BODY_BYTES`, 413); per-key and per-IP token buckets (429);
`max_tokens` cap (400); model allowlist (404/403); price cap (400); credit hold
under an advisory lock (402).

**Replay:** the same `Idempotency-Key` returns the original response and does
**not** place a second job or take a second hold; the same key with a different
body is `409 idempotency_key_reused`; a concurrent retry while the first is in
flight is `409 idempotency_in_progress`; a failed placement releases the key.

**Violation:** never a 500. Every rejection is `{"error": {"code", "message",
"details"}}` with a stable code from `app/ai/errors.py`.

## TB2 — SDK → Worker (encrypted request and stream)

**What crosses:** `{job_id, ticket, client_public_key, salt, ciphertext}`.

```
shared   = X25519(client ephemeral private, ticket.worker_public_key)
c2w key  = HKDF-SHA-256(shared, salt, info = "tcdn-ai/session/v1|<job_id>/c2w")
w2c key  = … same, info suffix "/w2c"
request  = AES-256-GCM(c2w, nonce, canonical_json(chat request))
AAD      = b"tcdn-ai/request/v1|" + job_id + b"|" + ticket_hash
```

**Response frames:** `kind(1) || length(4, big-endian) || body`, `kind 0x01`
token (AEAD with AAD `b"tcdn-ai/stream/v1|<job_id>|<sequence>"`), `kind 0x02`
final (canonical JSON receipt, signed, **not** encrypted — it carries no
prompt or output text). `MAX_FRAME_BYTES` = 8 MiB; an unknown kind or an
oversized length is a protocol error, not a partial read.

**Validation:** the worker verifies the ticket (TB4) *before* decrypting;
the AEAD tag binds the job id, so a ciphertext from another job cannot be
replayed into this one; a key mismatch fails closed with no plaintext output.

## TB3 — Worker → Coordinator (receipts, heartbeats, attestation)

**What crosses:** a signed envelope `{payload, key_id, alg, sig}` over
`domain || 0x00 || canonical_json(payload)`.

* receipt: `job_id, node_id, input_tokens, output_tokens, manifest_hash,
  transcript_root, sig` — token counts are the worker's claim, checked against
  the manifest and the tolerance rule;
* heartbeat: `node_id, load, models, at`, signed by the node identity key;
* attestation evidence: `node_id, platform, nonce, session_key_sha256,
  report_b64, issued_at`, self-signed, and the `session_key_sha256` **must**
  match the session key the node registered (otherwise `key_binding_mismatch`).

**Validation:** signature by the *assigned* node's identity key (a receipt from
any other node is `403`), domain separation, single-use nonces, tolerance
(`TCDN_AI_RECEIPT_TOKEN_TOLERANCE_PCT`), and the settlement state machine
(one receipt per job; a duplicate is `409`).

## TB4 — Coordinator → Worker (ticket)

**What crosses:** `PlacementTicket` = `job_id, account_id, node_id, model,
confidentiality, worker_public_key, max_tokens, max_price_micro, tier, jti,
issued_at, expires_at`, signed over `tcdn-ai/ticket/v1`.

**Validation at the worker:** key id resolves in the pinned coordinator keyring
and is not revoked; domain is the ticket domain; `expires_at` is in the future
within `TCDN_AI_TICKET_SKEW_SECONDS`; `jti` is claimed once; job id, node id and
confidentiality bind to this worker. A standard ticket cannot be upgraded, and a
`confidential` ticket is refused by a worker that is not itself attested
(`worker_not_attested`) even if the coordinator is compromised.

## TB5 — Coordinator → Seeder / model registry (manifest)

**What crosses:** `ModelManifestPayload` = `model_id, name, format, version,
total_size, shards[{index, sha256, size}], merkle_root, created_at`, signed over
`tcdn-ai/manifest/v1`.

**Invariants:** `format` must be allowlisted (`safetensors` only — formats that
execute code on load are refused); shard indexes are contiguous from 0; shard
sizes sum to `total_size`; the Merkle root matches the shard hashes. A worker
records `manifest_hash` in every receipt, so a substituted manifest is visible
after the fact.

## TB6 — Host OS → attested guest

**What crosses:** nothing. The confidential tier's claim is that the host cannot
read guest memory, so the contract is an *absence*: no code path in this repo
reads guest memory, and the one component that would — `GpuDirectSink` — fails
closed at construction rather than silently falling back to disk. Real hardware
attestation is not shipped (see `LIMITATIONS.md` gap 3), so this contract is
enforced by refusal, not by a working TEE.

## TB7 — Worker ↔ P2P swarm (shards)

**What crosses:** raw shard bytes, from any peer, trusted by nobody.

**Validation:** the shard must hash to the SHA-256 in the signed manifest *and*
match the declared size; a short or long payload is a corrupt shard even when
its prefix is right. The sender is banned for the rest of the fetch and reported
through the penalty callback. The last resort is the HTTPS origin, which is
https-only, host-allowlisted (via `ssrf.py`), does not follow cross-host
redirects, and is capped at the declared size. The sink filename comes from the
integer index, so a manifest cannot smuggle a path separator.

---

## What is deliberately *not* a contract here

* **Model output.** There is no correctness guarantee. Sampling is probabilistic
  (`LIMITATIONS.md` §"Result verification is probabilistic").
* **Inference-engine HTTP.** `VLLMBackend`/`OllamaBackend` speak the engine's
  own OpenAI-compatible surface; that contract belongs to the engine, and the
  adapter is exercised against a real Ollama in the nightly job.
* **Payment provider.** Paystack's webhook schema is theirs; our side is the
  HMAC over the raw body plus the field validation in `payments.py`.
