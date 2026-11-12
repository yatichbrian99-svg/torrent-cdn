# TCDN-AI console — frontend evidence

Everything in this directory was produced by the commands below on the branch
`feat/tcdn-ai-console`. Nothing here is inherited from an earlier run.

## How it was run

Local stack, flag **on**, no production calls, no live keys:

```
# services
docker run -d --name tcdn-audit-pg  -e POSTGRES_USER=audit -e POSTGRES_PASSWORD=auditpw \
  -e POSTGRES_DB=tcdn -p 127.0.0.1:55432:5432 postgres:16.4-alpine
docker run -d --name tcdn-audit-redis -p 127.0.0.1:56379:6379 redis:7.4.2-alpine

# backend (flag on, test seam on)
cd backend
JWT_SECRET=… APP_ENV=development DATABASE_URL=postgresql://audit:auditpw@127.0.0.1:55432/tcdn \
REDIS_URL=redis://127.0.0.1:56379/0 ENABLE_TCDN_AI=true TCDN_AI_FAKE_CHECKOUT=1 \
TCDN_AI_API_KEY_PEPPER=… .venv/bin/python -m uvicorn app.main:app --port 8000

# one model + a worker, so the journey bills a real job
JWT_SECRET=… DATABASE_URL=… ENABLE_TCDN_AI=true … \
  python scripts/ai_provision.py --account-id <uuid> --scopes inference,worker \
  --models-file models.json
TCDN_AI_COORDINATOR_URL=http://127.0.0.1:8000 TCDN_AI_WORKER_API_KEY=… \
TCDN_AI_WORKER_MODELS=llama-3.1-8b-instruct=<manifest sha> TCDN_AI_BACKEND=echo \
TCDN_AI_IDENTITY_FILE=/tmp/ai-worker-identity.json \
  python scripts/ai_worker.py --public-url http://127.0.0.1:9100 --allow-plaintext-loopback

# frontend, production build, behind the same-origin proxy the app expects
cd frontend
NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 npm run build
NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 npx next start -p 3001
E2E_API_URL=http://127.0.0.1:8000 E2E_FRONTEND_PORT=3001 node e2e/local-proxy.mjs   # :3000
```

An `echo` worker is a development stub and is refused in production; the
manifest hash in `models.json` is a local fixture, not a published model.

## Files

| File | What it is |
|---|---|
| `e2e-flag-on.json` | `node e2e/ai-console.mjs` — 17/17: login → buy test credits → create key → call the API → see the job, plus axe on every page and a no-horizontal-scroll check at 375/768/1280 |
| `e2e-flag-off.json` | `node e2e/ai-flag-off.mjs` — 10/10: no AI link, `/ai` shows the unavailable panel, only the feature probe touches an AI path (it 404s), existing pages unchanged |
| `screenshots/ai-*.png` | 21 full-page screenshots (7 pages × 3 widths) plus nothing else — read them, they are the visual record |
| `scripts/ai_bundle_report.mjs` output → `bundle-report.txt` | per-route first-load JS and a hard check that no existing page loads a console-only chunk |
| `lighthouse/ai-status.json`, `lighthouse/home.json` | Lighthouse desktop runs (JSON) for the public status page and the landing page |
| unit tests | `frontend/lib/aiConsole.test.ts` (11) and `frontend/components/__tests__/AiGate.test.tsx` (4) — run with `npx vitest run` |

## Measured results

- **e2e flag on:** 17/17 checks, exit 0. The purchase is a *simulated* checkout
  (`TCDN_AI_FAKE_CHECKOUT=1`), which is refused outside development/test.
- **e2e flag off:** 10/10 checks, exit 0. Zero AI requests other than the
  documented status probe, which returns 404.
- **axe (console subtree, WCAG 2.0/2.1 A+AA):** 0 serious/critical violations on
  all seven pages.
- **Contrast note:** the shared navbar/footer still fail colour contrast
  (2.81:1 with `--text-muted` on black) on *every* page of the app, including
  the landing page. That is pre-existing and outside this change; the console
  scopes its own accessible neutrals under `.ai-console` instead of restyling
  the whole site. axe was therefore scoped to `.ai-console`, and this is the
  one known accessibility gap.
- **Lighthouse (desktop, local production build):** `/ai/status` 88 performance
  / 96 accessibility / 96 best-practices (LCP 694 ms, TBT 0 ms); `/` 100 / 92 /
  96. The accessibility deductions are the navbar contrast above.
- **Bundle:** 10 chunks are unique to the console; `/`, `/dashboard` and
  `/billing` load none of them (`[OK]` in `bundle-report.txt`).

## Not covered here

- Authenticated Lighthouse runs: the session lives in `sessionStorage`, which
  Lighthouse cannot seed without driving a login, so the console's authenticated
  pages were measured by the e2e run and the bundle report rather than by
  Lighthouse.
- ZAP/DAST and the container scan run in CI on the PR, not locally.
- Anything involving a real Paystack transaction: checkout refuses when
  `TCDN_AI_PAYMENTS_LIVE` is on, by design.
