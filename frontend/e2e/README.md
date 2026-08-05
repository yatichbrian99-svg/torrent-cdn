# Production E2E suite (Playwright)

Real-browser end-to-end tests against the **live site** (default
`https://www.torrentcdn.store`). These suites drive actual headless Chromium
through the production UI — the same flows that were manually verified during
development, now saved as a repeatable suite.

## What's covered

| Suite | Command | Verifies |
|---|---|---|
| Direct upload | `npm run test:e2e:upload` | Primary upload path: `/dashboard/upload` → progress bar advances → `PUT` goes **direct to Supabase Storage** (no fallback) → success screen with real 40-hex magnet + embed code, zero JS errors |
| Chunked fallback | `npm run test:e2e:fallback` | Resilience path: blocks every `*.supabase.co` request so the direct PUT dies → "Connection lost — retrying…" → chunked upload via `/upload/session` + `/upload/chunk/*` + `/upload/finalize` → same success screen, real magnet |
| Embed playback | `npm run test:e2e:embed` | `/embed/{file_id}`: `<video>` renders, playback starts (`currentTime` advances), streams from Supabase (HTTP 200/206), and the WebTorrent P2P attempt does **not** throw `TypeError: e.toHex is not a function` (regression guard for `patches/uint8-util+2.3.2.patch`) |
| Auto-seed after upload | `npm run test:e2e:autoseed` | The uploader's tab becomes the seeder: upload through the real `/dashboard/upload` UI, the success screen shows the "seeding…" indicator (`lib/seeder.ts` reproduces the backend infohash from the magnet), then a leecher in a second browser plays `/embed/{file_id}` with every `*.supabase.co` request **blocked** — the only way it can play is WebRTC from the uploader's seed → "N peers connected", video plays, `chunk_downloaded` records `bytes_p2p > 0` |
| 2-peer P2P | `npm run test:e2e:p2p` | **Real WebTorrent P2P between two browsers**: a seeder reproduces the backend's exact infohash (name + 256 KiB pieces) and seeds the file; a leecher plays `/embed/{file_id}` with every `*.supabase.co` request **blocked** (so only WebRTC can deliver bytes) → "N peers connected" on both sides, video plays, and the backend records `chunk_downloaded` analytics with `bytes_p2p > 0` |
| API-key UI | `npm run test:e2e:apikey` | Dashboard API Key lifecycle through the real UI: Generate reveals the full `tcdn_` key + "only time shown" warning → Copy puts the exact key on the clipboard and flips to "Copied" → **reload shows only the prefix + date (the secret never returns)** → Revoke resets the button → the revoked key 401s on `/files` (fail-closed) |
| Finalize cold-start retry | `npm run test:e2e:finalize-retry` | Blocks the first `/upload/presign/finalize` with a **502** → the client's auto-retry re-sends it → success screen, no chunked fallback. `FINALIZE_LOST_RESPONSE=1` goes further: the first finalize **completes server-side** (session consumed, uploads row created) while the browser gets the 502, so the retry must be answered by the backend's idempotent lost-response path (`uploads` row by `id == upload_id`) — the exact Render cold-start signature the live probe caught |
| Login form | `npm run test:e2e:login` | Drives the REAL auth form (every other suite seeds sessionStorage): wrong password → the actual "Invalid email or password" detail (401); correct credentials → either the logged-in navbar (verified account via `E2E_EMAIL`) or the actual "Email not verified…" gate detail (403) — production gates password login on email verification by design |
| Visual smoke | `npm run test:e2e:visual` | Loads the landing page, docs, and the logged-in dashboard (+ a 375px mobile landing shot); saves full-page screenshots under `e2e/.artifacts/visual-*.png`; fails on any JS page error or non-resource console error |

All suites:
- create a **throwaway account + test file** and delete the file in cleanup; the
  account is deleted only when the suite created it (an account shared via
  `E2E_TOKEN` outlives the run — the CI job deletes it explicitly),
  so runs are safe to repeat against production,
- synthesize real playable media with **ffmpeg** (WebM for the embed suite —
  headless Chromium can't decode H.264; MP4 for the upload suites),
- print a JSON report to stdout and exit non-zero on any failed assertion,
- save a full-page screenshot under `e2e/.artifacts/` (gitignored).

## Prerequisites

```bash
cd frontend
npm ci                      # playwright installs without browsers (no postinstall)
npx playwright install chromium   # one-time: installs the headless browser
# ffmpeg on PATH (apt install ffmpeg / brew install ffmpeg) for media synthesis
```

## Running

```bash
npm run test:e2e                       # all nine suites
npm run test:e2e:upload                # direct upload only
npm run test:e2e:fallback              # chunked fallback only
npm run test:e2e:embed                 # embed playback only
npm run test:e2e:autoseed              # uploader auto-seeds; leecher plays via P2P
npm run test:e2e:p2p                   # real 2-peer P2P (seeder + leecher)
npm run test:e2e:apikey                # dashboard API-key lifecycle (generate/copy/revoke)
npm run test:e2e:finalize-retry        # finalize 502 → client re-sends → success
npm run test:e2e:finalize-retry-lost   # ... + lost-response (server completed, browser got 502)
npm run test:e2e:login                 # real login form (401/403 message surfacing)
npm run test:e2e:visual                # page renders + screenshots + console-error check
```

`node e2e/run-all.mjs <prefix>` also runs a single suite (e.g. `node e2e/run-all.mjs embed`).
The full batch additionally runs on every push and on manual dispatch via the
`p2p-e2e` GitHub Actions job (see "CI wiring" below).

## Env overrides

| Var | Purpose |
|---|---|
| `E2E_BASE_URL` | Base URL under test (default `https://www.torrentcdn.store`) |
| `E2E_TOKEN` | Existing account token — skips registration **and** user cleanup |
| `E2E_FILE` | Path to a pre-made file — skips ffmpeg synthesis |
| `E2E_FILE_ID` | (embed suite) Existing file id — skips the API upload |
| `E2E_FILE` | (p2p suite) The seeder reproduces the backend infohash, so a pre-made file must have been uploaded with the same bytes — prefer the default synthesis |
| `FFMPEG_PATH` | ffmpeg binary path (default `ffmpeg`) |

## CI wiring

The **full E2E batch is wired into GitHub Actions** (`.github/workflows/security.yml`
job `p2p-e2e`, runs on every push to `main` + manual `workflow_dispatch`, NOT
pull requests — the suites write to production). Because Vercel deploys lag the
push, the job does NOT hit the deployed site — it **builds the pushed code**,
serves it locally (`next start` behind `e2e/local-proxy.mjs`, which mirrors
vercel.json's rewrites), and runs `node e2e/run-all.mjs` (all nine suites)
against that build with the **production Render backend** underneath. So every
push validates:
- the pushed player's P2P path against production's magnet/tracker/analytics
  contract (2-peer swarm + auto-seed with origin streaming blocked),
- direct + fallback uploads, embed playback, API-key UI, the finalize
  cold-start retry, the real login form, and page rendering (visual smoke),
- infohash reproducibility (seeder == backend) and `chunk_downloaded`
  analytics with `bytes_p2p > 0`.

The job registers **one shared throwaway account** up front (`E2E_TOKEN`) —
nine per-suite registers plus the scheduled probe registers from GitHub's
shared runner IPs would exceed the live 10/hr/IP registration budget — and
deletes it in an `if: always()` step, so CI never accumulates test users. Each
suite still deletes its own test files. The job installs Playwright Chromium +
ffmpeg itself.

## Notes

- These suites **hit production** (they need live accounts + real storage). The
  `p2p-e2e` CI job runs the full batch against a local build + the production
  backend; running them directly against `www.torrentcdn.store` is the
  post-deploy verification tool — do it after any upload/player/auth change.
- The upload suites use CDP network throttling so the progress bar is
  observable; the fallback suite also blocks `*.supabase.co` to force the
  chunked path.
- `playwright` ships without a postinstall, so `npm ci` in CI/Vercel never
  downloads browsers — the ~170MB download only happens locally via the
  explicit `npx playwright install chromium`.
