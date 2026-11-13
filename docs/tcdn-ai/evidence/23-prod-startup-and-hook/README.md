# 23 — production startup must refuse, and the hook secret is not a URL

Three findings, one branch: the startup guards only logged, the MediaMTX shared
secret was compared with `==` and accepted in the query string, and an unused
auth dependency still resolved API keys.

## 1. Startup hardening (`app/config.py`)

Both production guards were `logger.warning` calls. A warning is a log line;
the process kept serving.

| Condition | Before | After |
| --- | --- | --- |
| `TRUSTED_PROXIES` unset with `APP_ENV=production` | warning | **`ValueError`** — the boot fails |
| `ADMIN_SETUP_ENABLED=true` without `ADMIN_SETUP_TOKEN` | `logger.error` | **`ValueError`** — the boot fails |

Why it matters: with `TRUSTED_PROXIES` unset the first `X-Forwarded-For` entry is
trusted from **any** peer, so a direct client can rotate that header and walk
past login/register/upload rate limits — i.e. no working per-IP limit at all.
With the admin bootstrap flag up and no token, `POST /auth/setup` promotes **any**
authenticated user to admin. Both are "the deployment is unsafe, do not run it",
which is exactly what a failed boot says.

Deployment configs were updated so nothing that ships breaks:

- `render.staging.yaml` gains `TRUSTED_PROXIES: 127.0.0.1/32` (staging is reached
  directly, so trusting only loopback is *stricter* than the old implicit
  "trust every peer");
- `render.yaml`'s comment now points at the boot failure instead of a warning.

## 2. MediaMTX hook secret (`app/routes/live_internal.py`)

- `presented == secret` → **`hmac.compare_digest`**, compared as **bytes**.
  Bytes matters: `compare_digest` raises `TypeError` on `str` with non-ASCII
  characters, and the header is attacker-controlled — a non-ASCII secret attempt
  must be a rejection, not a 500 (there is a test for exactly that, and it failed
  against the first version of this fix);
- **header-first and header-only by default**: `X-Hook-Secret` is the interface.
  `?secret=` is a **deprecated, warned, opt-in** path — refused in production
  unless `MEDIAMTX_ALLOW_QUERY_SECRET=1`, which exists only because stock
  MediaMTX ≤1.11 has no header option for `authHTTPAddress` (checked against the
  upstream configuration reference: the auth hook has `authHTTPAddress`,
  `authHTTPFingerprint`, `authHTTPExclude` — no headers setting);
- the two hooks we control (`runOnReady`/`runOnNotReady`) already sent the header.

Documented in `docs/live-streaming.md` §3 and the `docker-compose.yml` comment:
front the auth hook with a header-injecting proxy, then drop the flag.

## 3. `get_optional_user_id` deleted (`app/auth.py`)

Unused, but it accepted `tcdn_` API keys and resolved them to their owner — a
route mounted on it would accept a long-lived credential. Deleted; a test asserts
it stays deleted (`test_api_keys.py`).

## Evidence

| File | What it is |
| --- | --- |
| `hook-tests-before.log` | `test_hook_secret.py` on `main`: **3 failed, 7 passed** |
| `hook-tests-after.log` | **10 passed** |
| `startup-tests-before.log` | the two startup guards on `main`: **2 failed, 3 passed** |
| `startup-tests-after.log` | **5 passed** |

The `hook-tests-before` failures are the three behaviours the audit named: no
`compare_digest`, and `?secret=` accepted in production without an opt-in and
without a warning. The `startup-tests-before` failures are the two guards that
logged instead of raising.

Existing tests updated to the new contract (they asserted the old warnings):
`test_security.py` (both guards), `test_staging_config.py` (helper sets
`TRUSTED_PROXIES`), `test_api_keys.py` (the optional dependency is gone).

Regression: full backend suite **1283 passed, 132 skipped**; `ruff check` clean;
`mypy --config-file mypy-ai.ini` clean.

## Notes / limits

- Any production deployment missing either variable will now fail its health
  check instead of starting. That is the point, but it is a deploy-time contract
  change: `render.yaml` already sets `TRUSTED_PROXIES`; a self-hosted deployment
  must set both.
- The MediaMTX query fallback is a *deprecation path*, not a fix in itself: until
  the edge injects a header, the auth hook's secret still rides in a URL. This
  branch cannot change what MediaMTX is able to send.
