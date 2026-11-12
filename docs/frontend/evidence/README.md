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

## Limitations

These are real gaps, not caveats to be waved through:

1. **`/share` is not pixel-proven.** It needs a live token, so only
   `/embed/demo` is in the frozen diff. The mechanism is shared with `/embed`.
2. **Authed pages are not Lighthouse/axe audited.** `/dashboard`, `/admin`,
   `/live`, the AI console and upload pages require a backend login, and no
   production calls were permitted. They appear in the screenshot set, the
   contrast matrix and the bundle budget only.
3. **Playwright e2e was not run.** Those suites hit a live backend. The test
   numbers above are `tsc` + vitest + build, not e2e. No test was edited,
   skipped or weakened.
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
