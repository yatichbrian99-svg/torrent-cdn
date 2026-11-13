# Visual redesign — evidence index

Everything in this folder was produced by a script in `frontend/scripts/`
against a local production build (`next build` + `next start`). Nothing here is
hand-written or estimated. If a claim in the PR is not backed by a file in this
folder, treat it as unproven.

The plan, token map and the list of judgement calls live one level up in
[`../DESIGN_PLAN.md`](../DESIGN_PLAN.md); the copy audit lives in
[`../COPY_CLAIMS.md`](../COPY_CLAIMS.md).

## What is in the box

| File | What it shows |
| --- | --- |
| `baseline/` | 48 "before" screenshots (8 routes × 3 widths × 2 themes) and the pre-redesign Lighthouse reports (`landing-mobile.json`, `landing-desktop.json`, `docs-mobile.json`, `docs-desktop.json`) captured on `main`. |
| `screenshots/` | The same 48 shots, same routes/widths/themes, captured on `feat/visual-redesign`. Files are `after-*`; each pair is directly comparable. |
| `frozen-surface-diff.md` | Pixel diff of the six `/embed/demo` shots. All identical (0 differing pixels, max channel Δ 0). |
| `contrast.md` | 43 foreground/background pairs, both themes, computed WCAG ratios. All ≥ AA (4.5:1 body text, 3:1 large text / UI). |
| `axe.md` | axe-core results: 14 page/theme runs, 0 violations. |
| `keyboard.md` | Keyboard walk: 24 focus stops on the landing page, every stop has a visible focus ring; mobile menu open/close/Escape/scroll-lock walk. |
| `theme-audit.txt` | 9/9 behavioural checks: system default, stored choice wins, storage-blocked fallback, live OS change, no flash, `aria-pressed`/label, keyboard toggle, `color-scheme`, `theme-color`. |
| `bundle.md` | First-load JS per route, before vs after, against a ≤ 30 kB growth budget. Worst route growth is +1.0 kB. |
| `after/` | Lighthouse JSON (all four categories) for landing + docs, mobile and desktop. |
| `hero/` | The hero rework: `hero-audit.json` (worst-case contrast of every hero text element against the art behind it, plus the stat-card baseline check), six hero screenshots, and `lighthouse-{mobile,desktop}.json` from the build that shipped the final hero. |
| `gated/` | The authenticated / backend-gated routes the public pass cannot reach: `/dashboard`, `/dashboard/upload`, `/admin`, `/admin/health`, `/admin/payouts`, `/live` and the seven `/ai/*` console pages. 78 runs (13 routes × 3 widths × 2 themes) with screenshots, axe at 375 and 1280, a keyboard walk at 1280 and an overflow check. `gated-audit.json` is the machine-readable result. |
| `baseline-next-build.txt`, `after-next-build.txt` | Full build logs (route table, bundle sizes) the budget table is parsed from. |

## Measured results

Lighthouse, local production build, Chrome headless (mobile = default throttling,
desktop = `--preset=desktop`):

| Page | Before | After |
| --- | --- | --- |
| Landing, mobile | perf 96 · a11y 92 · LCP 2.4 s · CLS 0 | perf 94–97 · a11y 100 · LCP 2.3–2.8 s · CLS 0–0.01 |
| Landing, desktop | perf 100 · a11y 92 | perf 100 · a11y 100 |
| Docs, mobile | perf 98 · a11y 96 | perf 96 · a11y 100 |
| Docs, desktop | perf 100 · a11y 96 | perf 100 · a11y 100 |

Mobile landing varies 94–97 between runs on this machine; the number in the PR
is the range, not the best run. The 2-point docs mobile delta is inside that
noise band and is **not** claimed as an improvement.

Other measurements:

- Fonts added: 53,560 B of woff2 (`instrument-serif-italic-latin.woff2`
  22,128 B, `jetbrains-mono-latin.woff2` 31,432 B) plus ~8.8 kB of OFL licence
  text. Inter was already self-hosted and is unchanged.
- First-load JS: shared chunk 103 kB before and after; no route grows more than
  1.0 kB (`bundle.md`).
- Horizontal overflow: 0 px at 375 / 768 / 1280, both themes, all 8 routes.
- Tests: `tsc --noEmit` clean, vitest 211 passed / 23 files, `next build` clean.

## How to regenerate

```bash
cd frontend
npm run build
npx next start -p 3102 &          # keep this in its own shell/background task

node scripts/check_contrast.mjs                     # -> evidence/contrast.md
node scripts/a11y_audit.mjs                         # -> evidence/axe.md, keyboard.md
node scripts/theme_audit.mjs --base http://localhost:3102   # -> evidence/theme-audit.txt
node scripts/shot_pages.mjs --base http://localhost:3102 \
     --out ../docs/frontend/evidence/screenshots --tag after-
node scripts/diff_screenshots.mjs \
     --before ../docs/frontend/evidence/baseline \
     --after  ../docs/frontend/evidence/screenshots --prefix embed
node scripts/bundle_report.mjs --before /tmp/base-build.log --after /tmp/new-build.log
```

`diff_screenshots.mjs` exits non-zero when a frozen-surface pixel moves, so it
can be wired into CI as-is. `shot_pages.mjs` waits for hydration and scrolls
every `.reveal` into view before capturing, and fails the run if a page overflows
horizontally.

## Full-local verification pass (backend + frontend on this machine)

Run against a local production build (`next build` + `next start`), a local
Postgres/Redis/MinIO stack, the backend at `:8000` (AI flag on) and `:8001`
(flag off), and `e2e/local-proxy.mjs` reproducing the production same-origin
proxy contract.

| Check | Result |
| --- | --- |
| `scripts/a11y_audit.mjs` (7 restyled routes × 2 themes) | 0 axe violations, 14 runs |
| Keyboard walk, landing page | 24 focus stops, all with a visible indicator; mobile menu open/Escape/return pass |
| `scripts/gated_pages_audit.mjs` (13 gated routes × 3 widths × 2 themes) | 78 runs, 0 problems: no redirects, 0 px overflow, 0 axe violations at 375 and 1280, every keyboard stop (1280) shown |
| `scripts/hero_audit.mjs` | All hero text ≥ 4.5:1 against the art behind it at 375/768/1280 in both themes; stat cards equal height and on one baseline ≥ 768; 0 px overflow |
| `scripts/diff_screenshots.mjs --prefix embed` | Frozen surfaces pixel-identical |
| Lighthouse, landing (this build) | mobile perf 94 · a11y 100 · LCP 1.04 s · CLS 0; desktop perf 100 · a11y 100 · LCP 0.66 s · CLS 0 |
| `e2e/ai-console.mjs` (flag on) | 17/17 |
| `e2e/ai-flag-off.mjs` (flag off, `:3002`) | 10/10 |
| `e2e/admin-noselect.mjs`, `visual-pass.mjs`, `login-flow.mjs`, `api-key-ui.mjs`, `embed-speed.mjs`, `upload-fallback.mjs` | pass |

Two test files were touched, each for a stated reason: `e2e/ai-flag-off.mjs`
asserted that no nav text matches `\bAI\b`, which became false when commit
`2965c3a` deliberately added a flag-independent public `/tcdn-ai` page; it now
asserts the actual contract — no link into the gated console (`/ai`, `/ai/*`) —
which is stricter about what the flag protects. `e2e/admin-noselect.mjs` was
missing the `failedRequests` array that `attachDiagnostics()` writes to and
crashed before its first assertion; the array was added (no assertion changed).

Accessibility fixes made on gated pages (found by this pass, none introduced by
the redesign): `aria-label`s on the admin plan selects, the payout recipient
select, the admin back button and the upload file input; the payout
"Add recipient" button now uses `--inverse-fg` (white-on-purple failed AA at
2.81:1); the health status chips use the existing `--ok-chip-*` tokens (4.39:1
before); the five scrollable table/pre containers in the AI console are
keyboard-reachable regions.

## Limitations

These are real gaps, not caveats to be waved through:

1. **`/share` is not pixel-proven.** It needs a live token, so only
   `/embed/demo` is in the frozen diff. The mechanism is shared with `/embed`.
2. **Authed pages have no Lighthouse run.** They were screenshotted, contrast-
   checked, axe'd and keyboard-walked against a local backend (`gated/`), but
   Lighthouse scores are landing + docs only.
3. **Storage-dependent e2e suites cannot pass on this machine.** Uploads that
   stream from MinIO over plain http are blocked by the shipped CSP
   (`media-src 'self' blob: https:` and a `connect-src` that only allow-lists
   localhost for dev): `upload-direct`, `finalize-retry`, `embed-playback`,
   `auto-seed` and `p2p-two-peer` fail with a CSP refusal, not a product bug.
   `mfa-flow` needs `APP_ENV=production` policy (`mfa_required`), which this
   local stack does not run. The suites that do not need storage playback pass.
4. **Lighthouse is single-run local.** Not a field measurement, not CI, and the
   mobile landing score moves 3 points between runs.
5. **Preview build is local only.** Nothing was deployed and no production
   endpoint was touched; `ENABLE_TCDN_AI` is unchanged.

## Reviewing this locally

For a phone/desktop pass: open the same route at 375 px and 1280 px, toggle the
theme from the navbar, and compare against the `after-` screenshot pair for that
route. `after-<route>-<theme>-<width>.png` maps 1:1 to `before-…` in `baseline/`.
Steps to run the branch:

```bash
cd frontend
npm ci
npm run build && npx next start -p 3102
# http://localhost:3102  — theme toggle sits in the navbar (sun/moon, right side)
```

The theme resolves from `localStorage['tcdn_theme']` when set, otherwise from
`prefers-color-scheme`, and follows live OS changes while no choice is stored.
