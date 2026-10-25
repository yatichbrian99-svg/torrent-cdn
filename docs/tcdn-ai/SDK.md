# TCDN-AI Python SDK

`backend/tcdn_ai_sdk` — OpenAI-shaped client that verifies the coordinator's
tickets, encrypts each request to the chosen worker, and decrypts the streamed
result. Python 3.11+, `httpx` + `cryptography`.

## Install / import

The package lives inside the backend repo today:

```python
import sys
sys.path.insert(0, "/path/to/torrent-cdn/backend")
from tcdn_ai_sdk import Client
```

(It has no dependency on the backend package, so it can be moved to its own
distribution without code changes.)

## Basic use

```python
client = Client(api_key="tcdn_ai_…", base_url="https://api.torrentcdn.store")
reply = client.chat.completions.create(
    model="llama-3.1-8b-instruct",
    messages=[
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "What is HKDF?"},
    ],
    max_tokens=256,
    temperature=0.2,
)
print(reply.choices[0].message.content)
print(reply.usage.total_tokens)
print(reply.tcdn)   # job_id, node_id, tier, confidentiality, notice, attestation_ref
```

API key defaults to `TCDN_AI_API_KEY`; it is never logged or echoed.

## Streaming

```python
for chunk in client.chat.completions.create(..., stream=True):
    print(chunk.choices[0].delta.content or "", end="")
```

The final chunk carries `finish_reason="stop"` and `usage`.

## Confidential tier

```python
client = Client(
    api_key="…",
    base_url="https://api.torrentcdn.store",
    coordinator_public_keys={"<key_id>": "<base64url Ed25519 public key>"},
)
reply = client.chat.completions.create(..., confidential=True)
```

- Without pinned keys the client refuses a confidential request: fetching the key
  bundle from the coordinator and trusting it (TOFU) cannot prove attestation.
- The response's `tcdn.confidentiality` is `"attested-tee"` only when the
  coordinator placed the job on an attested worker.
- There is no flag to skip ticket verification, ignore TLS, or downgrade a tier.

## Retries and failures

| Situation | Behaviour |
|---|---|
| Coordinator 5xx / network error | Retried with exponential backoff + jitter (`max_retries`) |
| 429 / 402 / 401 | Raised immediately (`RateLimitError`, `InsufficientCreditsError`, `AuthenticationError`) |
| Worker fails before any token | Job is re-placed on a new worker (up to `max_retries`) |
| Worker fails after output started | `PartialResultError` with `.partial_text` and `.job_id`; the coordinator bills verified tokens only |
| Ticket signature/expiry/tier mismatch | `TicketVerificationError`; nothing is sent to the worker |

## Shape compatibility

`ChatCompletion`, `ChatCompletionChunk`, `Choice`, `Message`, `Delta`, `Usage`
mirror OpenAI field names; `model_dump()` returns plain dicts. Extra data is
namespaced under `tcdn` and is optional for consumers.

## Security properties

- TLS 1.3 minimum, certificate validation mandatory, no insecure flag.
- Fresh X25519 keypair per request; HKDF-SHA-256 with per-job context; AES-256-GCM.
- The prompt is encrypted to the worker's session key **before** the request leaves
  the process; the coordinator only ever sees model/token/price metadata.
- Output/SDK consumers must treat model output as untrusted data (OWASP LLM05):
  the SDK never renders, evaluates or executes it.

## Limits

- Only the Python SDK is implemented; see `SDK-PORT-SPEC.md` for the wire format.
- Receipt signature verification happens at the coordinator, not in the SDK (the
  SDK does not receive the worker's identity key); the SDK verifies the ticket and
  its own ack hash instead.
- No async client yet; the HTTP calls are synchronous (`httpx.Client`).
