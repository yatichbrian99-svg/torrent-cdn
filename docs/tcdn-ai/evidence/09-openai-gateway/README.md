# Evidence — OpenAI-compatible gateway (Section C item 1)

Generated 2026-10-24.

## `pytest.txt`

`ENABLE_TCDN_AI=true .venv/bin/python -m pytest tests/test_ai_openai_compat.py -q`

Covers, against the real ASGI app with an in-process worker running the real
X25519 + HKDF + AES-256-GCM protocol:

- the **official OpenAI Python client** non-streaming and streaming
  (`stream_options.include_usage`), plus rejected `tools` with OpenAI's error body;
- `/v1/models` shape and auth;
- canary test: the prompt never appears in captured logs or stored rows;
- worker failure before the first token is a real HTTP 503, not a 200;
- `ENABLE_TCDN_AI=false` (subprocess import) leaves `/v1/...` at 404.

## `full-suite-on.txt` / `full-suite-off.txt`

`ENABLE_TCDN_AI=true .venv/bin/python -m pytest -q` and the same with the flag
off, three consecutive runs each: **923 passed / 7 skipped** and **877 passed /
53 skipped**, 64-98 s, exit 0. Before the fix in this milestone both commands
hung (exit 124) at ~15 %.

*(Counts recorded at the time of this milestone. After the real-engine tests
were added in Section C item 2 the same commands report **925 / 11** and
**879 / 57** — the extra skips are `tests/test_ai_engine.py`, which needs a live
engine. See `STATE.md` §2.)*

Root cause of that hang, found this session: the app's lifespan startup calls
`main.py`'s **module-level** `get_db_pool` binding, which `tests/conftest.py`
did not patch, so every `TestClient` startup opened a real asyncpg pool against
`DATABASE_URL` (a stopped docker bridge IP that drops SYNs). `conftest.py` now
hands the lifespan a throwaway mocked pool, so no test touches the network and
`mock_conn` assertions (e.g. the billing webhook tests' `assert_not_called`)
stay meaningful. The same patch removed an order-dependent legacy failure:
`test_billing.py::TestPaystackWebhookPaymentGate` used to fail when it was the
first test to import `app.main` in the process.

## `check.mjs` / `evidence.json`

The **official OpenAI JavaScript client** (`openai` from npm) run against a live
uvicorn server on `127.0.0.1:8099`, serving the coordinator with one registered
in-process echo worker. The harness is a throwaway script (not committed to the
product). Reproduce:

```bash
# 1. serve the gateway + worker (from backend/, with ENABLE_TCDN_AI=true)
python /tmp/gateway_harness.py

# 2. run the official JS client
mkdir -p /tmp/openai-js-check && cd /tmp/openai-js-check
npm install openai
node check.mjs
```

`evidence.json` records what the JS client actually received: OpenAI list shape,
`chat.completion` with `chatcmpl-` id, streaming deltas plus final usage, and the
`400 unsupported_parameter` body for `tools`.

## What these runs do NOT prove

- No real inference engine: the worker's `EchoBackend` is a labelled dev stub.
  Real vLLM/Ollama is Section C item 2.
- No load, soak, or chaos test of the gateway (Section C item 4 / Appendix 4).
- The gateway was not exercised with a remote TLS worker; only loopback HTTP and
  the in-process transport. TLS 1.3 policy is shared with the SDK
  (`tcdn_ai_sdk._crypto.tls_context`) and asserted in `test_sdk_tls_context_is_tls13_only`.
