# TorrentCDN

P2P-assisted video delivery: viewers become peers, so bandwidth costs drop as your content scales. One line of embed code, and your audience's browsers share the bytes.

**Stack:** Next.js frontend (Vercel) → FastAPI backend (Render) → S3-compatible storage (Cloudflare R2) with WebTorrent magnet links generated server-side.

## Stack

| Layer | Tech | Location |
|---|---|---|
| Frontend | Next.js 14 (App Router) + TypeScript + Tailwind | `frontend/` |
| Backend | FastAPI + SQLAlchemy + Redis + PostgreSQL | `backend/` |
| Storage | S3-compatible (R2 / Supabase Storage), presigned URLs | — |
| P2P | WebTorrent; magnet links via torrentool; embed SDK (`/sdk.js`) | `frontend/public/` |
| Infra | Vercel (frontend), Render (API), Caddy, Docker Compose (local/CI) | root |

## Repo layout

```
frontend/   Next.js app (app router, components, lib/api + auth clients, vitest tests)
backend/    FastAPI app (routes/, services/, security.py, scripts/ CI helpers)
.github/    CI workflow — tests + security audits on every push/PR (see below)
SECURITY.md Security architecture, secrets hygiene, and deploy checklists
```

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

## Development workflow (protected `main`)

`main` is **protected** — no direct pushes, ever. Every change (feature or patch) ships through a pull request, and **merging to `main` is the ONLY way code reaches production** (Vercel + Render auto-deploy on `main` merges). This is deliberate: a deploy now requires a conscious, CI-green merge instead of a push accident.

```bash
# 1. Branch off main (feat/…, fix/…, chore/…)
git checkout -b feat/your-feature
# 2. Make changes; run the local checks below
# 3. Push the branch and open a PR
#    https://github.com/yatichbrian99-svg/torrent-cdn/compare/main...feat/your-feature
git push -u origin feat/your-feature
# 4. Merge the PR once the CI gates below are green — merging deploys
```

**What happens on merge:**

1. **Vercel** deploys the frontend and **Render** deploys the backend — only via the merge, never from a raw push.
2. CI's post-merge `p2p-e2e` job validates the freshly deployed build against production (2-peer swarm test + all E2E suites) — a broken deploy turns CI red immediately.
3. The daily scheduled jobs keep auditing production (secrets, dependencies, API-key + upload live probes, storage publicity).

Local checks before opening a PR: `cd backend && ./.venv/bin/python -m pytest`, `cd frontend && npm run test:coverage`, and `cd backend && ./.venv/bin/python scripts/security_audit.py`.

## Testing & pre-merge CI gates

Every push (post-merge) and pull request to `main` runs `.github/workflows/security.yml` — **all jobs must be green before merging**. On a leak, gitleaks posts an inline PR comment pointing at the offending line and the build fails.

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

21 tests across 6 files cover real behavior, not just types: the auth token store (`lib/auth.ts`), the API client interceptors — Bearer-header attachment and the 401 logout/redirect (`lib/api.ts`), UsageBar percentage math (incl. the unlimited plan), EmbedCode snippet generation, PricingTable checkout flow (incl. the error path), and FileCard's delete-confirm + copy interactions.

**Coverage gate (v8 provider):** `test:coverage` counts every file under `lib/` and `components/` — tested or not — and fails the build if totals drop below the configured thresholds (`statements 25`, `branches 33`, `functions 14`, `lines 32` — the measured baseline, raise them as coverage grows). Removing a test, breaking a covered path, or adding a new untested file all fail CI. `VideoPlayer.tsx` is excluded (WebRTC/webtorrent glue — covered by `next build` + the embed e2e, not meaningfully unit-testable in jsdom).

### Frontend build + OG image guard

`frontend-checks` compiles the Next.js app exactly like production (Node 20, matching `frontend/Dockerfile`) and guards link-preview cards: the `og:image` / `twitter:image` URLs in `app/layout.tsx` must resolve to a real, non-empty PNG under `public/` — a prior regression shipped a 0-byte dynamic route that blanked every preview card on Twitter/X, LinkedIn, Slack, Discord, and WhatsApp. npm packages and Next.js' `.next/cache` build cache are cached in CI so warm builds stay fast.

## Secrets & security

Never commit real credentials — `.env*` is gitignored (only `.env.example` is tracked), the docker-compose secrets fail closed (`${VAR:?}`), and gitleaks + a diff-scoped gate scan every push/PR plus **full history daily**. See [SECURITY.md](SECURITY.md) for the security architecture, secrets hygiene/rotation rules, and the pre-merge + post-deploy checklists.
