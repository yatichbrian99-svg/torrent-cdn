# Self-hosted Inter — evidence

The production/Vercel build failed intermittently in `next/dist/compiled/@next/font/dist/google/loader.js`
with `TypeError: Cannot read properties of null (reading '1')`: `next/font/google`
was fetching `fonts.googleapis.com` during `next build`. The fix is to ship the
font in the repository and load it with `next/font/local`.

## What changed

| | Before | After |
|---|---|---|
| Loader | `Inter` from `next/font/google` | `localFont` from `next/font/local` |
| Font bytes | downloaded at build time from Google | `frontend/app/fonts/inter-latin.woff2`, in the repo |
| Weights | 400 / 500 / 600 / 700 | 400 / 500 / 600 / 700 (four `@font-face` rules, same file) |
| Subset | `latin` | `latin` (the same glyph set: 518 glyphs, the exact file the previous build used) |
| CSS variable | none | `--font-inter` (additive; nothing depends on it) |
| `<body>` | `inter.className` | `inter.className` + `inter.variable` |
| Preconnect | `fonts.googleapis.com`, `fonts.gstatic.com` | removed |
| CSP `style-src` | `'self' 'unsafe-inline' fonts.googleapis.com` | `'self' 'unsafe-inline'` |
| CSP `font-src` | `'self' fonts.gstatic.com` | `'self'` |

Licence: Inter is SIL Open Font License 1.1. The licence text ships beside the
font at `frontend/app/fonts/LICENSE-Inter.txt`.

## Proof the build no longer touches Google

`npm ci` and `next build` were run inside a mount namespace whose `/etc/hosts`
points both Google font hosts at `127.0.0.1`:

```
== network isolation proof (final code) ==
hosts entries: 2 google font hosts -> 127.0.0.1
[OK] fonts.googleapis.com unreachable
[OK] fonts.gstatic.com unreachable
[OK] npm ci exit 0
[OK] next build exit 0
== emitted font assets ==
.next/static/media/e4af272ccee01ff0-s.p.woff2
```

The emitted asset hash is identical to the file the previous build produced, so
the served bytes did not change. No reference to `fonts.googleapis.com` or
`fonts.gstatic.com` remains in `.next/server` or `.next/static` outside Next's
own bundled loader source.

## Proof the rendering did not change

Two servers ran side by side on the same backend: the pre-change build (a clean
clone of `main`) on :3100 and the changed build on :3000. The same Playwright
script captured landing and dashboard, signed in, dark and light, at 1280 / 768
/ 375. Pixel comparison:

| Shot | Result |
|---|---|
| landing-1280-dark | identical |
| landing-1280-light | identical |
| landing-768-dark | identical |
| landing-768-light | identical |
| landing-375-dark | identical |
| landing-375-light | identical |
| dashboard-1280-dark | 51 px differ, inside the referral link |
| dashboard-1280-light | 51 px differ, inside the referral link |
| dashboard-768-dark / light | 51 px differ, inside the referral link |
| dashboard-375-dark / light | 51 px differ, inside the referral link |

The dashboard delta is not the font: the referral link renders the origin it is
being viewed from, so the pre-change shot contains `http://127.0.0.1:3100?...`
and the changed shot `http://127.0.0.1:3000?...`. It is the same glyphs at the
same positions. `landing-before.png` / `landing-after.png` and
`dashboard-before.png` / `dashboard-after.png` are the 1280 dark pair.

## CSP

Local production build, before → after:

```
style-src 'self' 'unsafe-inline' fonts.googleapis.com   ->  style-src 'self' 'unsafe-inline'
font-src 'self' fonts.gstatic.com                        ->  font-src 'self'
connect-src (unchanged)
```

No origin was added; two were removed. The backend's own CSP still lists the
Google font origins because FastAPI's `/docs` page loads Google Fonts — that page
is not the frontend and is unchanged by this work.

## Checks

- `tsc --noEmit` clean
- `vitest run` 22 files / 195 tests pass
- `frontend-checks` and the Vercel preview build on the PR
