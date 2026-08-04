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

All suites:
- create a **throwaway account + test file** and delete them in cleanup (safe to
  run repeatedly against production),
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
npm run test:e2e            # all five suites
npm run test:e2e:upload     # direct upload only
npm run test:e2e:fallback   # chunked fallback only
npm run test:e2e:embed      # embed playback only
npm run test:e2e:autoseed   # uploader auto-seeds; leecher plays via P2P
npm run test:e2e:p2p        # real 2-peer P2P (seeder + leecher)
```

`node e2e/run-all.mjs <prefix>` also runs a single suite (e.g. `node e2e/run-all.mjs embed`).

## Env overrides

| Var | Purpose |
|---|---|
| `E2E_BASE_URL` | Base URL under test (default `https://www.torrentcdn.store`) |
| `E2E_TOKEN` | Existing account token — skips registration **and** user cleanup |
| `E2E_FILE` | Path to a pre-made file — skips ffmpeg synthesis |
| `E2E_FILE_ID` | (embed suite) Existing file id — skips the API upload |
| `E2E_FILE` | (p2p suite) The seeder reproduces the backend infohash, so a pre-made file must have been uploaded with the same bytes — prefer the default synthesis |
| `FFMPEG_PATH` | ffmpeg binary path (default `ffmpeg`) |

## Notes

- These suites **hit production** and are intentionally **not** part of CI or
  the Vercel build (they need live accounts + real storage). They're the
  post-deploy verification tool: run them after any upload/player change.
- The upload suites use CDP network throttling so the progress bar is
  observable; the fallback suite also blocks `*.supabase.co` to force the
  chunked path.
- `playwright` ships without a postinstall, so `npm ci` in CI/Vercel never
  downloads browsers — the ~170MB download only happens locally via the
  explicit `npx playwright install chromium`.
