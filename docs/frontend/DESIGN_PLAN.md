# TorrentCDN visual redesign — design plan

Branch: `feat/visual-redesign` (draft PR, never merged from here).
Scope: **look only** — colour system, typography, layout polish, motion. No route,
API, copy, auth or behaviour change; `/embed`, `/share` and `public/sdk.js` are
frozen byte-for-byte in appearance.

Two moods, one token set:

| Theme | Working name | Feel |
| --- | --- | --- |
| Light | **Guardbase** | ivory paper, coal text, one royal-blue accent, hairlines, mono labels, halftone peer-mesh plate |
| Dark | **Fluxora** | pure black, warm white, burnt-orange stage light, floating glass pill nav, glowing peer network with light trails |

Default is the **system** preference; an explicit choice persists and stops
following the OS. A choice is only stored when the visitor makes one, so the
"no choice yet" state stays live-updatable.

---

## 1. Files to touch

### New

| File | Purpose |
| --- | --- |
| `frontend/lib/theme.ts` (rewrite) | resolution logic: stored choice → system → fallback; pure + unit-tested |
| `frontend/lib/__tests__/theme.test.ts` | new contract tests (system default, persist, storage blocked, live OS change) |
| `frontend/lib/__tests__/theme-parity.test.ts` (update) | keeps the inline pre-paint script and `lib/theme.ts` in lockstep (assertions strengthened, none removed) |
| `frontend/components/ThemeProvider.tsx` | client watcher: live `prefers-color-scheme` updates while no choice is stored |
| `frontend/components/ThemeToggle.tsx` (rewrite) | animated sun/moon, `aria-pressed`, `aria-label`, 44px target |
| `frontend/components/SiteFooter.tsx` | footer extracted from `app/page.tsx`, same links/copy/badge, new styling |
| `frontend/components/Reveal.tsx` | scroll-reveal + stagger wrapper (IntersectionObserver, reduced-motion aware) |
| `frontend/components/CopyButton.tsx` | copy-to-clipboard for the embed snippet card |
| `frontend/components/CodeSnippet.tsx` | syntax-coloured snippet (accent + neutrals only) |
| `frontend/components/FaqAccordion.tsx` | accessible accordion (`button` + `aria-expanded` + region), copy unchanged |
| `frontend/components/CompareTable.tsx` | comparison table + count-up + gap bars, final numbers in HTML |
| `frontend/components/hero/LightHeroArt.tsx` | halftone/dithered peer-mesh plate (inline SVG), slow drift |
| `frontend/components/hero/DarkHeroArt.tsx` | glowing peer network + travelling light trails (SVG/CSS), reduced-motion static frame |
| `frontend/components/LegacyChrome.tsx` | frozen nav markup for `/embed` + `/share` (today's exact nav) |
| `frontend/app/fonts/instrument-serif-italic-latin.woff2` + `LICENSE-InstrumentSerif.txt` | self-hosted display italic (OFL) |
| `frontend/app/fonts/jetbrains-mono-latin.woff2` + `LICENSE-JetBrainsMono.txt` | self-hosted mono (OFL) |
| `frontend/scripts/check_contrast.mjs` | WCAG matrix for every token pair, both themes → evidence |
| `frontend/scripts/theme_audit.mjs` | Playwright: no-flash (first-paint probe), CSP violations, storage-blocked fallback, live OS change, reduced-motion |
| `frontend/scripts/shot_pages.mjs` | before/after matrix screenshots at 375/768/1280 × light/dark |
| `frontend/scripts/diff_screenshots.mjs` | pixel diff (decoded in Chromium) → frozen-surface proof |
| `frontend/scripts/bundle_report.mjs` | per-page first-load JS + budget table |
| `docs/frontend/COPY_CLAIMS.md` | every number/claim on the redesigned pages + repo source |
| `docs/frontend/evidence/*` | screenshots, contrast table, axe, lighthouse, bundle, theme audit |

### Modified

| File | Change |
| --- | --- |
| `frontend/app/globals.css` | new token block + legacy-token aliases, typography scale, component classes, motion keyframes, frozen-surface pin |
| `frontend/app/layout.tsx` | theme script (nonce, system-aware), `color-scheme`, `theme-color`, font wiring, pathname-aware chrome for frozen routes |
| `frontend/middleware.ts` | one request header `x-pathname` (no CSP change) |
| `frontend/components/Navbar.tsx` | new nav shell (light hairline / dark floating pill), mobile menu (focus trap, Escape, `aria-expanded`, scroll lock) |
| `frontend/app/page.tsx` | hero + sections rebuilt with existing copy; footer extracted |
| `frontend/components/PricingTable.tsx` | cards, badge, check icons, hover lift, glow |
| `frontend/components/LegalPage.tsx` | typography/rhythm only |
| `frontend/app/docs/page.tsx`, `app/architecture/page.tsx`, `app/demo/page.tsx`, `app/tcdn-ai/page.tsx` | heading/rhythm/card polish, existing markup and copy kept |
| `frontend/app/dashboard/page.tsx` + inner console pages | token-driven cards/inputs/tables; markup preserved |
| `frontend/tailwind.config.ts` | font family vars only (no new colours) |

### Untouched on purpose

`public/sdk.js`, `app/embed/**`, `app/share/**` (markup frozen; scoped legacy
tokens + legacy nav), all `lib/api*`, auth, and every data flow.

---

## 2. Token map

Spec tokens are authoritative; legacy names stay as aliases so the ~1000
existing `var(--…)` usages restyle without a mass edit.

| Spec token | Light | Dark | Legacy alias(es) |
| --- | --- | --- | --- |
| `--bg` | `#FEFEFD` | `#000000` | `--bg-primary` |
| `--text` | `#080808` | `#FEFFFA` | `--text-primary` |
| `--accent` | `#154ECD` | `#D23A00` | (new) |
| `--accent-hover` | `#0F3BA0` | `#F04A0A` | (new) |
| `--surface` | `#F4F5F8` | `#0E0A08` | `--bg-secondary`, `--bg-raised` |
| `--border` | `#E2E5EC` | `#2A1A12` | `--border` |
| `--muted` | `#5B6270` | `#A39A94` | `--text-muted`, `--text-faint`, `--text-dim`, `--text-soft` |

Derived (no new hues):

| Token | Light | Dark | Rule |
| --- | --- | --- | --- |
| `--accent-contrast` | `#FFFFFF` | `#000000` | text on filled accent |
| `--accent-text` | `#154ECD` | `#FF5A1F` | accent as small text (dark `#D23A00` is ~4.3:1 on black) |
| `--ring` | `rgba(21,78,205,.35)` | `rgba(240,74,10,.45)` | focus ring |
| `--surface-2` | `#FFFFFF` | `#150D09` | raised card |
| `--shadow-glow` | blue-tinted shadow | orange glow | popular card, CTA |
| `--overlay` | `rgba(8,8,8,.45)` | `rgba(0,0,0,.65)` | mobile menu scrim |

Status colours (ok/warn/err/info/purple) stay: they carry meaning, are not
decoration, and every one of them already meets ≥3:1 on the new surfaces (the
matrix in `evidence/contrast.md` re-checks each).

**Accent is allowed only for**: primary CTAs, one highlighted headline word, key
numbers, link hover, focus rings, active states, the "Most popular" badge, and
the TorrentCDN column of the comparison table. Everything else is neutral.

---

## 3. Font plan

| Role | Family | Files | Bytes |
| --- | --- | --- | --- |
| Body + UI + headings | Inter (variable, already self-hosted) | `inter-latin.woff2` | ~108 KB (unchanged) |
| Display italic (dark accent word) | Instrument Serif *italic*, latin | `instrument-serif-italic-latin.woff2` | reported after download |
| Labels, code, numbers | JetBrains Mono, latin, 400+500 | `jetbrains-mono-latin.woff2` | reported after download |

- `next/font/local`, `display: 'swap'`, real fallbacks (`Georgia, serif` /
  `ui-monospace, SFMono-Regular, Menlo, monospace`), `font-src 'self'` unchanged.
  No Google origin at runtime; the files are committed with their OFL licence.
- Preload: only Inter. Instrument Serif and JetBrains Mono load on use (the
  hero italic is not LCP-critical text; mono is used below the fold).
- Headings: 600–700, `letter-spacing: -0.03em`, `line-height: ~1`.
  Body: 16px / 1.6. Labels: mono, uppercase, 11–12px, `0.14em`.
- Hero: `clamp(3rem, 8vw, 7rem)` in both themes; `clamp()` everywhere else.

---

## 4. Motion plan

| Element | Motion | Guard |
| --- | --- | --- |
| Hero copy | fade + 16px rise, 80ms stagger, one-shot on load | `prefers-reduced-motion` → static |
| Light halftone plate | 24s slow drift (translate/scale) | paused off-screen + tab hidden; static under reduced motion |
| Dark glow + trails | radial glow breathe 12s; 3 SVG dash-offset trails 8–14s | same guards; static frame for reduced motion |
| Step connector | dotted path `stroke-dashoffset` draw on scroll-in | one-shot, no loop |
| Stats / comparison numbers | count-up on first view | final value already in HTML; disabled under reduced motion; `tabular-nums` |
| Sections | fade + 16px reveal | one-shot per section |
| Cards/buttons | hover lift 2px, press `scale(.98)` | transform only, no layout |
| Theme change | 220ms colour transition | disabled under reduced motion |

All decorative graphics are `aria-hidden="true"`, `<canvas>`-free (inline SVG +
CSS), capped at DPR 1.5 where rasterised, never above the fold in a way that
delays LCP, and rendered as a static SVG for no-JS.

---

## 5. Risks

| Risk | Mitigation |
| --- | --- |
| **CSP blocks the theme script** (past incident: site went black) | the theme script is a raw inline `<script nonce={nonce}>` (synchronous, in `<head>`) — no `'unsafe-inline'` is needed and the policy is unchanged; `theme_audit.mjs` records zero CSP violations in both themes; the CSS self-release guard stays and its test stays |
| Theme flash | inline script still pre-paint; first-paint probe compares `data-theme` to the painted background pixel |
| `--text-muted` remap lowers contrast on console/docs pages | every text token is ≥4.5:1 on `--bg`/`--surface` in both themes (`check_contrast.mjs`) |
| New nav height shifts `main` padding and breaks anchored e2e waits | nav height is a single `--nav-h` token; `main` padding derives from it; e2e text selectors untouched |
| Restyling `/embed` + `/share` by inheritance | those routes render `LegacyChrome` and a `[data-frozen-surface]` scope that re-pins today's tokens; pixel diff proves 0 changed pixels |
| Cross-origin font fetch | woff2 committed to the repo + OFL licences; `font-src 'self'` unchanged; dependency audit unchanged |
| Bundle growth | no new runtime deps; `bundle_report.mjs` enforces ≤30 kB growth per existing page |
| Animated SVG hurting Lighthouse/CLS | fixed aspect-ratio boxes, no layout-affecting animation; the decorative hero plate (below the fold on phones) is the only `content-visibility: auto` surface, with an intrinsic placeholder size |
| Accent overuse | allowed-list above; audit is a grep over `globals.css`/components for `--accent` uses, re-run by hand at each stage and listed in the PR notes (no automated test — see §9) |

---

## 6. Rollback

1. The whole change is one branch + draft PR; production is untouched until an
   owner merge. `git revert <merge>` restores today's look; no data, schema or
   env change is involved.
2. `/embed` + `/share` need no rollback path: they are pinned to the old look
   by the `[data-frozen-surface]` scope and stay byte-identical either way.
3. No build-time kill switch is shipped. An earlier draft of this plan proposed
   `NEXT_PUBLIC_LEGACY_UI=1`; keeping a second full token set alive for a
   hypothetical rollback would double the CSS and quietly rot, so the plan is
   now a plain revert (step 1). Recorded in §9 rather than left as a promise.
4. Evidence is additive and lives under `docs/frontend/`; deleting it changes
   nothing at runtime.

---

## 7. Stages

1. **Tokens + theme + fonts + nav/footer** — `globals.css`, `layout.tsx`,
   `middleware.ts` (+`x-pathname`), `lib/theme.ts`, `ThemeProvider`,
   `ThemeToggle`, `Navbar`, `SiteFooter`, `LegacyChrome`, fonts, theme tests.
2. **Landing page** — hero (both themes), art components, how-it-works,
   comparison, pricing cards, FAQ accordion, footer, motion wrappers.
3. **Inner pages** — docs, architecture, demo, tcdn-ai, legal, auth, billing,
   dashboard, admin, verify-email, live, ai console, upload.
4. **Polish + evidence** — contrast matrix, axe, keyboard walkthrough, theme
   audit, screenshots, frozen-surface diff, Lighthouse before/after, bundle
   budget, test sweep, `COPY_CLAIMS.md`.

Each stage is pushed to the draft PR with its own report; nothing merges.

---

## 9. Judgement calls, and what is not proven

Written after the work, not before it. Everything here is a choice a reviewer
could reasonably have made differently, or a claim the evidence does not
actually support.

**Theme script shape.** The plan said the script would stay `next/script`
(`beforeInteractive`). In practice `beforeInteractive` is injected by the
router, which cost ~100 ms of visible-paint wait on mobile. It is now a raw
synchronous `<script nonce={nonce}>` in `<head>`: same CSP (no
`'unsafe-inline'`), same pre-paint guarantee, first visible frame ~106–133 ms.
One trap worth knowing: React escapes `&` in JSX text to `\u0026`, so an inline
script containing `&&` becomes a `SyntaxError` and silently never runs. The
script uses nested `if`s, and `lib/__tests__/theme-parity.test.ts` asserts it
contains no `&&`.

**Legacy token aliases.** The new spec tokens (`--bg`, `--text`, `--accent`, …)
sit beside ~1000 existing `var(--…)` uses, which are mapped to them as aliases
rather than rewritten. Cheap and low-risk, but it means `globals.css` carries a
translation layer that a later cleanup should collapse.

**Duplicated dark block.** Dark values appear twice — once under
`[data-theme='dark']`, once under
`@media (prefers-color-scheme: dark) :root:not([data-theme='light'])` — so that
a system change with no stored choice still re-themes. A vitest freezes both
blocks against each other; a future edit must touch both.

**Frozen surfaces.** `/embed` and `/share` keep their own pre-paint script
(dark unless `light` is stored), the verbatim old navbar (`LegacyChrome`), and a
`:root[data-frozen-surface]` token pin at higher specificity than the media
block. The body/heading base rules are scoped to
`html:not([data-frozen-surface])`; without that scoping, `line-height` on `body`
alone shifted frozen pages by 3 px. `ThemeProvider` is not rendered there.

**`content-visibility`.** Applied only to the decorative hero plate. Applying it
to whole sections measured 0.009 CLS from placeholder resizing, so it was
removed from `.section`; the plate keeps it because it is off-screen on phones.

**Reveal threshold.** Scroll-reveal originally required 8 % of an element to be
visible, which a fast scroll can outrun (the observer never fires and the
section stays hidden until it is scrolled again). Threshold is now 0.01 with a
−2 % bottom margin, and `shot_pages.mjs` scrolls each `.reveal` into view
explicitly instead of trusting a single pass.

**Not verified.**
- `/share` needs a live token; only `/embed/demo` is in the frozen screenshot
  set. The frozen mechanism is shared, but `/share` itself is not pixel-proven.
- `/dashboard`, `/admin` and the rest of the authed console could not be
  Lighthouse- or axe-audited locally (auth is backend-gated and no production
  calls were allowed). They are covered by the screenshot set, the contrast
  matrix and the bundle budget only.
- Playwright e2e suites target a live backend; they were not run. Test counts
  below are vitest + `tsc`, not e2e.
- Lighthouse numbers are single-run local `next start` on a shared machine:
  landing mobile 94–97 (baseline 96), docs mobile 96 (baseline 98). The docs
  delta is within run-to-run noise but is not an improvement, and should not be
  read as one.
- The accent-usage rule is audited by grep, not enforced by a test.
