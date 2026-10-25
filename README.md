# TorrentCDN

P2P-assisted video delivery: viewers become peers, so bandwidth costs drop as your content scales. One line of embed code, and your audience's browsers share the bytes.

**Stack:** Next.js frontend (Vercel) → FastAPI backend (Render) → S3-compatible storage (Cloudflare R2) with WebTorrent magnet links generated server-side.

## Stack

| Layer | Tech | Location |
|---|---|---|
| Frontend | Next.js 15 (App Router) + TypeScript + Tailwind (light/dark theme toggle, CSS-var tokens) | `frontend/` |
| Backend | FastAPI + SQLAlchemy + Redis + PostgreSQL | `backend/` |
| Storage | S3-compatible (R2 / Supabase Storage), presigned URLs | — |
| P2P | WebTorrent; magnet links via torrentool; embed SDK (`/sdk.js`) | `frontend/public/` |
| Live | MediaMTX (RTMP→HLS) + per-segment WebTorrent swarms + hls.js player | `backend/app/routes/live.py`, `frontend/lib/live/`, `deploy/mediamtx/` |
| Infra | Vercel (frontend), Render (API), Caddy, Docker Compose (local/CI) | root |

## Repo layout

```
frontend/   Next.js app (app router, components, lib/api + auth clients, vitest tests)
backend/    FastAPI app (routes/, services/, security.py, scripts/ CI helpers)
deploy/     MediaMTX config for live streaming (RTMP ingest → HLS), TCDN-AI
            dev stack + confidential-containers notes (deploy/tcdn-ai/)
docs/       Plans + feature guides: vision.md (the owner's north star, verbatim),
            future-roadmap.md (staged plan + decision log), dfs-phase-zero.md
            (repo audit + DFS M1), live-streaming.md (live MVP architecture/ops),
            LIVE_PITCH.md (live one-pager), pentest-report-2026-10-14.md,
            email-setup.md (transactional vs business mail, DNS records, runbook),
            tcdn-ai/ (decentralized inference: plan, threat model, security, API)
.github/    5 CI workflows — tests + security/static audits on every push/PR,
            e2e delivery gate on frontend pushes, lint, dependency audit (see below)
SECURITY.md Security architecture, secrets hygiene, and deploy checklists
```

## Live streaming (MVP)

P2P-assisted **live** delivery on the same platform: OBS → RTMP → MediaMTX → HLS →
TorrentCDN delivery layer → viewer peers with automatic origin fallback. Channel
management, hashed stream keys, derived LIVE/OFFLINE status, live analytics with a
measured P2P-offload metric, and a production hls.js player are included. See
[docs/live-streaming.md](docs/live-streaming.md) for architecture, the quick start,
OBS settings, verification steps, and deployment notes.

## TCDN-AI (decentralized inference)

A second product surface on the same platform: an OpenAI-compatible SDK that runs
open-weight LLM inference on rented GPU hosts, with multi-gigabyte weights delivered
peer-to-peer and settled in fiat-pegged compute credits. Code lives in
`backend/app/ai/` (coordinator, worker agent, weight transport, ledger) and
`backend/tcdn_ai_sdk/` (client); API under `/api/ai/v1`.

Confidentiality is labelled honestly everywhere: standard jobs are
`confidentiality: "none"` (the host operator can see prompts on their machine) and the
confidential tier is `attested-tee` on GPU CC + SEV-SNP/TDX hardware only. Consumer GPUs
and SGX are not treated as sufficient. Read
[docs/tcdn-ai/SECURITY.md](docs/tcdn-ai/SECURITY.md),
[docs/tcdn-ai/LIMITATIONS.md](docs/tcdn-ai/LIMITATIONS.md) and
[docs/tcdn-ai/DEV_SETUP.md](docs/tcdn-ai/DEV_SETUP.md) before deploying any of it.

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

## Theming (light/dark)

The default palette is dark; the navbar toggle switches to light. Theming is pure CSS variables: `:root` in `frontend/app/globals.css` holds the dark palette and `[data-theme='light']` remaps neutrals and darkens status tokens for contrast. The choice persists in `localStorage` (`tcdn_theme`) via `lib/theme.ts`.

**CSP nonce rule (do not break):** the pre-paint theme script (`THEME_SCRIPT` in `app/layout.tsx`) must be rendered via `next/script` with a nonce (`<Script id="theme-init" strategy="beforeInteractive" nonce={nonce}>`). A raw inline `<script>` gets no nonce, the nonce-CSP blocks it, and the anti-flash guard (`html:not([data-theme-resolved]) body { visibility: hidden }`) then hides the whole site. The nonce comes from `frontend/middleware.ts` (read in the async layout via `await headers()`), and a CSS animation failsafe releases the guard after 0.3s if anything goes wrong. `lib/__tests__/theme-parity.test.ts` pins the inline script ↔ `lib/theme.ts` agreement so they cannot drift.

## Testing & pre-merge CI gates

Five workflows run against `main` — **all jobs must be green before merging**. On a leak, gitleaks posts an inline PR comment pointing at the offending line and the build fails.

`.github/workflows/security.yml` — the secret + static gates are **always-on** (a secret can land in any directory); the two heavy jobs are path-scoped by its `changes` job, so a backend-only push skips the frontend pipeline and vice versa (the nightly cron and manual dispatch report both areas changed, so the full re-verify is preserved).

| Job | What it gates | Run locally |
|---|---|---|
| `secret-scan` | High-signal secrets (PATs, AWS/Stripe/SendGrid/Resend keys, private keys, webhooks) in the diff **and** the full tree | `bash backend/scripts/secret_scan.sh` (or `--full`) |
| `static-audit` | Gitleaks secret scan (pushed-range on push, **full history** on the daily 04:00 UTC cron / manual dispatch; PR comments + SARIF upload) + header/CSP/auth/API-docs-gating audit + **SQL-injection AST gate** + **dead-code gate** | `cd backend && python scripts/security_audit.py` · `python scripts/sql_injection_audit.py` · `python scripts/dead_code_audit.py` |
| `tests` | Backend unit suite (pytest, fully mocked — no infra) + backend domain guard (`scripts/check_domains.sh`) | `cd backend && python -m pytest` |
| `frontend-checks` | **Frontend pipeline:** `npm ci` → `tsc --noEmit` → **vitest unit tests + coverage gate** → `next build` → **OG image guard** + domain/API-baseURL guards | `cd frontend && npm run test:coverage` |
| `storage-publicity` | The object bucket still serves its public URL — a private bucket 404s all playback (read-only GET, no credentials) | `bash backend/scripts/check_storage_public.sh` |
| `upload-live` → `api-key-live` → `dedup-live` → `import-live` | **Scheduled production probes** (daily 04:00 UTC cron + manual dispatch only — they WRITE throwaway accounts/uploads, then clean up): presign→finalize→magnet; `tcdn_` key lifecycle incl. fail-closed revocation; content-dedup + refcount deletes; import-by-URL through the SSRF-pinned transport. Chained with `needs:` so they serialize against the shared per-IP register budget | the matching `backend/scripts/*_audit_prod.sh` against a real deployment |

`.github/workflows/ci-e2e.yml` — `p2p-e2e`: builds the pushed frontend, runs it behind the same-origin proxy, and drives the browser suites. **Real 2-peer WebTorrent delivery gate** in the push subset — every storage-host request (the R2 public base and S3 endpoint, plus `*.supabase.co` from before the R2 migration) is blocked, so only WebRTC can deliver the video. Runs on pushes that touch `frontend/**` or `.github/**` (the batch writes throwaway data to production, so it never runs on `pull_request`); the push subset is the ~7 delivery-critical suites and the full sweep (`e2e/run-all.mjs`, 12 suites) runs on the nightly cron and via manual dispatch.

`.github/workflows/ci-fullstack.yml` — `fullstack-audit`: Docker stack (Postgres/Redis/MinIO) + `curl_audit.sh` end-to-end; runs when `backend/**` or the IaC/Docker files change, and even when the secret gates are red so a leak PR keeps its e2e signal. Local: `cd backend && API_URL=http://localhost:8000 bash scripts/curl_audit.sh`.

`.github/workflows/backend-lint.yml` — `ruff`: backend lint/format gate. Local: `cd backend && ruff check app tests scripts`.

`.github/workflows/dependency-audit.yml` — `dependency-audit`: daily 06:00 UTC cron + PR + manual; `pip-audit` + `npm audit` vs the accepted baseline, failing on any new advisory (or any critical), plus a CycloneDX SBOM of the shipped backend environment as a build artifact. See [SECURITY.md](SECURITY.md) for the baseline-regeneration procedure.

### Frontend tests (vitest)

`frontend/` uses **vitest + Testing Library** (jsdom). Run them locally:

```bash
cd frontend
npm test             # one-shot (no coverage)
npm run test:coverage # one-shot WITH the coverage gate — exactly what CI runs
npm run test:watch   # watch mode during development
```

179 tests across 20 files cover real behavior, not just types: the auth token store incl. remember-me expiry handling (`lib/auth.ts`), the theme store (`lib/theme.ts`) — persistence, pre-paint resolution, and the inline pre-paint script parity check (`lib/__tests__/theme-parity.test.ts`), the live streaming client + P2P swarm (`lib/liveClient.ts`, `lib/live/*`), the API client interceptors — Bearer-header attachment and the 401 logout/redirect (`lib/api.ts`), UsageBar percentage math (incl. the unlimited plan), EmbedCode snippet generation, PricingTable checkout flow (incl. the error path), AuthModal incl. the MFA + remember-me flow, and FileCard's delete-confirm + copy interactions. The inline pre-paint theme script in `app/layout.tsx` (which cannot import modules) is kept in lockstep with `lib/theme.ts` by `lib/__tests__/theme-parity.test.ts`, so a future storage-key change fails CI if the two drift.

**Coverage gate (v8 provider):** `test:coverage` counts every file under `lib/` and `components/` — tested or not — and fails the build if totals drop below the configured thresholds (`statements 42`, `branches 45`, `functions 30`, `lines 48` — the measured baseline, raise them as coverage grows). Removing a test, breaking a covered path, or adding a new untested file all fail CI. `VideoPlayer.tsx` and `LivePlayer.tsx` are excluded (WebRTC/webtorrent/hls.js glue — covered by `next build` + the embed e2e, not meaningfully unit-testable in jsdom).

### Browser E2E suites (`frontend/e2e`)

Playwright + headless Chromium suites that drive the real UI. They target the **production site** by default (`E2E_BASE_URL` overrides it) and every run creates and deletes its own throwaway account + file, so they are repeatable. Prerequisites: `npx playwright install chromium` and `ffmpeg` on PATH (`E2E_FILE` skips synthesis; `E2E_TOKEN` reuses an existing account).

```bash
cd frontend
npm run test:e2e              # all suites (e2e/run-all.mjs)
npm run test:e2e:upload       # single suite — see package.json for every alias
```

Suites cover the upload paths (direct + fallback + finalize-retry incl. the lost-response case), embed playback and speed, auto-seed, real 2-peer P2P delivery, API-key UI, login + MFA flows, visual pass, and admin non-select. The same suite files are what `ci-e2e.yml` runs.

### Frontend build + OG image guard

`frontend-checks` compiles the Next.js app exactly like production (Node 20, matching `frontend/Dockerfile`) and guards link-preview cards: the `og:image` / `twitter:image` URLs in `app/layout.tsx` must resolve to a real, non-empty PNG under `public/` — a prior regression shipped a 0-byte dynamic route that blanked every preview card on Twitter/X, LinkedIn, Slack, Discord, and WhatsApp. npm packages and Next.js' `.next/cache` build cache are cached in CI so warm builds stay fast.

## Secrets & security

Never commit real credentials — `.env*` is gitignored (only `.env.example` is tracked), the docker-compose secrets fail closed (`${VAR:?}`), and gitleaks + a diff-scoped gate scan every push/PR plus **full history daily**. See [SECURITY.md](SECURITY.md) for the security architecture, secrets hygiene/rotation rules, and the pre-merge + post-deploy checklists.
