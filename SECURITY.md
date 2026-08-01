# Security Configuration

This document describes the security header configuration for the TorrentCDN API backend.

## Overview

All security headers are set by `CSPMiddleware` in `backend/app/main.py`. The middleware runs on every response and sets 7 security headers (8 in production). Additionally, middleware-level protections are configured for CORS, trusted hosts, and request body size.

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
| `connect-src` | `'self' wss://tracker.openwebtorrent.com wss://tracker.btorrent.xyz wss://tracker.fastcast.nz http://localhost:9000 <APP_URL>` | `'self' wss://tracker.openwebtorrent.com wss://tracker.btorrent.xyz wss://tracker.fastcast.nz <APP_URL>` |
| `frame-src` | `'self'` | `'self'` |
| `worker-src` | `'self' blob:` | `'self' blob:` |
| `base-uri` | `'self'` | `'self'` |
| `report-uri` | `/api/csp-report` | `/api/csp-report` |
| `report-to` | `csp-endpoint` | `csp-endpoint` |

**Script policy:** `script-src 'self'` — no inline scripts or third-party script CDNs. WebTorrent is self-hosted and pinned at `/vendor/webtorrent.min.js` with SRI. The **frontend** (Next.js) additionally enforces a nonce-based CSP via `frontend/middleware.ts` (which requires `export const dynamic = 'force-dynamic'` in the root layout).

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
- CI runs **Gitleaks** on every push/PR (`.github/workflows/security.yml`) — history and live files are scanned.
- Rotate anything that touched a log, a chat, or a fork.

### 6. Post-deploy verification (run after every deploy)

Two commands confirm the deploy is correct and hardened — no dashboard login required after the one-time key creation below:

```bash
# 1. Which commit is actually live on Render? (read-only API key)
RENDER_API_KEY=rnd_... bash backend/scripts/check_deploy.sh          # compare against local HEAD
RENDER_API_KEY=rnd_... bash backend/scripts/check_deploy.sh <commit> # compare against a specific commit

# 2. Full security audit against production (pre-flight gate + ~3 min paced audit)
bash backend/scripts/post_deploy_check.sh
```

- `check_deploy.sh` exits `0` on a commit match, `1` on mismatch/no live deploy, `2` on key/API error. Render's API is the source of truth — it does **not** write deploy statuses back to GitHub (the statuses you see are Vercel's).
- `post_deploy_check.sh` fast-fails (exit 1) if the hardened build isn't live yet (so you never audit a stale build), otherwise runs the full ~3-minute audit and exits `0` only when **every** check passes — security headers, CSP content, host-header protection, auth enforcement, email-verification gate, SQLi/input validation, CORS, HTTP methods, and the frontend nonce/SRI/proxy.
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
| Global | `backend/app/main.py` (`RateLimitMiddleware`) | 5 req / 10s per IP (`/health` and `/csp-report` exempt) |
| Login lockout | `backend/app/routes/auth.py` | 5 failed attempts / 15 min per **IP+email** (reset on success) |
| Forgot password | `backend/app/routes/auth.py` | 1 request / 60s per IP |
| CSP reports | `backend/app/routes/csp.py` | 100 / min per IP |
| Public file | `backend/app/routes/files.py` | 30 / min per IP |
| Analytics | `backend/app/routes/analytics.py` | 30 / min per IP (post + get) |

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
