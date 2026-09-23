# TorrentCDN

P2P-assisted video delivery: viewers become peers, so bandwidth costs drop as your content scales. One line of embed code, and your audience's browsers share the bytes.

**Stack:** Next.js frontend (Vercel) → FastAPI backend (Render) → S3-compatible storage (Cloudflare R2) with WebTorrent magnet links generated server-side.

## Stack

| Layer | Tech | Location |
|---|---|---|
| Frontend | Next.js 14 (App Router) + TypeScript + Tailwind (light/dark theme via CSS vars) | `frontend/` |
| Backend | FastAPI + SQLAlchemy + Redis + PostgreSQL | `backend/` |
| Storage | S3-compatible (R2 / Supabase Storage), presigned URLs | — |
| P2P | WebTorrent; magnet links via torrentool; embed SDK (`/sdk.js`) | `frontend/public/` |
| Live | MediaMTX (RTMP→HLS) + per-segment WebTorrent swarms + hls.js player | `backend/app/routes/live.py`, `frontend/lib/live/`, `deploy/mediamtx/` |
| Infra | Vercel (frontend), Render (API), Caddy, Docker Compose (local/CI) | root |

## Repo layout

```
frontend/   Next.js app (app router, components, lib/api + auth clients, vitest tests)
backend/    FastAPI app (routes/, services/, security.py, scripts/ CI helpers)
deploy/     MediaMTX config for live streaming (RTMP ingest → HLS)
docs/       Feature guides — docs/live-streaming.md covers the live MVP
.github/    CI workflow — tests + security audits on every push/PR (see below)
SECURITY.md Security architecture, secrets hygiene, and deploy checklists
```

## Live streaming (MVP)

P2P-assisted **live** delivery on the same platform: OBS → RTMP → MediaMTX → HLS →
TorrentCDN delivery layer → viewer peers with automatic origin fallback. Channel
management, hashed stream keys, derived LIVE/OFFLINE status, live analytics with a
measured P2P-offload metric, and a production hls.js player are included. See
[docs/live-streaming.md](docs/live-streaming.md) for architecture, the quick start,
OBS settings, verification steps, and deployment notes.

## Getting started (local dev)

```bash
# 1. Infrastructure (Postgres, Redis, MinIO)
docker compose up -d postgres redis minio

# 2. Backend (FastAPI — dev server)
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env   # fill in values
uvicorn app.main:app --reload

# 3. Frontend (Next.js)
cd frontend
npm ci
npm run dev
```

## Testing & pre-merge CI gates

Every push/PR to `main` runs `.github/workflows/security.yml` — **all jobs must be green before merging**. On a leak, gitleaks posts an inline PR comment pointing at the offending line and the build fails.

| Job | What it gates | Run locally |
|---|---|---|
| `tests` | Backend unit suite (pytest, fully mocked — no infra) | `cd backend && python -m pytest` |
| `secret-scan` | High-signal secrets (PATs, AWS/Stripe/SendGrid/Resend keys, private keys, webhooks) in the diff **and** the full tree | `bash backend/scripts/secret_scan.sh` (or `--full`) |
| `static-audit` | Gitleaks secret scan (pushed-range on push, **full history** on the daily 04:00 UTC cron / manual dispatch; PR comments + SARIF upload) + header/CSP/auth/API-docs-gating audit | `cd backend && python scripts/security_audit.py` |
| `frontend-checks` | **Frontend pipeline:** `npm ci` → `tsc --noEmit` → **vitest unit tests + coverage gate** → `next build` → **OG image guard** | `cd frontend && npm run test:coverage` |
| `fullstack-audit` | Docker stack (Postgres/Redis/MinIO) + `curl_audit.sh` end-to-end (push/PR only; runs even when the secret gates are red so a leak PR keeps its e2e signal) | `cd backend && API_URL=http://localhost:8000 bash scripts/curl_audit.sh` |

### Frontend tests (vitest)

`frontend/` uses **vitest + Testing Library** (jsdom). Run them locally:

```bash
cd frontend
npm test             # one-shot (no coverage)
npm run test:coverage # one-shot WITH the coverage gate — exactly what CI runs
npm run test:watch   # watch mode during development
```

175 tests across 19 files cover real behavior, not just types: the auth token store incl. remember-me expiry handling (`lib/auth.ts`), the theme store (`lib/theme.ts`) — persistence, pre-paint resolution, and the inline pre-paint script parity check (`scripts/verify-theme-script.mjs`), the live streaming client + P2P swarm (`lib/liveClient.ts`, `lib/live/*`), the API client interceptors — Bearer-header attachment and the 401 logout/redirect (`lib/api.ts`), UsageBar percentage math (incl. the unlimited plan), EmbedCode snippet generation, PricingTable checkout flow (incl. the error path), AuthModal incl. the MFA + remember-me flow, and FileCard's delete-confirm + copy interactions. The inline pre-paint theme script in `app/layout.tsx` (which cannot import modules) is kept in lockstep with `lib/theme.ts` by `scripts/theme-script-parity.test.ts`, so a future storage-key change fails CI if the two drift.

**Coverage gate (v8 provider):** `test:coverage` counts every file under `lib/` and `components/` — tested or not — and fails the build if totals drop below the configured thresholds (`statements 25`, `branches 33`, `functions 14`, `lines 32` — the measured baseline, raise them as coverage grows). Removing a test, breaking a covered path, or adding a new untested file all fail CI. `VideoPlayer.tsx` is excluded (WebRTC/webtorrent glue — covered by `next build` + the embed e2e, not meaningfully unit-testable in jsdom).

### Frontend build + OG image guard

`frontend-checks` compiles the Next.js app exactly like production (Node 20, matching `frontend/Dockerfile`) and guards link-preview cards: the `og:image` / `twitter:image` URLs in `app/layout.tsx` must resolve to a real, non-empty PNG under `public/` — a prior regression shipped a 0-byte dynamic route that blanked every preview card on Twitter/X, LinkedIn, Slack, Discord, and WhatsApp. npm packages and Next.js' `.next/cache` build cache are cached in CI so warm builds stay fast.

## Secrets & security

Never commit real credentials — `.env*` is gitignored (only `.env.example` is tracked), the docker-compose secrets fail closed (`${VAR:?}`), and gitleaks + a diff-scoped gate scan every push/PR plus **full history daily**. See [SECURITY.md](SECURITY.md) for the security architecture, secrets hygiene/rotation rules, and the pre-merge + post-deploy checklists.
