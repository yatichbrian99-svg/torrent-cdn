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
| `script-src` | `'self' 'unsafe-inline' unpkg.com` | `'self' 'unsafe-inline' unpkg.com` |
| `style-src` | `'self' 'unsafe-inline' fonts.googleapis.com` | `'self' 'unsafe-inline' fonts.googleapis.com` |
| `font-src` | `'self' fonts.gstatic.com` | `'self' fonts.gstatic.com` |
| `img-src` | `'self' data: blob:` | `'self' data: blob:` |
| `media-src` | `'self' blob: http://localhost:9000 https://*.torrentcdn.com` | `'self' blob: https://*.torrentcdn.com` |
| `connect-src` | `'self' wss://tracker.openwebtorrent.com wss://tracker.btorrent.xyz wss://tracker.fastcast.nz http://localhost:9000 https://*.torrentcdn.com` | `'self' wss://tracker.openwebtorrent.com wss://tracker.btorrent.xyz wss://tracker.fastcast.nz https://*.torrentcdn.com` |
| `frame-src` | `'self'` | `'self'` |
| `worker-src` | `'self' blob:` | `'self' blob:` |
| `base-uri` | `'self'` | `'self'` |
| `report-uri` | `/api/csp-report` | `/api/csp-report` |
| `report-to` | `csp-endpoint` | `csp-endpoint` |

**Why `'unsafe-inline'`?** Required for Next.js bootstrap scripts and Tailwind inline styles served by the frontend.

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
| `APP_URL` | `http://localhost:3000` | Frontend URL (used in CORS and CSP) |
| `CSP_REPORT_URL` | `/api/csp-report` | CSP report endpoint |
| `CSP_REPORT_ONLY` | `false` | Use `Content-Security-Policy-Report-Only` if `true` |

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
