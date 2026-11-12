# Public `/tcdn-ai` page — evidence

Produced on branch `feat/public-tcdn-ai-page`. Nothing here is inherited from an
earlier run, and nothing was measured against production.

The page is static marketing content: it makes **no** backend call of its own.
The only request is the existing console status probe inside the "Request early
access" button component, which decides whether an "Open dashboard" link can be
shown. It fails closed (flag off ⇒ only the mailto button) and is the same
memoised `GET /api/ai/v1/console/status` the console and navbar already use — no
new endpoint, no new data collection. With the flag off that probe returns 404,
which is expected and is one of the console-log entries in the Lighthouse JSON.

## How it was run

```
# local, flag OFF, production build, no production calls, no live keys
cd frontend
npx tsc --noEmit                       # clean
npx vitest run                         # 22 files, 195 tests passed
bash scripts/check_domains.sh          # PASSED (www only, no dead host)
npm run build                          # includes the webtorrent patch guard
npx next start -p 3002

node ../docs/tcdn-ai/evidence/public-page/check.mjs   # screenshots, axe, keyboard, nav/footer
node scripts/ai_bundle_report.mjs                     # bundle-report.txt
npx lighthouse@12 http://127.0.0.1:3002/tcdn-ai --preset=desktop ...   # lighthouse/
```

The flag-on screenshot is **not** a claim that the console was exercised here:
the status probe was stubbed with Playwright routing (200 + `feature_flag.on =
true`) to show the "Open dashboard" state. The console itself is unchanged and
still gated by `AiGate`.

## Measured results

| Check | Result |
| --- | --- |
| `tsc --noEmit` | clean |
| `vitest run` | 195/195 passed (includes `Navbar.test.tsx` and `AiGate.test.tsx`) |
| `check_domains.sh` | PASSED |
| horizontal overflow at 375 / 768 / 1280 | 0 px at all three |
| axe (WCAG 2.0/2.1 A+AA), page content, light theme | clean |
| axe, page content, dark theme | clean (scoped `.ai-console` neutrals) |
| axe, whole document, dark theme | 5 nodes: the shared navbar's `--text-muted` `#555` links (Docs, Architecture, Demo, TCDN-AI, Login). Pre-existing: `/` fails 44 nodes and `/architecture` 80 for the same colour. Fixing it means repainting every existing page's navbar, which this PR is not allowed to do. |
| keyboard | 29 stops, no `<body>` gap, every stop draws a focus ring; copy buttons and both CTAs reachable |
| Lighthouse desktop | 100 / 96 / 96 / 91 — byte-identical to `/architecture` (100/96/96/91) in the same run; FCP 0.3 s, LCP 0.6 s, TBT 0 ms, CLS 0 |
| Lighthouse residual | `color-contrast` (navbar, above), `meta-description` and `bf-cache` also fail on `/architecture`; `errors-in-console` is `favicon.ico` (local) plus the expected flag-off probe 404 |
| CSP | identical to `/architecture` on the same server (only the per-request nonce differs); `frontend/middleware.ts` untouched by this PR |
| bundle | `/tcdn-ai` 348.4 kB vs `/architecture` 343.9 kB (+4.5 kB); the probe's axios client is a dynamic import, so it is not in the first-load JS. `[OK] no existing page loads a console-only chunk` |
| existing pages | only additions are a `hidden sm:inline` nav link and one footer link; landing page overflow 0 px at 375 and 1280 |

## Files

| Path | What |
| --- | --- |
| `check.mjs` | the Playwright script behind `check-run.txt` (run from `frontend/`) |
| `check-run.txt` | its output, 20/23 — the 3 failures are the navbar contrast above |
| `axe-themes.txt` | axe over light and dark at 1280 |
| `bundle-report.txt` | `scripts/ai_bundle_report.mjs` |
| `lighthouse/tcdn-ai.json`, `lighthouse/architecture.json` | Lighthouse desktop runs, same server, same run |
| `screenshots/` | 375 / 768 / 1280 dark, 1280 light, 1280 with the probe stubbed on |
