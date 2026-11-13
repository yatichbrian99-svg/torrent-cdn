# 22 — logout without Redis still revokes the token (MED, product)

## The problem

`POST /auth/logout` wrote `token_block:{jti}` to Redis. Production runs without
Redis, where `get_redis()` returns `None` and the endpoint was a documented
no-op: the token stayed valid until `exp`, up to 24h after "Log out". The
password-change marker (`security_stamp`) covers the revoke-on-compromise case,
but not "this one token, now" — a shared laptop, a leaked header, a support
instruction to sign out everywhere.

## The fix

Two stores, chosen by what each is good at:

- **Redis** — unchanged fast path, one GET per request when present;
- **Postgres `revoked_tokens`** — durable fallback, consulted **only** when
  Redis is absent or raises. `logout` always writes both (each write
  independently best-effort, logged on failure, so a store outage never fails a
  logout).

Schema (added to the startup DDL/ALTER batch in `main.py`):

```sql
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti TEXT PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_revoked_tokens_expiry ON revoked_tokens(expires_at);
```

- **indexed**: `jti` is the primary key (the per-request lookup), plus an index
  on `expires_at` for the cleanup;
- **cleaned by expiry**: every write also runs
  `DELETE FROM revoked_tokens WHERE expires_at < NOW()`. A row is only needed
  until the token it names would have failed signature/exp validation anyway, so
  the table is bounded by the 24h token lifetime without a cron.

Failure policy is unchanged where it matters: Redis down → ask Postgres; **both**
down → warn and fail open, with token expiry as the backstop. Availability of
`/auth/*` must not depend on two datastores.

## Evidence

| File | What it is |
| --- | --- |
| `tests-before.log` | `tests/test_auth_logout_db_fallback.py` on `main`: **8 failed, 1 passed** |
| `tests-after.log` | the same file with the change: **9 passed** |

Covered:

- logout with no Redis records `(jti, exp)` and emits the expiry cleanup;
- logout returns 200 when Redis **and** Postgres both fail (never a 500);
- a token revoked in the table is refused with `401 "Session invalidated"` when
  Redis is absent — the exact gap that existed;
- a token **not** in the table still authenticates without Redis (the fallback
  must not turn "no Redis" into "no sessions");
- when Redis answers, the database is not consulted at all (fast path intact);
- a Redis exception falls back to the table; both stores failing fails open.

`test_auth.py::TestLogoutTokenRevocation::test_redis_failure_fails_open` was
updated to the new contract and split: it now asserts fail-open when the table
answers "not revoked", plus a new case for both stores down.

Regression: full backend suite **1283 passed, 132 skipped**; `ruff check` clean.

## Residual

- Fail-open when both stores are unreachable is deliberate and documented; the
  worst case is the old behaviour (token valid until expiry).
- No backfill is needed: absent rows mean "not revoked".
