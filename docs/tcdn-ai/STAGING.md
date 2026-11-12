# TCDN-AI staging on Render

A separate, disposable deployment of the feature branch so the engine can be
touched before it is merged. **Staging shares nothing with production.** No
database, no Redis, no env var, no domain, no Paystack live key.

| What | Where | Notes |
|---|---|---|
| Blueprint | `render.staging.yaml` | one web service + its own Postgres + its own Key Value |
| Branch | `fix/tcdn-ai-redis-replay-nonce` | auto-deploys on push to that branch |
| Secrets | Render dashboard, `sync: false` | listed in §2, generated in §3 |
| Owner steps | this file | §1–§6 |
| Status page | `GET /api/ai/v1/status` | public, read-only, no secrets |
| Smoke test | `backend/scripts/staging_smoke.py` | 8 steps, PASS/FAIL each |
| Worker | `backend/scripts/ai_worker.py` + Ollama | §5 |
| Rollback | §8 | delete the blueprint; production is untouched |

**Staging does not change the Level‑1 status in `PUSH_CHECKLIST.md`.** A
green smoke run here proves the deployment path works; it closes no checklist
row, because the rows ask for things (capacity numbers, SAST, a draft PR) that
a staging box cannot produce.

---

## 1. Create the blueprint

1. Render dashboard → **New → Blueprint**.
2. Pick the repository, branch **`fix/tcdn-ai-redis-replay-nonce`**, and set the
   blueprint path to **`render.staging.yaml`**.
3. Apply. Render creates three resources:
   - `torrentcdn-staging-ai` (web, Docker, `backend/Dockerfile`, health `/health`)
   - `torrentcdn-staging-db` (Postgres 16, private network only)
   - `torrentcdn-staging-redis` (Key Value)
4. The first deploy **will fail on purpose** — `TCDN_AI_COORDINATOR_KEYRING_JSON`
   is unset and the AI startup gate refuses to boot in production mode without
   it. Do §2 and §3, then **Manual Deploy → Deploy latest commit**.

Free-plan notes: the free web service sleeps after ~15 minutes idle (the first
request of a smoke run waits for a cold start), and free Postgres expires after
30 days. Switch the web service to `starter` if a run must never wait.

## 2. Env vars you must set yourself

Two are marked `sync: false` in the blueprint, so Render asks for them on the
first apply (and you can edit them later under **Environment**). None of these
may be the production value.

| Variable | What to put there |
|---|---|
| `TCDN_AI_COORDINATOR_KEYRING_JSON` | fresh keyring JSON from §3.1 — **staging only** |
| `TCDN_AI_PAYSTACK_SECRET_KEY` | Paystack **test** secret `sk_test_…` (§3.2) |
| `PAYSTACK_SECRET_KEY` | optional; CDN billing is not needed for the AI smoke test. If set, also `sk_test_…` |

Everything else the blueprint sets itself:

- generated once by Render: `JWT_SECRET`, `TCDN_AI_API_KEY_PEPPER`,
  `ADMIN_SETUP_TOKEN` (read it from the dashboard when creating the admin);
- hard-coded: `APP_ENV=production`, `STAGING_DEPLOYMENT=true`,
  `ENABLE_TCDN_AI=true`, `TCDN_AI_ENVIRONMENT=production`,
  `TCDN_AI_ALLOW_SIMULATED_ATTESTATION=false`,
  `TCDN_AI_ALLOW_DEV_STUB_BACKEND=false`, `TCDN_AI_PAYMENT_PROVIDER=paystack`;
- wired by reference: `DATABASE_URL` ← `torrentcdn-staging-db`, `REDIS_URL` ←
  `torrentcdn-staging-redis`.

`APP_ENV=production` + `TCDN_AI_ENVIRONMENT=production` are deliberate: staging
runs the **same config gates** the production deploy runs, so the code path
under test is the one that ships. The one thing that then cannot be inferred
from `APP_ENV` is "never move real money", so the blueprint also sets
`STAGING_DEPLOYMENT=true` — and `app/config.py` **refuses to start** if either
Paystack slot holds an `sk_live_` key while that flag is on.

## 3. Generating the secrets

### 3.1 Coordinator keyring

The coordinator signs ticket documents with an Ed25519 keyring that is injected
at runtime and never written back by the process. Generate it once, from a
checkout of the branch:

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time only
.venv/bin/python scripts/ai_keyring_init.py --name coordinator-staging
```

It prints one long JSON line. Paste it as the value of
`TCDN_AI_COORDINATOR_KEYRING_JSON`. Keep it somewhere safe: it is the only copy
of that private key, and a worker will reject tickets signed by any other key.

To check a keyring without printing it (what a pipeline should do):

```bash
.venv/bin/python scripts/ai_keyring_init.py --check TCDN_AI_COORDINATOR_KEYRING_JSON
```

### 3.2 Paystack test key

1. <https://dashboard.paystack.com> → switch to **Test mode** (toggle, top left).
2. **Settings → API Keys & Webhooks** → copy **Test Secret Key** (`sk_test_…`).
3. Paste it into `TCDN_AI_PAYSTACK_SECRET_KEY`.

A test key cannot charge a real card, and the account in test mode is not the
production account. Do **not** copy the production key.

Webhook URL for test-mode credit purchases (optional, only if you want to click
through a purchase): `https://<staging-url>/api/ai/v1/billing/webhook`.

### 3.3 Pepper

No action: Render generates `TCDN_AI_API_KEY_PEPPER` and keeps it stable. That
stability is what makes issued API keys keep working across deploys.

## 4. Verify it is alive

Open (or curl) the three surfaces. `status` is the one page you need:

```bash
STAGING=https://torrentcdn-staging-ai.onrender.com   # the real URL is in the dashboard

curl -s $STAGING/health            # {"status":"healthy"}
curl -s $STAGING/api/ai/v1/status | python3 -m json.tool
```

Expected `status` shape (values will differ):

```json
{
  "service": "tcdn-ai-coordinator",
  "commit": "1cfaf73a0f3e",
  "environment": "production",
  "feature_flag": {"name": "ENABLE_TCDN_AI", "on": true, "routes_mounted": true},
  "statement": "Gateway mode: the coordinator sees prompts in transit and stores none.",
  "database": {"status": "ok", "latency_ms": 3},
  "redis": {"status": "ok", "latency_ms": 1},
  "workers": {
    "registered": 1, "healthy": 1, "heartbeat_timeout_seconds": 120,
    "newest_heartbeat_age_seconds": 12, "oldest_heartbeat_age_seconds": 12,
    "models_served": ["qwen2.5:0.5b"]
  },
  "server_time": 1793000000
}
```

Health contract for staging:

| Check | Pass means |
|---|---|
| `/health` 200 | the process is up (Render's own health check uses this) |
| `feature_flag.on == true` | `ENABLE_TCDN_AI` reached the app and the AI env validates |
| `database.status == "ok"` | `SELECT 1` succeeded |
| `redis.status == "ok"` | ping succeeded. **`absent` is fatal for the AI** — ticket claims and nonce consumption fail closed, so every placement is refused |
| `workers.healthy >= 1` | a worker heartbeated within `heartbeat_timeout_seconds` (120 s) |
| `workers.newest_heartbeat_age_seconds` < 120 | that worker is still beating |

The payload carries counts and ages only: no node ids, no endpoints, no keys,
no prompts.

## 5. Bootstrap the registry, then attach a worker

The AI surface reuses the CDN's accounts, so a usable staging box needs four
things: an account, API keys, a model row, and a worker.

Run the bootstrap **inside the service**, where `DATABASE_URL` and the pepper
are already in the environment (Render dashboard → **Shell**, or
`render ssh torrentcdn-staging-ai`). The database is not exposed to the
internet, on purpose.

```bash
# 1. pull a small model on the worker machine (not in the web shell)
ollama pull qwen2.5:0.5b

# 2. read its manifest digest — this is the hash the registry and worker share
#    (Ollama reports it as 64 hex, sometimes prefixed with "sha256:")
curl -s http://127.0.0.1:11434/api/tags \
  | python3 -c "import json,sys; [print(m['name'], m['digest'].split(':')[-1]) for m in json.load(sys.stdin)['models']]"

# 3. in the Render shell
cd /app
python scripts/staging_bootstrap.py --manifest-hash <the 64-hex digest from step 2>
```

It registers `staging-smoke@example.test` over HTTP (so the real signup path
runs, including the email-verification gate and the free credit), seeds the
model row, and prints **two API keys once**:

```
export STAGING_BASE_URL=https://torrentcdn-staging-ai.onrender.com
export TCDN_AI_API_KEY=tcdn_ai_…          # inference
export TCDN_AI_WORKER_API_KEY=tcdn_ai_…    # worker, goes on the worker machine only
```

No `RESEND_API_KEY` is configured on purpose: with no mail provider, registration
auto-verifies, which is what lets the signup-credit step run end to end. In
production the same grant fires when the user clicks the emailed link.

### 5.1 How does a worker behind NAT/CGNAT receive jobs today?

**It cannot.** Today the coordinator is the client: placement resolves a node,
and the gateway opens `POST https://<node endpoint>/v1/jobs` to that node
(`gateway.http_worker_transport`). Registration and heartbeats are outbound
from the worker, but they carry no work. A worker with no inbound reachability
registers, looks healthy, and then fails every placement with
`worker_unreachable`.

So on staging the worker must be reachable from Render. Two supported ways:

**A. A VM with a public IP and a real certificate** (most stable)

- Any $5 VM; `ollama pull qwen2.5:0.5b`; Caddy or nginx in front of the worker
  on `:9100` with a Let's Encrypt certificate for e.g. `worker-staging.example.com`.
- Caddyfile is two lines:
  ```
  worker-staging.example.com {
      reverse_proxy 127.0.0.1:9100
  }
  ```
- Worker runs loopback-only plaintext; Caddy terminates TLS:
  ```bash
  export TCDN_AI_COORDINATOR_URL=https://torrentcdn-staging-ai.onrender.com
  export TCDN_AI_WORKER_API_KEY=tcdn_ai_…
  export TCDN_AI_WORKER_MODELS="qwen2.5:0.5b=<digest>"
  export TCDN_AI_BACKEND=ollama
  export TCDN_AI_OLLAMA_URL=http://127.0.0.1:11434
  export TCDN_AI_REGION=staging
  export TCDN_AI_GPU_MODEL=cpu
  export TCDN_AI_GPU_VRAM_GB=8
  export TCDN_AI_GPU_CLASS=cpu-8g
  export TCDN_AI_PRICE_MICRO_PER_MTOK=1000000
  export TCDN_AI_ENGINE_VERSION="ollama 0.40"
  export TCDN_AI_WORKER_DTYPE=float32
  export TCDN_AI_WORKER_QUANTIZATION=q4_k_m
  export TCDN_AI_IDENTITY_FILE=$HOME/.tcdn-ai/worker-identity.json
  # worker-side settings only: the coordinator's production gates are unaffected.
  # It lets the worker listen in plaintext on loopback (Caddy holds the cert) and
  # refuses the echo stub.
  export TCDN_AI_ENVIRONMENT=development
  export TCDN_AI_ALLOW_DEV_STUB_BACKEND=false
  # generate a self-signed pair only for the loopback hop Caddy proxies to
  cd backend
  python scripts/ai_worker.py --host 127.0.0.1 --port 9100 \
    --public-url https://worker-staging.example.com --allow-plaintext-loopback
  ```

**B. A tunnel, no public IP** (fastest; good for a laptop or a home box)

```bash
# on the machine running Ollama + the worker (plaintext on loopback)
cloudflared tunnel --url http://localhost:9100
# prints https://random-words.trycloudflare.com — that is your --public-url
python scripts/ai_worker.py --host 127.0.0.1 --port 9100 \
  --public-url https://random-words.trycloudflare.com --allow-plaintext-loopback
```

`trycloudflare.com` URLs are ephemeral: after a restart, re-register the worker
with the new URL (the bootstrap is idempotent; just restart the worker with the
new `--public-url` — it re-registers under the same identity file). The
coordinator dials the worker with TLS verification on, so a plain HTTP endpoint
or a self-signed certificate is refused by design.

### 5.2 Confirm the worker landed

```bash
curl -s https://<worker-host>/health   # {"status":"ok","node_id":…,"models":["qwen2.5:0.5b"]}
curl -s $STAGING/api/ai/v1/status | grep -A6 '"workers"'
```

Within ~30 s: `workers.registered: 1`, `workers.healthy: 1`, and the model in
`models_served`. Heartbeats are signed every 30 s and supervised, so a broken
tunnel shows up as a rising `newest_heartbeat_age_seconds`, not silence.

### 5.3 The outbound-connection design (checklist 3.4, P2)

NAT/CGNAT workers are a real product requirement (home GPUs, office machines)
and the current inbound-only model excludes them. The design — a long-poll job
channel where the worker dials out and the coordinator never connects in — is
written up in [`WORKER_NETWORKING.md`](WORKER_NETWORKING.md) and tracked as
checklist row **3.4** (P2, `MISSING`). Nothing in this staging kit depends on
it: staging uses a reachable worker.

## 6. Run the smoke test

From the Render shell (has `DATABASE_URL`) or locally with the three env vars:

```bash
cd /app
export STAGING_BASE_URL=https://torrentcdn-staging-ai.onrender.com
export TCDN_AI_API_KEY=tcdn_ai_…
# DATABASE_URL is already exported in the shell; export it yourself locally.

# The runtime image ships no dev dependencies, so install the official client
# once in the shell (it is the point of steps 5-6).
python -m pip install --user 'openai>=3,<4'
python scripts/staging_smoke.py
```

If pip refuses to write in the shell, run the script from a local checkout
against the staging URL instead — steps 1-3, 5-7 work, and 4/8 will fail with a
message saying `DATABASE_URL` is required (the database is not exposed to the
internet, by design).

Each step prints its own verdict; the exit code is 0 only if all pass.

| # | Step | What it proves |
|---|---|---|
| 1 | health | the process is up |
| 2 | status | flag on, DB and Redis answering, ≥1 worker with a fresh heartbeat |
| 3 | models | `/v1/models` lists the registry **and** a live worker serves the model the test will use |
| 4 | signup credit | the account has exactly one `ai_signup_grants` row and a `signup_credit` ledger entry of exactly 500 credits (SQL — a balance alone cannot prove a grant, so this step needs `DATABASE_URL`) |
| 5 | chat (non-streaming) | official OpenAI client, a **real** completion from the real worker, usage > 0, `X-TCDN-Job-Id` present, standard tier |
| 6 | chat (streaming) | SSE through the same client, ≥2 chunks, `finish_reason: stop`, `include_usage` honoured |
| 7 | balance and usage | both jobs are `settled`, both billed > 0, balance moved, `held_micro == 0` |
| 8 | ledger invariant | no non-positive entries, no self-referential entries, every settled job's split sums to its total, ledger sums equal `ai_jobs.total_micro`, and the ledger balance equals `/api/ai/v1/balance` |

Expected tail:

```
[PASS] 5. chat (non-streaming)    28+21 tokens, job 3f9a…, 'Small models fit in RAM.'
...
RESULT: PASS — 8/8 steps passed
```

**Verified 2026-10-28** on a local deployment shaped like this blueprint
(Postgres 16 container, Redis container, `APP_ENV=production`,
`TCDN_AI_ENVIRONMENT=production`, real Ollama `qwen2.5:0.5b` worker): bootstrap →
`credits 500.00`, smoke **8/8 PASS twice**, exit 0. That run is what found the
worker launcher's heartbeat bug (checklist 3.2): the node registered and served
jobs but stopped beating after the server took over, so the coordinator dropped
it 120 s later. Fixed in `scripts/ai_worker.py` and pinned by
`tests/test_ai_worker_launcher.py`; a Render deploy from this branch includes
the fix.

### 6.1 The same calls with curl

```bash
# models (OpenAI shape)
curl -s $STAGING/v1/models -H "Authorization: Bearer $TCDN_AI_API_KEY"

# non-streaming completion
curl -s $STAGING/v1/chat/completions \
  -H "Authorization: Bearer $TCDN_AI_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:0.5b","messages":[{"role":"user","content":"Say hi in five words."}],"max_tokens":32}' \
  -D /tmp/h.txt
grep -i x-tcdn-job-id /tmp/h.txt

# streaming completion (SSE)
curl -N -s $STAGING/v1/chat/completions \
  -H "Authorization: Bearer $TCDN_AI_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:0.5b","messages":[{"role":"user","content":"Say hi in five words."}],"max_tokens":32,"stream":true,"stream_options":{"include_usage":true}}'

# balance and usage
curl -s $STAGING/api/ai/v1/balance -H "Authorization: Bearer $TCDN_AI_API_KEY"
curl -s "$STAGING/api/ai/v1/usage?limit=5" -H "Authorization: Bearer $TCDN_AI_API_KEY"
```

## 7. What to try by hand

- `GET /api/ai/v1/status` — the page from §4; refresh it while a worker restarts
  to watch `healthy` drop to 0 after 120 s.
- Re-run the smoke test: the signup step still passes (one grant per account),
  the completion steps bill again, and the balance falls further.
- Set `TCDN_AI_SIGNUP_CREDITS=0` in the dashboard and redeploy if you want to see
  the "no free credit" path; remove the var afterwards.
- Kill the worker mid-stream on a large `max_tokens`: the SSE stream ends with an
  `{"error": …}` event, the job never settles, and the hold is released by the
  expiry sweep. That behaviour is checklist row 3.3 and is only partially
  covered — noting it here is the honest version.

## 8. Rollback

Staging is disposable, and production was never touched.

1. **Stop it:** Render dashboard → the blueprint's resources → **Suspend** (web)
   or **Delete** (web, Postgres, Key Value). Suspending the web service stops
   all access; the worker's heartbeats then fail and its supervisor backs off.
2. **Stop the worker:** Ctrl-C the `ai_worker.py` process. Nothing else on that
   machine needs cleaning up, except its identity file
   (`~/.tcdn-ai/worker-identity.json`), which is worthless once the staging
   database is gone.
3. **Data:** deleting the Postgres instance destroys the staging ledger, jobs,
   keys and accounts. Nothing is copied to production; there is no export step
   and no shared bucket.
4. **Redeploy after a rollback:** re-apply `render.staging.yaml`, set the two
   secrets again (§2), and re-run §5. A new keyring invalidates old tickets,
   which is correct — they are all expired anyway.
5. **The code:** rollback of staging is not rollback of the branch. The branch
   stays as it was; merging is still a separate, owner-only action.

Cost while it exists: free plan = $0 (web sleeps, Postgres expires after 30
days). `starter` web + `basic-256mb` Postgres ≈ $13/month if it must stay warm
and outlive a month.

## 9. Checklist honesty

- This kit closes **no** checklist row. Level 1 still says **NO**.
- Row **4.5** (signup credit) gained its missing trigger in this change: the
  grant was implemented and unit-tested but never called, so a real signup
  received nothing. It is now called from registration's auto-verify path and
  from `/auth/verify-email` (and the resend fallback). The staging smoke test's
  step 4 is the first end-to-end proof of it.
- Row **3.4** (outbound worker mode for NAT/CGNAT) is new, P2, `MISSING`, with
  the design in `WORKER_NETWORKING.md`.
- Everything else is unchanged; the counts in `PUSH_CHECKLIST.md` say so.
