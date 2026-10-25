# TCDN-AI — local development

Everything runs in the repo's existing Python stack. No GPU is required for the
dev path (the worker uses the labelled `echo` backend).

## 1. Coordinator

```bash
cd backend
python3 -m venv .venv                # if not already present
.venv/bin/pip install -r requirements.txt

export JWT_SECRET="this-is-a-32-char-secret-for-testing-only!!"
export TCDN_AI_ENVIRONMENT=development
export TCDN_AI_API_KEY_PEPPER="dev-pepper-change-me"
export TCDN_AI_PAYMENT_PROVIDER=fake

# Postgres + Redis only (compose file ships them)
docker compose -f ../deploy/tcdn-ai/docker-compose.dev.yml up -d postgres redis
export DATABASE_URL="postgresql://tcdn:tcdn@127.0.0.1:5432/tcdn"
export REDIS_URL="redis://127.0.0.1:6379/0"

.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

The AI tables are created at startup. In development the coordinator keyring is
generated in memory (keys do not survive a restart) unless you set
`TCDN_AI_COORDINATOR_KEY_FILE` to a 0600 file.

## 2. Provision a key and a model

You need an existing `users.id` (register through the normal app, or insert one).
Then:

```bash
cd backend
cp ../deploy/tcdn-ai/models.example.json /tmp/models.json
# replace every manifest_hash with the hash of a manifest you actually signed
.venv/bin/python scripts/ai_provision.py \
  --account-id <uuid> --scopes inference,worker --models-file /tmp/models.json
```

## 3. Worker (echo backend, loopback only)

```bash
cd backend
export TCDN_AI_COORDINATOR_URL=http://127.0.0.1:8000
export TCDN_AI_WORKER_API_KEY=tcdn_ai_…          # from step 2
export TCDN_AI_WORKER_MODELS='llama-3.1-8b-instruct=<manifest sha256>'
export TCDN_AI_BACKEND=echo
export TCDN_AI_IDENTITY_FILE=/tmp/tcdn-ai-worker.json

.venv/bin/python scripts/ai_worker.py \
  --host 127.0.0.1 --port 9100 --public-url http://127.0.0.1:9100 \
  --allow-plaintext-loopback
```

`echo` is a deterministic stub, labelled `dev-stub` in `/health`, and refused in
production. Real engines:

```bash
export TCDN_AI_BACKEND=vllm   # expects http://127.0.0.1:8000
export TCDN_AI_BACKEND=ollama # expects http://127.0.0.1:11434
```

## 4. SDK

```bash
cd backend
export TCDN_AI_API_KEY=tcdn_ai_…
.venv/bin/python - <<'PY'
from tcdn_ai_sdk import Client

client = Client(base_url="http://127.0.0.1:8000")
reply = client.chat.completions.create(
    model="llama-3.1-8b-instruct",
    messages=[{"role": "user", "content": "hello"}],
)
print(reply.choices[0].message.content)
print(reply.usage, reply.tcdn)
PY
```

For an end-to-end encrypted request the SDK talks to the worker endpoint returned
in the ticket; in this dev setup that is the loopback worker. The SDK refuses
plain HTTP to a non-loopback host.

### 4b. OpenAI-compatible gateway (no SDK, standard tier only)

```bash
pip install -r requirements-dev.txt   # official openai client, tests only
.venv/bin/python - <<'PY'
from openai import OpenAI

client = OpenAI(api_key="tcdn_ai_…", base_url="http://127.0.0.1:8000/v1")
print(client.models.list().data[0].id)
print(client.chat.completions.create(
    model="llama-3.1-8b-instruct",
    messages=[{"role": "user", "content": "hello"}],
).choices[0].message.content)
PY
```

Gateway mode relays the prompt through the coordinator, so it is **standard tier
only** and is labelled `tcdn.mode="gateway"` in every response. Use the SDK for
the confidential tier. `tools`, JSON mode, `logprobs`, `seed` and `n>1` return
`400 unsupported_parameter` rather than being ignored.

## 5. Tests and lint

```bash
cd backend
ENABLE_TCDN_AI=true .venv/bin/python -m pytest -q    # whole suite: 925 passed, 11 skipped
ENABLE_TCDN_AI=false .venv/bin/python -m pytest -q   # 879 passed, 57 skipped
ENABLE_TCDN_AI=true .venv/bin/python scripts/break_tests.py
.venv/bin/python -m ruff check app tests scripts
```

`ENABLE_TCDN_AI=true` matters: the host app decides at import time whether to
mount the AI routers, so a plain `pytest` run has no AI routes at all (and the
break-test harness reports baseline failures instead of proven controls). With
the flag off the AI HTTP-surface tests skip with an explicit reason; the AI unit
tests (crypto, tickets, ledger, shards, verification) still run in both states.

The AI suite mocks no cryptography: it exercises real Ed25519/X25519/HKDF/AES-GCM,
real signed tickets and receipts, an in-memory store, and an in-process worker.
The full backend suite (`pytest tests`) also covers the CDN.

## 6. Real engine + real email verification (Section C item 2)

Two things the mocked suite cannot prove are real inference and the real
email-verification flow. This brings both up locally (CPU is enough for a 0.5B
model) and runs the whole journey against them.

```bash
# 1. engine — 0.5B model, ~400 MB download
ollama serve &                                 # or your system service
ollama pull qwen2.5:0.5b
curl -s http://127.0.0.1:11434/api/version

# 2. email provider stand-in (SIMULATED transport, real application code)
cd backend
.venv/bin/python scripts/dev/fake_resend.py \
  --port 8765 --out /tmp/tcdn-ai-emails.jsonl &

# 3. coordinator with the provider redirected to the stand-in
#    RESEND_API_KEY must be present so the app does NOT auto-verify new
#    accounts; the value is a stand-in, not a credential.
RESEND_API_KEY="local-email-provider-standin-not-a-real-key" \
TCDN_FAKE_RESEND_URL="http://127.0.0.1:8765" \
PYTHONPATH=scripts/dev \
ENABLE_TCDN_AI=true TCDN_AI_ENVIRONMENT=development \
TCDN_AI_API_KEY_PEPPER=dev-pepper-change-me TCDN_AI_PAYMENT_PROVIDER=fake \
JWT_SECRET="this-is-a-32-char-secret-for-testing-only!!" \
DATABASE_URL=postgresql://tcdn:tcdn@127.0.0.1:5432/tcdn \
REDIS_URL=redis://127.0.0.1:6379/0 \
.venv/bin/python -m uvicorn app.main:app --port 8000

# 4. adapter tests against the live engine (skipped when TCDN_AI_ENGINE_URL is unset)
TCDN_AI_ENGINE_URL=http://127.0.0.1:11434 TCDN_AI_ENGINE_MODEL=qwen2.5:0.5b \
  .venv/bin/python -m pytest tests/test_ai_engine.py -q

# 5. the whole journey: real inference (Ollama) + real email verification
.venv/bin/python scripts/live_ai_journey.py \
  --base-url http://127.0.0.1:8000 \
  --database-url postgresql://tcdn:tcdn@127.0.0.1:5432/tcdn \
  --backend ollama --engine-url http://127.0.0.1:11434 \
  --model qwen2.5:0.5b --gpu-vram-gb 2 --gpu-model cpu-only \
  --email-stub-url http://127.0.0.1:8765 --email-out /tmp/tcdn-ai-emails.jsonl \
  --out ../docs/tcdn-ai/evidence/10-real-engine/journey.json
```

What is real and what is substituted:

| Piece | Status |
|---|---|
| Inference | **real** (Ollama, CPU, 0.5B model) |
| Streaming, usage counts, receipts, ledger | **real** |
| Account state machine | **real**: unverified until the emailed token is used |
| Email transport | **SIMULATED**: `scripts/dev/fake_resend.py` answers the exact Resend call and records the message; `scripts/dev/sitecustomize.py` redirects the SDK only when `TCDN_FAKE_RESEND_URL` is set |
| vLLM | adapter exercised against Ollama's OpenAI-compatible `/v1` (same wire protocol), **not** against vLLM itself |

The nightly/manual CI job runs exactly this (`.github/workflows/tcdn-ai-engine.yml`).
Without `--email-stub-url` the journey still runs, but it records
`email_verification.mode = "auto-verified"` and prints that the provider was
absent — it never claims a verification flow it did not perform.

## 7. Load test (placement only)

```bash
.venv/bin/python scripts/ai_load_test.py \
  --base-url http://127.0.0.1:8000 --api-key "$TCDN_AI_API_KEY" \
  --requests 200 --concurrency 20 --model llama-3.1-8b-instruct
```

It measures placement latency and error rate. It does **not** measure inference
throughput — that depends on the engine and GPU, and must be measured on the real
worker.
