# TCDN-AI API (v1)

Native base path: `/api/ai/v1`. A second, OpenAI-compatible surface lives at the
application root (`/v1/...`) for gateway mode — see below. All timestamps are UTC
epoch seconds. All money is integer micro-credits (1 credit = 1,000,000 micro;
100 credits = US$1).
The generated OpenAPI document is served by the backend at `/openapi.json`
(disabled in production builds as with the rest of the API).

## Authentication

| Surface | Credential |
|---|---|
| Developer endpoints | `Authorization: Bearer tcdn_ai_…` (or `X-API-Key`) |
| Worker receipt/heartbeat | Request body signed with the node's Ed25519 identity key |
| Worker register/attestation | API key with the `worker` scope **and** a signed body |
| Admin | Existing admin JWT (`users.is_admin`); AI API keys are rejected |
| Payment webhook | Provider HMAC signature over the raw body |

API keys are shown once. Only `HMAC-SHA-256(pepper, key)` is stored.

## Endpoints

### Catalogue

`GET /models` — allowlisted models with launch prices.

```json
[{
  "model_id": "llama-3.1-8b-instruct",
  "format": "safetensors",
  "manifest_hash": "…64 hex…",
  "input_micro_per_mtok": 3000000,
  "output_micro_per_mtok": 6000000,
  "confidential_input_micro_per_mtok": 3750000,
  "confidential_output_micro_per_mtok": 7500000,
  "launch_pricing_until": "2027-04-01"
}]
```

### Placement

`POST /placements`

```json
{"model": "llama-3.1-8b-instruct", "max_tokens": 512,
 "prompt_tokens_estimate": 40, "confidential": false,
 "max_price_micro": 100000, "region": "eu-west", "latency_target_ms": 300}
```

**No messages field exists.** Prompt content never reaches the coordinator.

Response:

```json
{
  "job_id": "8f…",
  "model": "llama-3.1-8b-instruct",
  "confidentiality": "none",
  "confidentiality_notice": "Standard tier (confidentiality: none). …",
  "worker_endpoint": "https://worker.example",
  "worker_public_key": "<X25519 base64url>",
  "key_attestation_ref": null,
  "expires_at": 1760000120,
  "tier": "standard",
  "ticket": {"key_id": "…", "alg": "Ed25519", "domain": "tcdn-ai/ticket/v1",
             "payload": {"job_id": "…", "worker_public_key": "…", "max_tokens": 512, …},
             "sig": "…"},
  "coordinator_key_id": "…"
}
```

Credit behaviour: a hold for the worst-case cost (`prompt estimate + max_tokens`)
is placed atomically. Insufficient balance → `402 insufficient_credits`.

### Receipt and acknowledgement

`POST /jobs/{job_id}/receipt` (worker; body signed, no API key)

```json
{"payload": {"job_id": "…", "node_id": "node_…", "account_id": "…",
             "model": "…", "manifest_hash": "…", "input_tokens": 40,
             "output_tokens": 120, "output_sha256": "…",
             "started_at": 1760000000, "finished_at": 1760000002},
 "key_id": "…", "alg": "Ed25519", "sig": "…"}
```

`POST /jobs/{job_id}/ack` (developer)

```json
{"job_id": "…", "tokens_received": 120, "output_sha256": "…", "acked_at": 1760000003}
```

Settlement (both endpoints return the same shape):

```json
{"job_id": "…", "status": "settled",
 "billable_input_tokens": 40, "billable_output_tokens": 120,
 "total_micro": 840, "compute_micro": 588, "seeder_micro": 84,
 "platform_micro": 168, "reason": null}
```

`status` is `running` while awaiting the other side, `settled` when hashes agree
within tolerance, `disputed` (nothing charged, review required) otherwise.

### Node registry

`POST /nodes/register` — API key (`worker` scope) + `SignedEnvelope` whose payload
is a `WorkerRegistrationPayload`; signature domain `tcdn-ai/node-register/v1`.
The server derives `node_id` from the identity key; a client cannot choose it.

`POST /nodes/heartbeat` — signed `HeartbeatPayload`, domain `tcdn-ai/heartbeat/v1`.

`POST /nodes/{node_id}/attestation-nonce` — API key; returns a single-use nonce.

`POST /attestations` — API key + signed evidence (`AttestationEvidencePayload`).
The `session_key_sha256` must equal `SHA-256(raw session X25519 public key)`.

`GET /keys/coordinator` — signed public key bundle and revocation list. Public;
SDKs pin these keys out of band for confidential workloads.

### Account

`GET /balance` → `{account_id, credits, micro_credits, held_micro, available_micro}`.

`GET /usage?limit=100` → jobs owned by the authenticated key, with token counts,
status and cost. Another account's jobs are never visible (404 on direct access).

### Billing

`POST /billing/webhook` — raw provider event with `X-Paystack-Signature`
(HMAC-SHA-512 over the raw body) or `X-TCDN-Signature` for the dev provider.
Credits are idempotent per provider reference.

### Admin

All require the existing admin JWT.

- `POST /admin/api-keys?account_id=<uuid>&scopes=inference,worker` — returns the
  raw key once.
- `POST /admin/verification?job_id=…` with body `{"node_a": "<hash>", "node_b": "<hash>"}`
  — records sampled execution hashes, resolves majority and penalises/suspends the
  faulty worker.

## OpenAI-compatible surface (`gateway mode`)

Mounted at the application root only when `ENABLE_TCDN_AI=true`, so an
unmodified OpenAI client works unchanged:

```python
from openai import OpenAI
client = OpenAI(api_key="tcdn_ai_…", base_url="https://api.torrentcdn.store/v1")
client.models.list()
client.chat.completions.create(model="llama-3.1-8b-instruct",
                               messages=[{"role": "user", "content": "hi"}])
```

| Method | Path | Notes |
|---|---|---|
| `GET` | `/v1/models` | OpenAI list shape; each entry also carries prices/licence under `tcdn` |
| `POST` | `/v1/chat/completions` | JSON or SSE (`stream: true`), same API key auth and rate limits |

**This is not the end-to-end-encrypted path, and it is labelled as such.** In
gateway mode the coordinator relays the request to the worker, so **the platform
gateway sees the prompt and the completion in transit**. It never logs, stores,
or persists them (canary tests in `tests/test_ai_openai_compat.py`), and the
tier is always **standard** (`confidentiality: "none"`). Responses carry
`tcdn.mode = "gateway"` and `tcdn.prompt_visibility = "gateway"`.

Use the TCDN-AI SDK (`chat.completions.create(..., confidential=True)`) for
end-to-end encryption and the confidential tier; gateway mode rejects a
`confidential` parameter instead of silently downgrading.

Supported request parameters: `model`, `messages` (string content, roles
`system`/`user`/`assistant`), `temperature`, `top_p`, `max_tokens` /
`max_completion_tokens`, `stop` (≤4), `stream`, `stream_options.include_usage`,
`n` (must be 1), `user` (accepted and discarded — never stored).

Everything else is rejected with `400 unsupported_parameter` and the offending
`param`, never ignored: `tools`, `tool_choice`, `response_format`, `logprobs`,
`top_logprobs`, `presence_penalty`, `frequency_penalty`, `logit_bias`, `seed`,
`modalities`, `audio`, `parallel_tool_calls`, `service_tier`, `metadata`,
`store`, `confidential`.

Errors on this surface use OpenAI's shape:

```json
{"error": {"message": "tools is not supported: tool calling is not enabled on this deployment",
           "type": "invalid_request_error", "param": "tools",
           "code": "unsupported_parameter"}}
```

Status mapping: validation 400, auth 401, insufficient credits 402, forbidden
403, unknown model 404, rate limit 429 (with `Retry-After`), no worker 503.

## Error format

```json
{"error": {"code": "insufficient_credits",
           "message": "insufficient credit balance for this job",
           "details": {"available_micro": 0, "required_micro": 630}}}
```

Stable codes: `unauthenticated`, `forbidden`, `not_found`, `invalid_request`,
`conflict`, `insufficient_credits`, `limit_exceeded`, `rate_limited`,
`body_too_large`, `no_eligible_worker`, `no_attested_worker`, `invalid_ticket`,
`replayed_ticket`, `expired_ticket`, `crypto_failure`, `aead_auth_failed`,
`bad_signature`, `invalid_receipt`, `attestation_failed`,
`attestation_verifier_unavailable`, `simulated_verdict_rejected`,
`worker_not_attested`, `unsupported_currency`, `burn_disabled`.

## SDK

```python
from tcdn_ai_sdk import Client

client = Client(api_key="tcdn_ai_…", base_url="https://api.torrentcdn.store")
completion = client.chat.completions.create(
    model="llama-3.1-8b-instruct",
    messages=[{"role": "user", "content": "Explain HKDF in one line."}],
)
print(completion.choices[0].message.content)
print(completion.tcdn["job_id"], completion.tcdn["confidentiality"])
```

Streaming:

```python
for chunk in client.chat.completions.create(..., stream=True):
    print(chunk.choices[0].delta.content or "", end="")
```

Confidential tier requires pinned coordinator keys:

```python
client = Client(..., coordinator_public_keys={"<key_id>": "<base64url>"})
client.chat.completions.create(..., confidential=True)
```

See `SDK.md` for full behaviour and `SDK-PORT-SPEC.md` for the wire protocol.
