# Security Configuration

This document describes the security header configuration for the TorrentCDN API backend.

## Overview

All security headers are set by `CSPMiddleware` in `backend/app/main.py`. The middleware runs on every response and sets 8 security headers (9 in production). Additionally, middleware-level protections are configured for CORS, trusted hosts, and request body size.

---

## Response Headers

### 1. Content-Security-Policy

**Header:** `Content-Security-Policy`

Controls which resources the browser is allowed to load. The policy is environment-aware — dev origins (`localhost:9000`) are stripped in production.

| Directive | Development | Production |
|-----------|-------------|------------|
| `default-src` | `'self'` | `'self'` |
| `script-src` | `'self'` | `'self'` |
| `style-src` | `'self' 'unsafe-inline' fonts.googleapis.com` | `'self' 'unsafe-inline' fonts.googleapis.com` |
| `font-src` | `'self' fonts.gstatic.com` | `'self' fonts.gstatic.com` |
| `img-src` | `'self' data: blob:` | `'self' data: blob:` |
| `media-src` | `'self' blob: http://localhost:9000 <APP_URL>` | `'self' blob: <APP_URL>` |
| `connect-src` | `'self' wss://tracker.webtorrent.dev wss://tracker.openwebtorrent.com wss://tracker.btorrent.xyz http://localhost:9000 <APP_URL>` | `'self' wss://tracker.webtorrent.dev wss://tracker.openwebtorrent.com wss://tracker.btorrent.xyz <APP_URL>` |
| `frame-src` | `'self'` | `'self'` |
| `worker-src` | `'self' blob:` | `'self' blob:` |
| `base-uri` | `'self'` | `'self'` |
| `report-uri` | `/api/csp-report` | `/api/csp-report` |
| `report-to` | `csp-endpoint` | `csp-endpoint` |

**Script policy:** `script-src 'self'` — no inline scripts or third-party script CDNs. WebTorrent is self-hosted and pinned at `/vendor/webtorrent.min.js` with SRI. The **frontend** (Next.js) additionally enforces a nonce-based CSP via `frontend/middleware.ts` (which requires `export const dynamic = 'force-dynamic'` in the root layout), and sends `X-Frame-Options: SAMEORIGIN` + `Cross-Origin-Opener-Policy: same-origin` via `next.config.js`. CORP/COEP are intentionally **not** set on the frontend: the embed SDK (`sdk.js` + `/vendor/webtorrent.min.js`) must stay loadable cross-origin from customers' pages, and COEP `require-corp` would break no-cors thumbnails served from the storage provider.

**Environment switching:** Set `APP_ENV=production` to strip `localhost:9000` from `media-src` and `connect-src`.

**Report-only mode:** Set `CSP_REPORT_ONLY=true` to use `Content-Security-Policy-Report-Only` instead of enforcing.

### 2. Reporting-Endpoints

**Header:** `Reporting-Endpoints: csp-endpoint="/api/csp-report"`

Defines the named endpoint used by the `report-to` CSP directive. CSP violation reports are sent to the API's built-in report collector.

### 3. X-Content-Type-Options

**Header:** `X-Content-Type-Options: nosniff`

Prevents browsers from MIME-type sniffing. Forces the browser to respect the `Content-Type` header as-served.

### 4. X-Frame-Options

**Header:** `X-Frame-Options: DENY`

Prevents the page from being displayed in a frame, protecting against clickjacking attacks. The embed player is served by the Next.js frontend, not the API, so DENY is appropriate.

### 5. Referrer-Policy

**Header:** `Referrer-Policy: strict-origin-when-cross-origin`

Controls how much referrer information is sent with requests:
- **Same-origin:** Full URL is sent
- **Cross-origin (HTTPS→HTTPS):** Only the origin is sent
- **Cross-origin (HTTPS→HTTP):** No referrer is sent

### 6. X-XSS-Protection

**Header:** `X-XSS-Protection: 1; mode=block`

Enables the browser's built-in reflected XSS filter and blocks the page if an attack is detected. This header is deprecated in modern browsers (CSP `script-src` is the modern defense) but provides backward compatibility with older browsers.

### 7. Permissions-Policy

**Header:** `Permissions-Policy: accelerometer=(), ambient-light-sensor=(), autoplay=(), camera=(), display-capture=(), document-domain=(), encrypted-media=(), fullscreen=(), gamepad=(), geolocation=(), gyroscope=(), interest-cohort=(), magnetometer=(), microphone=(), midi=(), payment=(), picture-in-picture=(), publickey-credentials-get=(), screen-wake-lock=(), speaker-selection=(), usb=(), web-share=()`

Blocks all browser APIs. All 22 features are set to `()` (disallowed), including:
- 🎤 `camera`, `microphone` — no recording
- 📍 `geolocation` — no location tracking
- 💳 `payment` — no browser payment API
- 🔌 `usb`, `midi`, `serial` — no device access
- 🧭 `accelerometer`, `gyroscope`, `magnetometer` — no sensor data
- 🔐 `interest-cohort` — blocks Google's FLoC
- 🖥️ `display-capture` — no screen sharing

The frontend can set its own `Permissions-Policy` if these features are needed for video playback.

### 8. Strict-Transport-Security (production only)

**Header:** `Strict-Transport-Security: max-age=31536000; includeSubDomains; preload`

**Only set when `APP_ENV=production`.** Instructs browsers to always connect over HTTPS for 1 year (31536000 seconds). Includes all subdomains and requests browser preload list inclusion.

### 9. Cross-Origin-Resource-Policy

**Header:** `Cross-Origin-Resource-Policy: same-origin`

Blocks no-cors cross-origin embedding of API payloads (e.g. `<img>`/`<script>` tags pointing at API JSON). Safe on the API because browser clients consume it via CORS-enabled fetch, where CORP is ignored.

---

## Middleware Protections

### CORS

| Setting | Value |
|---------|-------|
| Allowed origins | `http://localhost:3000`, `http://frontend:3000`, `APP_URL` env var |
| Credentials | `true` (Authorization header) |
| Methods | `*` |
| Headers | `*` |

Configure `APP_URL` to set the production frontend origin.

### API Documentation (production)

`/docs`, `/redoc`, and `/openapi.json` are **disabled in production** (`docs_url=None`, `redoc_url=None`, `openapi_url=None` in `backend/app/main.py`) — a public OpenAPI schema is an unnecessary attack-surface map (OWASP API3:2019). They stay available in development, or can be force-enabled with `ENABLE_API_DOCS=true`.

### Security Contact (security.txt)

RFC 9116 `/.well-known/security.txt` is served on the frontend (`frontend/app/.well-known/security.txt/route.ts`) and on the API origin (`GET /.well-known/security.txt` in `backend/app/main.py`).

### Health Endpoint

`GET /health` returns only `{"status": "healthy"}` — no service name — to avoid implementation fingerprinting.

### Trusted Host

`TrustedHostMiddleware` with `allowed_hosts=["*"]`. In production, restrict to specific hostnames.

### Request Size Limit

`MaxBodySizeMiddleware` rejects requests with a `Content-Length` exceeding **2 GB** with a `413 Payload Too Large` response.

---

## CSP Violation Reporting

**Endpoint:** `POST /api/csp-report`

A built-in endpoint that receives CSP violation reports from browsers.

**Features:**
- Accepts both legacy `application/csp-report` and modern `application/reports+json` formats
- Parses and logs violations via `logger.warning()` with structured details (blocked URI, violated directive, source file, line/column numbers)
- Rate limited at **100 reports/minute/IP** via Redis
- Returns `204 No Content` (no body, no redirects)
- Trailing-slash loop protection ensures no infinite report cycles

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_ENV` | `development` | Set to `production` for HSTS + hardened CSP |
| `APP_URL` | `http://localhost:3000` | Frontend URL (used in CORS and CSP; must be the canonical `https://www.torrentcdn.store` in prod — dead `torrentcdn.com` is auto-corrected at startup) |
| `CSP_REPORT_URL` | `/api/csp-report` | CSP report endpoint |
| `CSP_REPORT_ONLY` | `false` | Use `Content-Security-Policy-Report-Only` if `true` |

---

## Pre-merge CI Gates

Every push/PR to `main` runs `.github/workflows/security.yml` + `.github/workflows/dependency-audit.yml` — **all jobs must be green before merging** (the scheduled jobs also run on their daily crons; `p2p-e2e` is push/manual-dispatch only and `api-key-live` + `upload-live` are schedule/manual-dispatch only because they write to production). PRs get inline gitleaks comments pointing at the offending line when a secret is introduced; the build fails on anything below:

| Job | What it gates |
|---|---|
| `tests` | Backend unit suite (pytest, fully mocked — no infra) + **domain guard** (dead `api.torrentcdn.store` host / apex-only `torrentcdn.store` URLs in `backend/app`, `Caddyfile`, `render.yaml`) |
| `secret-scan` | Diff-scoped high-signal secret gate (`backend/scripts/secret_scan.sh`) + whole-tree scan — GitHub PATs, AWS/Stripe/SendGrid/Resend keys, private keys, webhooks |
| `static-audit` | Gitleaks secret scan (pushed-range on push, **full history** on the daily cron/dispatch; posts inline PR comments, uploads SARIF) + `security_audit.py` header/CSP/auth/API-docs-gating checks |
| `frontend-checks` | **Frontend pipeline:** `npm ci` → `tsc --noEmit` → vitest unit tests **+ coverage gate** → `next build` → **OG image guard** + **API baseURL guard** + **domain guard** (dead `api.torrentcdn.store` host / apex-only `torrentcdn.store` URLs) |
| `fullstack-audit` | Docker stack (Postgres/Redis/MinIO) + `curl_audit.sh` end-to-end (push/PR only; runs even when the secret gates are red, so a leak PR keeps its e2e signal) |
| `p2p-e2e` | **Real 2-peer WebTorrent delivery gate** (push/manual only — writes throwaway accounts/files to production): builds the pushed frontend, serves it behind the same-origin proxy, and runs `e2e/p2p-two-peer.mjs` — seeder reproduces the backend infohash and seeds; leecher plays `/embed/{id}` with every `*.supabase.co` request BLOCKED (only WebRTC can deliver) → peers connect, video plays, `chunk_downloaded` with `bytes_p2p > 0` lands in analytics |
| `storage-publicity` | Credential-free live check that the Supabase bucket is still **public** (`check_storage_public.sh`): a private bucket 404s all playback, so a regression here breaks every `minio_url` |
| `api-key-live` | **Scheduled production probe** (daily 04:00 UTC cron + manual dispatch only — writes a throwaway account+key, then cleans up): runs `backend/scripts/api_key_audit_prod.sh` against the live deployment — register → generate `tcdn_` key (plaintext once) → key-authed `/files` + `/auth/me` + metadata (`api_key:null`) → no-auth 401 → revoke → **revoked key 401 (fail-closed)** → `DELETE /auth/me` |
| `upload-live` | **Scheduled production probe** (daily 04:00 UTC cron + manual dispatch only — writes a throwaway upload+account, then cleans up): runs `backend/scripts/upload_audit_prod.sh` — register → `/upload/presign` → **PUT bytes direct to the presigned Supabase URL** (never via Render/Vercel) → `/upload/presign/finalize` → real 40-hex btih magnet → file listed under `/files` → `DELETE /files/{id}` + `DELETE /auth/me` |
| `dependency-audit` | **Supply-chain gate** (daily 06:00 UTC cron + PR + manual): runs `pip-audit` + `npm audit`, posts the report as a PR comment, and fails on any advisory **beyond the accepted baseline** (`backend/scripts/dependency-audit-baseline.json` — currently the `ip`-via-webtorrent highs) or on any critical |

> **When the dependency-audit job goes red**, the report/comment show exactly which advisories are new. If a newly-found advisory is genuinely accepted (no safe fix), regenerate the baseline so CI reflects the new decision — then record the acceptance in the Hardening Log below:
>
> ```bash
> python backend/scripts/dependency_audit_report.py \
>   --write-baseline backend/scripts/dependency-audit-baseline.json
> ```

### Frontend build + OG image guard (protects link previews)

The `frontend-checks` job compiles the Next.js app exactly like production (Node 20, matching `frontend/Dockerfile`) and asserts share-card metadata can't silently break:

- `npm ci` — locked dependency install (fails loudly on lockfile drift).
- `npx tsc --noEmit` — strict typecheck of the whole frontend (test files included).
- `npm test` — vitest unit tests (auth token store, API interceptor Bearer/401-logout, UsageBar math, EmbedCode snippet, PricingTable checkout, FileCard delete-confirm/copy).
- `npm run build` — full `next build`; a type error or build break now fails CI instead of shipping.
- **OG image guard** (`frontend/scripts/check_og_image.sh`) — the `og:image` / `twitter:image` URLs in `app/layout.tsx` must resolve to a real, non-empty PNG under `public/`. A prior regression shipped a 0-byte dynamic route + a 404 twitter image that blanked every preview card on Twitter/X, LinkedIn, Slack, Discord, and WhatsApp — this guard exists so that can't happen again.
- **Domain guard** (`frontend/scripts/check_domains.sh`) — the frontend **source** must never reference the dead `api.torrentcdn.store` host (no DNS record — the docs `BASE_URL` once pointed there and every curl example was broken) or apex-only `torrentcdn.store` URLs (the apex 308-redirects to `www`, adding a redirect hop and breaking the embed SDK's origin resolution). Only the documented warning comment in `docs/page.tsx` is exempt; `scripts/` guard files are intentionally not scanned (they legitimately document hosts, e.g. `check_api_baseurl.sh`'s same-origin allow-list).
- **Backend domain guard** (`backend/scripts/check_domains.sh`, runs in the `tests` job) — the backend **source and IaC** (`backend/app/`, `Caddyfile`, `render.yaml`) must never reference the dead `api.torrentcdn.store` host or apex-only `torrentcdn.store` URLs. A stale apex entry in the CORS allow-list is dead weight (the apex 308-redirects to `www`, so its `Origin` header never legitimately occurs — `backend/app/main.py` and `render.yaml` now list only the canonical `www` origin); the welcome-email template likewise links to `www`. Bare words (`noreply@torrentcdn.store` from-addresses, `.torrentcdn.store` in `TRUSTED_HOSTS`) are fine — only URLs fail; documented `torrentcdn.com` removal notes pass because that dead TLD doesn't match the patterns. **A denylist file** (`backend/scripts/check_domains.denylist`) additionally fails on any occurrence of dead/placeholder hosts (`storage.example.com`, `proj.supabase.co`, `minio.example`, `example.org`, …) — one host per line, `#` comments, matched as literal substrings. The **live** Render backend origin `torrent-cdn.onrender.com` (the Vercel proxy rewrite target, health-checked 200) is deliberately NOT denylisted.

CI keeps the frontend fast: npm packages are cached via `setup-node`'s `cache: npm`, and Next.js' persistent build cache (`.next/cache`) is cached between runs (`actions/cache`) so warm builds skip recompiling unchanged modules (~2× faster Build step).

---

## Hardening Log

A chronological record of security hardening work. Each entry lists what was fixed, the severity, and how it was verified — so a future reviewer can tell at a glance whether a finding was already addressed and which tests/commits prove it.

### 2026-08-04 — Full audit hardening pass (`78eff90`)

A systematic security audit (OWASP-style, zero-trust assumptions) produced the fixes below. Verified by 221 backend tests (23 new regression tests in `backend/tests/test_audit_hardening.py`), 53/53 frontend vitest tests, clean `tsc`, a green production build, and green embed/auto-seed/P2P E2E suites; `pip-audit` clean.

#### Critical / High

| Finding | Impact | Fix |
|---|---|---|
| **Next.js 14.2.0 advisory batch** — SSRF in Server Actions, cache-confusion ×3, unbounded Server Action payload, Server Function endpoint disclosure (1 critical + 5 high, June-2026 batch) | SSRF / cache poisoning / DoS | Bumped `next` 14.2.0 → **14.2.35** (closes that advisory batch) + `postcss` 8.5.24; `poweredByHeader: false` (no `X-Powered-By` fingerprint). *Subsequently superseded by the Next 15 migration — see `aceaa97` below.* |
| **`passlib` 1.7.4** — unmaintained since 2020, breaks with modern bcrypt | Unmaintained password hashing; supply-chain risk | Replaced with **direct `bcrypt` 4.x** (cost 12, `$2b$` format preserved — existing hashes still verify) |
| **`python-jose` → `ecdsa`** — `PYSEC-2026-1325`, **no patched release** | Known vulnerable transitive dependency | Migrated to **PyJWT 2.x** (maintained); HS256-only with an explicit `algorithms` list (blocks algorithm-confusion attacks); removes RSA/ECDSA transitive deps entirely |

#### Medium

| Finding | Impact | Fix |
|---|---|---|
| **No input length caps** (email / password / token / filename / ref_code) | Huge emails poisoned Redis rate-limit keys; 2 GB passwords were a memory DoS; >255-char emails/filenames 500'd the `VARCHAR(255)` inserts | Caps added in the Pydantic models: email 255, password 72, token 2048, filename 255, ref_code 20 |
| **bcrypt 72-byte silent truncation** | Two distinct long passwords could hash identically (multibyte UTF-8 ≠ 72 chars) | 72-**byte** limit enforced at register/reset with a clear 400 — a valid-length-but-over-bytes password no longer silently truncates or collides |
| **Login timing-oracle (user enumeration)** | Attacker distinguishes registered emails by response time (no bcrypt work on unknown email) | Unknown-email logins now run a **real bcrypt verify against a fixed dummy hash**, equalizing timing |
| **Unbounded in-memory rate-limit stores** | Rotated/forged `X-Forwarded-For` could grow memory without bound | Opportunistic pruning in the global middleware + login/forgot/resend/public-file/analytics/upload fallback stores (bounded by live-IP count) |
| **Dead CORS origins** (`torrentcdn.com` / `api.torrentcdn.com` — domain no longer owned) | Re-registering the domain would grant credentialed cross-origin reads (`allow_credentials=True`) | Origins removed from the CORS allow-list; env-supplied origins still pass through `CORS_ORIGINS` |
| **CSP-report log injection** | Attacker-controlled `blocked_uri`/`document_uri` could forge log lines | Report fields sanitized (control chars stripped, length-capped) before logging |

#### Low / Functionality

| Finding | Impact | Fix |
|---|---|---|
| **`docker-compose` exposed ports on `0.0.0.0`** | LAN-visible Postgres/Redis/MinIO with default creds | All three bound to `127.0.0.1` |
| **`sdk.js` used removed `file.renderTo`** (webtorrent 2.x) | SDK embeds silently lost P2P | Replaced with MediaSource streaming so SDK embeds keep working P2P |

#### Already solid (verified, no changes needed)

- **SQL injection:** all queries parameterized (asyncpg `$1`), zero string concatenation.
- **Uploads:** magic-byte + content-type validation, blocked signatures (ZIP/ELF/PE/tar…), compression-bomb check, sanitized filenames, UUID object keys, server-side size enforcement, per-IP upload caps, idempotent presign/chunk/finalize.
- **Auth:** bcrypt hashing, fail-closed JWT secret (>32 chars, `changeme` rejected), per-IP+email login lockout, anti-enumeration forgot-password, one-time reset tokens (Redis tombstone), admin bootstrap gated by env, admin-only routes server-verified.
- **Ownership:** every file/analytics/notification query scoped by `user_id` (no IDOR).
- **Headers:** nonce-CSP with `strict-dynamic`, HSTS, X-Frame-Options, CORP, Permissions-Policy; API docs disabled in production; `security.txt` present.

### 2026-08-04 — Next.js 15 migration (`aceaa97`) + chunk-upload rate-limit fix

- **Next 14.2.35 → 15.5.21, React 19.2.x, recharts 3.10.1, lucide-react 1.28.0** — clears the **July-2026 Next.js advisory batch** (see Accepted Residual Risk #1 below), which had **no 14.x patch** (requires 15.5.21+ / 16.2.11+). Async route params migrated (`useParams()`), React 19 `useRef` fixes, recharts 3 formatter types; `npm` overrides force `postcss`/`sharp` out of vulnerable ranges. `npm audit` now reports only the accepted `ip`-via-webtorrent advisories.
- **Global rate limiter vs. chunked uploads:** the global per-IP limiter counted every chunk PUT, 429-ing any multi-chunk file mid-upload. `/upload/chunk/*` is now exempt from the **global** limiter (route-layer protections remain: JWT auth, session ownership, index bounds, size cap, plus the per-IP upload limiter + usage gate at finalize; session **creation** stays rate-limited). Regression test: `test_global_middleware_exempts_chunk_uploads`.
- Verified: 222 backend tests, 53 frontend tests, **5/5 E2E suites against production** (direct, fallback, embed, auto-seed, 2-peer P2P), all 7 CI jobs green (incl. real 2-peer `p2p-e2e`).

### 2026-08-04 — Scheduled dependency-audit job + baseline-file gate (`0c2fca7`, `e3308e7`, `664175d`)

Supply-chain advisories are now caught **automatically**, not just during manual audits:

- **New `dependency-audit` CI job** (`.github/workflows/dependency-audit.yml`, `0c2fca7`): runs `pip-audit` (backend env) + `npm audit` (frontend lockfile) on a **daily 06:00 UTC cron**, on every PR, and via manual dispatch. It renders a combined markdown report (`backend/scripts/dependency_audit_report.py`), **posts it as a PR comment** (marker-based upsert — no comment stacking), writes it to the run summary, and uploads it as an artifact.
- **Baseline-file gate** (`e3308e7`): the job fails when advisories appear **beyond the accepted baseline** (`backend/scripts/dependency-audit-baseline.json`), not only on criticals. The baseline captures the current accepted risk set (the `ip`-via-webtorrent highs, `new=0`); anything new shows a "New since last baseline" section in the report/comment and turns CI red. The critical gate remains as a second, always-on check. Baseline identity rules: pip keys on advisory-ID, npm on package+severity (so a severity escalation is also caught). Regenerate the baseline when accepting a risk with `--write-baseline` (see the callout under Pre-merge CI Gates).
- **Unit tests** (`664175d`, `backend/tests/test_dependency_audit.py`): 17 tests covering finding normalization, identity rules, baseline save/load round-trip, new-finding detection (new npm package / second pip advisory / npm severity escalation), fix-not-new, and `main()` exit codes.
- Verified: 239 backend tests, the workflow ran green end-to-end on a real GitHub Actions `workflow_dispatch`, and a real test PR confirmed the comment posts and updates in place across pushes.

### 2026-08-04 — Second audit pass (round 2): upload/file/auth/billing/infra hardening

A follow-up audit pass (zero-trust checklist: dead code, session lifecycle, payment bypass, DoS, unicode, logging, infra-as-code) produced the fixes below. Verified by **267 backend tests** (28 new regression tests in `backend/tests/test_audit_pass2.py`), 53 frontend tests, clean `tsc`, and a passing `security_audit.py`.

#### High

| Finding | Impact | Fix |
|---|---|---|
| **`DELETE /files/{id}` referenced an undefined `settings` module global** — the `NameError` was swallowed by the storage-deletion `try/except`, so the DB row was removed but the object stayed in the public bucket forever | Deleted files remained publicly downloadable (data-retention failure); storage orphans accumulated | Added the missing `get_settings()` import; the storage object is now actually deleted (key = last 3 URL segments, matching the account-deletion path). Regression tests assert `delete_object` is called with the correct key for both Supabase and path-style MinIO URLs. |

#### Medium

| Finding | Impact | Fix |
|---|---|---|
| **`POST /analytics/event` (public, unauthenticated) had no rate-limit fallback when Redis is down** | A Redis outage opened an unbounded write path into the analytics table | In-memory bounded fallback (60/min/IP) mirroring the GET-endpoint pattern. Regression tests: `TestAnalyticsEventRateLimitFallback`. |
| **`POST /auth/register` had no per-IP throttle** | Free account creation spam + transactional-email quota burning (every registration sends a verification email) | Per-IP register limiter (10/hour, Redis primary + bounded in-memory fallback) before any work. Tests: `TestRegisterRateLimit`. |
| **Password change did not invalidate existing JWTs** | A stolen token survived a password reset for up to 24h | Tokens now carry an `iat` claim; the reset route records `pw_change:{user_id}` in Redis; `get_current_user_id` rejects any token issued before the marker (fail-open when Redis is down, consistent with the app's optional-Redis convention). Admin `toggle-admin` also bumps the marker so privilege changes rotate the target's sessions. Tests: `TestSessionRevocationOnPasswordChange`. |
| **Stripe webhook granted plans on unpaid sessions** | A `checkout.session.completed` with `payment_status != paid` (async payment methods) upgraded the plan for free | Webhook now requires `payment_status == "paid"` **and** `mode == "subscription"`, and never 500s on malformed `user_id` metadata (bad UUID previously triggered Stripe retries). Tests: `TestStripeWebhookPaymentGate`. |
| **No pagination on `GET /files`** | One user with a huge library forced an arbitrarily large response | `limit` (default 100, max 500) + `offset` query params; response shape unchanged (bare array, frontend-compatible). Tests: `TestListFilesPagination`. |
| **Unbounded concurrent chunk/presign sessions per user** | An authenticated attacker could exhaust temp disk (each chunk session grows a ≤1GB file) and Redis/in-memory session storage | Per-user caps: 50 growable chunk sessions, 50 presign sessions. The chunk cap counts only sessions that can still grow (a fully-received session can't write more), so legitimate power users aren't falsely 429'd. **Also:** chunk writes are now size-bounded to the declared `file_size` — a session declaring a small size with a large `total_chunks` could previously grow a temp file far beyond the plan limit. Tests: `TestSessionCaps` (+ size-guard tests). |

#### Low / Infra

| Finding | Impact | Fix |
|---|---|---|
| **Email templates interpolated the user-controlled email address unescaped** | A crafted email address (e.g. containing HTML) could inject markup into the email the user receives | `html.escape(email)` in both reset and verification templates. Tests: `TestEmailHtmlEscaping`. |
| **Emails stored in arbitrary Unicode normalization forms** | Confusable-account vector (composed vs decomposed forms as distinct accounts) | NFC-normalize at the API boundary; login/forgot look up the raw form first (pre-existing accounts) then the NFC form (new accounts) — no lockout. Tests: `TestEmailNfcNormalization`. |
| **Backend container ran as root** | A container compromise handed the attacker root | Non-root `appuser` (uid 1000, shell `/bin/false`) owns `/app` + `/tmp`; verified in `python:3.11-slim`. |
| **`minio/minio:latest` unpinned in docker-compose** | Supply-chain drift / silent breaking changes | Pinned to `RELEASE.2025-09-07T16-13-09Z`. |

#### Verified clean (no changes needed)

- **Mass assignment / prototype pollution:** Pydantic models whitelist fields; no `eval`/`pickle`/`yaml.load`/`Object.assign` on user input anywhere.
- **HTTP request smuggling / response splitting / cache poisoning:** uvicorn + Starlette handle framing; no user input reaches headers; no user-controlled cache keys.
- **ReDoS:** all regexes are bounded linear patterns (email format, filename sanitization).
- **Insecure redirects / host-header injection:** `TrustedHostMiddleware` (prod), redirect URLs built only from validated `APP_URL`.
- **Secrets hygiene:** no hardcoded credentials in source (gitleaks + secret_scan CI gates remain green).

### 2026-08-04 — Frontend zero-trust audit round (`fdffe94`)

A client-side audit pass (components, SDK, embed player, middleware, configs) under the same zero-trust assumptions as the backend rounds. No XSS sinks existed — React escapes everywhere, the only `dangerouslySetInnerHTML` is static JSON-LD with `<` escaped, and the vendored WebTorrent bundle is SRI-pinned — so the fixes focus on **validating attacker-influenceable boundary values** before they shape request URLs, redirect targets, or storage.

#### Low / Defense-in-depth

| Finding | Impact | Fix |
|---|---|---|
| **`/embed/{file_id}` accepted arbitrary ids** | A crafted `file_id` (e.g. `../../admin/stats`) reached the request URL — browser URL normalization could redirect the fetch to a different same-origin route (path confusion) | New `frontend/lib/security.ts` `isUuid()` gate: `VideoPlayer` rejects non-UUID ids before `api.getPublicFile` (the `demo` route still short-circuits) |
| **SDK trusted DOM attributes blindly** (`public/sdk.js`) | `data-torrent-cdn` (file id) and `data-api-url` arrive from the customer's page (attacker-influenceable) yet shaped XHR/sendBeacon destinations | Both validated at the boundary (UUID / absolute http(s) origin); invalid file ids render the error div instead of fetching; error rendering deduped into one helper (mirrors `lib/security.ts` — keep the two in sync) |
| **Checkout redirect accepted any server-returned URL** | `window.location.href = data.url` — a `javascript:`/`data:`/relative value would execute or mis-navigate | `isSafeHttpsUrl()` gate: only absolute `https://` checkout URLs are navigated; unsafe/missing URLs now error + reset loading instead of hanging |
| **`?ref=` referral param stored raw** | Arbitrary junk persisted to sessionStorage, then echoed into the register flow | `sanitizeRefCode()` — alphanumeric only, capped at the backend's 20-char `UPPER(referral_code)` lookup |
| **`target="_blank"` without `rel`** | Reverse-tabnabbing hygiene on the Terms/Privacy links | `rel="noopener noreferrer"` added |
| **CSP missing frame/form directives** | `X-Frame-Options` was the only frame protection; no `form-action` bound | `frame-ancestors 'self'` + `form-action 'self'` added to `frontend/middleware.ts` (embedding is SDK-based — no iframes or native cross-origin forms exist) |

**Tests** (`fdffe94` + `1ce4c87`): 13 guard cases in `frontend/lib/__tests__/security.test.ts` + a PricingTable unsafe-URL rejection case; `1ce4c87` added 5 `VideoPlayer` component tests (invalid / empty / demo / valid-success / fetch-failure) locking in the UUID gate and raised the vitest coverage thresholds from 25/33/14/32 → **42/45/30/48** (measured 46.7/50.7/35.4/54.7 once `security.ts` landed fully tested; `VideoPlayer.tsx` stays coverage-excluded as WebRTC glue). Verified: 71 frontend tests, clean `tsc`, all 7 CI jobs green (incl. the real 2-peer `p2p-e2e`).

### 2026-08-04 — Docs/FAQ/dashboard accuracy (`6dadd9d`)

Live-verified the public documentation claims against production and corrected three misleading items:

| Finding | Impact | Fix |
|---|---|---|
| **Docs `BASE_URL` pointed at a dead host** (`https://api.torrentcdn.store` — no DNS record, curl 000) | Every curl example in the API docs was broken | Pointed at the real same-origin Vercel proxy `https://www.torrentcdn.store` (live-probed: every documented endpoint answers through it), with a warning never to document a cross-origin API base (page CSP `connect-src` + CORS would block it); added a working "Get a token" curl example; embed snippets moved off the apex domain (which 308-redirects) to `www` |
| **Fake API-key credentials advertised** | The docs intro, landing FAQ, and dashboard all presented a derived `tcdn_…` string as an authenticating key — the backend is Bearer-JWT-only (verified: no `tcdn_` auth path), so the key 401'd on use | Docs/FAQ now document Bearer tokens from `/auth/login`; the dashboard's dead API Key box was removed |
| **Wrong privacy/provider claims** | FAQ said files lived in a "private Cloudflare R2 bucket" (storage is Supabase, deliberately public/unlisted) | Reworded to "stored in object storage and served from unlisted public URLs — links behave like keys" |

### 2026-08-04 — Real API-key auth (tcdn_) + dashboard key management

Implements the long-lived credentials that the `6dadd9d` docs-accuracy pass flagged as missing (the backend was Bearer-JWT-only and the old dashboard "API key" was a derived fake that 401'd). Supersedes that finding.

- **Key model:** one active key per user, format `tcdn_` + `secrets.token_urlsafe(32)` (~256 bits of entropy). The dashboard generates, copies-once, and revokes; generating again **rotates** (the old key dies immediately).
- **Hash-only storage:** only the SHA-256 hash is stored (`api_keys.key_hash`); the plaintext is returned exactly once at creation and can never be recovered — a DB leak exposes hashes, not usable keys. The dashboard shows a truncated prefix + age.
- **Validation in `get_current_user_id` / `get_optional_user_id`** (`backend/app/auth.py`): a `Bearer tcdn_…` credential is resolved by hash lookup (indexed equality, fail-closed on DB errors), then treated exactly like a JWT — same ownership-scoped endpoints, same rate limits. Keys intentionally survive password changes (they are long-lived and revoked explicitly); JWT `pw_change` revocation is untouched.
- **Endpoints:** `POST /auth/api-key` (generate/rotate), `GET /auth/api-key` (metadata only — never the secret), `DELETE /auth/api-key` (revoke). `api_keys` table (FK → users, `ON DELETE CASCADE` so account deletion cleans up) created in `run_startup_migrations` + lazily in the routes, matching the app's fresh-DB pattern.
- **Frontend:** dashboard API Key box restored with generate/copy-once/revoke (no client-side derivation of a fake key); docs + landing FAQ updated to document Bearer-token **or** API-key auth.
- Verified: **277 backend tests** (9 new in `backend/tests/test_api_keys.py` covering hash-only storage, rotation, revocation, 401 on unknown/revoked keys, and auth-required; +1 migration-order test), **74 frontend tests** (3 new api-client cases), clean `tsc`, passing `security_audit.py`.

### Accepted residual risks

Risk is accepted **only** when the fix would break the product or require a breaking change with no safe migration path. Both are continuously re-assessed.

#### 1. July-2026 Next.js advisories on 14.x — **RESOLVED by `aceaa97` (Next 15.5.21)**

The advisory database updated *after* the 14.2.35 fix shipped a new batch with **no 14.x patch** (SSRF class; requires 15.5.21+/16.2.11+). At the time it was **not exploitable in this app's configuration** (no Server Actions, no `next/image`, all rewrites use static destinations), and the long-term fix was the Next 15 major migration — which has since landed in `aceaa97`. Kept on this list for the historical record and because the migration must not be reverted.

#### 2. `ip` advisory (transitive via `webtorrent`) — **accepted**

`npm audit` reports `ip` (DoS via malformed IP string) as a transitive dependency of `webtorrent`. The only "fix" npm offers is downgrading `webtorrent` to 0.7.3, which would **destroy the entire P2P delivery feature** (the product's core differentiator). Exposure is minimal: it executes in the browser client (not the server), and the malformed-input path is not reachable from user-controlled surfaces in the embed player. Revisit if `webtorrent` upstreams a fix or drops the `ip` dependency.

---

## Production Deployment Checklist

### 1. Purge secrets from git history (one-time)

If a secret file (e.g. `.env.production`) was ever committed, untracking it is NOT enough — it stays in history.

```bash
# 1. Commit/stash any pending work, then create a safety backup
git clone --mirror . /tmp/torrent-cdn-backup.git

# 2. Install git-filter-repo (once)
pip install git-filter-repo        # or: brew install git-filter-repo / apt install git-filter-repo
# If pip is blocked (PEP 668 'externally managed'):
#   python3 -m venv /tmp/fr-venv && /tmp/fr-venv/bin/pip install git-filter-repo
#   and use /tmp/fr-venv/bin/git-filter-repo below

# 3. Remove the secret file(s) from ALL history
#    (run once per leaked path, or use --path with multiple paths)
git filter-repo --path .env.production --invert-paths --force

# 4. Verify nothing remains
git log --all --oneline -- .env.production          # empty output = purged
git rev-list --all --objects | grep -E '\.env'     # no .env* files left (except .env.example)

# 5. Re-add the remote WITHOUT any embedded token, then force-push (destructive — coordinate with the team)
git remote add origin https://github.com/<owner>/<repo>.git
git push --force --all origin
git push --force --tags origin
```

> ⚠️ **Secrets in history must still be rotated** — the purge removes the strings, but anyone who ever had repo access may have copied them. Rewriting history also invalidates all existing clones/forks; every collaborator must re-clone.

### 2. Rotate exposed secrets (treat as compromised)

| Secret | Where to rotate |
|--------|-----------------|
| `JWT_SECRET` | Render dashboard → new random value (≥32 chars). Old tokens invalid on restart. |
| `DATABASE_URL` password | Supabase/Postgres — reset the DB password, update env var |
| `MINIO_*` / storage keys | Supabase Storage / R2 — regenerate access keys |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Stripe dashboard → roll API keys |
| `RESEND_API_KEY` | Resend dashboard → revoke & recreate |
| GitHub tokens in remote URLs | Revoke at github.com/settings/tokens; use `gh auth login` or SSH instead of `https://user:TOKEN@github.com/...` |

### 3. Bootstrap the first admin (ADMIN_SETUP_ENABLED)

Admins are **never** auto-created. Bootstrap is a temporary, gated process:

```bash
# 1. Deploy with ADMIN_SETUP_ENABLED=true (Render env var)
# 2. Register your account via the app, then promote it.
#    Replace $API with your API base, e.g. https://torrent-cdn.onrender.com
curl -X POST "$API/auth/setup?email=you@example.com" \
  -H "Authorization: Bearer <your-jwt-token>"
# 3. REMOVE ADMIN_SETUP_ENABLED from Render immediately (back to unset/false)
# 4. Verify: GET $API/admin/users with your token returns 200
```

### 4. Required Render env vars

| Variable | Value | Notes |
|----------|-------|-------|
| `APP_ENV` | `production` | Enables HSTS, hardened CSP, email-verification-required |
| `APP_URL` | `https://www.torrentcdn.store` | Canonical frontend domain — drives email verify/reset links, CORS/CSP/embed URLs. Never `https://torrentcdn.com` (dead DNS) |
| `JWT_SECRET` | random ≥32 chars | Use `generateValue: true` or set manually |
| `DATABASE_URL` | Supabase pooled URL | Set in dashboard (never commit) |
| `REDIS_URL` | Upstash/Redis Cloud URL | Required for distributed rate limiting |
| `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`, `MINIO_BUCKET`, `MINIO_PUBLIC_URL`, `MINIO_SECURE=true` | Supabase Storage S3 values | Never `minioadmin` in prod |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_PRO`, `STRIPE_PRICE_BUSINESS` | Stripe keys/price IDs | |
| `RESEND_API_KEY`, `RESEND_FROM` | Resend API key + verified sender | |
| `CORS_ORIGINS` | Comma-separated frontend origins | |
| `TRUSTED_HOSTS` | `.onrender.com,.vercel.app,.torrentcdn.store,localhost` | |
| `CSP_REPORT_URL` | `/api/csp-report` | |
| `EMAIL_VERIFICATION_REQUIRED` | (unset in prod = true) | Set `false` only to disable |

### 5. Secrets hygiene rules

- All secrets come from env vars / secret managers. **Never** hardcode fallback values (see `docker-compose.yml` — JWT/DB/MinIO now fail closed with `${VAR:?}`).
- `.env*` files are gitignored (only `.env.example` is tracked).
- CI runs **Gitleaks** on every push/PR (`.github/workflows/security.yml`) — history and live files are scanned. A **daily 04:00 UTC cron** (plus a manual `workflow_dispatch` trigger) re-scans **full history** on the `main` branch — push-time gitleaks only sees the pushed range, so the daily job catches anything that slipped through an older commit (e.g. a secret committed before gitleaks existed). Gitleaks scans the whole repo on schedule/dispatch events; the `secret-scan` job runs its `--full` tree scan on those events too.
- CI also runs a **diff-scoped secret gate** (`backend/scripts/secret_scan.sh`, the `secret-scan` job): every push/PR is checked for high-signal secrets (GitHub PATs, AWS/Stripe/SendGrid/Resend keys, Slack/npm/PyPI/Google tokens, private keys, webhooks) in the changeset *and* the full tree, and the build fails with the offending file/line (secrets redacted in logs) before the expensive full-stack audit even starts. Run it locally anytime: `bash backend/scripts/secret_scan.sh` (diff vs `origin/main`) or `--full` (whole tree).
- Gitleaks also **uploads a SARIF report** to GitHub Code Scanning (the `Upload SARIF to Code Scanning` step in `static-audit`). Note: the Security tab only displays code-scanning results on **public** repos or private repos with GitHub Advanced Security — on a private personal-account repo the upload succeeds but findings aren't displayed (the build-failure + rotation flow remains the real defense).
- Rotate anything that touched a log, a chat, or a fork.

### 6. Post-deploy verification (run after every deploy)

Two commands confirm the deploy is correct and hardened — no dashboard login required after the one-time key creation below:

```bash
# 1. Which commit is actually live on Render? (read-only API key)
RENDER_API_KEY=rnd_... bash backend/scripts/check_deploy.sh          # compare against local HEAD
RENDER_API_KEY=rnd_... bash backend/scripts/check_deploy.sh <commit> # compare against a specific commit

# 2. Full security audit + frontend tests against production (pre-flight gate
#    + vitest/coverage gate + ~3 min paced audit)
bash backend/scripts/post_deploy_check.sh
```

- `check_deploy.sh` exits `0` on a commit match, `1` on mismatch/no live deploy, `2` on key/API error. Render's API is the source of truth — it does **not** write deploy statuses back to GitHub (the statuses you see are Vercel's).
- `post_deploy_check.sh` fast-fails (exit 1) if the hardened build isn't live yet (so you never audit a stale build), otherwise runs the **frontend unit-test gate** (`frontend/` — `npm run test:coverage`, the same vitest suite + coverage thresholds that gate PRs; `npm ci` auto-runs only when `node_modules` is missing — if tests behave oddly against stale deps, `rm -rf node_modules` first) and then the full ~3-minute audit, exiting `0` only when **every** check passes — security headers, CSP content, host-header protection, auth enforcement, email-verification gate, **API-docs gating (`/api/openapi.json` → 404)**, SQLi/input validation, CORS, HTTP methods, the frontend nonce/SRI/proxy, **and frontend unit tests/coverage**.
- Frontend health is therefore verified **after** deploy too, not just before merge: the unit-test gate runs against the deployed commit's checkout, so a broken build that slipped past pre-merge CI (or a stale/partial deploy) is caught here. `--preflight-only` skips both the frontend tests and the full audit (it just confirms the hardened build is live).
- **Key creation (one-time):** Render dashboard → Account Settings → API Keys → Create API Key. A plain (read-only) key is sufficient — both scripts use only GET endpoints. Store it in the deployer's env / secrets manager; never commit it.
- Expected result after a healthy deploy: `✅ LIVE commit matches expected` from step 1 and a `0` exit with zero `❌` lines from step 2.

---

## Operations Runbook

What to expect on deploy, so the next deployer isn't surprised by startup log noise or a burst of 429s.

### 1. APP_URL startup guard (production)

`APP_URL` drives every user-facing link the backend emits — email verification, password reset, referral URLs, billing redirects, embed URLs, and the CSP `media-src`/`connect-src`. A stale value silently breaks email verification: the link points at a dead domain and users can never verify. `get_settings()` in `backend/app/config.py` therefore validates `APP_URL` at process startup **in production only**. It fails loudly (but never crashes — the process still boots) and applies the safest fix it can for the current process.

| Condition in production | Log level | What the backend does | Required action |
|---|---|---|---|
| Host is `localhost` / `127.0.0.1` | `ERROR` | Keeps booting with the broken value | Set `APP_URL` to the real frontend domain and redeploy |
| Host is `torrentcdn.com` / `www.torrentcdn.com` (dead DNS) | `WARNING` | Auto-overrides to `https://www.torrentcdn.store` **for this process only** | Update `APP_URL` in the Render dashboard to `https://www.torrentcdn.store` and redeploy so the fix is permanent |
| Missing scheme or hostname (e.g. empty, `torrentcdn.store`) | `ERROR` | Keeps booting — links are unresolvable | Set a full `https://` URL and redeploy |
| Scheme is `http://` | `WARNING` | Upgrades to `https://` for this process (HSTS would otherwise block the links in browsers) | Set an `https://` URL in the dashboard |
| `MINIO_SECRET_KEY` is `minioadmin` | `WARNING` | None (credential hygiene) | Set a real secret |
| `STRIPE_SECRET_KEY` is a placeholder (`sk_test_xxx` / `sk_live_xxx`) | `WARNING` | None (credential hygiene) | Set a real Stripe key |

**Deploy check:** after a deploy, search the Render logs for the strings `APP_URL`, `minioadmin`, and `STRIPE_SECRET_KEY`. An `ERROR`/`WARNING` that mentions `APP_URL` means the deployed instance is running on a **corrected-but-temporary** value — the fix only becomes permanent once the dashboard env var is updated and the service is redeployed.

> The guard is a safety net, not a substitute for correct config: both `render.yaml` and the Render dashboard must point `APP_URL` at `https://www.torrentcdn.store` (the canonical frontend domain — `torrentcdn.com` no longer resolves).

### 2. Rate limiting behind the Vercel proxy (X-Forwarded-For)

Vercel proxies every `/api/*` request, and its edge IPs **vary per request**. Keying rate limits on the direct peer would make limits silently dead (or worse, hit innocent users at random). All rate limiters therefore key on the **real client IP** via `get_client_ip()` in `backend/app/security.py`:

- Uses the **first `X-Forwarded-For` entry** — the client IP appended by Vercel.
- Validates with `ipaddress.ip_address` (forged junk like `not-an-ip<script>` can't poison Redis keys), strips IPv4 ports (`1.2.3.4:8080` → `1.2.3.4`), preserves IPv6.
- Falls back to the direct peer address, then `"unknown"`, if the header is absent or malformed.

| Limiter | Where | Limit |
|---|---|---|
| Global | `backend/app/main.py` (`RateLimitMiddleware`) | 30 req / 10s per IP — raised from 5 after the dashboard's 6 concurrent mount calls self-tripped every page load (static/no-state endpoints exempt: `/health`, `/csp-report`, `/docs`, `/redoc`, `/openapi.json`, `/.well-known/security.txt`) |
| Login lockout | `backend/app/routes/auth.py` | 5 failed attempts / 15 min per **IP+email** (reset on success) |
| Forgot password | `backend/app/routes/auth.py` | 1 request / 60s per IP |
| Registration | `backend/app/routes/auth.py` | 10 / hour per IP |
| CSP reports | `backend/app/routes/csp.py` | 100 / min per IP |
| Public file | `backend/app/routes/files.py` | 30 / min per IP |
| Analytics | `backend/app/routes/analytics.py` | 30 / min per IP (POST public; GET auth + ownership-scoped) |
| Upload | `backend/app/routes/upload.py` | 10 / hour per IP (counts uploads that reach storage — validation/usage rejections don't consume the budget; tune `RATE_LIMIT_UPLOADS` upward if shared egress IPs/NAT are a concern) |

**Expected behavior on deploy:** rate limits **tighten immediately** — limits that never fired behind Vercel's varying edge IPs now fire as designed. A temporary rise in 429s right after a deploy is the fix working, not a regression. No Redis migration needed; old keys expire harmlessly.

**Verifying the fix is live:** three rapid `POST /auth/forgot-password` calls from a fresh IP should return `200 → 429 → 429` (first passes, the next two blocked in the same 60s window). A mixed `200/429/200` pattern means the old peer-keyed code is still serving — wait for the deploy to swap and re-probe.

**Known tradeoff:** clients that reach the backend *directly* (bypassing Vercel) can forge `X-Forwarded-For` to rotate past these limits. This is accepted and documented in the `get_client_ip()` docstring — the public surface is the Vercel domain, which strips/replaces client-supplied XFF.

### 3. Clock-skew guard (production)

JWT `exp` (email verify 24h, password reset 1h) is minted and validated against the **local clock** — a badly drifted instance silently shortens or lengthens token lifetimes. At startup (non-blocking, fail-open) `check_clock_skew()` in `backend/app/config.py` compares the app clock against public NTP-over-HTTP references (Cloudflare `/cdn-cgi/trace`, Google `Date` header, worldtimeapi).

| Condition | Log line |
|---|---|
| Skew ≤ `CLOCK_SKEW_MAX_MINUTES` (default 5) | `INFO` — "Clock skew check OK: app clock within Ns of real time" |
| Skew > threshold | `ERROR` — "CLOCK SKEW: app clock is N seconds off real time …" — check instance time / NTP sync |
| No reference reachable / fetch error | `WARNING` — "Clock skew check skipped …" (fail-open, app still boots) |

**Deploy check:** a healthy instance logs the `INFO` "Clock skew check OK" line at startup; an `ERROR` with `CLOCK SKEW` means the instance clock needs fixing before token lifetimes can be trusted.

---

## Verification

### Quick header check

```bash
curl -sI http://localhost:8000/health | grep -i -E "(policy|security|options|frame|referrer|xss|permissions|endpoints|transport)"
```

### Production CSP test

```bash
APP_ENV=production curl -sI http://localhost:8000/health | grep -i content-security-policy
# Should NOT contain localhost:9000
```

### Full audit

```bash
# Verify all headers are present using the security audit script
python backend/scripts/security_audit.py
```

### Verify the live Render deploy commit (no dashboard needed)

`backend/scripts/check_deploy.sh` confirms which commit is **live** on Render via the read-only REST API — no dashboard login required.

```bash
# Create a read-only API key: Render dashboard → Account Settings → API Keys → Create API Key
RENDER_API_KEY=rnd_... bash backend/scripts/check_deploy.sh          # compare against local HEAD
RENDER_API_KEY=rnd_... bash backend/scripts/check_deploy.sh 7c71b03  # compare against a specific commit
```

It finds the service (default `torrent-cdn` — the live Render service name; override with `SERVICE_NAME=`), reads the newest `status: "live"` deploy, prints its commit id/message/timestamp, and exits `0` on a match. Only GET endpoints are used, so a read-only key is sufficient. Exit codes: `0` = match, `1` = mismatch/no live deploy, `2` = usage/API/key error.

> **Why not the GitHub deployments API?** Render does not write deploy statuses back to GitHub for this service (the commit statuses you see are Vercel's, for the frontend). The Render API is the authoritative source for the backend's live commit.

> **Run it as step 1 of the post-deploy checklist** (section 6 above), then `post_deploy_check.sh` for the full security audit.
