# cloudflared edge — named tunnel + DDNS

This directory is the permanent-URL edge for the live pipeline (Option A):

```
OBS ──RTMP──> rtmp.torrentcdn.store:1935 ──port-forward──> MediaMTX :1935 (this host)
viewers ──HTTPS──> hls.torrentcdn.store ──named tunnel──> MediaMTX :8888 (this host)
```

Nothing here is run by plain `docker compose up` — both services live behind
the `edge` compose profile so dev machines are unaffected.

| Service | Purpose | Run |
|---|---|---|
| `update_dns.sh` container | DDNS: keeps the `rtmp` A record on the current public IP | `--profile edge up -d ddns-rtmp` |
| `cloudflared-hls` container | Named tunnel: `hls.` → MediaMTX HLS | `--profile edge up -d cloudflared-hls` |
| `setup_tunnel.sh` | ONE-TIME: `cloudflared tunnel login` + create + route DNS | host, interactive |
| `write_tunnel_json.sh` | ONE-TIME: writes `host/config.json` from the template | host |

## One-time setup (operator)

1. Move DNS for `torrentcdn.store` to Cloudflare (free plan is enough).
2. Create an API token: My Profile → API Tokens → **Edit zone DNS** template,
   scope to zone `torrentcdn.store`. Save as `CF_API_TOKEN` in `.env`
   (plus `CF_ZONE_ID` from the zone's Overview page).
3. Install cloudflared → `./setup_tunnel.sh` (opens browser once).
   It prints/copies `TUNNEL_ID`, writes `host/config.json`, and routes
   `hls.torrentcdn.store`.
4. Copy `~/.cloudflared/<TUNNEL_ID>.json` → `host/credentials.json`.
5. Port-forward TCP 1935 on the router to this host.
6. `docker compose --profile edge up -d ddns-rtmp cloudflared-hls`
7. Point Render at the permanent URLs (see render.yaml live section):
   - `MEDIAMTX_RTMP_URL=rtmp://rtmp.torrentcdn.store`
   - `MEDIAMTX_HLS_URL=https://hls.torrentcdn.store`

## Verify

```sh
# DDNS worked (A record = this machine's public IP)
dig +short rtmp.torrentcdn.store

# tunnel serves HLS (CORS open for the web player)
curl -sI https://hls.torrentcdn.store/live/<slug>/index.m3u8 | head

# TCP ingest reachable (network-level; auth is separate)
nc -vz rtmp.torrentcdn.store 1935
```

## Files

- `update_dns.sh` — Cloudflare-API DDNS loop (idempotent, state file)
- `host/config.json` — generated named-tunnel config (gitignored)
- `host/credentials.json` — tunnel credentials (gitignored, SECRET)
- `host/credentials.template.json` — shape reference only
