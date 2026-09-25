# TorrentCDN live edge on an Oracle Cloud Always-Free VM

Permanent, **$0** public home for RTMP ingest + HLS playback. The backend stays
on Render; the media edge moves to a VM with a **static public IP** — no DDNS,
no tunnels, no router port-forwards.

```
OBS / Wirecast                     viewers (browser player)
     │ RTMP :1935                       │ HTTPS :443
     ▼                                  ▼
rtmp.torrentcdn.store ──► ORACLE VM ──► hls.torrentcdn.store
                           ├─ MediaMTX (RTMP→HLS, auth hooks)
                           └─ Caddy    (TLS, caching, CORS)
                                        │
                                        ▼
                       Render backend (auth hook, ready hooks, dashboard)
```

---

## Step 0 — Create the VM (~5 min)

1. [oracle.com/cloud/free](https://oracle.com/cloud/free) → **Start for free**
   (needs a card for identity verification only — Always-Free resources are
   never charged).
2. Console → **Compute → Instances → Create instance**:
   - **Image:** Ubuntu 22.04 (canonical image)
   - **Shape:** `VM.Standard.A1.Flex` — **2 OCPU / 12 GB RAM** (the Always-Free
     allowance is 4 OCPU / 24 GB total; leave room for a second VM)
     - *x86 fallback:* `VM.Standard.E2.1.Micro` if ARM capacity is exhausted in
       your region (Micro is 1/8 GB, enough for the edge; try another region
       or come back later for Ampere first)
   - **Networking:** new VCN + public subnet; **"Assign a public IPv4
     address" = ON**
   - **SSH keys:** paste your `~/.ssh/id_ed25519.pub`
3. Create → note the **Public IP address** shown on the instance page.
   **This IP is static** for the life of the instance.

## Step 1 — Open the firewall (Oracle's side)

Two independent firewalls exist; BOTH must be opened (this is the classic
Oracle gotcha):

**a) Security List** (Oracle's cloud firewall):
Console → your instance → **Subnet** → **Security List** → *Add Ingress Rule*:

| Source     | Protocol | Dest. port | For          |
|------------|----------|------------|--------------|
| 0.0.0.0/0  | TCP      | 80         | ACME + HTTP  |
| 0.0.0.0/0  | TCP      | 443        | HLS playback |
| 0.0.0.0/0  | TCP      | 1935       | RTMP ingest  |

(Leave 22 = SSH as the default rule has it. Do **not** open 9997/9998/8888 —
they stay loopback-only on the VM.)

**b) OS firewall** — already handled: `bootstrap.sh` inserts the ACCEPT rules
and persists them (Oracle's Ubuntu image ships a REJECT-all INPUT chain; this
is the #1 cause of "security list open but port still dead").

## Step 2 — Run the bootstrap

```bash
ssh ubuntu@<VM_PUBLIC_IP>
# from your repo checkout, upload then run:
scp -r deploy/oracle ubuntu@<VM_PUBLIC_IP>:~
sudo bash ~/oracle/bootstrap.sh
```

The script: installs Docker, writes `/opt/torrentcdn-edge/` (MediaMTX +
Caddy), generates secrets, opens the OS firewall, starts the stack, and prints
the Render env values. Prompts once for the HLS hostname (Enter =
`hls.torrentcdn.store`).

## Step 3 — DNS

At Namecheap (`torrentcdn.store` → Advanced DNS → A Record), add **two**
records, both → the VM's **public IP**:

| Host | Value      | TTL    |
|------|------------|--------|
| hls  | `<VM_IP>`  | 5 min  |
| rtmp | `<VM_IP>`  | 5 min  |

Then from your machine:

```bash
dig +short hls.torrentcdn.store    # → <VM_IP>
dig +short rtmp.torrentcdn.store   # → <VM_IP>
```

## Step 4 — Point Render at the VM

Render dashboard → `torrentcdn-backend` → Environment:

```
MEDIAMTX_RTMP_URL = rtmp://rtmp.torrentcdn.store
MEDIAMTX_HLS_URL  = https://hls.torrentcdn.store
MEDIAMTX_HOOK_SECRET = <see note below>
MEDIAMTX_API_USER    = <the value bootstrap.sh printed>
MEDIAMTX_API_PASS    = <the value bootstrap.sh printed>
```

> **Which MEDIAMTX_HOOK_SECRET?** Exactly ONE value must exist on both sides.
> If you apply the Render Blueprint, `render.yaml` auto-generates the secret
> (`generateValue: true`) — copy **Render's** value from the dashboard's
> Environment tab into the VM's `.env` and restart the edge. Otherwise use
> **bootstrap.sh's** printed value in both places. A mismatch = every publish
> rejected with 401/403. Never commit the value anywhere.

Save → backend redeploys. The dashboard's OBS card and player URLs are built
from these, so every channel instantly shows the permanent public endpoints.

Also set on **Vercel** (frontend): `NEXT_PUBLIC_LIVE_HLS_ORIGIN =
https://hls.torrentcdn.store` — it is already in `vercel.json`'s `env` block,
so a normal push-deploy carries it; it feeds the page CSP's `connect-src` so
hls.js may fetch playlists/segments from the new host (without it the browser
**silently blocks** the live stream). Redeploy the frontend after the VM is
live.

*(Optional — CLI alternative: `render.yaml` already carries placeholder values
for these keys; update them to the same values and `git push` instead.)*

## Step 5 — Verify (outside the VM)

```bash
# 1. ports reachable (TCP ≠ RTMP-authenticated — both tested here)
nc -vz rtmp.torrentcdn.store 1935

# 2. TLS + HLS serving (cert auto-issued on first request)
curl -sI https://hls.torrentcdn.store/ | head -3

# 3. END-TO-END: publish a test stream with the key from a real channel
ffmpeg -re -f lavfi -i testsrc2=size=1280x720:rate=30 \
       -f lavfi -i sine=frequency=1000 \
       -c:v libx264 -preset veryfast -b:v 2500k -g 60 -pix_fmt yuv420p \
       -c:a aac -b:a 128k -f flv \
       "rtmp://rtmp.torrentcdn.store/live/<channel-slug>?pass=<stream-key>"

# 4. dashboard shows the channel LIVE within ~1s (runOnReady hook), and:
curl -s https://hls.torrentcdn.store/live/<channel-slug>/index.m3u8 | head -5
```

Then open OBS against `rtmp://rtmp.torrentcdn.store/live` + the channel's
stream key — same flow as always, now on a permanent public host.

---

## What changed vs the tunnel/dev setup

| Concern | Before | Now |
|---|---|---|
| RTMP endpoint | localhost / bore.pub tunnel | `rtmp://rtmp.torrentcdn.store` |
| HLS endpoint | trycloudflare (random host) | `https://hls.torrentcdn.store` |
| DNS | ephemeral / DDNS needed | static VM IP, plain A records |
| TLS | tunnel-terminated | Caddy on the VM (auto, on-demand) |
| Cost | $0 | $0 (Always-Free, no card charge) |

## Known limits (honest)

- **Render can no longer poll MediaMTX's Control API** (9997 is loopback by
  design; exposing it would let anyone list/kill streams). Consequences:
  - `/api/health/live` reports `mediamtx_reachable=false` (cosmetic — the
    pipeline works; the probe just can't see the VM).
  - Channel status falls back to the **runOnReady/not-ready hooks** (instant)
    + HLS playlist polling (correctness net). No functional loss.
- **Egress bandwidth**: Always-Free includes **10 TB/month outbound** — ~55
  Mbps sustained 24/7. Per-viewer segments are served P2P-first by design, so
  origin (VM) egress stays low; measure before claiming headroom.
- **Single VM = single point of failure.** If it dies, ingest dies with it.
  Acceptable for the MVP; the fix later is a second region + DNS failover.

## Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| `nc` to 1935 fails from outside | Security List rule missing, **or** iptables REJECT (re-run bootstrap step 5), **or** ISP blocks inbound 1935 (test from a phone hotspot) |
| TLS cert never issues | DNS not propagated (`dig +short hls.…` ≠ VM IP) or 80 blocked — Caddy needs 80 for the ACME HTTP-01 challenge |
| 401/403 on publish (OBS) | `MEDIAMTX_HOOK_SECRET` on Render ≠ VM's `.env`, or the channel's stream key is wrong/rotated |
| Channel stuck STARTING | Hook URL unreachable from VM: check `docker logs mediamtx` for hook errors; confirm `MEDIAMTX_READY_HOOK_URL` points at the Render origin |
| Player CORS errors | Caddy's `Access-Control-Allow-Origin` must be exactly the frontend origin (`https://www.torrentcdn.store`) |
| OBS drops frames (71% like Adam's test) | Encoder uplink or tunnel — NOT this stack: the VM has no tunnel hop; test at 2500k first, then raise |
