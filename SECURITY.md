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
| `connect-src` | `'self' wss://tracker.webtorrent.dev wss://tracker.openwebtorrent.com http://localhost:9000 <APP_URL>` | `'self' wss://tracker.webtorrent.dev wss://tracker.openwebtorrent.com <APP_URL>` |
| `frame-src` | `'self'` | `'self'` |
| `worker-src` | `'self' blob:` | `'self' blob:` |
| `base-uri` | `'self'` | `'self'` |
| `report-uri` | `/api/csp-report` | `/api/csp-report` |
| `report-to` | `csp-endpoint` | `csp-endpoint` |

**Script policy:** `script-src 'self'` — no inline scripts or third-party script CDNs. WebTorrent is self-hosted and pinned at `/vendor/webtorrent.min.js` with SRI. The **frontend** (Next.js) additionally enforces a nonce-based CSP via `frontend/middleware.ts` (which requires `export const dynamic = 'force-dynamic'` in the root layout), and sends `X-Frame-Options: SAMEORIGIN` + `Cross-Origin-Opener-Policy: same-origin` via `next.config.js`. CORP/COEP are intentionally **not** set on the frontend: the embed SDK (`sdk.js` + `/vendor/webtorrent.min.js`) must stay loadable cross-origin from customers' pages, and COEP `require-corp` would break no-cors thumbnails served from the storage provider.

**Frontend inline scripts must use `next/script` with the nonce (hard rule):** any inline script that must run pre-paint (e.g. the theme-init script `THEME_SCRIPT` in `app/layout.tsx`) is rendered as `<Script id="..." strategy="beforeInteractive" nonce={nonce}>` — the nonce is minted by `frontend/middleware.ts` and read in the async layout via `await headers()`. A raw inline `<script>` tag gets no nonce, the CSP silently blocks it, and any CSS that depends on it (the theme anti-flash guard) then hides or breaks the page — this caused a production incident where the entire site rendered black until the fix shipped (9fce773). When adding inline behavior, prefer extracting a module; when pre-paint execution is unavoidable, use `next/script` + nonce and add a CSS/JS failsafe. `lib/__tests__/theme-parity.test.ts` pins the theme script's `<Script>` rendering so the pattern can't silently regress.

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
-  `camera`, `microphone` — no recording
-  `geolocation` — no location tracking
-  `payment` — no browser payment API
-  `usb`, `midi`, `serial` — no device access
-  `accelerometer`, `gyroscope`, `magnetometer` — no sensor data
-  `interest-cohort` — blocks Google's FLoC
-  `display-capture` — no screen sharing

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

## Account Recovery — MFA Reset (admin procedure)

Mandatory 2FA means a user who loses their authenticator app **and** every
single-use backup code can never pass the second-factor gate again — there is
no self-service escape hatch (by design: a password alone must never be able to
strip 2FA). Recovery is a **manual, admin-gated database operation**:

```sql
-- Only run after verifying account ownership (e.g. email control + payment
-- history / a support ticket). Wipes the TOTP secret + backup codes so the
-- user re-enrolls at their next sign-in (login answers needs_setup=true).
UPDATE users
SET mfa_enabled = FALSE, mfa_secret = NULL, mfa_backup_codes = NULL
WHERE id = '<user-uuid>';
```

- The user's next login goes through the mandatory-enrollment flow and mints a
  fresh secret + new backup codes.
- Pre-existing session tokens are untouched by this SQL (sessions are only
  rotated by the MFA enable/disable endpoints themselves); the account simply
  signs in again.
- Prefer resetting over deleting the account — the account keeps its files,
  plan, and referral history.

---

## Pre-merge CI Gates

Every push/PR to `main` runs `.github/workflows/security.yml` + `.github/workflows/dependency-audit.yml` — **all jobs must be green before merging** (the scheduled jobs also run on their daily crons; `p2p-e2e` is push/manual-dispatch only and `api-key-live` + `upload-live` are schedule/manual-dispatch only because they write to production). PRs get inline gitleaks comments pointing at the offending line when a secret is introduced; the build fails on anything below:

| Job | What it gates |
|---|---|
| `tests` | Backend unit suite (pytest, fully mocked — no infra) + **domain guard** (dead `api.torrentcdn.store` host / apex-only `torrentcdn.store` URLs in `backend/app`, `Caddyfile`, `render.yaml`) |
| `secret-scan` | Diff-scoped high-signal secret gate (`backend/scripts/secret_scan.sh`) + whole-tree scan — GitHub PATs, AWS/Stripe/SendGrid/Resend keys, private keys, webhooks |
| `static-audit` | Gitleaks secret scan (pushed-range on push, **full history** on the daily cron/dispatch; posts inline PR comments, uploads SARIF) + `security_audit.py` header/CSP/auth/API-docs-gating checks + **SQL-injection static audit** (`sql_injection_audit.py` — AST gate: every asyncpg call must pass plain-literal SQL with `$1`/`$2` placeholders; f-string/format/%/concat/variable SQL fails with file:line) |
| `frontend-checks` | **Frontend pipeline:** `npm ci` → `tsc --noEmit` → vitest unit tests **+ coverage gate** → `next build` → **OG image guard** + **API baseURL guard** + **domain guard** (dead `api.torrentcdn.store` host / apex-only `torrentcdn.store` URLs) |
| `fullstack-audit` | Docker stack (Postgres/Redis/MinIO) + `curl_audit.sh` end-to-end (push/PR only; runs even when the secret gates are red, so a leak PR keeps its e2e signal) |
| `p2p-e2e` | **Real 2-peer WebTorrent delivery gate** (push/manual only — writes throwaway accounts/files to production): builds the pushed frontend, serves it behind the same-origin proxy, and runs `e2e/p2p-two-peer.mjs` — seeder reproduces the backend infohash and seeds; leecher plays `/embed/{id}` with every `*.supabase.co` request BLOCKED (only WebRTC can deliver) → peers connect, video plays, `chunk_downloaded` with `bytes_p2p > 0` lands in analytics |
| `storage-publicity` | Credential-free live check that the Supabase bucket is still **public** (`check_storage_public.sh`): a private bucket 404s all playback, so a regression here breaks every `minio_url` |
| `api-key-live` | **Scheduled production probe** (daily 04:00 UTC cron + manual dispatch only — writes a throwaway account+key, then cleans up): runs `backend/scripts/api_key_audit_prod.sh` against the live deployment — register → generate `tcdn_` key (plaintext once) → key-authed `/files` + `/auth/me` + metadata (`api_key:null`) → no-auth 401 → revoke → **revoked key 401 (fail-closed)** → `DELETE /auth/me` |
| `upload-live` | **Scheduled production probe** (daily 04:00 UTC cron + manual dispatch only — writes a throwaway upload+account, then cleans up): runs `backend/scripts/upload_audit_prod.sh` — register → `/upload/presign` → **PUT bytes direct to the presigned Supabase URL** (never via Render/Vercel) → `/upload/presign/finalize` → real 40-hex btih magnet → file listed under `/files` → `DELETE /files/{id}` + `DELETE /auth/me` |
| `dependency-audit` | **Supply-chain gate** (daily 06:00 UTC cron + PR + manual): runs `pip-audit` + `npm audit`, posts the report as a PR comment, and fails on any advisory **beyond the accepted baseline** (`backend/scripts/dependency-audit-baseline.json`) or on any critical |

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

A chronological record of security hardening work.

### 2026-10-03 — Dependabot triage: undici + ip-address cleared, dev-only braces chain accepted

GitHub reported **11 Dependabot alerts** on `main` (2 high, 6 moderate, 3 low) — all npm transitive dependencies, none in the backend. Triage + resolution:

- **`undici` ×10 advisories (2 high, 5 medium, 3 low, all patched in 7.29.1)** — bumped 7.29.0 → 7.30.0 via in-range `npm audit fix`. Reachability: **none** — the only path into the tree is **jsdom** (vitest test tooling); undici is never bundled for the browser and never runs in production. Includes the two highs: TLS-cert-validation bypass via dropped connect options in BalancedPool (GHSA-w293-vg96-wgc3) and DoS via unrequested WebSocket subprotocol (GHSA-rfgv-xxqx-mfg5).
- **`ip-address` (GHSA-2vr4-cq9g-pvrc, medium ReDoS, patched in 10.5.1)** — bumped 10.4.0 → 10.7.3 via the same in-range fix. Reachability: low — it parses tracker-supplied IP strings inside `webtorrent → load-ip-set → ip-set` (runtime path, browser bundle), so a hostile tracker could in principle stall a peer's IP parsing; tracker URLs are configured by us, not by viewers.
- **`braces` chain (7 npm-audit findings, 1 advisory) — ACCEPTED, see Residual Risk #7 below.** `npm audit` (not Dependabot — its DB lagged at triage time) flags GHSA-vfj7-8cjw-p6xm whose range is now `<= 3.0.3`: every published braces release, **no patched version exists**; npm's only proposed fix is the breaking Tailwind v4 migration. All 7 packages (braces/chokidar/micromatch/fast-glob/find-yarn-workspace-root/patch-package/tailwindcss) are dev-only build tooling.
- **Follow-up (same day): 2 new npm MODERATE advisories joined the same cluster — `postcss-selector-parser` (GHSA-rj75-hqrm-r3gf, range `< 7.1.6`, whole 6.x unpatched, Tailwind 3 pins 6.1.4) and its dependent `postcss-nested`. Same dev-only acceptance — Residual Risk #7 extended to `npm=9`.** The 3 repairable advisories that surfaced alongside (`sharp` <0.35.5, `source-map-js` <1.2.2 — plus `undici`/`ip-address` above) were all fixed in-range.
- The same `npm audit fix` also refreshed **axios 1.19.0 → 1.20.0** in-range (not advisory-driven).
- Baseline regenerated with **pip-audit installed locally** (the earlier npm-only regen had silently dropped the pip set — pip was missing from `backend/.venv`): accepted set is now `pip=19` (aiohttp/gitpython set shrank by one) + `npm=9`. `dependency_audit_report.py` exits OK with `new=0`.

Verified: **179/179 frontend tests pass**, `npm audit` reports nothing beyond the accepted baseline, Dependabot's 11 alerts auto-resolve on the next lockfile re-scan.

### 2026-08-08 — Full-scope security audit: admin-bootstrap token + infra hardening

Full attack-surface re-audit (auth, authorization/IDOR, input validation, XSS, SSRF/CSRF, file security, API, business logic, rate limiting, headers/CORS, cloud/containers/CI-CD, secrets, dependencies, DoS, race conditions, privacy, logging). Baseline is strong — every DB call parameterized (SQL-injection gate + 459-test suite), HMAC-verified webhooks, DNS-pinned SSRF guard on import, magic-byte/blocked-signature/compression-bomb upload validation, nonce-CSP + `strict-dynamic`, JWT fail-closed config, per-IP lockouts with Redis + in-memory fallbacks, session revocation (DB `security_stamp` authoritative), refcount-aware storage deletion, loopback-bound datastores, non-root backend container, `secret-scan` + gitleaks clean, **npm audit 0 vulnerabilities**, pip baseline 0 critical/0 high (20 low accepted). Fixes from this pass:

- **[Medium] Admin bootstrap open door** — with `ADMIN_SETUP_ENABLED=true`, any authenticated user could promote themselves (or anyone) to admin; a flag left on after bootstrap is a standing vertical-privilege-escalation hole. New optional **`ADMIN_SETUP_TOKEN`**: when set, `POST /auth/setup` requires it in the **`X-Admin-Setup-Token` header** (constant-time `hmac.compare_digest`; header transport keeps the secret out of access logs, which capture request URIs verbatim). Config logs an ERROR at startup in production when the flag is on without a token. Existing no-token behavior is unchanged when unset (backwards compatible). Tests: 3 new in `TestAuthSetupGating` (missing header → 403 no-promotion, wrong token → 403, correct token → 200) + 2 config fail-loud tests.
- **[Low] Backend API bound to `0.0.0.0` in docker-compose** — the dev API (no TLS) was LAN-reachable on :8000 while the datastores were loopback-only. Bound to `127.0.0.1` like the rest.
- **[Low] Frontend container ran as root + `npm install`** — `frontend/Dockerfile` now runs as the image's `node` user (least privilege) and uses `npm ci` (lockfile-exact, no silent dependency drift); `frontend/vercel.json` `installCommand` → `npm ci`.
- **[Low] Mutable base-image tags** — patch-pinned for reproducible builds: `python:3.11.9-slim`, `node:20.18.0-alpine`, `postgres:16.4-alpine`, `redis:7.4.2-alpine`, `caddy:2.8.4-alpine` (MinIO was already pinned).

Verified: **464 backend tests** (5 new), secret scan clean, scanner green.

### 2026-08-08 — SQL-injection regression gate (AST static audit)

Full-tree re-read confirmed **zero SQL-injection vectors**: all **130** asyncpg call sites pass a plain string literal with `$1`/`$2` placeholders and values as separate arguments (the driver binds values — it never re-parses them as SQL); a scan found no f-string/`.format()`/`%`-format/concatenation SQL and no string-built queries. To keep it that way, the audit is now a **CI gate**:

- **`backend/scripts/sql_injection_audit.py`** — AST-based scanner (stdlib `ast`, no deps). **Gate A** flags any asyncpg SQL method call (`execute`/`fetch`/`fetchrow`/`fetchval`/`executemany`/`prepare`/`copy_*`) whose first argument isn't a provably-plain string literal — f-strings, `.format()`, `%`-format, `+` concatenation, and unverifiable variables all fail with `file:line`. Module-level constants (e.g. the `*_TABLE_DDL` in `database.py`) resolve through `from X import NAME` / `import X` chains and stay accepted. **Gate B** flags any f-string/format/`%`/concat expression anywhere whose value contains an unambiguous SQL marker (`SELECT `/`INSERT INTO`/`UPDATE `/`DELETE FROM`/`CREATE TABLE`...), catching a query assembled in a helper far from the call site. Exit 0 = clean.
- **`backend/tests/test_sql_injection.py`** — 3 regression tests: the whole tree must stay clean; the legitimate forms (DDL constants incl. relative-import resolution + `$N` placeholders) must pass; and **guard-the-guard** — the scanner itself must flag every dynamic-SQL vector (f-string, concatenation, `%`-format, `.format()`, local variable) so a weakened scanner can't slip through.
- **CI** — `static-audit` job in `security.yml` now runs `python scripts/sql_injection_audit.py` (next to `security_audit.py`); the pytest test additionally reds the `tests` job on any regression.

Verified: **459 backend tests** (3 new), scanner green on the real tree, and the poisoned-fixture run flags all 5 vector classes.

### 2026-08-08 — Paystack Transfers (admin payouts to M-PESA)

Implements the **Transfers API** (the payout half of the Paystack integration — receiving money lives in `billing.py`): a new admin-only surface (`/admin/payouts` page + `/billing/payouts/*` API) to create M-PESA recipients (`transferrecipient`, `mobile_money`/`MPESA`), initiate transfers (`transfer`, `source=balance`, amount KES→cents), fetch/refresh transfer status, deactivate recipients, and view the available balance. Webhook now also handles `transfer.success` / `transfer.failed` / `transfer.reversed`. Security posture:

- **Admin-only everywhere** — every payout endpoint is gated by `require_admin` (403 for regular users), matching `/admin/*`.
- **Only locally-created recipients can be paid** — `POST /billing/payouts` looks up the recipient in the local `payout_recipients` table first (404 otherwise); an admin can never hand-type a `recipient_code`.
- **Minimum amount enforced before any Paystack call** — KES 10 (Paystack's documented mobile-money floor, 1000 cents); amounts are converted KES→cents server-side (same smallest-unit convention as payments).
- **Transfer webhooks never grant plans** — the HMAC-SHA512 webhook treats `transfer.*` events as status updates + admin notification only; only `charge.success` with a configured `plan_code` can upgrade a plan (regression-tested: `TestTransferWebhooks::test_transfer_event_never_grants_plan`).
- **Money-motion audit trail** — every recipient/transfer row records `created_by` (admin UUID); initiations and deactivations are logged.

Verified: **464 backend tests** (20 new in `backend/tests/test_payouts.py` covering admin gating, KES→cents conversion, minimum-amount enforcement, unknown/inactive-recipient rejection, status refresh, and all three transfer webhooks), **108 frontend tests** (5 new api-client cases), clean `tsc`. Each entry lists what was fixed, the severity, and how it was verified — so a future reviewer can tell at a glance whether a finding was already addressed and which tests/commits prove it.

### 2026-08-07 — Cleared the 4 HIGH `ip`-via-webtorrent advisories (npm override → `@bybrave/ip2`)

The last 4 HIGH findings (`npm audit`: `webtorrent` → `torrent-discovery` → `bittorrent-tracker` → `ip`, all one advisory — **GHSA-2p57-rm9w-gvfp** / CVE-2024-29415, SSRF via improper `isPublic` categorization) are now **cleared with zero webtorrent changes**. `ip` has no patched release (2.0.1 is latest, all versions affected), so npm's only "fix" was a **downgrade to webtorrent 0.7.3** — a 2014-era version that would destroy the P2P feature, and even the real major (3.0.21) still pulls the same `ip` chain. Instead, `frontend/package.json` adds an npm `overrides` pin: `ip → npm:@bybrave/ip2@^3.0.0` (maintained drop-in fork with CVE-2024-29415 fixed, same `lib/ip.js` API). Lockfile resolves `ip@npm:@bybrave/ip2@3.0.0` under `bittorrent-tracker`; the app never imports `ip` directly and never runs the tracker server that uses it, so this is risk-free. Baseline regenerated (`high=0 npm=0`). **Verified: `npm audit` 0 vulnerabilities, `tsc` clean, 103/103 frontend tests pass.**

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

- **SQL injection:** all queries parameterized (asyncpg `$1`), zero string concatenation — and now **enforced as a CI gate** (`scripts/sql_injection_audit.py` + `tests/test_sql_injection.py`): a non-parameterized query added anywhere reds both `static-audit` and `tests`.
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
| **Paystack webhook granted plans on failed/one-time charges** | A `charge.success` webhook for a failed charge or a one-time (non-subscription) payment upgraded the plan for free | Webhook now requires `data.status == "success"` **and** a `data.plan` object whose `plan_code` maps to a configured plan (server-side field — authoritative), and never 500s on malformed `user_id` metadata (bad UUID previously triggered Paystack retries). Tests: `TestPaystackWebhookPaymentGate`. |
| **No pagination on `GET /files`** | One user with a huge library forced an arbitrarily large response | `limit` (default 100, max 500) + `offset` query params; response shape unchanged (bare array, frontend-compatible). Tests: `TestListFilesPagination`. |
| **Unbounded concurrent chunk/presign sessions per user** | An authenticated attacker could exhaust temp disk (each chunk session grows a ≤1GB file) and Redis/in-memory session storage | Per-user caps: 50 growable chunk sessions, 50 presign sessions. The chunk cap counts only sessions that can still grow (a fully-received session can't write more), so legitimate power users aren't falsely 429'd. **Also:** chunk writes are now size-bounded to the declared `file_size` — a session declaring a small size with a large `total_chunks` could previously grow a temp file far beyond the plan limit. Tests: `TestSessionCaps` (+ size-guard tests). |

#### Low / Infra

| Finding | Impact | Fix |
|---|---|---|
| **Email templates interpolated the user-controlled email address unescaped** | A crafted email address (e.g. containing HTML) could inject markup into the email the user receives | `html.escape(email)` in both reset and verification templates. Tests: `TestEmailHtmlEscaping`. |
| **Emails stored in arbitrary Unicode normalization forms** | Confusable-account vector (composed vs decomposed forms as distinct accounts) | NFC-normalize at the API boundary; login/forgot look up the raw form first (pre-existing accounts) then the NFC form (new accounts) — no lockout. Tests: `TestEmailNfcNormalization`. |
| **Backend container ran as root** | A container compromise handed the attacker root | Non-root `appuser` (uid 1000, shell `/bin/false`) owns `/app` + `/tmp`; verified in `python:3.11-slim`. |
| **`minio/minio:latest` unpinned in docker-compose** | Supply-chain drift / silent breaking changes | Pinned to `RELEASE.2025-09-07T16-13-09Z`. *Update 2026-10-07: MinIO deleted `minio/minio`/`minio/mc` from Docker Hub (2026-09-11) and closed quay anonymous pulls (2026-09-24); community edition is source-only. The compose stack now pins the maintained **`pgsty/silo:RELEASE.2026-09-16T00-00-00Z`** fork (same entrypoint contract, curl present for the healthcheck). Dev/CI only — production object storage is Supabase S3. |

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

### 2026-08-04 — Scheduled API-key live probe (`c471b09`) + upload live probe (`66fff59`)

The API-key and upload flows are now **automatically re-verified against the live deployment every day**, so a production regression (broken rotate, hash-only storage drift, fail-closed revocation, dead presign signature, bucket CORS blocking the direct PUT, torrent/magnet generation failure) turns CI red instead of surfacing as user reports:

- **New `api-key-live` CI job** (`.github/workflows/security.yml`, `c471b09`): runs `backend/scripts/api_key_audit_prod.sh` against production on the **daily 04:00 UTC cron + manual dispatch** — register throwaway account → generate `tcdn_` key (plaintext returned exactly once) → key-authed `/files` + `/auth/me` + metadata (`api_key: null`, never recoverable) → no-auth 401 → revoke → **revoked key 401 (fail-closed)** → `DELETE /auth/me` cleanup. The probe paces 2.2s under the 30 req/10s global limiter, retries auth POSTs once on Render cold starts (Vercel-proxy path, same as the browser), and cleans up its throwaway account on every path — so scheduled runs never accumulate test users.
- **New `upload-live` CI job** (`.github/workflows/security.yml`, `66fff59`): runs `backend/scripts/upload_audit_prod.sh` against production on the same cron — register → `POST /upload/presign` → **PUT bytes DIRECT to the presigned Supabase URL** (never via Render/Vercel — the whole point of the presign design) → finalize → real 40-hex btih magnet + `minio_url` → file listed under `/files` → `DELETE /files/{id}` + `DELETE /auth/me`. The probe synthesizes a minimal **real WebM** inline with python3 (EBML header + 2048 varied bytes) so it passes the backend's magic-byte/entropy/content-type validation without ffmpeg (keeping the CI job a single checkout+run step), and asserts both cleanup DELETE codes for real.
- **Gating rationale** (both jobs): schedule + manual dispatch ONLY — they **write to production** (throwaway uploads/accounts), so per-push runs would burn the 10/hr/IP register & upload budgets from GitHub's shared runner IPs, fork PRs could trigger unlimited production writes from untrusted code, and on push they'd validate the *previous* deploy anyway (Render/Vercel lag). A red cron run is the alert that the flow is broken live.
- Verified: workflow YAML parses, both probes pass live (**12/12** api-key, **13/13** upload), and a real `workflow_dispatch` confirmed both jobs run green on GitHub Actions — `api-key-live` and `upload-live` alongside `p2p-e2e` with no rate-limit collisions. Documented in the Pre-merge CI Gates table above.

### 2026-08-05 — Presign finalize lost-response retry (`7cf64d2`)

**Bug found by the live probe:** a real production cold-start bug. On a Render cold start, the first presign finalize can COMPLETE (session deleted, uploads row inserted with `id == upload_id`) while its response dies at the Vercel proxy with a transient 502. The client's auto-retry then 404'd with **"Upload session not found"** — the session was already consumed and the idempotency-key dedup was unreachable because the client never held the key. The browser's finalize auto-retry fails identically for real users on a cold-start finalize.

**Fix** (`backend/app/routes/upload.py` → `presign_finalize`): a session-less finalize is now answered from the **uploads row** (`SELECT … FROM uploads WHERE id = $1 AND user_id = $2` — the uploads row is keyed by `upload_id`):

- Row found → returns the stored upload (identical response shape to a fresh finalize), so the retry succeeds instead of 404ing.
- No row → genuine 404 as before; a non-UUID `upload_id` skips the query entirely (a malformed path param can never 500).
- **Authorization preserved:** the `user_id` filter means a caller can only read their OWN rows; UUIDv4 ids aren't enumerable and a user's own files are already visible via `GET /files` — no new oracle.
- **No double accounting:** the retry returns BEFORE the usage/rate-limit gates (the first finalize consumed them) — the same contract as the idempotency-key dedup.
- **No orphan-sweep interaction:** objects with an uploads row are never swept.

**Supporting probe hardening** (same stream): `req`/`post_proxy` retry transient 502s (`9aad42a`), registration is 429-aware with the two probe jobs serialized so they never register simultaneously (`fb3e7aa` — back-to-back dispatches had drained the 10/hr/IP register budget from GitHub's shared runner IPs), and probe results post to the GitHub run summary (`ef81ea7`). These made the cold-start bug surface reliably instead of a register-collision red.

**Verification:**

- 3 regression tests in `backend/tests/test_upload_presign.py`: lost-response retry → 200 with the stored row and NO storage round-trip; no row → 404; query args pinned to `(id, caller user_id)`. Full backend suite: **282 passed**.
- **Browser-proof** — new Playwright suite `frontend/e2e/finalize-retry.mjs` (`d49c4f2`), run against production: block the first finalize with a 502 → the client re-sends it → success screen with a real 40-hex magnet, no chunked fallback engaged. A lost-response mode (`route.fetch()` lets the finalize complete server-side, then 502s the browser) exercises the EXACT cold-start path — the retried finalize's 200 comes from the stored-row lookup. **4/4 runs green**.
- **Chunked-path equivalent** covered by a unit test (`d16605d`): the session-present retry returns the SAME `file_id` with exactly one insert / one storage write / one rate-limit draw. Docstring records the honest scope boundary — a session-GONE chunked retry (30-min TTL prune or Render restart; chunk sessions are in-memory only) still 404s, a known fallback-path limitation.

### 2026-08-05 — Zero-trust audit round 2: CSP tightening, no-store, audit logging (`a58ab8d`)

Full re-baseline against OWASP Top 10 / ASVS / CWE Top 25 (the "ultimate hardening" checklist). Baseline was already strong (282 tests, all static-header checks, secret scan clean, live header set excellent) — four genuine gaps found and fixed:

1. **Stale cross-origin API origin in the production CSP (Medium)** — the live `connect-src` baked in `https://torrent-cdn.onrender.com` because `NEXT_PUBLIC_API_URL` on the Vercel deploy was stale. `lib/api.ts` ignores that env var in production (same-origin proxy always; `check_api_baseurl.sh` enforces it), so the entry only widened the CSP. `frontend/middleware.ts` now allow-lists a cross-origin API origin **in dev only**; production denies by default — a stale/compromised env value can no longer widen `connect-src`.
2. **No `Cache-Control` on API responses (Medium)** — sensitive JSON (`/auth/me`, `/files`, `/billing`, `/admin/*`) could be served stale from a shared intermediary. `CSPMiddleware` (central, wraps every route) now stamps `Cache-Control: no-store` on all responses.
3. **Authentication events not logged (Low)** — brute-force/credential-stuffing was invisible until the lockout counter tripped. Failed logins log `WARNING` with email + client IP (never the password); successful logins log `INFO` with user id only.
4. **Admin actions not fully audited (Low)** — plan changes, toggle-admin, and user deletion had no audit trail. All four mutating admin routes (incl. verify, which lacked the actor) now log actor + target + outcome.

**Regression tests (+8):** `Cache-Control: no-store` on health/authed/admin endpoints; login audit-log records (failed: email+IP present, password absent; success: user id); admin audit-log records for toggle/plan/delete with actor + target.

**Verification:** backend **290 passed**, frontend **74 passed** + `tsc` clean, `security_audit.py` **all checks passed**, secret scan clean.

### 2026-08-05 — Residual-risk closure: TOTP MFA, opt-in malware scan, WAF + restore-drill tooling (`a089b7f`, review fixes `97eadee`)

Closed or converted four of the six accepted residual risks into working mitigations. A code review of the change set found two HIGH issues that were fixed in `97eadee` before merge: (1) an `mfa_pending` token could authenticate as a session token (same signing secret, no claim rejection) — `decode_access_token` now rejects the claim; (2) the VirusTotal submit path closed the caller's upload stream, 500-ing direct/chunked uploads — `_submit_file` now buffers file-like sources into a bounded `BytesIO` and never closes the caller's stream. Plus a LOW fix: successful MFA confirms reset the IP's failed-attempt counter (mirroring login). All three have regression tests.

1. **TOTP two-factor authentication (RFC 6238, stdlib-only)** — login now returns `mfa_required` + a 5-min MFA token for MFA-enabled accounts; `/auth/mfa/confirm` mints the real session token only after a valid TOTP or single-use backup code. Backup codes (10 × 10-char) are stored **hashed** and consumed atomically (a replayed code fails; concurrent use can't double-succeed). Enable/disable require a live code; both rotate sessions via the `pw_change` marker. Failed confirms are rate-limited 5/15min/IP. New columns `mfa_secret` / `mfa_enabled` / `mfa_backup_codes` (migration + DDL). Dashboard: QR (`qrcode`), secret + otpauth link copy, one-time backup-code display, admin-recommended nudge. AuthModal: 6-digit step after password.
2. **Opt-in malware scanning (VirusTotal, fail-open)** — `app/malware_scan.py` with streamed SHA-256 lookup, ≤32MB submission + polling, and fail-open on every error path; wired into both finalize paths (`_store_and_finalize` before storage write; `presign_finalize` before record insert, deleting the object on a malicious verdict). Gated by `VIRUSTOTAL_API_KEY` (render.yaml + .env.example).
3. **WAF/DDoS** — verified live the domain is directly on Vercel; `backend/scripts/check_waf.sh` reports the active edge layer and prints the Vercel-Firewall (no DNS change) / Cloudflare enablement steps.
4. **Backups** — `backend/scripts/RESTORE_RUNBOOK.md` (quarterly drill) + `backend/scripts/restore_drill.sh` (read-only pre-flight: tooling, connectivity, table counts, MFA-column schema sanity).

**`ip` advisory updated:** the current advisory is **GHSA-2p57-rm9w-gvfp** (SSRF via `isPublic`) with **no patched release** (2.0.1 is latest) — remains an accepted, browser-side-only risk; a future `ip` release can be pinned via npm `overrides` (bittorrent-tracker allows `^2.0.1`).

**Regression tests (+39):** `test_mfa.py` (RFC 6238 vector, window tolerance, login gate, backup-code atomic consumption + replay rejection, enable/disable, rate limit, token claim separation) and `test_malware_scan.py` (fail-open paths, verdict mapping, lookup→submit→poll, 422 wire-in with no bytes stored) — backend **326 passed**; AuthModal MFA-step component tests — frontend **77 passed** + `tsc` clean; security audit all checks passed; secret scan clean.

### 2026-08-05 — Streaming finalize hashing: zero-copy hasher (`3eb0e26`, `f156864`) + benchmark tooling (`8a338a6`)

Presign-finalize used to download the object from Supabase Storage and then let torrentool **re-read the whole file** a second time to SHA-1 its pieces. It now hashes the bytes **while they stream to disk** (`torrent_builder.hash_pieces_from_stream`), so the hashing overlaps the dominant network cost instead of extending finalize with a sequential second pass — same infohash, one less full-file read on every upload.

- **Streaming hash (`3eb0e26`)** — new `backend/app/torrent_builder.py` reproduces torrentool's info-dict bencode **byte-for-byte** (sorted keys: `length, name, piece length, pieces`; piece-length rule 32768/262144; trailing partial piece) and builds the magnet directly. `presign_finalize` now downloads + hashes in a single pass; the direct/chunked fallback path hashes while writing its temp file. Dead code removed (torrentool import, `existing_path` param, unused `urllib.parse`); thumbnails extracted to a shared `_generate_thumbnail`.
- **Zero-copy hasher (`f156864`)** — the benchmark surfaced that the first streaming draft was *slower* than torrentool in isolation (571 ms vs 325 ms for a 200 MiB file) because it copied each piece (`bytes()`) and memmoved the buffer per piece (`del`). Whole pieces are now digested straight from large chunks via `memoryview` (zero-copy, hashlib buffer protocol), with the buffered path only for piece-boundary straddles: **360–460 ms — parity with torrentool within noise**. Output is unchanged (same infohash).
- **Byte-identity pinned** — `TestStreamingTorrentBuilder` oracle tests compare the bencoded info bytes AND infohash against `Torrent.create_from` across every piece-length boundary (32768/32769, 262144/262145, 2.6 MB multi-piece) and reader chunk sizes (3 B / 4 KiB / 16 KiB). The `p2p-e2e` two-peer suite re-verifies live on every push: the browser seeder reproduces the backend's infohash from the magnet, peers connect, and `chunk_downloaded` reports `bytes_p2p > 0` (bytes served from the peer, not the origin).
- **Benchmark tooling (`8a338a6`)** — `backend/scripts/bench_finalize_hash.py` (generates a file, times both paths best-of-3 on a warm cache, asserts infohash equality, prints simulated wall-clocks) and `backend/scripts/perf-notes.md` (50/200/1000 MiB scaling table: 84/381/1712 ms streaming vs 80/323/1631 ms torrentool; the ~80 ms→~1.6 s wall-clock saving is the old sequential pass now hidden inside the download).

**Verification:** backend **348 passed** (incl. the 21 torrent/streaming tests), `security_audit.py` **all checks passed**, the benchmark asserts `infohash identical: True` at every size, and the `f156864` CI run **completed green** — the `p2p-e2e` job confirmed the deployed streaming hasher's magnet is reproduced byte-for-byte by the browser seeder and a real 2-peer swarm played the video over P2P.

### Accepted residual risks

Risk is accepted **only** when the fix would break the product or require a breaking change with no safe migration path. Both are continuously re-assessed.

#### 1. July-2026 Next.js advisories on 14.x — **RESOLVED by `aceaa97` (Next 15.5.21)**

The advisory database updated *after* the 14.2.35 fix shipped a new batch with **no 14.x patch** (SSRF class; requires 15.5.21+/16.2.11+). At the time it was **not exploitable in this app's configuration** (no Server Actions, no `next/image`, all rewrites use static destinations), and the long-term fix was the Next 15 major migration — which has since landed in `aceaa97`. Kept on this list for the historical record and because the migration must not be reverted.

#### 2. `ip` advisory (transitive via `webtorrent`) — **RESOLVED (npm override → `@bybrave/ip2`)**

`npm audit` reported `ip` (**GHSA-2p57-rm9w-gvfp**, SSRF via improper `isPublic` categorization — all published versions affected, latest is 2.0.1, no patched release). The only "fix" npm offered was downgrading `webtorrent` to 0.7.3, which would **destroy the entire P2P delivery feature** (the product's core differentiator). Resolved without touching webtorrent: `frontend/package.json` now overrides `ip` → **`@bybrave/ip2`** (a maintained drop-in fork with CVE-2024-29415 fixed, API-compatible `lib/ip.js`), so the lockfile resolves `ip@npm:@bybrave/ip2@3.0.0` under `bittorrent-tracker`. `npm audit` is back to **0 vulnerabilities**; all 4 HIGH findings cleared. Verified: `tsc` clean, **103/103 frontend tests pass**, `dependency_audit_report.py` reports `high=0 npm=0`. **Revisit:** drop the `ip` override if upstream `ip` ever publishes a patched release — the fork pin is itself a small supply-chain trust decision (documented here so a future dev doesn't strip it as unused).

#### 3. Admin MFA — **IMPLEMENTED (`a089b7f`, TOTP stdlib)**

TOTP two-factor authentication (RFC 6238, stdlib-only — no new dependency) is live for **all users**, with an admin-recommendation nudge in the dashboard: login issues a short-lived MFA token instead of a session token, `/auth/mfa/confirm` completes it with a TOTP or single-use backup code (backup codes are stored hashed and consumed atomically — replay fails), enable/disable require a live code, and every posture change rotates sessions (`pw_change` marker). Failed MFA attempts are rate-limited (5/15min/IP). MFA is **optional** (not forced) to avoid a lockout of the sole admin — a forced-enrollment migration remains a future option.

#### 4. Malware scanning of uploads — **magic bytes + opt-in VirusTotal (`a089b7f`)**

Uploads are validated by magic-byte signature, content-type, size, and compression-ratio checks (rejects executables and archive-bomb classes). Additionally, setting `VIRUSTOTAL_API_KEY` (Render env) activates a **fail-open** VirusTotal check at both finalize paths: streamed SHA-256 hash lookup, with ≤32MB file submission + ~30s polling for unknown hashes. A definitive malicious/suspicious verdict rejects the upload (422) and deletes the stored object; any scanner outage or missing key leaves uploads working exactly as before. Verification: 36 new unit tests, incl. the wire-in (malicious verdict → 422, bytes never stored).

#### 5. WAF / DDoS protection — **not deployed; Vercel Firewall is the no-DNS-change path**

App-level controls (rate limiting per endpoint, lockouts, body caps, request timeouts, TrustedHost) are in place; the domain is served **directly by Vercel** (verified live: `server: Vercel`, no `cf-ray`). Recommended: enable the **native Vercel Firewall** (Project → Firewall → Managed rules) — zero DNS changes. Cloudflare proxying (CNAME www → cname.vercel-dns.com with proxy ON, Full-strict TLS, OWASP managed rules) is the alternative. `backend/scripts/check_waf.sh` probes the live edge and reports which layer is active (run it after enabling).

#### 6. Backup encryption + restore drills — **provider-managed; runbook shipped**

Render's managed Postgres performs automated daily backups; encryption at rest and retention are provider-controlled. **`backend/scripts/RESTORE_RUNBOOK.md`** documents the quarterly drill (verify backup exists → download → restore into a scratch DB → verify row counts + MFA columns → clean up → log), and **`backend/scripts/restore_drill.sh`** runs the read-only pre-flight (tooling, connectivity, table counts, schema sanity). The encryption/restore *execution* still requires Render dashboard access — the quarterly drill is now a 30-minute checklist instead of an unimplemented recommendation.

#### 7. Tailwind 3 toolchain advisories (dev-only build tooling) — **ACCEPTED, no patched release on the pinned majors**

Two advisory clusters share one root cause: **Tailwind 3.4.19's own dependency graph**, which only `tailwindcss@4` (breaking major) would replace.

1. **`braces` glob chain — GHSA-vfj7-8cjw-p6xm (high, stack-exhaustion DoS via deeply nested globs):** range `<= 3.0.3` — **every published braces version is affected and no fix exists**. Flags 7 packages: `braces`, `chokidar`, `micromatch`, `fast-glob`, `find-yarn-workspace-root`, `patch-package`, `tailwindcss`.
2. **`postcss-selector-parser` — GHSA-rj75-hqrm-r3gf (moderate, quadratic-complexity CPU exhaustion parsing crafted selectors):** range `< 7.1.6` — the entire 6.x line is unpatched and Tailwind 3 pins 6.1.4 (directly and via `postcss-nested`). Tailwind only parses **this repo's own CSS at build time** — no attacker-controlled selector input exists anywhere. Flags: `postcss-selector-parser`, `postcss-nested`.

All 9 packages are **dev-only build tooling**: nothing ships in the browser bundle, and no attacker-controlled glob/selector input reaches them in production or CI. Accepted in the baseline (2026-10-03: `npm=7` for braces; extended to `npm=9` when the postcss cluster arrived). **Revisit when:** migrating to Tailwind v4 (its oxide toolchain drops this chain entirely), or patched releases ship. Note for future triage: Dependabot also lags npm audit here (initially reported only the braces chain) — new alerts for these packages can be dismissed referencing this entry.

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

> [WARN] **Secrets in history must still be rotated** — the purge removes the strings, but anyone who ever had repo access may have copied them. Rewriting history also invalidates all existing clones/forks; every collaborator must re-clone.

### 2. Rotate exposed secrets (treat as compromised)

| Secret | Where to rotate |
|--------|-----------------|
| `JWT_SECRET` | Render dashboard → new random value (≥32 chars). Old tokens invalid on restart. |
| `DATABASE_URL` password | Supabase/Postgres — reset the DB password, update env var |
| `MINIO_*` / storage keys | Supabase Storage / R2 — regenerate access keys |
| `PAYSTACK_SECRET_KEY` | Paystack dashboard → Settings → API Keys & Webhooks → roll key (same key signs webhooks) |
| `RESEND_API_KEY` | Resend dashboard → revoke & recreate |
| GitHub tokens in remote URLs | Revoke at github.com/settings/tokens; use `gh auth login` or SSH instead of `https://user:TOKEN@github.com/...` |

### 3. Bootstrap the first admin (ADMIN_SETUP_ENABLED)

Admins are **never** auto-created. Bootstrap is a temporary, gated process:

```bash
# 1. Deploy with ADMIN_SETUP_ENABLED=true + a random ADMIN_SETUP_TOKEN (Render env vars)
# 2. Register your account via the app, then promote it. The token travels in a
#    HEADER (never a query param — access logs capture URLs verbatim).
#    Replace $API with your API base, e.g. https://torrent-cdn.onrender.com
curl -X POST "$API/auth/setup?email=you@example.com" \
  -H "Authorization: Bearer <your-jwt-token>" \
  -H "X-Admin-Setup-Token: <your-ADMIN_SETUP_TOKEN>"
# 3. REMOVE ADMIN_SETUP_ENABLED and ADMIN_SETUP_TOKEN from Render immediately
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
| `PAYSTACK_SECRET_KEY`, `PAYSTACK_PLAN_PRO`, `PAYSTACK_PLAN_BUSINESS` | Paystack API key + plan codes (Kenya — Stripe unavailable to Kenyan merchants) | |
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
- Expected result after a healthy deploy: `[OK] LIVE commit matches expected` from step 1 and a `0` exit with zero `[FAIL]` lines from step 2.

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
| `PAYSTACK_SECRET_KEY` is a placeholder (`sk_test_xxx` / `sk_live_xxx`) | `WARNING` | None (credential hygiene) | Set a real Paystack key |

**Deploy check:** after a deploy, search the Render logs for the strings `APP_URL`, `minioadmin`, and `PAYSTACK_SECRET_KEY`. An `ERROR`/`WARNING` that mentions `APP_URL` means the deployed instance is running on a **corrected-but-temporary** value — the fix only becomes permanent once the dashboard env var is updated and the service is redeployed.

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
