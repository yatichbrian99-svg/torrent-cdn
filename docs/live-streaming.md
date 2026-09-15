# TorrentCDN Live Streaming — Architecture & Operations Guide

P2P-assisted **live** video delivery, built into the existing TorrentCDN platform without touching VOD.

```
Publisher / OBS / Encoder
        │ RTMP (1935)
        ▼
     MediaMTX  ← external auth (every publish/read verified by TorrentCDN)
        │ HLS playlist + ~2s segments
        ▼
TorrentCDN Live Delivery Layer (backend :8000)
        │  • channel registry + hashed stream keys
        │  • per-segment infohash registry (WebTorrent swarms)
        │  • Redis-backed presence / availability / heartbeats (TTL'd)
        ▼
┌─────────────────────┬──────────────────────┐
│   P2P peers         │  Origin fallback     │
│ (WebRTC/WebTorrent) │  (HLS via proxy)     │
└─────────┬───────────┴──────────┬───────────┘
          └──────────┬───────────┘
                     ▼
                  Viewer (hls.js + LivePlayer)
```

- **MediaMTX owns all media.** RTMP ingest, HLS muxing, protocol conversion. TorrentCDN never implements RTMP/HLS.
- **The playlist is not the P2P object.** `index.m3u8` stays plain HTTP (never cached); only `.m4s` segment *bytes* travel over WebRTC.
- **Never P2P-only.** Loader order: healthy peer → another peer → origin. Origin failure reports a playback error and recovers.

---

## 1. Components

| Component | Role | Code |
|---|---|---|
| MediaMTX | RTMP→HLS, external auth hook, Control API | `deploy/mediamtx/mediamtx.yml`, docker-compose `mediamtx` service |
| Live REST API | channels, keys, status, analytics, playback, sessions | `backend/app/routes/live.py` |
| MediaMTX integration | external auth (`/api/internal/mediamtx/auth`), ingest + shutdown webhooks | `backend/app/routes/live_internal.py` |
| Ephemeral state | presence, availability, counters — Redis-first w/ TTLs, in-memory fallback | `backend/app/live_state.py` |
| Status derivation | OFFLINE→STARTING→LIVE from *actual* MediaMTX/segment activity + monitor loop | `backend/app/routes/live.py` (`derive_channel_status`), `live_state.start_live_monitor` |
| Playback tokens | short-lived HMAC tokens (stateless, 10 min) | `backend/app/mediamtx.py` (`mint/verify_playback_token`) |
| DB schema | `live_channels`, `live_sessions`, `live_analytics_events` | `backend/app/database.py` (`LIVE_TABLES_DDL`) |
| Viewer client | API + pure helpers | `frontend/lib/liveClient.ts` |
| Segment registry + fallback | live-edge window, peer→origin pipeline | `frontend/lib/live/segments.ts` |
| P2P swarm | WebTorrent per-segment swarms (same trackers as VOD) | `frontend/lib/live/swarm.ts` |
| Player | hls.js + custom loader, live edge, reconnect | `frontend/components/LivePlayer.tsx` |
| Dashboard | `/live`, `/live/channels/[id]`, `/live/channels/[id]/analytics`, `/live/watch/[slug]` | `frontend/app/live/**` |
| Acceptance smoke | RTMP→HLS→status→session→counters→OFFLINE | `backend/scripts/live_smoke.py` |

## 2. Multi-tenancy & isolation

Every channel carries `tenant_id`/`owner_id`; all queries scope by owner and cross-tenant access returns the **same 404 as a missing channel** (no existence oracle). Each channel has isolated:

- stream key (SHA-256 hashed; OBS sends it as the RTMP password)
- peer swarm (`tcdnlive:<slug>`; per-segment infohashes derived from channel+sequence)
- Redis namespace (`live:v1:chan:<slug>:…`) — one channel can never read another's state
- analytics rows (`live_analytics_events.channel_id`)

## 3. Stream keys & auth

- Keys are `live_<43 url-safe chars>` (~256 bits, `secrets.token_urlsafe(32)`), shown **once** at creation/rotation; only the SHA-256 hash is stored (same model as `tcdn_` API keys).
- Rotation replaces the hash — the old key stops working at the next publish (MediaMTX calls the auth hook on every publish).
- Publishing: MediaMTX POSTs `{action:"publish", path:"live/<slug>", password:<key>, …}` to the backend; the backend hashes + constant-time compares and answers 2xx/403.
- Playback: enabled channels serve public HLS; viewer *APIs* (sessions/heartbeats) require a short-lived playback token (`Authorization: Bearer <token>`), minted via `/live/channels/{id}/playback` (owner) or `/live/play/{slug}` (public, enabled channels only). A guessed slug cannot open a session on a disabled channel (404).
- The stream key is never logged; the auth hook records only the slug + outcome in the security-event feed.

## 4. Status lifecycle (derived, never manual)

```
OFFLINE → publisher authenticates (auth hook sets STARTING marker)
STARTING → MediaMTX path ready / first segments registered
LIVE → MediaMTX reports path ready, or freshest segment < LIVE_MAX_SEGMENT_AGE_SECONDS
OFFLINE → publisher gone: segments age out, monitor + /shutdown webhook clear state
```

The monitor loop (default every 5s) polls MediaMTX, expires segment/peer state, and folds Redis counters into durable `live_analytics_events` rows (historical analytics are preserved; only ephemeral state expires). One broken channel never blocks another — every tick isolates per-channel failures.

## 5. Delivery & fallback (client)

`LivePlayer` mounts hls.js with a custom loader:

1. Playlist requests go **straight to HTTP** (control plane).
2. Segment requests go through `fetchSegmentWithFallback`:
   - try the peer swarm (`LiveSwarm.fetchSegment`, bounded 2.5s),
   - on miss/failure, plain XHR from origin (Range supported),
   - every outcome is reported (`segments_served` / `segments_failed` / bytes) — measured, never modeled.
3. Failed transfers scrub availability server-side (`mark_peer_dead`), so dead peers stop being selected; the backend also expires peers on heartbeat TTL (default 15s).
4. hls.js handles ABR, stalls, and recovery; drift >15s behind the live edge snaps back; fatal network errors call `hls.startLoad()` instead of killing playback.

## 6. Quick start (local)

```bash
# 0. configure
cp .env.example .env
# set: JWT_SECRET (32+ chars), MEDIAMTX_HOOK_SECRET (openssl rand -hex 32),
#      POSTGRES_PASSWORD, MINIO_ROOT_PASSWORD

# 1. start everything (postgres, redis, minio, backend, frontend, caddy, mediamtx)
docker compose up -d --build

# 2. create an account + admin (first run), open the dashboard
#    http://localhost → Live → New channel

# 3. publish from OBS
#    Server:     rtmp://localhost:1935/live
#    Stream key: <paste the key from the channel page (Rotate to reveal)>

# 4. watch
#    http://localhost/live/watch/<channel-slug>   (or the HLS URL: http://localhost/hls/<slug>/index.m3u8)
```

Without Docker: start `mediamtx` with `deploy/mediamtx/mediamtx.yml` (env vars per `.env.example`), then the backend and frontend as in the main README.

## 7. Verification

```bash
# backend unit tests (live + full VOD suite)
cd backend && python -m pytest tests/test_live.py tests/test_live_state.py
cd backend && python -m pytest                    # full suite (595 tests)

# frontend unit tests + coverage gate
cd frontend && npm test && npm run test:coverage

# end-to-end acceptance (requires the stack + ffmpeg):
#   creates a channel, publishes a synthetic RTMP stream, verifies HLS,
#   derives LIVE status, joins a session, sends heartbeats/availability,
#   reads analytics, then verifies OFFLINE after disconnect.
python3 backend/scripts/live_smoke.py --api http://localhost:8000 \
  --email <admin> --password <pw> --rtmp rtmp://localhost:1935 --hls http://localhost:8888

# load test (SIMULATED viewers — labeled as such, never production analytics)
python3 backend/scripts/load_test.py --url http://localhost:8000 --people 10 100 1000
```

How to verify P2P: open two viewers on the same channel; the player badge shows peers, the channel page shows `Peers`, and analytics show `bytes_p2p > 0` + offload %. (In the current MVP the browser swarm seeds segments it holds; when no peer holds a segment yet, delivery is origin — check `Segments served/failed` for the measured split.)

How to test origin fallback: stop MediaMTX mid-playback (or publish nothing after the first segments). Playback must continue from already-buffered segments, the player must attempt recovery (`hls.startLoad`), and `playback_errors` must increment — without a page reload.

## 8. Deployment (production)

- Only expose: `443` (Caddy/TLS), `1935` (RTMP — terminate at a TLS edge or firewall to publisher ranges).
- Never expose MediaMTX `9997`/`9998` (bind loopback / private network; the Caddyfile does not route them).
- HLS is served same-origin through Caddy (`/hls/*` → `mediamtx:8888`): playlist `Cache-Control: no-store, must-revalidate`; segments short cache (`max-age=2`). Never cache `index.m3u8`.
- Set in the environment: `MEDIAMTX_API_URL`, `MEDIAMTX_RTMP_URL` (public `rtmp://stream.example.com:1935`), `MEDIAMTX_HLS_URL` (public `https://stream.example.com/hls`), `MEDIAMTX_HOOK_SECRET`, `PLAYBACK_TOKEN_SECRET`, `PUBLIC_PLAYBACK_DOMAIN` (+ existing DB/Redis/JWT vars). See `.env.example`.
- `/api/health/live` reports `{monitor_running, mediamtx_configured, mediamtx_reachable, active_channels}` (unauthenticated, detail-free). Component-level checks live on the admin health dashboard.

## 9. API endpoints (live)

Owner (session JWT or API key):

| Method | Path | Purpose |
|---|---|---|
| POST | `/live/channels` | create (returns slug; key shown once via rotate) |
| GET | `/live/channels` | list + live metrics |
| GET | `/live/channels/{id}` | detail |
| PATCH | `/live/channels/{id}` | rename / describe |
| DELETE | `/live/channels/{id}` | delete (+ clear ephemeral state) |
| POST | `/live/channels/{id}/enable` · `/disable` | gate publishing+playback |
| POST | `/live/channels/{id}/rotate-key` | regenerate stream key (shown once) |
| GET | `/live/channels/{id}/status` | derived status, viewers, peers, bitrate, uptime, latest segment |
| GET | `/live/channels/{id}/analytics?hours=` | bytes, offload %, transfer rates, buffering, errors, peaks |
| GET | `/live/channels/{id}/segments` | availability map (owner) |
| GET | `/live/channels/{id}/playback` | playback info + fresh token |

Viewer (playback token):

| Method | Path | Purpose |
|---|---|---|
| GET | `/live/play/{slug}` | public playback info (enabled channels) |
| POST | `/live/token` | API-key exchange for a viewer token |
| POST | `/live/session` | join swarm (creates `live_sessions` row) |
| POST | `/live/heartbeat` | presence + counters |
| POST | `/live/segment/availability` | haves + measured transfer outcomes |
| POST | `/live/leave` | leave (ends session, drops presence) |

Internal (never public):

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/internal/mediamtx/auth` | MediaMTX external auth (publish/read) |
| POST | `/api/internal/mediamtx/ingest` | segment-registration webhook (`X-Hook-Secret`) |
| POST | `/api/internal/mediamtx/shutdown` | stream-end cleanup |
| GET | `/api/internal/mediamtx/channels/{slug}/peers` | diagnostics (peer ids only) |
| GET | `/api/health/live` | live pipeline health |

## 10. Environment variables (live)

See `.env.example` — `MEDIAMTX_API_URL`, `MEDIAMTX_RTMP_URL`, `MEDIAMTX_HLS_URL`, `MEDIAMTX_WEBRTC_URL`, `MEDIAMTX_HOOK_SECRET`, `PLAYBACK_TOKEN_SECRET`, `PUBLIC_PLAYBACK_DOMAIN`, `LIVE_HLS_SEGMENT_DURATION`, `LIVE_HLS_PART_DURATION`, `LIVE_HEARTBEAT_INTERVAL_SECONDS`, `LIVE_PEER_TIMEOUT_SECONDS`, `LIVE_MONITOR_INTERVAL_SECONDS`, `LIVE_SEGMENT_RETENTION_SECONDS`, `LIVE_MAX_SEGMENT_AGE_SECONDS`, `LIVE_TOKEN_RATE_LIMIT`.

## 11. Known limitations (MVP)

- **Segment-level P2P is seeded from viewers that already buffered a segment**; the infohash registry (backend-derived per-segment swarms) is wired through `LiveSwarm`, but peers currently discover each other via the shared WSS trackers with the backend as the infohash authority — a production rollout should add a small `/live/segment/{slug}/{seq}/info` endpoint serving the derived infohash+size so the swarm's `tryPeer` path can resolve infohashes just-in-time (the plumbing in `swarm.ts` accepts it).
- WebRTC playback (WHEP) and SRT/RTSP ingest are disabled by default; architecture supports them via MediaMTX config.
- End-to-end latency is *not yet measured* in production — the design targets ~2s segments; do not promise numbers until measured (spec §14).
- Offload percentages are measured delivery metrics only; no "dollars saved" claims (no pricing configured).
- `live_analytics_events` rollups are interval-based from Redis counters; very short streams (< rollup window) may record a single consolidated row.
- Geographic peer preference is not yet applied live (VOD has no geo layer either); reliability-first selection (health + served-volume) is used.

## 12. Recommended next improvements

1. Infohash resolution endpoint + WebTorrent `infoHash`-pinned joining for full cross-viewer swarm formation.
2. LL-HLS parts over the same pipeline (MediaMTX `hlsVariant: lowLatency` already on).
3. Prometheus scrape of MediaMTX `:9998` into the admin health dashboard (CPU/RAM/bandwidth per publish).
4. Signed playback *URLs* (query-param HMAC) for CDN-friendly private channels.
5. Geo/locality preference in `peer_scores` once a geo source exists.
6. Replace the segment webhook with an inotify watcher on MediaMTX's HLS directory for zero-latency registration.
