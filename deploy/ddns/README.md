# Namecheap DDNS + Caddy edge — permanent public URLs, $0/month

This is the implemented "Option A" edge for the live pipeline. It gives the
app **permanent** ingest/playback hostnames on your own domain with no VPS,
no tunnels, and no DNS migration:

```
OBS ──RTMP──> rtmp.torrentcdn.store:1935 ──router-forward──> MediaMTX :1935
viewers ──HTTPS──> hls.torrentcdn.store ──router-forward──> Caddy (TLS) ──> mediamtx:8888
```

## Components

| Piece | File | Purpose |
|---|---|---|
| DDNS updater | `deploy/ddns/update_dns.sh` | Keeps `rtmp`/`hls` A records on the current public IP via Namecheap's built-in Dynamic DNS |
| HLS TLS site | `deploy/caddy/hls.caddy` | Imported by the Caddyfile — permanent HTTPS host for HLS, on-demand cert |
| compose service | `ddns` (edge profile) | Runs the updater; `docker compose up` without `--profile edge` is unchanged |

## One-time setup (~10 minutes, 4 steps)

1. **Namecheap → Advanced DNS → Dynamic DNS: enable it**, then click
   "Dynamic DNS Password" → set a password (or let it generate one). This
   single password is the entire credential surface — put it in `.env` as
   `NAMECHEAP_DDNS_PASSWORD`. (Namecheap DDNS updates A records for
   subdomains automatically; no manual record creation needed.)

2. **Router: forward two ports** to this machine (`192.168.2.109` or DHCP-reserved):
   - TCP **1935** → MediaMTX RTMP
   - TCP **80 + 443** → Caddy (if not already forwarded)

3. **Start the edge**:
   ```sh
   docker compose --profile edge up -d ddns
   # restart caddy so it picks up the hls.caddy import:
   docker compose restart caddy
   ```

4. **Render dashboard**: confirm
   `MEDIAMTX_RTMP_URL=rtmp://rtmp.torrentcdn.store` and
   `MEDIAMTX_HLS_URL=https://hls.torrentcdn.store` (blueprint values; set
   manually if the dashboard overrides them).

## Verify

```sh
dig +short rtmp.torrentcdn.store        # → your public IP
dig +short hls.torrentcdn.store         # → your public IP
nc -vz rtmp.torrentcdn.store 1935       # TCP reachable (network layer)
curl -sI https://hls.torrentcdn.store/live/<slug>/index.m3u8   # TLS + playlist
```

Then publish from OBS (`rtmp://rtmp.torrentcdn.store/live` + channel key)
and confirm the dashboard flips STARTING → LIVE.

## Notes

- **CGNAT check first**: `curl ifconfig.me` vs your router's WAN IP — if they
  differ, the ISP runs carrier-grade NAT and port forwarding is impossible
  (this setup was verified non-CGNAT on the operator's connection).
- The updater is busybox-only (wget/grep), fails **soft** (wrong password or
  network blips log clearly and retry on the next tick), and only touches the
  API when the IP actually changed.
- On-demand TLS: the `hls` certificate is issued at the first real request,
  so nothing happens in dev before DNS/forwarding exist.
- Some ISPs filter inbound ports; if `nc` fails after forwarding, test with a
  different port or contact the ISP — that failure is at the network layer,
  not the application.
