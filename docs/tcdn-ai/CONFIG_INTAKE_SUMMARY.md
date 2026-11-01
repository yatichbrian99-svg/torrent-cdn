# TCDN-AI — config intake summary

Source of truth for this intake is `.env.example` (the `TCDN_AI_` section, lines
185–225). This document maps each ingested field to its normalized internal name,
type, required status, and any normalization notes or confirmation requests. It
is intentionally scoped to the `TCDN_AI_` prefix only — the surrounding app/Redis
/S3/live env is out of scope here.

## How to read this table

- **Env key** — literal key in `.env.example`.
- **Internal name** — the settings attribute/path at `backend/app/ai/config.py`
  (`AISettings`, env prefix `TCDN_AI_`).
- **Type** — how the backend parses it today.
- **Required** — whether startup validation refuses to serve AI routes when it is
  missing in production.
- **Notes / action** — normalization notes or explicit confirmation requests for
  anything ambiguous, optional-but-meaningful, or potentially typo'd.

## Intake table

| Env key | Internal name | Type | Required (prod) | Notes / action |
|---|---|---|---|---|
| `TCDN_AI_ENVIRONMENT` | `AISettings.environment` | string enum (`development` / `staging` / `production`) | No (defaults to `development`) | **Confirm intent:** the documented hardening goal is for production to *require* this be `production`. Today it defaults to development, which is the config trap listed in `STATE.md` §6 #3 and `HANDOFF_AUDIT.md` C-3. Treat as confirmed-stale until the flag work lands. |
| `TCDN_AI_ENABLED` | `AISettings.enabled` | string → bool | No (not wired) | **Confirm / resolve ambiguity:** present in `.env.example` but, per the audit, not read anywhere that gates routing or DDL. Do not rely on it. If it is meant to stay, wire it or remove it. |
| `TCDN_AI_COMPUTE_SHARE_PCT` | payout split `compute` | int (%) | Yes (with the other two) | Part of the 70/10/20 split. Must sum to 100 with seeder + platform. |
| `TCDN_AI_SEEDER_SHARE_PCT` | payout split `seeder` | int (%) | Yes (with the other two) | Part of the 70/10/20 split. |
| `TCDN_AI_PLATFORM_SHARE_PCT` | payout split `platform` | int (%) | Yes (with the other two) | Absorbs the remainder after compute + seeder. |
| `TCDN_AI_COORDINATOR_KEY_FILE` | keyring loader (file) | path (0600 JSON) | Yes in production (with the JSON alternative) | Either this **or** `TCDN_AI_COORDINATOR_KEYRING_JSON` must be set in production. Production refuses auto-generation. |
| `TCDN_AI_COORDINATOR_KEYRING_JSON` | keyring loader (injected JSON) | JSON string | Yes in production (with the file alternative) | Read-only; never written back. Mutually exclusive choice with the file var. |
| `TCDN_AI_KEY_OVERLAP_SECONDS` | `KeyRing` overlap window | int seconds | No (default 3600) | Controls how long a retired coordinator key can still verify tickets after rotation. |
| `TCDN_AI_TICKET_TTL_SECONDS` | placement ticket TTL | int seconds | No (default 120) | Controls ticket expiry. |
| `TCDN_AI_API_KEY_PEPPER` | API-key HMAC pepper | 256-bit secret string | Yes in production | Versioned by `TCDN_AI_API_KEY_PEPPER_ID`. Production refuses boot without it. |
| `TCDN_AI_API_KEY_PEPPER_ID` | pepper version id | string | No (default `v1`) | Keys carry the pepper id so they can be re-hashed on next use after a pepper rotation. |
| `TCDN_AI_PAYMENT_PROVIDER` | payment provider selector | string enum (`fake` / `paystack`) | Yes (value `paystack` in production) | `fake` is development-only; production validation refuses boot with it. |
| `TCDN_AI_PAYSTACK_SECRET_KEY` | Paystack secret key | string | Yes in production when provider is paystack | Signs API calls and webhooks. |
| `TCDN_AI_SIGNUP_CREDITS` | new-account credit grant | int micro-credits (default 500 = ~$5) | No (default 500) | Funded by a ledger transfer out of the platform wallet; subject to the per-IP grant cap. |
| `TCDN_AI_CONFIDENTIAL_UPLIFT_PCT` | confidential price uplift | int (%) | No (default 25) | Multiplier applied to confidential-tier pricing. |
| `TCDN_AI_LAUNCH_PRICING_UNTIL` | launch price table effective-until | date string (`YYYY-MM-DD`) | No (default `2027-04-01`) | Price table is effective from a fixed start and until this date; confirm the start date is documented wherever the launch table is defined. |
| `TCDN_AI_MAX_TOKENS_PER_REQUEST` | per-request token cap | int tokens | No (default 4096) | Request-size guardrail. |
| `TCDN_AI_MAX_MESSAGES` | per-request message cap | int messages | No (default 64) | Request-size guardrail. |
| `TCDN_AI_MAX_BODY_BYTES` | request body size cap | int bytes (default 262144) | No (default 262144) | Request-size guardrail. |
| `TCDN_AI_RATE_LIMIT_PER_MINUTE` | per-key/per-IP rate limit | int requests/min | No (default 120) | In-process token bucket. Documented as per-instance; Redis-backed counting is the documented production upgrade. |
| `TCDN_AI_RATE_LIMIT_BURST` | per-key/per-IP burst | int requests | No (default 30) | Token-bucket burst for the above. |
| `TCDN_AI_ALLOW_SIMULATED_ATTESTATION` | attestation policy | bool | No (default true, but must be false in production) | Production validation refuses boot with it true. Simulated adapter is labelled in responses. |
| `TCDN_AI_ALLOW_DEV_STUB_BACKEND` | inference backend policy | bool | No (default true, but must be false in production) | Production validation refuses boot with it true. The echo/dev stub exposes `/health` with `dev-stub`. |
| `TCDN_AI_ENABLE_TCDN_BURN` | burn module on/off | bool | No (default false; must be false unless ack'd) | Disabled and dry-run only. Production refuses boot with it true unless `TCDN_AI_BURN_LEGAL_REVIEW_ACK=true`. |
| `TCDN_AI_BURN_LEGAL_REVIEW_ACK` | burn legal acknowledgement | bool | No (default false) | Gates enabling burn in production; the only implemented strategy is dry-run. |
| `TCDN_AI_TRANSCRIPT_RETENTION_SECONDS` | worker transcript retention | int seconds (default 86400) | No (default 86400) | Workers keep encrypted per-token records for the challenge window, then wipe them; receipts carry only the Merkle root. |
| `TCDN_AI_VERIFICATION_SAMPLE_PCT` | result-verification sample rate | int percent (default 5) | No (default 5) | Nominal sample rate; the verification engine is still being wired (see `STATE.md` §6 #6). |

## Normalization notes

- Integers: share percentages, TTLs, caps, retention seconds, sample rate, uplift
  and rate-limit values are all parsed as integers from environment strings. A
  non-integer or out-of-range value should fail validation rather than silently
  coerce.
- Booleans: the `true`/`false` values in `.env.example` are parsed as booleans by
  the settings loader; do not pass `1`/`0` or `yes`/`no` unless the loader is
  explicitly extended to accept them.
- Secrets: `TCDN_AI_API_KEY_PEPPER`, `TCDN_AI_PAYSTACK_SECRET_KEY`, and the
  keyring JSON/file are secret material. They are not logged; the pepper is
  hashed with its version id, not stored raw.
- Mutually exclusive pairs: `TCDN_AI_COORDINATOR_KEY_FILE` and
  `TCDN_AI_COORDINATOR_KEYRING_JSON` are alternative keyring sources. Pick one
  per deployment; do not set both expecting the loader to merge them.
- Split invariant: `TCDN_AI_COMPUTE_SHARE_PCT +
  TCDN_AI_SEEDER_SHARE_PCT + TCDN_AI_PLATFORM_SHARE_PCT` must equal 100. If a
  deployment sets only some of them, treat the missing ones as a validation
  error, not as zero.

## Resolution for the intake-confirmation items

The three items below were open when this intake was written. The config was then
wired to resolve the two that affect `.env.example` and startup behaviour; the
third stays open because it is a missing date, not a gating issue.

1. **`TCDN_AI_ENABLED` vs `ENABLE_TCDN_AI`** — **resolved by wiring.**
   `.env.example` no longer documents `TCDN_AI_ENABLED` as the feature gate. The
   real gate is `ENABLE_TCDN_AI`, which now defaults to `false` in
   `.env.example`. The host app (`main.py`) uses `ENABLE_TCDN_AI` to decide
   whether to mount `/api/ai/v1`, run `AI_TABLES_DDL`, and start the AI expiry
   sweep. `AISettings.enabled` (`TCDN_AI_ENABLED`) is retained only so existing
   `.env` files do not break, and is marked deprecated in `config.py`.

2. **Production default for `TCDN_AI_ENVIRONMENT`** — **resolved by wiring.**
   When `APP_ENV=production` and `ENABLE_TCDN_AI` is on, the host app now checks
   that `TCDN_AI_ENVIRONMENT=production` **and** that at least one coordinator
   secret source is present (`TCDN_AI_COORDINATOR_KEY_FILE`,
   `TCDN_AI_COORDINATOR_KEYRING_JSON`, or `TCDN_AI_API_KEY_PEPPER`). If either
   is missing, the AI routes and `AI_TABLES_DDL` stay off instead of silently
   falling back to development defaults. The existing `AISettings` validator still
   refuses insecure production combinations (simulated attestation, dev stub,
   fake payments, missing pepper, missing keyring, burn without ack) when
   `TCDN_AI_ENVIRONMENT=production`.

3. **Launch pricing start date** — **still open.** `.env.example` gives
   `TCDN_AI_LAUNCH_PRICING_UNTIL=2027-04-01` but does not state the effective-from
   date. The intake record cannot invent one; confirm where the launch table's
   start date is defined so both ends of the window are documented. Tracked in
   `OPEN_QUESTIONS.md` until resolved.

## Fields explicitly NOT in scope for this intake

The following were requested as intake topics but are **not present in the
repo's `.env.example`**, so they are carried as open questions instead of mapped
fields:

- HPA fields — no `HPA_*`, `HORIZONTAL_POD_AUTOSCALER_*`, or equivalent appears
  in `.env.example` or the TCDN-AI docs. Recorded in `OPEN_QUESTIONS.md`.
- DPS `redis_delimiter` — no `DPS_*`, `REDIS_DELIMITER`, or equivalent appears
  in `.env.example`. Recorded in `OPEN_QUESTIONS.md`.
- Storage `type`/`value` debug log — the storage env in this repo is the standard
  S3/MinIO block (`MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, etc.); there is no
  `storage.type` or `storage.value` debug-log variable in `.env.example`. Recorded
  in `OPEN_QUESTIONS.md`.

## Source and status

- Source snapshot: `.env.example` lines 185–225 at the time this document was
  written.
- Status: intake mapping complete for the fields present. The three items above
  remain open and are tracked in `OPEN_QUESTIONS.md`.
