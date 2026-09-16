# TorrentCDN Live — P2P-assisted live streaming that cuts your origin bill

> One-pager for prospective customers. Every number in here is either measured by
> the platform or clearly labeled as simulated — we don't quote offload percentages
> we haven't seen with real audiences.

---

## The problem

Live video is brutally expensive to deliver. Every viewer pulls the **same segments,
at the same time, from your origin or CDN** — you pay egress for each of them
individually, and your origin scales linearly with audience size. At 1,000 concurrent
viewers on a 5 Mbps stream, that's ~5 Gbps of repeated egress for identical bytes.

## The solution

TorrentCDN Live turns your audience into the CDN.

```
OBS / encoder ──RTMP──▶ MediaMTX ──HLS──▶ TorrentCDN live layer
                                            │
                              ┌─────────────┴─────────────┐
                              ▼                           ▼
                        viewer-to-peer               origin fallback
                        segment delivery          (automatic, always on)
```

- **You publish exactly like today.** OBS → RTMP. No new encoder, no new workflow.
- **Viewers who are watching a channel share segments with each other** over the
  same swarm, so the origin serves each segment far fewer times.
- **Fallback is automatic and mandatory**: peer fails → next peer → origin.
  Playback never depends on P2P being available. Your stream never becomes
  a science experiment.
- **Multi-channel, multi-tenant** from day one: isolated keys, swarms, analytics
  and authorization per channel and per customer.

## What's actually built (not a roadmap slide)

| Capability | Status |
| --- | --- |
| RTMP ingest → HLS (MediaMTX — industry standard media server) | ✅ production path |
| Channel management: keys, rotation, revocation, disable | ✅ dashboard + API |
| Stream-key security (hashed storage, one-time reveal, per-channel) | ✅ |
| Live peer swarms per channel with heartbeats and TTLs | ✅ |
| Segment registry with expiry — live state never grows forever | ✅ |
| Automatic origin fallback on any peer failure | ✅ verified in E2E |
| Measured delivery analytics (P2P vs origin bytes, offload %) | ✅ real-time |
| Live player (HLS.js, live-edge, reconnect, error recovery) | ✅ |
| Multi-tenant isolation (slug-unique, tenant-scoped, rate-limited) | ✅ tested |
| Automated acceptance suite: 41/41 checks incl. a **real ffmpeg→RTMP publish** | ✅ runs on demand |

## Measured, not promised

The dashboard reports **real byte accounting**: every segment transfer is reported
by the peers that performed it, aggregated per channel:

- P2P-served bytes vs origin-served bytes → **P2P offload %**
- Peer transfer success / failure rates
- Viewers, peers, bitrate, latest segment, live uptime

**Demo mode, honestly labeled.** The included pitch demo
(`backend/scripts/live_pitch_demo.py`) runs a real publish (branded ffmpeg source →
RTMP → MediaMTX → HLS) plus *simulated* viewer load that is explicitly labeled
on-screen and in the dashboard as SIMULATED. In our local verification run with
3 simulated viewers the measured aggregation showed ~90% offload — treat that as a
demo of the *measurement system*, not a customer guarantee. Your real offload
depends on audience size, session length, and network topology; we measure it
from the first minute instead of guessing it.

## Security & privacy posture

- Stream keys stored **hashed** (SHA-256), shown once, rotated in one click —
  same contract as our API keys.
- Publishing requires authorization (MediaMTX external auth hooked to TorrentCDN).
- **Viewer IP addresses are never exposed** to other viewers or the dashboard.
- Control APIs (MediaMTX 9997/9998) are internal-only, never proxied publicly.
- Playback tokens are short-lived and signed; private channels can't be watched
  by guessing URLs.
- Rate limiting on every viewer and admin surface.

## Integration effort

1. Point OBS at the RTMP server URL we generate; paste the stream key.
2. Share the HLS URL or embed our player.
3. Watch the dashboard: viewers, peers, offload — live.

That's the whole onboarding. Nothing to install on viewer devices; delivery is
plain browser HLS with peer assistance happening behind the scenes.

## See it in 5 minutes

```bash
docker compose up -d                    # stack: backend, MediaMTX, Postgres, Redis
cd backend && .venv/bin/python scripts/live_pitch_demo.py --viewers 25 --minutes 6
# → real publish + labeled simulated audience + live measured dashboard
# → open http://localhost:3000/live to watch the same numbers in the product UI
```

Or bring your own stream: create a channel in the dashboard, copy the two OBS
fields, go live, open the player link. P2P and fallback take over from there.

## Why now

Every live platform is renegotiating egress contracts while their costs compound.
Peer-assisted delivery is the only lever that grows *cheaper* per viewer as the
audience grows — and TorrentCDN already runs the VOD version of it in production.
Live is the same proven machinery, tuned for seconds-level latency instead of files.
