# TCDN-AI SDK port specification (v1)

Purpose: let a second-language client be written without reading the Python SDK.
Everything here is normative. Primitives must come from a vetted library; do not
reimplement them.

## 1. Coordinator calls

Base URL `B`. Requests use `Authorization: Bearer <api key>` and JSON.

1. `POST B/api/ai/v1/placements`

```json
{"model": "<id>", "max_tokens": 512, "stream": false,
 "confidential": false, "prompt_tokens_estimate": 42,
 "region": null, "latency_target_ms": null, "max_price_micro": null}
```

Response: `job_id`, `model`, `confidentiality` (`none` | `attested-tee`),
`confidentiality_notice`, `worker_endpoint`, `worker_public_key`,
`key_attestation_ref`, `expires_at`, `tier` (`standard` | `confidential`),
`ticket`, `coordinator_key_id`.

2. `GET B/api/ai/v1/keys/coordinator` (bootstrap only; confidential requires
   out-of-band pinned keys). Response: `{bundle, revocations}` where each is
   `{key_id, alg:"Ed25519", domain, payload, sig}`.

3. `POST B/api/ai/v1/jobs/{job_id}/ack`

```json
{"job_id": "…", "tokens_received": 120, "output_sha256": "<hex>", "acked_at": 1760000003}
```

4. Optional account: `GET /api/ai/v1/balance`, `GET /api/ai/v1/usage`.

## 2. Canonical JSON and signatures

`canonical_json(x)` = UTF-8 `json.dumps` with sorted keys, `,`/`:` separators,
no ASCII escaping.

`domain_message(domain, payload)` = `utf8(domain) || 0x00 || canonical_json(payload)`.

Signature verification: Ed25519 over `domain_message`, `sig` and keys are
base64url **without padding**.

Ticket domain: `tcdn-ai/ticket/v1`. Before using a ticket, verify:

- `document.domain == "tcdn-ai/ticket/v1"`, `alg == "Ed25519"`;
- `key_id` is pinned and not revoked;
- signature over `document.payload` with the pinned key;
- `payload.job_id == job_id` from the placement response;
- `payload.tier` is `confidential` iff the request was confidential;
- `now <= payload.expires_at + 5` and `payload.issued_at <= now + 5`;
- `payload.worker_public_key` is present.

## 3. Request encryption

```
ephemeral = X25519_keypair()
salt = 32 random bytes
shared = X25519(ephemeral.private, ticket.payload.worker_public_key)
c2w = HKDF-SHA256(shared, salt, info = "tcdn-ai/session/v1|<job_id>/c2w", 32)
w2c = HKDF-SHA256(shared, salt, info = "tcdn-ai/session/v1|<job_id>/w2c", 32)
ticket_hash = hex(SHA256(canonical_json(ticket_document)))
aad = "tcdn-ai/request/v1" || "|" || job_id || "|" || ticket_hash
plaintext = canonical_json({model, messages, max_tokens, temperature, top_p, stop, stream})
ciphertext = AES-256-GCM(c2w, random_96bit_nonce, plaintext, aad)   // nonce || ciphertext
```

Worker request body:

```json
{"job_id": "…", "ticket": {…}, "client_public_key": "<b64url>",
 "salt": "<b64url>", "ciphertext": "<b64url nonce||ct>"}
```

POST it to `worker_endpoint + "/v1/jobs"` over TLS 1.3, certificate validation on.

## 4. Response frames

Each frame: `kind:u8 || length:u32be || body`.

- `0x01` token: `body = nonce || AES-256-GCM(w2c, chunk_utf8,
  aad = "tcdn-ai/stream/v1|<job_id>|<sequence>")`; `sequence` starts at 0 and
  increments per token frame. A gap or a tag failure must abort the response.
- `0x02` final: `body = canonical_json(receipt_document)` (signed, no prompt or
  output text). Contains `payload.input_tokens`, `payload.output_tokens`,
  `payload.output_sha256`.

`output_sha256` for the ack = hex SHA-256 of the concatenated **plaintext** tokens
in order. `tokens_received` = `payload.output_tokens` (or the token count actually
consumed, whichever is smaller).

## 5. Ack and errors

Submit the ack after the final frame. If the connection fails before the first
token, re-place; after the first token, return a partial result to the caller and
do not silently retry.

Coordinator errors: `{"error": {"code", "message", "details"}}`. Map at least
`unauthenticated`, `insufficient_credits`, `limit_exceeded`, `rate_limited`,
`no_eligible_worker`, `no_attested_worker`.

## 6. Test vectors

`backend/tests/test_ai_sdk_e2e.py` and `test_ai_service.py` are executable
conformance tests: a port is compatible when it can complete a job against the
same coordinator/worker pair and produce the same ack hash. Known-answer values
are not hard-coded because every request uses fresh keys and nonces; compatibility
is behavioural, not byte-for-byte.
